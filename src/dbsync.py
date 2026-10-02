"""Keeping a user's working database and the Money Manager app in step.

A sync merges a snapshot of the app's database into the working one
(merge.py) and hands the result back:

- Google Drive: the newest MM*.mmbak in the folder is merged, then the
  result replaces that file's content on Drive, keeping its name, since
  the app only offers its own backups for restoring. When nothing is new on
  Drive, local changes are uploaded the same way.
- Upload: the user uploads a newer export; the merged result can be
  downloaded and restored in the app.

The common ancestor for each merge is the last snapshot taken from the app
(Store.base_path). Whatever was handed back since (Store.pushed_dir) is
also a candidate: if the user restored it in the app, the app's next
backup descends from it instead, and merge.choose_base() picks whichever is
closest. A merge that brings changes from the app waits in the user's
sync/ folder until reviewed on /sync, which lists what each side changed
and any conflicts, likely duplicates or problems to settle first. Every
replacement backs up the working db first (with the incoming snapshot next
to it).
"""
import hashlib
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime

import dbstore
import gdrive
import merge
from dbstore import UnsupportedDatabase, check_db_supported


class SyncError(Exception):
    """A problem worth showing to the user as-is."""


class Result:
    """What a sync step did. status: installed, up_to_date, merged, pushed,
    local_only, push_skipped, push_failed, nothing_new, review, stale.
    level is the flash category; an empty message isn't flashed."""

    def __init__(self, status, message, level="ok"):
        self.status, self.message, self.level = status, message, level

    def __repr__(self):
        return f"Result({self.status!r}, {self.message!r})"


def _remote_info(file):
    if not file:
        return {}
    return {"remote_id": file["id"], "remote_md5": file.get("md5Checksum"), "remote_modified": file.get("modifiedTime")}


def _download(store, file):
    staged = store.staging_path()
    try:
        gdrive.download(store, file, staged)
        if file.get("md5Checksum") and dbstore.file_md5(staged) != file["md5Checksum"]:
            raise gdrive.GDriveError("Download was corrupted (checksum mismatch); try again.")
    except Exception:
        os.remove(staged)
        raise
    return staged


def _ensure_base(store, drive=False):
    """Installs from before sync existed have no base snapshot. Use the
    working db if it hasn't been edited since it was installed, else the
    Drive file it came from if that's unchanged. Without either the merge is
    two-way."""
    if store.base_candidates() or not store.db_exists():
        return
    if not store.local_modified():
        store.set_base(store.db_path)
        return
    info = store.db_info()
    if drive and info.get("remote_id") and info.get("installed_md5"):
        try:
            meta = gdrive.file_meta(store, info["remote_id"])
            if meta.get("md5Checksum") == info["installed_md5"]:
                staged = _download(store, meta)
                try:
                    store.set_base(staged)
                finally:
                    os.remove(staged)
        except gdrive.GDriveError:
            pass


def _known_md5s(store):
    return {dbstore.file_md5(p) for p in store.base_candidates()}


def plan(store):
    """The merge plan for the pending sync."""
    local = merge.Snapshot(store.db_path)
    remote = merge.Snapshot(store.incoming_path)
    # A generator, so only the best candidate so far stays in memory.
    return merge.build_plan((merge.Snapshot(p) for p in store.base_candidates()), local, remote)


def fingerprint(store):
    """Identifies the inputs of the pending merge, so a review submitted
    after the database changed underneath it is caught."""
    h = hashlib.sha1()
    for path in (store.db_path, store.incoming_path):
        h.update(dbstore.file_md5(path).encode())
    return h.hexdigest()[:20]


# ---------------------------------------------------------------------------
# Starting a sync
# ---------------------------------------------------------------------------

