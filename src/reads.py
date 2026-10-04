"""Every read of a user's MM database outside sync: the transaction list,
its filters and their counts, accounts with their balances, the category
trees and their totals, and the lookups the pages fill their pickers with.

The pages and the public /api/v1/ routes both read through here, so a
transaction a page lists and the same one from the API come from the same
SQL, and a new page or client starts from what is already here. Shaping the
rows for a page or an API answer is the caller's job.

Each function takes a connection from the caller (app.read_db(), or the
write connection when a read has to see an edit in progress) with MMW's
change log attached as "mmw" (app.get_db() does that, see
dbstore.Store.stamp_changes), and returns sqlite3.Row rows, or plain lists
and dicts. Like edits.py, this module doesn't import Flask.
"""
from dbstore import CHANGES_TABLE


def _rows(con, sql, params=()):
    return con.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def get_accounts(con):
    """Every account with its currency, group and status (ZDATA, see
    edits.ACCOUNT_STATUSES), in the app's group/account order."""
    return _rows(con, """
        SELECT a.uid, a.NIC_NAME, a.currencyUid, c.ISO, a.groupUid AS group_uid, g.ACC_GROUP_NAME,
               a.ZDATA AS account_flags
        FROM ASSETS a
        LEFT JOIN CURRENCY c ON c.uid = a.currencyUid
        LEFT JOIN ASSETGROUP g ON g.uid = a.groupUid
        ORDER BY g.ORDERSEQ, a.ORDERSEQ, a.NIC_NAME
    """)


def get_account_rows(con):
    """Every account with its group, currency, live-row count and balance,
    in the app's group/account order."""
    return _rows(con, """
        SELECT a.uid, a.NIC_NAME, a.currencyUid AS currency_uid, c.ISO, g.uid AS group_uid, g.ACC_GROUP_NAME,
               a.ORDERSEQ, a.TYPE AS account_type, a.ZDATA AS account_flags,
               a.CARD_ACCOUNT_NAME,
               datetime(CAST(a.A_UTIME AS INTEGER) / 1000, 'unixepoch', 'localtime') AS updated_str,
               COUNT(i.uid) AS tx_count,
               ROUND(SUM(CASE i.DO_TYPE
                   WHEN '0' THEN i.AMOUNT_ACCOUNT
                   WHEN '7' THEN i.AMOUNT_ACCOUNT
                   WHEN '4' THEN i.AMOUNT_ACCOUNT
                   ELSE -i.AMOUNT_ACCOUNT END), 2) AS balance
        FROM ASSETS a
        LEFT JOIN CURRENCY c ON c.uid = a.currencyUid
        LEFT JOIN ASSETGROUP g ON g.uid = a.groupUid
        LEFT JOIN INOUTCOME i ON i.assetUid = a.uid AND i.IS_DEL = 0
        GROUP BY a.uid
        ORDER BY g.ORDERSEQ, a.ORDERSEQ, a.NIC_NAME
    """)


def get_asset_groups(con):
    """Account groups in the app's order, with their TYPE (11: cash)."""
    return _rows(con, "SELECT uid, ACC_GROUP_NAME, TYPE FROM ASSETGROUP ORDER BY ORDERSEQ")


def get_bookmarks(con):
    """The app's bookmarks (FAVTRANSACTION): transactions saved to enter
    again. PAYEE holds the note and MEMO the description, as in the app's
    repeat templates."""
    return _rows(con, """
        SELECT uid, DO_TYPE, assetUid, toAssetUid, ctgUid, AMOUNT_SUB, currencyUid, PAYEE, MEMO
        FROM FAVTRANSACTION WHERE IFNULL(IS_DEL, 0) = 0
        ORDER BY ORDERSEQ, USETIME DESC
    """)


def get_settings(con):
    """The app's own settings (ZETC: week_start_day, start_day, ...) as
    {key: text}."""
    return {r["dataTypeKey"]: r["ZDATA"] for r in _rows(
        con, "SELECT dataTypeKey, ZDATA FROM ZETC WHERE IFNULL(dataTypeKey, '') <> ''")}


