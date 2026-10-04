"""Three-way merge of Money Manager databases.

The working database ("local") and a snapshot from the app ("remote") both
descend from a common ancestor ("base"): the snapshot the previous sync
started from. Rows are matched by uid (ZCATEGORY by uid + TYPE, since the
two category trees may reuse a uid), never by the numeric primary keys,
which differ between copies. For every row:

- added on one side only: kept, or copied over from the app
- deleted on one side and untouched on the other: deleted
- changed: merged column by column. A column changed differently on both
  sides is a conflict for the user to settle; last-write timestamps just
  keep the later value.
- deleted on one side and edited on the other: a conflict
- missing from the app's snapshot in a table the app only soft-deletes
  from (SOFT_DELETE_TABLES): kept, since the app never had it

Transactions added on both sides that look like the same purchase entered
twice (same account, amount, date, ...) are offered as possible duplicates.
References the merge itself would break (a new transaction whose category
the other side deleted, a transfer left with one leg, ...) are problems that
block applying it until they're fixed.

Without a base it's a two-way comparison: rows on one side only are kept and
every row that differs is a conflict.
"""
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

SKIP_TABLES = {"android_metadata", "sqlite_sequence"}
# Row identity per table; uid everywhere else. Matched case-insensitively
# (BUDGET_AMOUNT calls it UID).
KEY_COLUMNS = {"ZCATEGORY": ("uid", "TYPE")}
# Last-write timestamps: if both sides changed one, keep the later value
# rather than calling it a conflict.
TIMESTAMP_COLUMNS = {"UTIME", "A_UTIME", "C_UTIME", "E_UTIME", "USETIME", "MODIFY_DATE"}
TX = "INOUTCOME"
# Tables the app deletes from by setting a flag (IS_DEL, C_IS_DEL, an
# account's ZDATA status), never by removing the row. A row in the base but
# missing from the app's snapshot was never in the app (it restored an
# older file than the last push), so it's kept and goes back to the app.
SOFT_DELETE_TABLES = {TX, "ZCATEGORY", "ASSETS"}
# Columns that hold one value between them and so merge as a unit: taking
# WDATE from one side and ZDATE from the other leaves the date and the
# timestamp days apart. If both sides changed the group differently, every
# column in it that differs is in the one conflict, so a decision takes the
# whole group from one side.
COLUMN_GROUPS = {TX: [("WDATE", "ZDATE")]}
# Columns that tie a transfer's two legs and its fee row together.
TX_LINK_COLUMNS = ("txUidTrans", "txUidFee")
DUPLICATE_THRESHOLD = 6.5
DUPLICATE_MAX_DAYS = 3


def _blank(v):
    return v is None or v == ""


def _truthy(v):
    """IS_DEL-style flags: 0, '0', '' and NULL are all false."""
    return not (_blank(v) or v == 0 or v == "0")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _day(v):
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

class Table:
    """One table: `cols` are every column but the integer primary key, and
    `rows` maps each row's key to its values over `cols`. A table whose key
    column is missing, empty or repeated is unkeyed and only compared
    whole."""

    def __init__(self, con, name):
        info = con.execute(f'PRAGMA table_info("{name}")').fetchall()  # cid, name, type, notnull, default, pk
        pks = [r for r in info if r[5]]
        self.name = name
        self.pk = pks[0][1] if len(pks) == 1 and pks[0][2].upper() == "INTEGER" else None
        self.cols = [r[1] for r in info if r[1] != self.pk]
        by_lower = {c.lower(): c for c in self.cols}
        self.key = tuple(by_lower.get(k.lower()) for k in KEY_COLUMNS.get(name, ("uid",)))
        self.keyed = all(self.key)
        select = ", ".join([f'"{self.pk}"' if self.pk else "NULL"] + [f'"{c}"' for c in self.cols])
        data = con.execute(f'SELECT {select} FROM "{name}"').fetchall()
        self.rows, self.pks = {}, {}
        if self.keyed:
            idx = [self.cols.index(c) for c in self.key]
            for r in data:
                row = tuple(r[1:])
                k = tuple(row[i] for i in idx)
                if any(_blank(v) for v in k) or k in self.rows:
                    self.keyed = False
                    break
                self.rows[k] = row
                self.pks[k] = r[0] if self.pk else None
        if not self.keyed:
            # Synthetic keys, only meaningful within this snapshot.
            self.rows = {(i,): tuple(r[1:]) for i, r in enumerate(data)}
            self.pks = {(i,): (r[0] if self.pk else None) for i, r in enumerate(data)}

    def bag(self):
        return Counter(self.rows.values())

    def index(self, col):
        try:
            return self.cols.index(col)
        except ValueError:
            return None

    def as_dict(self, row):
        return dict(zip(self.cols, row))