def sync_gdrive(store, replace=False):
    """Sync with the newest snapshot on Drive. replace=True (or no working
    db yet) installs it as-is instead, discarding local changes."""
    folder = gdrive.folder_name(store)
    snapshots = gdrive.list_snapshots(store, gdrive.find_folder(store, folder))
    if not snapshots:
        raise gdrive.GDriveError(f"No MM*.mmbak snapshots in the \"{folder}\" folder.")
    latest = snapshots[0]
    if replace or not store.db_exists():
        store.install_db(_download(store, latest), "gdrive", latest["name"], **_remote_info(latest))
        return Result("installed", f"Installed {latest['name']} from Google Drive.")
    _ensure_base(store, drive=True)
    if latest.get("md5Checksum") in _known_md5s(store):
        # Nothing new from the app. In step if Drive still has what the last
        # sync left there (or this exact file) and nothing changed here since.
        same = latest["md5Checksum"] in (store.db_info().get("remote_md5"), dbstore.file_md5(store.db_path))
        if same and not store.unsynced():
            store.update_db_info(synced_at=_now())
            return Result("up_to_date", f"Already up to date with {latest['name']}.")
        return push(store, latest, lead="Nothing new on Drive. ")
    staged = _download(store, latest)
    try:
        check_db_supported(staged)
    except UnsupportedDatabase:
        os.remove(staged)
        raise
    store.save_pending(staged, {"source": "gdrive", "name": latest["name"], "remote": latest})
    return _start_merge(store)


def sync_file(store, staged, name):
    """Sync with an uploaded snapshot (consumed). With no working db yet it's
    simply installed."""
    name = os.path.basename(name)
    if not store.db_exists():
        store.install_db(staged, "manual", name)
        return Result("installed", f"Installed {name}.")
    try:
        check_db_supported(staged)
    except UnsupportedDatabase:
        os.remove(staged)
        raise
    _ensure_base(store)
    if dbstore.file_md5(staged) in _known_md5s(store):
        os.remove(staged)
        if store.unsynced():
            return Result("nothing_new", f"{name} has nothing new since the last sync. The changes made here "
                                         "aren't in the app yet: download the database and restore it in Money Manager.", "warn")
        return Result("up_to_date", f"Already up to date with {name}.")
    store.save_pending(staged, {"source": "manual", "name": name})
    return _start_merge(store)


def _start_merge(store):
    """Show the merge for review, or apply it right away when the app
    changed nothing (only the base moves on, and Drive gets local changes)."""
    pending = store.load_pending()
    p = plan(store)
    needs_review = p.needs_review() or bool(p.problems(p.resolve({})))
    if needs_review:
        return Result("review", f"{pending['name']} needs your review before it's merged.", "warn")
    if _changed(p, "remote"):
        return Result("review", "")  # the review page lists the changes
    return _finish(store, pending, p, p.resolve({}))


def _changed(p, side):
    return any(keys for table in p.changes.values() for keys in table[side].values())


# ---------------------------------------------------------------------------
# Finishing one
# ---------------------------------------------------------------------------

def apply(store, decisions, fp):
    """Apply the pending merge with the user's decisions ({item id: choice})."""
    pending = store.load_pending()
    if not pending:
        return Result("none", "There's no sync waiting for review.", "warn")
    if fp != fingerprint(store):
        return Result("stale", "The database changed while you were reviewing. Check the review again.", "warn")
    p = plan(store)
    if p.schema_problems:
        return Result("review", "This file can't be merged; see below.", "error")
    res = p.resolve(decisions)
    if res.unresolved:
        n = len(res.unresolved)
        return Result("review", f"Choose which version to keep for every conflict ({n} left).", "error")
    if p.problems(res):
        return Result("review", "Fix the problems listed below first, then check again.", "error")
    return _finish(store, pending, p, res)


def _finish(store, pending, p, res):
    name, source, remote = pending["name"], pending["source"], pending.get("remote")
    summary = summarize(p, "remote") or "no changes"
    if p.matches_remote(res):
        # Nothing from this side survives: take the app's file as-is.
        store.install_db(store.incoming_path, source, name, **_remote_info(remote))
        return Result("installed", f"Updated to {name} ({summary} from the app).")
    merged = store.staging_path()
    try:
        p.write(res, merged)
        check = sqlite3.connect(merged)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if ok != "ok":
            raise SyncError(f"The merged database failed its integrity check ({ok}). Nothing was changed.")
    except sqlite3.Error as e:
        os.remove(merged)
        raise SyncError(f"Couldn't build the merged database ({e}). Nothing was changed.") from e
    except SyncError:
        os.remove(merged)
        raise
    store.install_merged(merged, store.incoming_path, source, name, **_remote_info(remote))
    store.clear_pending()
    msg = f"Merged {name}: {summary} from the app; the changes made here were kept."
    if source == "gdrive":
        pushed = push(store, remote)
        return Result("merged", f"{msg} {pushed.message}", "ok" if pushed.status == "pushed" else "warn")
    return Result("merged", f"{msg} Download the database and restore it in Money Manager to get those changes into the app.")