def get_budgets(con, month):
    """Each live budget (BUDGET: IS_TOTAL, or a category in targetUid) with
    the amount in force in month (YYYYMM): its latest BUDGET_AMOUNT period
    at or before it, else its default (period 0)."""
    return _rows(con, """
        SELECT b.uid, b.targetUid AS category, b.IS_TOTAL AS is_total, b.DO_TYPE, b.PERIOD_TYPE,
               (SELECT a.AMOUNT FROM BUDGET_AMOUNT a
                WHERE a.budgetUid = b.uid AND IFNULL(a.IS_DEL, 0) = 0 AND CAST(a.BUDGET_PERIOD AS INTEGER) <= ?
                ORDER BY CAST(a.BUDGET_PERIOD AS INTEGER) DESC LIMIT 1) AS amount
        FROM BUDGET b
        WHERE IFNULL(b.IS_DEL, 0) = 0
        ORDER BY b.ORDER_SEQ
    """, (int(month),))


def get_currencies(con):
    """Every currency, with its rate to the main currency (RATE: units of
    the main currency per unit), decimal places, symbol, whether it's the
    main one and the app's order for it (ORDER_SEQ)."""
    return _rows(con, "SELECT uid, ISO, RATE, DECIMAL_POINT, SYMBOL, IS_MAIN_CURRENCY, ORDER_SEQ FROM CURRENCY ORDER BY ISO")


def get_used_currencies(con):
    """The currencies transactions were entered in."""
    return _rows(con, """
        SELECT DISTINCT cu.uid, cu.ISO
        FROM INOUTCOME i
        JOIN CURRENCY cu ON cu.uid = i.currencyUid
        ORDER BY cu.ISO
    """)


def get_transaction_years(con):
    rows = _rows(con, "SELECT DISTINCT substr(WDATE, 1, 4) AS y FROM INOUTCOME "
                      "WHERE WDATE IS NOT NULL AND WDATE <> '' ORDER BY y")
    return [r["y"] for r in rows]


# The free-text fields get_suggestions() can complete, and their columns.
SUGGESTION_COLUMNS = {"note": "ZCONTENT", "description": "ZDATA"}


def get_suggestions(con, field, limit=1000):
    """Distinct non-empty values of a live transaction's note or
    description, most recently used first, for autocomplete on free-text
    inputs."""
    column = SUGGESTION_COLUMNS[field]
    rows = _rows(con, f"""
        SELECT {column} AS v FROM INOUTCOME
        WHERE IFNULL({column}, '') <> '' AND IS_DEL = 0
        GROUP BY {column}
        ORDER BY MAX(CAST(UTIME AS INTEGER)) DESC
        LIMIT ?
    """, (limit,))
    return [r["v"] for r in rows]


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def get_categories(con, type_filter=None, deleted=False):
    """Live categories (deleted ones too with deleted=True), of one tree (0
    income, 1 expense) or both, roots first. is_deleted is 1 for a deleted
    one."""
    sql = """
        SELECT c.uid, c.NAME, c.TYPE, c.STATUS, c.pUid, c.ORDERSEQ, c.C_IS_DEL, p.NAME AS parent_name,
               IFNULL(NULLIF(c.C_IS_DEL, ''), 0) + 0 = 1 AS is_deleted
        FROM ZCATEGORY c
        LEFT JOIN ZCATEGORY p ON p.uid = c.pUid AND p.TYPE = c.TYPE
        WHERE (? OR IFNULL(NULLIF(c.C_IS_DEL, ''), 0) + 0 <> 1)
    """
    params = (bool(deleted),)
    if type_filter is not None:
        sql += " AND c.TYPE = ?"
        params += (type_filter,)
    sql += " ORDER BY c.TYPE DESC, c.STATUS, c.ORDERSEQ"
    return _rows(con, sql, params)