class Snapshot:
    """Every table of a .mmbak, loaded into memory (a few MB for a
    15,000-transaction database)."""

    def __init__(self, path):
        self.path = path
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            self.user_version = con.execute("PRAGMA user_version").fetchone()[0]
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            self.tables = {n: Table(con, n) for n in names if n not in SKIP_TABLES}
        finally:
            con.close()


def schema_problems(a, b):
    """Why snapshot b can't be merged with a, if anything (list of str)."""
    out = []
    if a.user_version != b.user_version:
        out.append(f"Schema versions differ ({a.user_version} here, {b.user_version} in the incoming file).")
    for name in sorted(a.tables.keys() | b.tables.keys()):
        ta, tb = a.tables.get(name), b.tables.get(name)
        if ta is None or tb is None:
            out.append(f"Table {name} exists only {'here' if tb is None else 'in the incoming file'}.")
        elif (ta.pk, ta.cols) != (tb.pk, tb.cols):
            out.append(f"Table {name} has different columns here and in the incoming file.")
    return out


def _table_distance(ta, tb):
    if not (ta.keyed and tb.keyed):
        ca, cb = ta.bag(), tb.bag()
        return sum(((ca - cb) + (cb - ca)).values())
    n = len(ta.rows.keys() ^ tb.rows.keys())
    for k in ta.rows.keys() & tb.rows.keys():
        ra, rb = ta.rows[k], tb.rows[k]
        if ra != rb:
            n += sum(x != y for x, y in zip(ra, rb))
    return n


def distance(a, b):
    """How far apart two snapshots are: rows on one side only plus differing
    values in shared rows. None if their schemas differ."""
    if schema_problems(a, b):
        return None
    return sum(_table_distance(a.tables[n], b.tables[n]) for n in a.tables)


def choose_base(candidates, remote):
    """The candidate snapshot `remote` most likely descends from: the
    closest one. Candidates come safest first (the last snapshot taken
    from the app, then states handed back to it, which it may or may not
    have restored), and a later one only wins by being strictly closer."""
    best, best_d = None, None
    for snap in candidates:
        d = distance(snap, remote)
        if d is not None and (best_d is None or d < best_d):
            best, best_d = snap, d
    return best


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def _item_id(prefix, *parts):
    return prefix + hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:16]


@dataclass
class Conflict:
    """A row both sides changed incompatibly. kind: "edit" (a column changed
    differently on both sides, or differs at all without a base),
    "edit_delete" (edited here, deleted in the app; never in
    SOFT_DELETE_TABLES), "delete_edit" (deleted
    here, edited in the app), or "table" (an unkeyed table changed on both
    sides). fields: [(column, base, local, remote)] for the columns at
    issue."""
    id: str
    table: str
    key: tuple
    kind: str
    fields: list = field(default_factory=list)


@dataclass
class Duplicate:
    """A transaction added on both sides that looks like the same one.
    local/remote: the INOUTCOME keys of each side's rows (one transaction,
    or a transfer's legs and fee row)."""
    id: str
    local: list
    remote: list
    score: float