def push(store, target, lead=""):
    """Upload the working db over Drive file `target` (the snapshot the sync
    started from), unless a newer snapshot appeared meanwhile."""
    if not gdrive.can_write(store):
        msg = ("Your changes here aren't on Drive: MMW is connected read-only. Reconnect Google Drive "
               "with write access to upload them.") if gdrive.wants_write(store) else \
              "Your changes here aren't on Drive (Drive is connected read-only)."
        return Result("local_only", lead + msg, "warn")
    try:
        snapshots = gdrive.list_snapshots(store, gdrive.find_folder(store, gdrive.folder_name(store)))
        newest = snapshots[0] if snapshots else {}
        if (newest.get("id"), newest.get("md5Checksum")) != (target["id"], target.get("md5Checksum")):
            return Result("push_skipped", lead + "A newer backup appeared on Drive, so nothing was uploaded. "
                                                 "Sync again to merge it.", "warn")
        current_md5 = dbstore.file_md5(store.db_path)
        copy = store.staging_path()
        try:
            dbstore.copy_db(store.db_path, copy)
            md5 = dbstore.file_md5(copy)
            meta = gdrive.upload(store, target, copy)
            if meta.get("md5Checksum") and meta["md5Checksum"] != md5:
                raise gdrive.GDriveError("the uploaded file doesn't match (checksum mismatch)")
            store.add_pushed(copy)
        finally:
            os.remove(copy)
    except gdrive.GDriveError as e:
        return Result("push_failed", lead + f"Uploading to Drive failed: {str(e).rstrip('.')}. Sync again to retry.", "warn")
    store.update_db_info(name=meta.get("name", target["name"]), **_remote_info(meta), installed_md5=current_md5,
                         unsynced=False, synced_at=_now())
    return Result("pushed", lead + f"Uploaded the result to {meta.get('name', target['name'])} on Drive. Restore that "
                                   "backup in Money Manager to get the changes made here onto your phone.")


def cancel(store):
    store.clear_pending()


def replace_with_incoming(store):
    """Install the pending snapshot as-is, discarding local changes (they're
    backed up first)."""
    pending = store.load_pending()
    if not pending:
        return Result("none", "There's no sync waiting for review.", "warn")
    store.install_db(store.incoming_path, pending["source"], pending["name"], **_remote_info(pending.get("remote")))
    return Result("installed", f"Replaced the database with {pending['name']}. The previous one was backed up.")


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

_STAMP = re.compile(r"\(\d{1,2}-\d{1,2}-\d{2}-\d{6}\)")


def download_name(store):
    """The working db's file name with the app's (m-d-yy-hhmmss) stamp set
    to now, so the download sorts as the newest backup."""
    name = store.db_info().get("name") or "MMW.mmbak"
    now = datetime.now()
    name = _STAMP.sub(f"({now.month}-{now.day}-{now:%y-%H%M%S})", name, count=1)
    return name if name.endswith(".mmbak") else name + ".mmbak"


def download_copy(store):
    """A consistent copy of the working db to download (the caller deletes
    it), remembered as a state handed back to the app."""
    current_md5 = dbstore.file_md5(store.db_path)
    path = store.staging_path()
    dbstore.copy_db(store.db_path, path)
    store.add_pushed(path)
    if store.load_config().get("db_source") != "gdrive":
        store.update_db_info(installed_md5=current_md5, unsynced=False)
    return path


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Presenting a plan
# ---------------------------------------------------------------------------