def build_category_tree(con, type_filter, deleted=False):
    """[{uid, name, deleted, children:[{uid, name, deleted}]}] for one tree
    (0=income, 1=expense), with deleted categories too if deleted=True."""
    rows = get_categories(con, type_filter, deleted)
    roots = [r for r in rows if r["STATUS"] == 0]
    children = [r for r in rows if r["STATUS"] == 2]
    tree = []
    for r in roots:
        kids = [{"uid": c["uid"], "name": c["NAME"], "deleted": bool(c["is_deleted"])}
                for c in children if c["pUid"] == r["uid"]]
        tree.append({"uid": r["uid"], "name": r["NAME"], "deleted": bool(r["is_deleted"]), "children": kids})
    return tree


def category_lookup(con, type_filter):
    """{uid: {name, status, parent_uid}} for one tree."""
    rows = get_categories(con, type_filter)
    return {r["uid"]: {"name": r["NAME"], "status": r["STATUS"], "parent_uid": r["pUid"]} for r in rows}


def category_lookups(con):
    """category_lookup() of both trees, by tree (0 income, 1 expense)."""
    return {0: category_lookup(con, 0), 1: category_lookup(con, 1)}


def category_path(lookup, uid):
    """(root uid, "Root > Child" or "Root") for a category in a
    category_lookup() tree, or ("", "") if it isn't there."""
    cat = lookup.get(uid)
    if not cat:
        return "", ""
    if cat["status"] == 0:
        return uid, cat["name"]
    root_uid = cat["parent_uid"]
    return root_uid, f'{lookup.get(root_uid, {}).get("name", "")} > {cat["name"]}'


def get_category_totals(con):
    """{category uid: [(account currency ISO or "?", total, rows), ...]} over
    live transactions, one entry per currency since amounts are never
    converted between currencies."""
    totals = {}
    for r in _rows(con, """
        SELECT c.uid, acu.ISO AS currency_iso,
               ROUND(SUM(i.AMOUNT_ACCOUNT), 2) AS total, COUNT(i.uid) AS rows
        FROM ZCATEGORY c
        JOIN INOUTCOME i ON i.ctgUid = c.uid AND i.IS_DEL = 0
        LEFT JOIN ASSETS a ON a.uid = i.assetUid
        LEFT JOIN CURRENCY acu ON acu.uid = a.currencyUid
        GROUP BY c.uid, acu.ISO
        ORDER BY acu.ISO
    """):
        totals.setdefault(r["uid"], []).append((r["currency_iso"] or "?", r["total"], r["rows"]))
    return totals


# ---------------------------------------------------------------------------
# Transaction filters
# ---------------------------------------------------------------------------