@dataclass
class Resolution:
    """The operations for a set of decisions, applied to a copy of local."""
    inserts: dict  # table -> {key: (primary key, row)} copied from remote
    updates: dict  # table -> {key: {column index: value}}
    deletes: dict  # table -> set of keys
    replace: dict  # table -> remote Table, for unkeyed tables taken whole
    unresolved: list  # conflicts without a decision


class Plan:
    """What merging `remote` into `local` does, given their common `base`
    (None for a two-way comparison). Building it changes nothing; resolve()
    turns the user's decisions into operations and write() applies them."""

    def __init__(self, base, local, remote):
        self.base, self.local, self.remote = base, local, remote
        self.two_way = base is None
        self.inserts = defaultdict(dict)
        self.updates = defaultdict(dict)
        self.deletes = defaultdict(set)
        self.replace = {}
        self.conflicts = []
        self.duplicates = []
        # table -> side ("remote"/"local") -> what ("added"/"changed"/"deleted"/"missing") -> [key]:
        # what each side did since the base, conflicting changes included.
        # "missing" (local only): kept here, though not in the app's snapshot.
        self.changes = defaultdict(lambda: {"remote": defaultdict(list), "local": defaultdict(list)})
        self.schema_problems = schema_problems(local, remote)
        if base is not None and schema_problems(local, base):
            self.schema_problems.append("The snapshot from the last sync has a different schema.")
        if not self.schema_problems:
            for name in local.tables:
                self._merge_table(name)
            self._find_duplicates()

    # -- diffing -------------------------------------------------------------

    def _merge_table(self, name):
        L, R = self.local.tables[name], self.remote.tables[name]
        B = self.base.tables[name] if self.base else None
        if not (L.keyed and R.keyed and (B is None or B.keyed)):
            return self._merge_unkeyed(name, B, L, R)
        brows = B.rows if B else {}
        stamps = {i for i, c in enumerate(L.cols) if c.upper() in TIMESTAMP_COLUMNS}
        rch, lch = self.changes[name]["remote"], self.changes[name]["local"]
        for k in L.rows.keys() | R.rows.keys() | brows.keys():
            b, l, r = brows.get(k), L.rows.get(k), R.rows.get(k)
            if l == r:
                continue
            if b is None:
                if l is None:
                    self.inserts[name][k] = (R.pks[k], r)
                    rch["added"].append(k)
                elif r is None:
                    lch["added"].append(k)
                else:  # on both sides with no base: every difference is a conflict
                    self._merge_row(name, k, None, l, r, stamps)
            elif l is None:
                if r != b:
                    self._conflict(name, k, "delete_edit", [(L.cols[i], b[i], None, r[i]) for i in range(len(b)) if b[i] != r[i]])
                    rch["changed"].append(k)
                lch["deleted"].append(k)
            elif r is None:
                if name in SOFT_DELETE_TABLES:
                    lch["missing"].append(k)
                    continue
                if l == b:
                    self.deletes[name].add(k)
                else:
                    self._conflict(name, k, "edit_delete", [(L.cols[i], b[i], l[i], None) for i in range(len(b)) if b[i] != l[i]])
                    lch["changed"].append(k)
                rch["deleted"].append(k)
            else:
                self._merge_row(name, k, b, l, r, stamps)

    def _merge_row(self, name, k, b, l, r, stamps):
        cols = self.local.tables[name].cols
        update, clash = {}, []
        local_changed = remote_changed = False
        upper = [c.upper() for c in cols]
        grouped = set()
        for group in COLUMN_GROUPS.get(name, ()):
            g = [upper.index(c.upper()) for c in group if c.upper() in upper]
            grouped.update(g)
            lg, rg = [l[i] for i in g], [r[i] for i in g]
            bg = None if b is None else [b[i] for i in g]
            if lg == rg:
                if bg is not None and lg != bg:
                    local_changed = remote_changed = True
            elif lg == bg:
                update.update({i: r[i] for i in g if l[i] != r[i]})
                remote_changed = True
            elif rg == bg:
                local_changed = True
            else:
                clash += [(cols[i], None if b is None else b[i], l[i], r[i]) for i in g if l[i] != r[i]]
        for i, (lv, rv) in enumerate(zip(l, r)):
            if i in grouped:
                continue
            if lv == rv:
                if b is not None and lv != b[i]:
                    local_changed = remote_changed = True
                continue
            if b is not None and lv == b[i]:
                update[i] = rv
                remote_changed = True
            elif b is not None and rv == b[i]:
                local_changed = True
            elif i in stamps:
                newer = max(lv, rv, key=lambda v: (_num(v) is not None, _num(v) or 0))
                if newer != lv:
                    update[i] = newer
            else:
                clash.append((cols[i], None if b is None else b[i], lv, rv))
        if update:
            self.updates[name][k] = update
        if clash:
            clash.sort(key=lambda f: cols.index(f[0]))
            self._conflict(name, k, "edit", clash)
        if b is not None:
            if remote_changed or clash:
                self.changes[name]["remote"]["changed"].append(k)
            if local_changed or clash:
                self.changes[name]["local"]["changed"].append(k)

    def _merge_unkeyed(self, name, B, L, R):
        lb, rb = L.bag(), R.bag()
        bb = B.bag() if B is not None else None
        if lb == rb:
            return
        if bb is not None and lb == bb:
            self.replace[name] = R
            self.changes[name]["remote"]["changed"].append(())
        elif bb is not None and rb == bb:
            self.changes[name]["local"]["changed"].append(())
        else:
            self._conflict(name, (), "table", [])
            if bb is not None:
                for side in ("remote", "local"):
                    self.changes[name][side]["changed"].append(())

    def _conflict(self, name, k, kind, fields):
        self.conflicts.append(Conflict(_item_id("c", name, k), name, k, kind, fields))

    # -- duplicates ----------------------------------------------------------

    def _find_duplicates(self):
        L, R = self.local.tables.get(TX), self.remote.tables.get(TX)
        if L is None or not (L.keyed and R.keyed):
            return
        base = self.base.tables[TX].rows if self.base else {}
        local_new = [k for k in L.rows if k not in base and k not in R.rows]
        remote_new = [k for k in self.inserts.get(TX, {})]
        if not local_new or not remote_new:
            return
        roots = {}
        for snap in (self.local, self.remote):
            cats = snap.tables.get("ZCATEGORY")
            if cats is None or None in (cats.index("uid"), cats.index("TYPE"), cats.index("STATUS"), cats.index("pUid")):
                continue
            iu, it, ist, ip = (cats.index(c) for c in ("uid", "TYPE", "STATUS", "pUid"))
            for row in cats.rows.values():
                roots[(row[iu], row[it])] = row[ip] if row[ist] == 2 else row[iu]
        lents = [e for e in (_Entity(L, ks) for ks in _group(L, local_new)) if e.live]
        rents = [e for e in (_Entity(R, ks) for ks in _group(R, remote_new)) if e.live]
        by_day = defaultdict(list)
        for e in rents:
            by_day[e.day].append(e)
        pairs = []
        for a in lents:
            if a.day is None:
                near = rents
            else:
                near = [e for d in range(-DUPLICATE_MAX_DAYS, DUPLICATE_MAX_DAYS + 1)
                        for e in by_day.get(date.fromordinal(a.day.toordinal() + d), [])] + by_day.get(None, [])
            for b in near:
                s = _similarity(a, b, roots)
                if s >= DUPLICATE_THRESHOLD:
                    gap = abs((a.time or 0) - (b.time or 0))
                    pairs.append((-s, gap, a.keys[0], b.keys[0], a, b))
        pairs.sort(key=lambda p: p[:4])
        used = set()
        for neg, _gap, _ka, _kb, a, b in pairs:
            if id(a) in used or id(b) in used:
                continue
            used.update((id(a), id(b)))
            self.duplicates.append(Duplicate(_item_id("d", a.keys, b.keys), a.keys, b.keys, -neg))

    # -- decisions -----------------------------------------------------------

    def resolve(self, decisions):
        """Operations for `decisions` ({item id: "local" | "remote" |
        "both"}). A conflict needs a decision ("local" keeps this database's
        side, "remote" the app's); a duplicate defaults to "remote" (keep the
        app's copy and drop the local one), "local" keeps the local copy and
        "both" keeps both."""
        inserts = {t: dict(v) for t, v in self.inserts.items()}
        updates = {t: {k: dict(u) for k, u in v.items()} for t, v in self.updates.items()}
        deletes = {t: set(v) for t, v in self.deletes.items()}
        replace = dict(self.replace)
        unresolved = []
        for c in self.conflicts:
            choice = decisions.get(c.id)
            if choice not in ("local", "remote"):
                unresolved.append(c)
            elif choice == "remote":
                R = self.remote.tables[c.table]
                if c.kind == "edit":
                    cols = R.cols
                    updates.setdefault(c.table, {}).setdefault(c.key, {}).update(
                        {cols.index(col): rv for col, _b, _l, rv in c.fields})
                elif c.kind == "edit_delete":
                    deletes.setdefault(c.table, set()).add(c.key)
                elif c.kind == "delete_edit":
                    inserts.setdefault(c.table, {})[c.key] = (R.pks[c.key], R.rows[c.key])
                elif c.kind == "table":
                    replace[c.table] = R
        for d in self.duplicates:
            choice = decisions.get(d.id, "remote")
            if choice == "local":
                for k in d.remote:
                    inserts[TX].pop(k, None)
            elif choice == "remote":
                deletes.setdefault(TX, set()).update(d.local)
        return Resolution(inserts, updates, deletes, replace, unresolved)

    def _merged_rows(self, res):
        """{table: {key: row}} of the merged result."""
        out = {}
        for name, L in self.local.tables.items():
            if name in res.replace:
                out[name] = res.replace[name].rows
                continue
            ins, upd, dels = res.inserts.get(name), res.updates.get(name), res.deletes.get(name)
            if not (ins or upd or dels):
                out[name] = L.rows
                continue
            rows = dict(L.rows)
            for k in dels or ():
                rows.pop(k, None)
            for k, u in (upd or {}).items():
                if k in rows:
                    row = list(rows[k])
                    for i, v in u.items():
                        row[i] = v
                    rows[k] = tuple(row)
            for k, (_pk, row) in (ins or {}).items():
                rows[k] = row
            out[name] = rows
        return out

    def problems(self, res):
        """References the merged result would break that neither side had
        broken: [(kind, table, key, value)] (see _reference_issues)."""
        if self.schema_problems:
            return []
        cols = {n: t.cols for n, t in self.local.tables.items()}
        merged = _reference_issues(cols, self._merged_rows(res))
        before = _reference_issues(cols, {n: t.rows for n, t in self.local.tables.items()})
        before |= _reference_issues(cols, {n: t.rows for n, t in self.remote.tables.items()})
        return sorted(merged - before, key=str)

    def matches_remote(self, res):
        """True when the merged result would be exactly the remote snapshot
        (nothing from this side survives the merge)."""
        merged = self._merged_rows(res)
        for name, R in self.remote.tables.items():
            rows = merged[name]
            if R.keyed and self.local.tables[name].keyed and name not in res.replace:
                if rows != R.rows:
                    return False
            elif Counter(rows.values()) != R.bag():
                return False
        return True

    def needs_review(self):
        return bool(self.schema_problems or self.conflicts or self.duplicates)

    def write(self, res, out_path):
        """Write the merged database to out_path: a copy of local with the
        resolution applied. Rows copied from the app keep their primary key
        when it's free here, so anything still joined on the numeric ids
        keeps working."""
        src = sqlite3.connect(f"file:{self.local.path}?mode=ro", uri=True)
        dst = sqlite3.connect(out_path)
        try:
            src.backup(dst)
            src.close()
            dst.execute("PRAGMA foreign_keys = OFF")
            with dst:
                for name, R in res.replace.items():
                    dst.execute(f'DELETE FROM "{name}"')
                    for k, row in R.rows.items():
                        self._insert(dst, R, R.pks[k], row)
                for name, keys in res.deletes.items():
                    T = self.local.tables[name]
                    for k in keys:
                        dst.execute(f'DELETE FROM "{name}" WHERE {_where(T)}', k)
                for name, upd in res.updates.items():
                    T = self.local.tables[name]
                    for k, u in upd.items():
                        sets = ", ".join(f'"{T.cols[i]}" = ?' for i in u)
                        dst.execute(f'UPDATE "{name}" SET {sets} WHERE {_where(T)}', [*u.values(), *k])
                for name, ins in res.inserts.items():
                    T = self.local.tables[name]
                    for k, (pk, row) in ins.items():
                        self._insert(dst, T, pk, row)
        finally:
            src.close()
            dst.close()

    @staticmethod
    def _insert(con, T, pk, row):
        cols = list(T.cols)
        values = list(row)
        if T.pk and pk is not None and not con.execute(f'SELECT 1 FROM "{T.name}" WHERE "{T.pk}" = ?', (pk,)).fetchone():
            cols.insert(0, T.pk)
            values.insert(0, pk)
        names = ", ".join(f'"{c}"' for c in cols)
        con.execute(f'INSERT INTO "{T.name}" ({names}) VALUES ({", ".join("?" for _ in cols)})', values)