TABLE_NAMES = {
    "INOUTCOME": ("transaction", "transactions"),
    "ASSETS": ("account", "accounts"),
    "ASSETGROUP": ("account group", "account groups"),
    "ZCATEGORY": ("category", "categories"),
    "CURRENCY": ("currency", "currencies"),
    "BUDGET": ("budget", "budgets"),
    "BUDGET_AMOUNT": ("budget amount", "budget amounts"),
    "REPEATTRANSACTION": ("repeating transaction", "repeating transactions"),
    "FAVTRANSACTION": ("favourite", "favourites"),
    "ZETC": ("app setting", "app settings"),
    "TAG": ("tag", "tags"),
    "TX_TAG": ("transaction tag", "transaction tags"),
    "MEMO": ("memo", "memos"),
    "PHOTO": ("photo", "photos"),
}
TYPE_LABELS = {"0": "Income", "1": "Expense", "3": "Transfer", "4": "Transfer (incoming leg)",
               "7": "Adjustment +", "8": "Adjustment -"}
FIELD_LABELS = {
    "WDATE": "Date", "ZDATE": "Date and time", "DO_TYPE": "Type", "AMOUNT_ACCOUNT": "Amount",
    "IN_ZMONEY": "Entered amount", "ZMONEY": "Amount in main currency", "assetUid": "Account",
    "toAssetUid": "To account", "ctgUid": "Category", "currencyUid": "Currency", "IS_DEL": "Deleted",
    "UTIME": "Updated", "NIC_NAME": "Name", "NAME": "Name", "ORDERSEQ": "Order", "groupUid": "Group",
    "pUid": "Parent", "ACC_GROUP_NAME": "Name", "ISO": "Code", "RATE": "Rate", "C_IS_DEL": "Deleted",
    "txUidTrans": "Transfer link", "txUidFee": "Fee link",
}
# Columns whose meaning depends on the table.
TABLE_FIELD_LABELS = {merge.TX: {"ZCONTENT": "Note", "ZDATA": "Description"}, "ASSETS": {"ZDATA": "Status"}}
ACCOUNT_STATUS = {"0": "normal", "1": "deleted", "3": "hidden"}  # ASSETS.ZDATA


def summarize(p, side):
    """"27 new transactions, 1 changed category" for one side's changes."""
    return _side_changes(p, _Names.merged(p), side, {})["summary"]


def _summary(counts):
    """The summary line for a Counter of (table, what), in list order."""
    parts = []
    for (table, what), n in sorted(counts.items(), key=lambda i: (i[0][0] != merge.TX, i[0][0], CHANGE_ORDER[i[0][1]])):
        one, many = TABLE_NAMES.get(table, (f"{table} row", f"{table} rows"))
        parts.append(f"{n} {CHANGE_WORDS[what]} {one if n == 1 else many}")
    return ", ".join(parts)