def _escape_like(s):
    """Escape %, _ and \\ so a value can be embedded in a LIKE pattern literally."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _text_search_clause(column, raw):
    """Build a LIKE/NOT LIKE clause for a free-text search box. A leading '!'
    negates the match (e.g. '!foo' = does not contain 'foo')."""
    negate = raw.startswith("!")
    term = raw[1:] if negate else raw
    op = "NOT LIKE" if negate else "LIKE"
    clause = f"IFNULL({column}, '') {op} ? ESCAPE '\\'"
    return clause, f"%{_escape_like(term)}%"


def parse_transaction_filters(con, args):
    """Parse the transaction filters (the /editor/transactions query string, which
    /api/v1/transactions takes too; args has get() and getlist(), like
    request.args) into (a) the raw values a filter bar needs to re-render and
    (b) a `build_where` closure that every transaction read below uses to
    build the same WHERE clause. Values that don't parse are ignored."""
    account_uids = [v for v in args.getlist("account") if v]
    category_uids = [v for v in args.getlist("category") if v]
    # Root categories selected on their own, without their children.
    category_only_uids = [v for v in args.getlist("category_only") if v]
    do_types = [v for v in args.getlist("type") if v]
    date_from = args.get("from", "")
    date_to = args.get("to", "")
    q_note = args.get("q_note", "")
    q_desc = args.get("q_desc", "")
    has_note = args.get("has_note", "")  # "", "yes", "no"
    has_desc = args.get("has_desc", "")
    amount_min = args.get("amount_min", "")
    amount_max = args.get("amount_max", "")
    entered_currency = args.get("entered_currency", "")
    show_deleted = args.get("show_deleted", "hide")
    if show_deleted not in ("hide", "show", "only"):
        show_deleted = "hide"
    show_mirror = args.get("show_mirror", "hide")
    if show_mirror not in ("hide", "show", "only"):
        show_mirror = "hide"

    # A selected root category also pulls in all of its children, in either tree.
    expanded_category_uids = set(category_uids) | set(category_only_uids)
    if category_uids:
        roots = [r for t in (0, 1) for r in build_category_tree(con, t) if r["uid"] in category_uids]
        expanded_category_uids.update(c["uid"] for r in roots for c in r["children"])

    # Named filter clauses, so per-facet counts (transaction_facets()) can
    # recompute with any one of them left out (e.g. "how many deleted rows
    # match everything else?").
    filter_specs = []  # [(name, sql_clause, params)]

    if account_uids:
        placeholders = ",".join("?" for _ in account_uids)
        filter_specs.append(("account", f"i.assetUid IN ({placeholders})", list(account_uids)))
    if expanded_category_uids:
        placeholders = ",".join("?" for _ in expanded_category_uids)
        filter_specs.append(("category", f"i.ctgUid IN ({placeholders})", list(expanded_category_uids)))
    if do_types:
        placeholders = ",".join("?" for _ in do_types)
        filter_specs.append(("type", f"i.DO_TYPE IN ({placeholders})", list(do_types)))
    if date_from:
        filter_specs.append(("date_from", "i.WDATE >= ?", [date_from]))
    if date_to:
        filter_specs.append(("date_to", "i.WDATE <= ?", [date_to]))
    if q_note:
        clause, param = _text_search_clause("i.ZCONTENT", q_note)
        filter_specs.append(("q_note", clause, [param]))
    if q_desc:
        clause, param = _text_search_clause("i.ZDATA", q_desc)
        filter_specs.append(("q_desc", clause, [param]))
    if has_note == "yes":
        filter_specs.append(("has_note", "IFNULL(i.ZCONTENT, '') <> ''", []))
    elif has_note == "no":
        filter_specs.append(("has_note", "IFNULL(i.ZCONTENT, '') = ''", []))
    if has_desc == "yes":
        filter_specs.append(("has_desc", "IFNULL(i.ZDATA, '') <> ''", []))
    elif has_desc == "no":
        filter_specs.append(("has_desc", "IFNULL(i.ZDATA, '') = ''", []))
    if amount_min:
        try:
            filter_specs.append(("amount_min", "i.AMOUNT_ACCOUNT >= ?", [float(amount_min)]))
        except ValueError:
            amount_min = ""
    if amount_max:
        try:
            filter_specs.append(("amount_max", "i.AMOUNT_ACCOUNT <= ?", [float(amount_max)]))
        except ValueError:
            amount_max = ""
    if entered_currency:
        filter_specs.append(("entered_currency", "i.currencyUid = ?", [entered_currency]))

    def build_where(exclude=None):
        clauses = []
        p = []
        if exclude != "show_deleted":
            if show_deleted == "hide":
                clauses.append("i.IS_DEL = 0")
            elif show_deleted == "only":
                clauses.append("i.IS_DEL <> 0")
        if exclude != "show_mirror":
            if show_mirror == "hide":
                clauses.append("i.DO_TYPE <> '4'")
            elif show_mirror == "only":
                clauses.append("i.DO_TYPE = '4'")
        for name, clause, cparams in filter_specs:
            if name == exclude:
                continue
            clauses.append(clause)
            p.extend(cparams)
        return (" AND ".join(clauses) if clauses else "1=1"), p

    return {
        "account_uids": account_uids, "category_uids": category_uids,
        "category_only_uids": category_only_uids, "do_types": do_types,
        "date_from": date_from, "date_to": date_to, "q_note": q_note, "q_desc": q_desc,
        "has_note": has_note, "has_desc": has_desc, "amount_min": amount_min, "amount_max": amount_max,
        "entered_currency": entered_currency, "show_deleted": show_deleted, "show_mirror": show_mirror,
        "build_where": build_where,
    }