def _where(T):
    return " AND ".join(f'"{c}" IS ?' for c in T.key)


def build_plan(base_candidates, local, remote):
    """Plan merging `remote` into `local`, on top of whichever of the
    candidate base snapshots `remote` descends from (two-way if none)."""
    return Plan(choose_base(base_candidates, remote), local, remote)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def _group(T, keys):
    """Split INOUTCOME keys into transactions: a transfer's legs and its fee
    row share txUidTrans/txUidFee values and belong together."""
    links = [i for i in (T.index(c) for c in TX_LINK_COLUMNS) if i is not None]
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen = {}
    for k in keys:
        for i in links:
            v = T.rows[k][i]
            if _blank(v):
                continue
            if (i, v) in seen:
                parent[find(k)] = find(seen[(i, v)])
            else:
                seen[(i, v)] = k
    groups = defaultdict(list)
    for k in keys:
        groups[find(k)].append(k)
    return list(groups.values())


class _Entity:
    """The comparable facts of one transaction (a transfer is summarised by
    its outgoing leg)."""

    def __init__(self, T, keys):
        rows = [T.as_dict(T.rows[k]) for k in keys]
        self.keys = sorted(keys)
        self.shape = tuple(sorted(str(r.get("DO_TYPE")) for r in rows))
        p = next((r for r in rows if r.get("DO_TYPE") == "3"), rows[0])
        self.live = not any(_truthy(r.get("IS_DEL")) for r in rows)
        self.account = (p.get("assetUid") or "", p.get("toAssetUid") or "")
        self.amount = _num(p.get("AMOUNT_ACCOUNT"))
        self.day = _day(p.get("WDATE"))
        self.time = _num(p.get("ZDATE"))
        self.category = (p.get("ctgUid") or "", 0 if p.get("DO_TYPE") == "0" else 1)
        self.text = " ".join(str(p.get(c) or "") for c in ("ZCONTENT", "ZDATA")).strip().lower()