class _Names:
    """uid -> display name lookups across snapshots (earlier ones win; see
    merged() for a plan's)."""

    LOOKUPS = ("accounts", "account_iso", "groups", "categories", "currencies")

    @classmethod
    def merged(cls, p):
        """Names as the merge leaves them: the working db's, unless only the
        app changed one since the base."""
        local, remote, base = cls(p.local), cls(p.remote), cls(p.base)
        out = cls()
        for attr in cls.LOOKUPS:
            l, r, b = (getattr(n, attr) for n in (local, remote, base))
            names = {**b, **r, **l}
            names.update({k: v for k, v in r.items() if k in b and k in l and l[k] == b[k]})
            setattr(out, attr, names)
        return out

    def __init__(self, *snapshots):
        self.accounts, self.account_iso, self.groups = {}, {}, {}
        self.categories, self.currencies = {}, {}
        for snap in snapshots:
            if snap is None:
                continue
            t = snap.tables
            for row in self._rows(t.get("CURRENCY")):
                self.currencies.setdefault(row.get("uid"), row.get("ISO") or row.get("uid"))
            for row in self._rows(t.get("ASSETGROUP")):
                self.groups.setdefault(row.get("uid"), row.get("ACC_GROUP_NAME") or row.get("uid"))
            for row in self._rows(t.get("ASSETS")):
                self.accounts.setdefault(row.get("uid"), row.get("NIC_NAME") or row.get("uid"))
                self.account_iso.setdefault(row.get("uid"), row.get("currencyUid"))
            cats = list(self._rows(t.get("ZCATEGORY")))
            names = {(c.get("uid"), c.get("TYPE")): c.get("NAME") for c in cats}
            for c in cats:
                key = (c.get("uid"), c.get("TYPE"))
                parent = names.get((c.get("pUid"), c.get("TYPE"))) if c.get("STATUS") == 2 else None
                self.categories.setdefault(key, f"{parent} > {c.get('NAME')}" if parent else c.get("NAME"))

    @staticmethod
    def _rows(table):
        if table is None:
            return []
        return (table.as_dict(row) for row in table.rows.values())

    def category(self, uid, do_type):
        if uid == "-4":
            return "(adjustment)"
        if not uid:
            return ""
        return self.categories.get((uid, 0 if do_type == "0" else 1), uid)

    def fmt(self, col, value, row=None, table=None):
        """A column value as the user would recognise it."""
        if value is None or value == "":
            return ""
        if table == "ASSETS" and col == "ZDATA":
            return ACCOUNT_STATUS.get(str(value), str(value))
        if col in ("assetUid", "toAssetUid", "cardAssetUid"):
            return self.accounts.get(value, value)
        if col == "ctgUid":
            return self.category(value, (row or {}).get("DO_TYPE"))
        if col == "currencyUid":
            return self.currencies.get(value, value)
        if col == "groupUid":
            return self.groups.get(value, value)
        if col == "pUid":
            return self.categories.get((value, (row or {}).get("TYPE")), value)
        if col == "DO_TYPE":
            return TYPE_LABELS.get(str(value), value)
        if col in ("IS_DEL", "C_IS_DEL"):
            return "yes" if merge._truthy(value) else "no"
        if col == "ZDATE" or col.upper() in merge.TIMESTAMP_COLUMNS:
            ms = merge._num(value)
            if ms and ms > 10 ** 11:
                return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        if col == "AMOUNT_ACCOUNT":
            n = merge._num(value)
            if n is not None:
                return f"{n:,.2f}"
        return str(value)

    def tx(self, row):
        """A transaction row (dict) for display."""
        ms = merge._num(row.get("ZDATE"))
        amount = merge._num(row.get("AMOUNT_ACCOUNT"))
        iso = self.currencies.get(self.account_iso.get(row.get("assetUid")), "")
        account = self.accounts.get(row.get("assetUid"), row.get("assetUid") or "")
        if row.get("DO_TYPE") in ("3", "4") and row.get("toAssetUid"):
            account += " → " + self.accounts.get(row["toAssetUid"], row["toAssetUid"])
        return {
            "uid": row.get("uid"),
            "date": row.get("WDATE") or "",
            "time": datetime.fromtimestamp(ms / 1000).strftime("%H:%M") if ms and ms > 10 ** 11 else "",
            "type": TYPE_LABELS.get(str(row.get("DO_TYPE")), row.get("DO_TYPE")),
            "income": row.get("DO_TYPE") in ("0", "7"),
            "account": account,
            "category": self.category(row.get("ctgUid"), row.get("DO_TYPE")),
            "amount": f"{amount:,.2f}" if amount is not None else "",
            "currency": iso,
            "note": row.get("ZCONTENT") or "",
            "description": row.get("ZDATA") or "",
            "deleted": merge._truthy(row.get("IS_DEL")),
        }

    def title(self, table, row):
        """One line naming a row of any table."""
        if table == merge.TX:
            t = self.tx(row)
            bits = [t["date"], t["type"], f"{t['amount']} {t['currency']}".strip(), t["account"], t["category"], t["note"]]
            return " · ".join(b for b in bits if b)
        if table == "ASSETS":
            return f"Account “{row.get('NIC_NAME')}”"
        if table == "ZCATEGORY":
            tree = "income" if row.get("TYPE") == 0 else "expense"
            return f"Category “{self.category(row.get('uid'), '0' if row.get('TYPE') == 0 else '1')}” ({tree})"
        if table == "ASSETGROUP":
            return f"Account group “{row.get('ACC_GROUP_NAME')}”"
        if table == "CURRENCY":
            return f"Currency {row.get('ISO')}"
        if table == "ZETC":
            return f"App setting {row.get('dataTypeKey') or row.get('uid')}"
        one = TABLE_NAMES.get(table, (f"{table} row",))[0]
        return f"{one[0].upper()}{one[1:]} {row.get('uid') or row.get('UID') or ''}".strip()