def narrow_by_text(f, text):
    """Filters f, also requiring text in the note or the description (as
    the app's search box does; a leading '!' still negates)."""
    if not text:
        return f
    negate = text.startswith("!")
    note, param = _text_search_clause("i.ZCONTENT", text)
    desc, _ = _text_search_clause("i.ZDATA", text)
    clause = f"({note} {'AND' if negate else 'OR'} {desc})"
    build = f["build_where"]

    def build_where(exclude=None):
        where, params = build(exclude)
        return f"{where} AND {clause}", params + [param, param]

    return {**f, "build_where": build_where}


def _where(f, updated_since=None):
    """f's WHERE clause and params, narrowed to rows MMW saw change at or
    after updated_since (Unix ms) if given; the query needs _CHANGES_JOIN."""
    where, params = f["build_where"]()
    if updated_since is not None:
        where += f" AND {CHANGED} >= ?"
        params.append(updated_since)
    return where, params


def transaction_facets(con, f):
    """Counts for the filter bar of filters f. Each facet is counted with
    every filter except its own, so an option shows how many rows it would
    add or remove rather than just the current total.

    {deleted, mirror: n, note, description: {"yes": n, "no": n},
     account: {account uid: n}, type: {DO_TYPE: n},
     category: {category uid: n, a root's including its children's},
     category_own: {category uid: n, its own rows only}}
    """
    def count(exclude, condition):
        where, params = f["build_where"](exclude=exclude)
        return _rows(con, f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE ({where}) AND {condition}", params)[0]["n"]

    def count_by(exclude, column):
        where, params = f["build_where"](exclude=exclude)
        return {r["k"]: r["n"] for r in _rows(
            con, f"SELECT {column} AS k, COUNT(*) AS n FROM INOUTCOME i WHERE {where} GROUP BY {column}", params)}

    category_own = count_by("category", "i.ctgUid")
    category = dict(category_own)
    for t in (0, 1):
        for r in build_category_tree(con, t):
            category[r["uid"]] = category_own.get(r["uid"], 0) + sum(
                category_own.get(c["uid"], 0) for c in r["children"])
    return {
        "deleted": count("show_deleted", "i.IS_DEL <> 0"),
        "mirror": count("show_mirror", "i.DO_TYPE = '4'"),
        "note": {"yes": count("has_note", "IFNULL(i.ZCONTENT,'')<>''"),
                 "no": count("has_note", "IFNULL(i.ZCONTENT,'')=''")},
        "description": {"yes": count("has_desc", "IFNULL(i.ZDATA,'')<>''"),
                        "no": count("has_desc", "IFNULL(i.ZDATA,'')=''")},
        "account": count_by("account", "i.assetUid"),
        "type": count_by("type", "i.DO_TYPE"),
        "category": category,
        "category_own": category_own,
    }


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

TX_SORT_OPTIONS = {
    "date": "i.WDATE {dir}, CAST(i.ZDATE AS INTEGER) {dir}",
    "amount": "i.AMOUNT_ACCOUNT {dir}",
    "entered_amount": "i.IN_ZMONEY {dir}",
    "account": "a.NIC_NAME COLLATE NOCASE {dir}",
    "updated": "i.UTIME {dir}",
}

# When MMW saw a row change: its stamp in the change log, or the app's own
# UTIME for a row never stamped.
CHANGED = "COALESCE(ch.changed, CAST(i.UTIME AS INTEGER))"
_CHANGES_JOIN = f"LEFT JOIN mmw.{CHANGES_TABLE} ch ON ch.uid = i.uid"
# list_transactions() orders by these: the pages' TX_SORT_OPTIONS, and
# "changed", by MMW's change time.
_ORDERS = {**TX_SORT_OPTIONS, "changed": CHANGED + " {dir}"}

# A transaction row as the pages and the API list it: INOUTCOME's own
# columns, its time of day and last change in local time, when MMW saw it
# change (changed_ms), and its accounts' names and currencies.
_TX_SELECT = f"""
    SELECT i.uid, i.WDATE, i.ZDATE, i.DO_TYPE, i.IS_DEL, i.AMOUNT_ACCOUNT, i.IN_ZMONEY,
           i.ZCONTENT, i.ZDATA, i.ctgUid, i.txUidTrans, i.UTIME, {CHANGED} AS changed_ms,
           time(CAST(i.ZDATE AS INTEGER) / 1000, 'unixepoch', 'localtime') AS tx_time,
           datetime(CAST(i.UTIME AS INTEGER) / 1000, 'unixepoch', 'localtime') AS updated_str,
           i.currencyUid AS currency_uid, cu.ISO AS currency_iso,
           a.currencyUid AS account_currency_uid, acu.ISO AS account_currency_iso,
           a.uid AS account_uid, a.NIC_NAME AS account_name,
           ta.uid AS to_account_uid, ta.NIC_NAME AS to_account_name
    FROM INOUTCOME i
    LEFT JOIN ASSETS a     ON a.uid = i.assetUid
    LEFT JOIN ASSETS ta    ON ta.uid = i.toAssetUid
    LEFT JOIN CURRENCY cu  ON cu.uid = i.currencyUid
    LEFT JOIN CURRENCY acu ON acu.uid = a.currencyUid
    {_CHANGES_JOIN}
"""


def count_transactions(con, f, updated_since=None):
    """How many transactions match filters f (see _where())."""
    where, params = _where(f, updated_since)
    return _rows(con, f"SELECT COUNT(*) AS n FROM INOUTCOME i {_CHANGES_JOIN} WHERE {where}", params)[0]["n"]


def list_transactions(con, f, sort="date", direction="desc", limit=100, offset=0, updated_since=None):
    """One page of the transactions matching filters f (see _where()), as
    _TX_SELECT rows, ordered by sort (a key of TX_SORT_OPTIONS, or "changed"
    for MMW's change time) in direction ("asc" or "desc"), then by uid so
    pages don't overlap. limit None: all of them."""
    if sort not in _ORDERS or direction not in ("asc", "desc"):
        raise ValueError(f"can't sort by {sort} {direction}")
    where, params = _where(f, updated_since)
    order_by = _ORDERS[sort].format(dir=direction)
    return _rows(con, f"{_TX_SELECT} WHERE {where} ORDER BY {order_by}, i.uid LIMIT ? OFFSET ?",
                 params + [-1 if limit is None else limit, offset])


def get_transaction(con, uid):
    """One transaction's _TX_SELECT row, or None."""
    rows = _rows(con, f"{_TX_SELECT} WHERE i.uid = ?", (uid,))
    return rows[0] if rows else None


def transfer_other_uid(con, uid):
    """The uid of the other row of uid's transfer, or None if it isn't one."""
    rows = _rows(con, """
        SELECT o.uid FROM INOUTCOME i
        JOIN INOUTCOME o ON o.txUidTrans = i.txUidTrans AND o.uid <> i.uid AND o.DO_TYPE IN ('3', '4')
        WHERE i.uid = ? AND i.DO_TYPE IN ('3', '4') AND IFNULL(i.txUidTrans, '') <> ''
    """, (uid,))
    return rows[0]["uid"] if len(rows) == 1 else None


def get_transactions(con, uids):
    """{uid: _TX_SELECT row} for these uids; missing ones are left out."""
    uids, out = list(uids), {}
    for n in range(0, len(uids), 500):
        part = uids[n:n + 500]
        for r in _rows(con, f"{_TX_SELECT} WHERE i.uid IN ({','.join('?' for _ in part)})", part):
            out[r["uid"]] = r
    return out


# What sums() can total by.
_SUM_KEYS = {"date": "i.WDATE", "category": "i.ctgUid", "account": "i.assetUid"}
_MAIN_DECIMALS = "IFNULL((SELECT DECIMAL_POINT FROM CURRENCY WHERE IS_MAIN_CURRENCY = 1 LIMIT 1), 2)"


def sums(con, f, by):
    """The amounts of the transactions matching filters f, summed per `by`
    (date, category or account), type and account currency: rows of key,
    DO_TYPE, currency_uid, total (in that currency, what balances add up)
    and in_main: each amount converted to the main currency and rounded to
    its decimals first, as the app adds up income and expense (the ZMONEY
    it stores per row; restated amounts carry fractions of a cent)."""
    key = _SUM_KEYS[by]
    where, params = f["build_where"]()
    return _rows(con, f"""
        SELECT {key} AS key, i.DO_TYPE, a.currencyUid AS currency_uid, SUM(i.AMOUNT_ACCOUNT) AS total,
               SUM(ROUND(i.AMOUNT_ACCOUNT * IFNULL(c.RATE, 1), {_MAIN_DECIMALS})) AS in_main
        FROM INOUTCOME i
        LEFT JOIN ASSETS a ON a.uid = i.assetUid
        LEFT JOIN CURRENCY c ON c.uid = a.currencyUid
        WHERE {where}
        GROUP BY {key}, i.DO_TYPE, a.currencyUid
    """, params)


def matching_uids(con, f):
    """The uid of every transaction matching filters f, not just one page's."""
    where, params = f["build_where"]()
    return [r["uid"] for r in _rows(con, f"SELECT i.uid FROM INOUTCOME i WHERE {where}", params)]


def group_stats(con, f, seconds=60):
    """Grouping stats over every transaction matching filters f, where rows
    of the same account within `seconds` of each other form a group. The
    point of grouping is the stats, so they're computed here rather than by
    a client fetching however many thousand rows match."""
    threshold_ms = seconds * 1000
    where, params = f["build_where"]()
    rows = _rows(con, f"""
        SELECT i.assetUid AS account, CAST(i.ZDATE AS INTEGER) AS t
        FROM INOUTCOME i WHERE {where}
        ORDER BY t ASC
    """, params)

    group_count = 0
    group_sizes = []
    gaps_ms = []
    prev_account = None
    prev_t = None
    cur_size = 0
    for r in rows:
        account, t = r["account"], r["t"]
        same_cluster = prev_account is not None and account == prev_account and abs(prev_t - t) <= threshold_ms
        if not same_cluster:
            if group_count > 0:
                group_sizes.append(cur_size)
            group_count += 1
            cur_size = 0
        else:
            gaps_ms.append(abs(t - prev_t))
        cur_size += 1
        prev_account, prev_t = account, t
    if group_count > 0:
        group_sizes.append(cur_size)

    total = len(rows)
    multi_groups = sum(1 for g in group_sizes if g > 1)
    avg_size = total / group_count if group_count else 0
    avg_gap_sec = (sum(gaps_ms) / len(gaps_ms) / 1000) if gaps_ms else 0
    median_gap_sec = 0
    if gaps_ms:
        s = sorted(gaps_ms)
        mid = len(s) // 2
        median_ms = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
        median_gap_sec = median_ms / 1000

    return {
        "total_rows": total, "groups": group_count, "multi_groups": multi_groups,
        "avg_size": avg_size, "avg_gap_sec": avg_gap_sec, "median_gap_sec": median_gap_sec,
    }