def _similarity(a, b, roots):
    """How alike two transactions are; DUPLICATE_THRESHOLD and up is a
    likely duplicate. Same kind of transaction and at most
    DUPLICATE_MAX_DAYS apart, or not comparable at all."""
    if a.shape != b.shape:
        return 0
    days = abs((a.day - b.day).days) if a.day and b.day else None
    if days is not None and days > DUPLICATE_MAX_DAYS:
        return 0
    s = 0.0
    if a.amount is not None and b.amount is not None:
        gap = abs(a.amount - b.amount)
        rel = gap / max(abs(a.amount), abs(b.amount), 0.01)
        s += 4 if gap < 0.005 else 3 if rel <= 0.02 else 1.5 if rel <= 0.10 else 0
    if a.account == b.account:
        s += 2
    if days is not None:
        s += {0: 2, 1: 1.5}.get(days, 0.5)
    if a.time is not None and b.time is not None and abs(a.time - b.time) <= 15 * 60 * 1000:
        s += 0.5
    if a.category[0] and a.category == b.category:
        s += 1.5
    elif roots.get(a.category) and roots.get(a.category) == roots.get(b.category):
        s += 0.75
    if a.text and b.text:
        if a.text == b.text:
            s += 1.5
        elif SequenceMatcher(None, a.text, b.text).ratio() >= 0.6:
            s += 0.75
    return s


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _reference_issues(cols, tables):
    """Broken references in a set of rows ({table: {key: row}}), as
    (kind, table, key, value) tuples:

    account / to_account / category / currency: a live transaction points
        at an account, category (in its tree) or currency that isn't there
    transfer: a live transfer doesn't have exactly one outgoing and one
        incoming leg (key is the txUidTrans value)
    account_currency / account_group: an account's currency or group is
        missing
    category_parent: a subcategory's parent is missing from its tree
    """
    def ix(table, *names):
        c = cols.get(table) or []
        return [c.index(n) if n in c else None for n in names]

    def values(table, col):
        (i,) = ix(table, col)
        return {row[i] for row in tables.get(table, {}).values()} if i is not None else None

    out = set()
    assets, currencies, groups = values("ASSETS", "uid"), values("CURRENCY", "uid"), values("ASSETGROUP", "uid")
    cats = None
    iu, it, ist, ip, idel = ix("ZCATEGORY", "uid", "TYPE", "STATUS", "pUid", "C_IS_DEL")
    if None not in (iu, it):
        cats = {(row[iu], row[it]) for row in tables.get("ZCATEGORY", {}).values()}
        if None not in (ist, ip):
            for k, row in tables["ZCATEGORY"].items():
                if row[ist] == 2 and not (idel is not None and _truthy(row[idel])) and (row[ip], row[it]) not in cats:
                    out.add(("category_parent", "ZCATEGORY", k, row[ip]))

    names = ("DO_TYPE", "IS_DEL", "assetUid", "toAssetUid", "ctgUid", "currencyUid", "txUidTrans")
    itype, idel, iasset, ito, ictg, icur, itrans = ix(TX, *names)
    legs = defaultdict(list)
    for k, row in tables.get(TX, {}).items():
        if itype is None or (idel is not None and _truthy(row[idel])):
            continue
        t = row[itype]
        if assets is not None and iasset is not None and not _blank(row[iasset]) and row[iasset] not in assets:
            out.add(("account", TX, k, row[iasset]))
        if assets is not None and ito is not None and t in ("3", "4") and not _blank(row[ito]) and row[ito] not in assets:
            out.add(("to_account", TX, k, row[ito]))
        if cats is not None and ictg is not None and t in ("0", "1") and row[ictg] not in ("", "-4", None):
            if (row[ictg], 0 if t == "0" else 1) not in cats:
                out.add(("category", TX, k, row[ictg]))
        if currencies is not None and icur is not None and not _blank(row[icur]) and row[icur] not in currencies:
            out.add(("currency", TX, k, row[icur]))
        if itrans is not None and t in ("3", "4") and not _blank(row[itrans]):
            legs[row[itrans]].append(t)
    for trans, types in legs.items():
        if sorted(types) != ["3", "4"]:
            out.add(("transfer", TX, (trans,), tuple(sorted(types))))

    icur, igrp = ix("ASSETS", "currencyUid", "groupUid")
    for k, row in tables.get("ASSETS", {}).items():
        if currencies is not None and icur is not None and not _blank(row[icur]) and row[icur] not in currencies:
            out.add(("account_currency", "ASSETS", k, row[icur]))
        if groups is not None and igrp is not None and not _blank(row[igrp]) and row[igrp] not in groups:
            out.add(("account_group", "ASSETS", k, row[igrp]))
    return out