def _field_label(table, col):
    return TABLE_FIELD_LABELS.get(table, {}).get(col) or FIELD_LABELS.get(col, col)


CONFLICT_TEXT = {
    "edit": ("Changed here and in the app", "Keep this database's version", "Take the app's version"),
    "edit_delete": ("Changed here, deleted in the app", "Keep it, with the changes made here", "Delete it, as in the app"),
    "delete_edit": ("Deleted here, changed in the app", "Keep it deleted", "Restore it with the app's changes"),
    "table": ("Changed here and in the app", "Keep this database's table", "Take the app's table"),
}
PROBLEM_WHAT = {"account": "account", "to_account": "destination account", "category": "category", "currency": "currency"}


def review(store, decisions=None):
    """Everything the review page shows for the pending sync, or None."""
    pending = store.load_pending()
    if not pending:
        return None
    decisions = decisions or {}
    p = plan(store)
    names = _Names.merged(p)
    view = {
        "pending": pending,
        "fingerprint": fingerprint(store),
        "two_way": p.two_way,
        "schema_problems": p.schema_problems,
        "from_app": "",
        "from_here": "",
        "changes": {},
        "duplicates": [],
        "conflicts": [],
        "problems": [],
    }
    if p.schema_problems:
        return view
    R, L = p.remote.tables.get(merge.TX), p.local.tables.get(merge.TX)
    marks = {}
    for c in p.conflicts:
        marks[("remote", c.table, c.key)] = marks[("local", c.table, c.key)] = "conflict"
    for d in p.duplicates:
        marks.update({("local", merge.TX, k): "duplicate" for k in d.local})
        marks.update({("remote", merge.TX, k): "duplicate" for k in d.remote})
    for side in ("remote", "local"):
        view["changes"][side] = _side_changes(p, names, side, marks)
    view["from_app"], view["from_here"] = (view["changes"][s]["summary"] for s in ("remote", "local"))

    for d in p.duplicates:
        local = [names.tx(L.as_dict(L.rows[k])) for k in d.local]
        remote = [names.tx(R.as_dict(R.rows[k])) for k in d.remote]
        differs = {f for f in ("date", "time", "type", "account", "category", "amount", "note", "description")
                   if local[0][f] != remote[0][f]}
        view["duplicates"].append({"id": d.id, "local": local, "remote": remote, "differs": differs,
                                   "choice": decisions.get(d.id, "remote")})

    for c in p.conflicts:
        view["conflicts"].append(_conflict_view(p, names, c, decisions.get(c.id)))

    for issue in p.problems(p.resolve(decisions)):
        view["problems"].append(_problem_view(p, names, issue))
    return view


# Bookkeeping columns: a row where nothing else changed is only counted, and
# they're left out of the fields listed for the others. ZMONEY is restated
# at export time (docs/MM_DB_SCHEMA.md, rule 4).
QUIET_COLUMNS = merge.TIMESTAMP_COLUMNS | {"SYNCTIME", "SYNCVERSION", "ISSYNCED", "SYNC_CHECK", "A_SYNC_CHECK",
                                           "C_SYNC_CHECK", "ZMONEY"}
# The INOUTCOME columns behind the transaction table's columns.
TX_SHOWN = {"WDATE", "ZDATE", "DO_TYPE", "assetUid", "toAssetUid", "ctgUid", "AMOUNT_ACCOUNT", "ZCONTENT", "ZDATA",
            "IS_DEL"}
CHANGE_ORDER = {"added": 0, "changed": 1, "restored": 2, "deleted": 3}
CHANGE_WORDS = {"added": "new", "changed": "changed", "restored": "restored", "deleted": "deleted"}


def _tx_was(old, new):
    """The transaction table's cells that differ between two names.tx()
    dicts, with the old value to show."""
    out = {f: old[f] for f in ("type", "account", "category", "note", "description") if old[f] != new[f]}
    if (old["date"], old["time"]) != (new["date"], new["time"]):
        out["date"] = f"{old['date']} {old['time']}".strip()
    if (old["amount"], old["currency"]) != (new["amount"], new["currency"]):
        out["amount"] = f"{old['amount']} {old['currency']}".strip()
    return out


def _side_changes(p, names, side, marks):
    """What one side ("remote" or "local") changed since the base, for the
    review page: transactions (names.tx() dicts plus what happened, the old
    values of changed cells and other changed fields), rows of other tables,
    how many rows changed only in bookkeeping columns, and the summary line
    counting all of them."""
    snap = p.remote if side == "remote" else p.local
    txs, rows, quiet = [], [], Counter()
    for table in p.changes:
        S, B = snap.tables[table], (p.base.tables[table] if p.base else None)
        for what, keys in p.changes[table][side].items():
            for k in keys:
                mark = marks.get((side, table, k))
                if k == ():  # an unkeyed table, compared whole
                    rows.append({"table": table, "what": what, "fields": [], "mark": mark,
                                 "title": f"All {TABLE_NAMES.get(table, (None, table))[1]}"})
                    continue
                new = S.as_dict(S.rows[k]) if what != "deleted" else None
                old = B.as_dict(B.rows[k]) if what != "added" else None
                fields = [c for c in S.cols if old[c] != new[c] and c.upper() not in QUIET_COLUMNS] \
                    if what == "changed" else []
                if what == "changed" and not fields:
                    quiet[table] += 1
                    continue
                # Soft deletes (IS_DEL and the like) read as deleted/restored.
                flag = next((c for c in fields if c in ("IS_DEL", "C_IS_DEL")), None)
                shown = ("deleted" if merge._truthy(new[flag]) else "restored") if flag else what
                hidden = TX_SHOWN if table == merge.TX else {flag}
                other = [(_field_label(table, c), names.fmt(c, old[c], old, table), names.fmt(c, new[c], new, table))
                         for c in fields if c not in hidden]
                if table != merge.TX:
                    rows.append({"table": table, "what": shown, "title": names.title(table, new or old),
                                 "fields": other, "mark": mark})
                    continue
                row = new or old
                t = names.tx(row)
                t.update(what=shown, was=_tx_was(names.tx(old), t) if what == "changed" else {}, other=other,
                         mark=mark, trans=row.get("txUidTrans"), leg=row.get("DO_TYPE"))
                txs.append(t)
    # A transfer is listed once, by its outgoing leg, unless only the
    # incoming one changed.
    outgoing = {(t["what"], t["trans"]) for t in txs if t["leg"] == "3" and t["trans"]}
    txs = [t for t in txs if not (t["leg"] == "4" and (t["what"], t["trans"]) in outgoing)]
    txs.sort(key=lambda t: (t["date"], t["time"]), reverse=True)
    txs.sort(key=lambda t: CHANGE_ORDER[t["what"]])
    rows.sort(key=lambda r: (CHANGE_ORDER[r["what"]], r["title"]))
    for item in txs + rows:
        item["word"] = CHANGE_WORDS[item["what"]]
    counts = Counter((merge.TX, t["what"]) for t in txs) + Counter((r["table"], r["what"]) for r in rows)
    counts += Counter({(table, "changed"): n for table, n in quiet.items()})
    return {"summary": _summary(counts), "transactions": txs, "rows": rows, "quiet": sum(quiet.values())}


def _conflict_view(p, names, c, choice):
    heading, keep_local, take_remote = CONFLICT_TEXT[c.kind]
    out = {"id": c.id, "kind": c.kind, "heading": heading, "local_label": keep_local,
           "remote_label": take_remote, "choice": choice, "fields": [], "two_way": p.two_way}
    if c.kind == "table":
        out["title"] = f"All {TABLE_NAMES.get(c.table, (None, c.table))[1]}"
        return out
    rows = [s.tables[c.table].rows.get(c.key) for s in (p.local, p.remote, p.base) if s is not None]
    T = p.local.tables[c.table]
    row = T.as_dict(next(r for r in rows if r is not None))
    out["title"] = names.title(c.table, row)
    for col, b, l, r in c.fields:
        out["fields"].append({
            "label": _field_label(c.table, col),
            "base": names.fmt(col, b, row, c.table),
            "local": "(deleted)" if c.kind == "delete_edit" else names.fmt(col, l, row, c.table),
            "remote": "(deleted)" if c.kind == "edit_delete" else names.fmt(col, r, row, c.table),
        })
    return out


def _find_row(p, table, key):
    for snap, origin in ((p.local, "here"), (p.remote, "app")):
        T = snap.tables.get(table)
        if T is not None and key in T.rows:
            return T.as_dict(T.rows[key]), origin
    return {}, ""


def _problem_view(p, names, issue):
    kind, table, key, value = issue
    lt, rt = p.local.tables, p.remote.tables
    if kind == "transfer":
        legs = []
        for snap in (p.local, p.remote):
            T = snap.tables[table]
            i = T.index("txUidTrans")
            legs += [T.as_dict(r) for r in T.rows.values() if r[i] == key[0]]
        row = next((r for r in legs if r.get("DO_TYPE") == "3"), legs[0] if legs else {})
        return {"text": "This transfer would be left with only one of its two legs, because one leg was deleted "
                        "on one side and the other was changed on the other.",
                "tx": names.tx(row) if row else None,
                "fix": "Delete or restore the transfer on one side, then check again."}
    row, origin = _find_row(p, table, key)
    in_local = _has(lt, kind, value, row)
    deleted_where = "in the app" if in_local else "here"
    if kind in PROBLEM_WHAT:
        what = PROBLEM_WHAT[kind]
        shown = names.category(value, row.get("DO_TYPE")) if kind == "category" else \
            names.currencies.get(value, value) if kind == "currency" else names.accounts.get(value, value)
        source = "added here" if origin == "here" else "from the app"
        return {"text": f"A transaction {source} uses the {what} “{shown}”, which was deleted {deleted_where}.",
                "tx": names.tx(row),
                "fix": f"Give the transaction another {what} (or restore the {what}) on one side, then check again."}
    if kind in ("account_currency", "account_group"):
        what = "currency" if kind == "account_currency" else "account group"
        shown = names.currencies.get(value, value) if kind == "account_currency" else names.groups.get(value, value)
        return {"text": f"{names.title('ASSETS', row)} uses the {what} “{shown}”, which was deleted {deleted_where}.",
                "tx": None, "fix": f"Give the account another {what} on one side, then check again."}
    return {"text": f"{names.title('ZCATEGORY', row)} belongs to a parent category that was deleted {deleted_where}.",
            "tx": None, "fix": "Move or delete the subcategory on one side, then check again."}


def _has(tables, kind, value, row):
    """Whether the thing an issue points at exists in these tables."""
    lookup = {
        "account": ("ASSETS", "uid"), "to_account": ("ASSETS", "uid"), "currency": ("CURRENCY", "uid"),
        "account_currency": ("CURRENCY", "uid"), "account_group": ("ASSETGROUP", "uid"),
    }
    if kind == "category":
        T = tables.get("ZCATEGORY")
        return T is not None and (value, 0 if row.get("DO_TYPE") == "0" else 1) in {
            (r[T.index("uid")], r[T.index("TYPE")]) for r in T.rows.values()}
    if kind == "category_parent":
        T = tables.get("ZCATEGORY")
        return T is not None and (value, row.get("TYPE")) in {
            (r[T.index("uid")], r[T.index("TYPE")]) for r in T.rows.values()}
    table, col = lookup[kind]
    T = tables.get(table)
    return T is not None and value in {r[T.index(col)] for r in T.rows.values()}


def decisions_from_form(form):
    """{item id: choice} from the review form's choice_<id> fields."""
    out = {}
    for name, value in form.items():
        if name.startswith("choice_") and value in ("local", "remote", "both"):
            out[name[len("choice_"):]] = value
    return out
