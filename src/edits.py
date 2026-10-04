"""Every edit to a user's MM database outside sync: adding, changing and
deleting transactions, and changing accounts and categories.

The pages' /api/ routes and the public /api/v1/ routes both go through here,
so the rules that keep the database the way the app expects live in one
place: WDATE and ZDATE written together, a transfer's two rows kept in step,
IN_ZMONEY and ZMONEY restated with the amount, categories from the right
tree, and the last-write time bumped. Callers name fields (note, amount,
...), never MM columns, so nothing outside this module picks the columns.

Each function takes a writable connection from the caller, who commits it
(app.write_db()). A refused edit raises EditError, and the caller then
discards the connection, so it writes nothing.
"""
import math
import uuid
from datetime import datetime


class EditError(Exception):
    """An edit refused: a message for the user and the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


TRANSACTION_FIELDS = {"date", "time", "amount", "note", "description", "category", "account", "type",
                      "entered_amount", "entered_currency", "to_amount"}
# Type changes allowed: they keep the row's shape (an adjustment stays an
# adjustment, a categorised row stays categorised).
TYPE_SWAPS = ({"0", "1"}, {"7", "8"})
ACCOUNT_FIELDS = {"name", "group", "order", "status"}
# ASSETS.ZDATA: 0 normal, 1 soft-deleted, 2 unknown, 3 hidden.
ACCOUNT_STATUSES = {"0", "1", "2", "3"}
CATEGORY_FIELDS = {"name", "order"}
# C_IS_DEL is '' or NULL on a live category, never 0 (docs/MM_DB_SCHEMA.md).
LIVE_CATEGORY = "IFNULL(NULLIF(C_IS_DEL, ''), 0) + 0 <> 1"


def _rows(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def _now_ms():
    return int(datetime.now().timestamp() * 1000)


def _update(con, table, cols, key):
    """UPDATE the rows matching key ({column: value}). Column names come from
    this module, never the client."""
    con.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} "
                f"WHERE {' AND '.join(f'{c} = ?' for c in key)}", [*cols.values(), *key.values()])


def _check_fields(changes, allowed):
    unknown = set(changes) - allowed
    if unknown:
        raise EditError(f"can't change: {', '.join(sorted(unknown))}")
    if not changes:
        raise EditError("nothing to change")


def _find(con, sql, uid):
    """The first row sql (one ? for a uid) returns, or None, also for a uid
    that isn't text."""
    rows = _rows(con, sql, (uid,)) if isinstance(uid, str) else []
    return rows[0] if rows else None


def _parse_datetime(date_str, time_str):
    """A "YYYY-MM-DD" date and optional "HH:MM[:SS]" time as a datetime."""
    time_str = time_str or "00:00:00"
    if not isinstance(date_str, str) or not isinstance(time_str, str):
        raise EditError("invalid date/time")
    if time_str.count(":") == 1:
        time_str += ":00"
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise EditError("invalid date/time")


def _check_category(con, uid, tree):
    """uid must be a live category in that tree (0 income, 1 expense)."""
    if not isinstance(uid, str) or not _rows(
            con, f"SELECT 1 FROM ZCATEGORY WHERE uid = ? AND TYPE = ? AND {LIVE_CATEGORY}", (uid, tree)):
        raise EditError("unknown category for this type")


def _amount_arg(value, name="amount", allow_zero=False):
    """A finite amount: more than 0, or 0 or more."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise EditError(f"invalid {name}")
    if not math.isfinite(v):
        raise EditError(f"invalid {name}")
    if v < 0 or (v == 0 and not allow_zero):
        raise EditError(f"{name} must " + ("not be negative" if allow_zero else "be positive"))
    return v


def _text_arg(value, name):
    """Non-blank text, without surrounding spaces."""
    if not isinstance(value, str) or not value.strip():
        raise EditError(f"{name} must not be empty")
    return value.strip()


def _order_arg(value):
    """A display order (ORDERSEQ): a whole number, 0 or more."""
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise EditError("order must be a whole number, 0 or more")


def _currency_uid(con, code):
    """A currency's uid from its ISO code (as transactions show it) or its uid."""
    if not isinstance(code, str) or not code:
        raise EditError("unknown currency")
    found = {r["uid"] for r in _rows(con, "SELECT uid FROM CURRENCY WHERE ISO = ? OR uid = ?", (code, code))}
    if len(found) != 1:
        raise EditError(f"{code} matches more than one currency; give its uid" if found else "unknown currency")
    return found.pop()


def _currency_rate(con, uid):
    """CURRENCY.RATE, the currency's rate to the main currency (1 if unknown)."""
    rows = _rows(con, "SELECT RATE FROM CURRENCY WHERE uid = ?", (uid,))
    try:
        return float(rows[0]["RATE"]) if rows else 1.0
    except (TypeError, ValueError):
        return 1.0


def _fmt_money(x, decimals):
    return str(int(round(x))) if decimals == 0 else str(round(x, decimals))


def _amount_columns(con, uid, value, account_uid=None):
    """The columns to write to set a row's AMOUNT_ACCOUNT: it, plus IN_ZMONEY
    and ZMONEY restated to match. account_uid: the account the row is moving
    to, if it is, whose currency's rate ZMONEY then uses.

    IN_ZMONEY keeps the row's existing entered/account ratio (the app's own
    conversion, e.g. the BGN peg), so it scales with the new amount. ZMONEY is
    the amount in the main currency: AMOUNT_ACCOUNT * the account currency's
    RATE, rounded to 2 places.
    """
    new = _amount_arg(value, allow_zero=True)
    rows = _rows(con, """
        SELECT i.AMOUNT_ACCOUNT, i.IN_ZMONEY, cu.DECIMAL_POINT AS tx_dec, acu.RATE AS acct_rate
        FROM INOUTCOME i
        LEFT JOIN ASSETS a ON a.uid = IFNULL(?, i.assetUid)
        LEFT JOIN CURRENCY acu ON acu.uid = a.currencyUid
        LEFT JOIN CURRENCY cu ON cu.uid = i.currencyUid
        WHERE i.uid = ?""", (account_uid, uid))
    if not rows:
        raise EditError("not found", 404)
    r = rows[0]
    try:
        old = float(r["AMOUNT_ACCOUNT"])
        old_in = float(r["IN_ZMONEY"])
    except (TypeError, ValueError):
        old, old_in = 0.0, None
    tx_dec = r["tx_dec"] if r["tx_dec"] is not None else 2
    acct_rate = float(r["acct_rate"] or 1.0)
    cols = {"AMOUNT_ACCOUNT": new, "ZMONEY": _fmt_money(new * acct_rate, 2)}
    if old_in is not None:
        cols["IN_ZMONEY"] = _fmt_money(old_in * new / old, tx_dec) if old else _fmt_money(new, tx_dec)
    return cols


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def create_transaction(con, data):
    """Insert a transaction (two linked rows for a transfer) and return its
    uid. data: type (a DO_TYPE code), account, amount, date, and as the type
    needs, time, category, to_account, to_amount, note, description, and
    entered_amount with entered_currency."""
    do_type = data.get("type", "")
    if do_type not in ("0", "1", "3", "7", "8"):
        raise EditError("invalid type")

    account_uid = data.get("account", "")
    account = _find(con, "SELECT currencyUid FROM ASSETS WHERE uid = ?", account_uid)
    if not account:
        raise EditError("unknown account")
    currency_uid = account["currencyUid"]

    is_transfer = do_type == "3"
    to_account_uid = data.get("to_account", "") if is_transfer else ""
    if is_transfer:
        if not to_account_uid or to_account_uid == account_uid:
            raise EditError("pick two different accounts for a transfer")
        to_account = _find(con, "SELECT currencyUid FROM ASSETS WHERE uid = ?", to_account_uid)
        if not to_account:
            raise EditError("unknown destination account")
        to_currency_uid = to_account["currencyUid"]

    amount = _amount_arg(data.get("amount", ""))
    # What was typed in, when it differs from the amount in the account's
    # currency; without it, the amount itself.
    entered_amount, entered_currency_uid = amount, currency_uid
    if "entered_amount" in data or "entered_currency" in data:
        if not ("entered_amount" in data and "entered_currency" in data):
            raise EditError("give entered_amount and entered_currency together")
        entered_amount = _amount_arg(data["entered_amount"], "entered_amount")
        entered_currency_uid = _currency_uid(con, data["entered_currency"])
    if "to_amount" in data and not is_transfer:
        raise EditError("to_amount is only for transfers")

    date_str = data.get("date", "")
    dt = _parse_datetime(date_str, data.get("time", ""))

    is_adjustment = do_type in ("7", "8")
    category_uid = "-4" if is_adjustment else ("" if is_transfer else data.get("category", ""))
    if not is_adjustment and not is_transfer:
        if not category_uid:
            raise EditError("category required")
        _check_category(con, category_uid, int(do_type))

    note = data.get("note", "") or ""
    if is_adjustment and not note:
        note = "Difference"
    description = data.get("description", "") or ""
    if not isinstance(note, str) or not isinstance(description, str):
        raise EditError("note and description must be text")

    rate = _currency_rate(con, currency_uid)
    now_ms = _now_ms()
    zdate = str(int(dt.timestamp() * 1000))
    uid = str(uuid.uuid4())

    if is_transfer:
        to_rate = _currency_rate(con, to_currency_uid)
        if "to_amount" in data:
            to_amount = _amount_arg(data["to_amount"], "to_amount")
        else:
            # amount is in the source account's currency; convert to the
            # destination account's currency via each currency's rate to the
            # shared base currency.
            to_amount = round(amount * rate / to_rate, 2) if to_rate else amount
        mirror_uid = str(uuid.uuid4())
        tx_uid_trans = str(uuid.uuid4())
        con.execute(
            """
            INSERT INTO INOUTCOME
                (uid, WDATE, ZDATE, DO_TYPE, ZCONTENT, ZDATA, AMOUNT_ACCOUNT, IN_ZMONEY, ZMONEY,
                 assetUid, toAssetUid, currencyUid, ctgUid, txUidTrans, txUidFee,
                 IS_DEL, CARDDIVIDMONTH, MARK, syncVersion, isSynced, UTIME)
            VALUES
                (?, ?, ?, '3', ?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', 0, '0', 0, 0, 0, ?),
                (?, ?, ?, '4', ?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', 0, '0', 0, 0, 0, ?)
            """,
            (uid, date_str, zdate, note, description, amount, entered_amount, round(amount * rate, 2),
             account_uid, to_account_uid, entered_currency_uid, tx_uid_trans, now_ms,
             mirror_uid, date_str, zdate, note, description, to_amount, to_amount, round(to_amount * to_rate, 2),
             to_account_uid, account_uid, to_currency_uid, tx_uid_trans, now_ms),
        )
        return uid

    con.execute(
        """
        INSERT INTO INOUTCOME
            (uid, WDATE, ZDATE, DO_TYPE, ZCONTENT, ZDATA, AMOUNT_ACCOUNT, IN_ZMONEY, ZMONEY,
             assetUid, toAssetUid, currencyUid, ctgUid, txUidTrans, txUidFee,
             IS_DEL, CARDDIVIDMONTH, MARK, syncVersion, isSynced, UTIME)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, '', '', 0, '0', 0, 0, 0, ?)
        """,
        (uid, date_str, zdate, do_type, note, description,
         amount, entered_amount, round(amount * rate, 2),
         account_uid, entered_currency_uid, category_uid, now_ms),
    )
    return uid


def update_transaction(con, uid, changes):
    """Change any of TRANSACTION_FIELDS in one go (type as a DO_TYPE code).
    Transfers keep their accounts (delete and re-add to move one), and only
    income and expense rows have a category. Changing either row of a
    transfer carries the date, time, note and description over to the other
    row (the amount: see _transfer_mirror_changes). Returns the other row's
    uid, or None if the row isn't part of a transfer."""
    _check_fields(changes, TRANSACTION_FIELDS)
    rows = _rows(con, "SELECT DO_TYPE, WDATE, ZDATE, AMOUNT_ACCOUNT, assetUid, txUidTrans FROM INOUTCOME "
                      "WHERE uid = ?", (uid,))
    if not rows:
        raise EditError("not found", 404)
    row = rows[0]
    do_type = row["DO_TYPE"]
    is_transfer = do_type in ("3", "4")
    cols = {}
    if "type" in changes:
        new_type = changes["type"]
        if new_type != do_type:
            if not isinstance(new_type, str) or {do_type, new_type} not in TYPE_SWAPS:
                raise EditError("type can only change between income and expense, "
                                "or between balance_increase and balance_decrease")
            if new_type in ("0", "1") and "category" not in changes:
                raise EditError("changing between income and expense needs a category from the new tree")
            cols["DO_TYPE"] = do_type = new_type
    for key, col in (("note", "ZCONTENT"), ("description", "ZDATA")):
        if key in changes:
            if not isinstance(changes[key], str):
                raise EditError(f"{key} must be text")
            cols[col] = changes[key]
    if "category" in changes:
        if do_type not in ("0", "1"):
            raise EditError("only income and expense transactions have a category")
        _check_category(con, changes["category"], int(do_type))
        cols["ctgUid"] = changes["category"]
    if "account" in changes:
        if is_transfer:
            raise EditError("a transfer's accounts can't be changed; delete it and add a new one")
        if not _find(con, "SELECT 1 FROM ASSETS WHERE uid = ?", changes["account"]):
            raise EditError("unknown account")
        cols["assetUid"] = changes["account"]
    if "to_amount" in changes and do_type != "3":
        raise EditError("to_amount is for the sending row of a transfer" if do_type == "4"
                        else "to_amount is only for transfers")
    if "date" in changes or "time" in changes:
        try:
            old_time = datetime.fromtimestamp(int(row["ZDATE"]) / 1000).strftime("%H:%M:%S")
        except (TypeError, ValueError):
            old_time = ""
        dt = _parse_datetime(changes.get("date", row["WDATE"]), changes.get("time", old_time))
        cols["WDATE"], cols["ZDATE"] = dt.strftime("%Y-%m-%d"), str(int(dt.timestamp() * 1000))
    if "amount" in changes or "account" in changes:
        # A new account can mean a new currency rate, so ZMONEY is restated too.
        cols.update(_amount_columns(con, uid, changes.get("amount", row["AMOUNT_ACCOUNT"]), changes.get("account")))
    # Given entered values are written as they are, not rescaled with the amount.
    if "entered_amount" in changes:
        cols["IN_ZMONEY"] = _amount_arg(changes["entered_amount"], "entered_amount", allow_zero=True)
    if "entered_currency" in changes:
        cols["currencyUid"] = _currency_uid(con, changes["entered_currency"])

    mirror_uid, mirror_cols = (_transfer_mirror_changes(con, uid, row, changes, cols) if is_transfer
                               else (None, {}))
    now = _now_ms()
    for u, c in ((uid, cols), (mirror_uid, mirror_cols)):
        if u and c:
            _update(con, "INOUTCOME", {**c, "UTIME": now}, {"uid": u})
    return mirror_uid


def _transfer_mirror_changes(con, uid, row, changes, cols):
    """(the other row's uid, its columns to write) for a change to one row of
    a transfer: the same date, time, note and description, and its amount
    from to_amount, or the same amount when both accounts share a currency.
    Across currencies a new amount leaves the other row's alone, so changing
    both rows gives each the amount asked for. (None, {}) if the other row
    is missing."""
    link = row["txUidTrans"]
    other = _rows(con, """SELECT i.uid, a.currencyUid = b.currencyUid AS same_currency
                          FROM INOUTCOME i
                          LEFT JOIN ASSETS a ON a.uid = i.assetUid
                          LEFT JOIN ASSETS b ON b.uid = ?
                          WHERE i.txUidTrans = ? AND i.uid <> ? AND i.DO_TYPE IN ('3', '4')""",
                  (row["assetUid"], link, uid)) if link else []
    if len(other) != 1:
        return None, {}
    other_uid = other[0]["uid"]
    out = {c: cols[c] for c in ("WDATE", "ZDATE", "ZCONTENT", "ZDATA") if c in cols}
    if "to_amount" in changes:
        out.update(_amount_columns(con, other_uid, _amount_arg(changes["to_amount"], "to_amount", allow_zero=True)))
    elif "amount" in changes and other[0]["same_currency"]:
        out.update(_amount_columns(con, other_uid, cols["AMOUNT_ACCOUNT"]))
    return other_uid, out


def _linked_uids(con, uid):
    """uid plus every row tied to it by txUidTrans/txUidFee (a transfer's
    two legs and its fee): the merge treats them as one transaction."""
    uids, links = {uid}, set()
    while True:
        marks = ",".join("?" for _ in uids)
        rows = _rows(con, f"SELECT uid, txUidTrans, txUidFee FROM INOUTCOME WHERE uid IN ({marks})", list(uids))
        new_links = {r[c] for r in rows for c in ("txUidTrans", "txUidFee") if r[c]} - links
        if not new_links:
            return uids
        links |= new_links
        marks = ",".join("?" for _ in links)
        uids |= {r["uid"] for r in _rows(
            con, f"SELECT uid FROM INOUTCOME WHERE txUidTrans IN ({marks}) OR txUidFee IN ({marks})",
            list(links) * 2)}


def delete_transaction(con, uid):
    """Soft-delete (IS_DEL = 1), like the app: the row stays, and sync
    carries the deletion over. A transfer goes with its other leg and fee.
    Returns every uid deleted."""
    if not _rows(con, "SELECT 1 FROM INOUTCOME WHERE uid = ?", (uid,)):
        raise EditError("not found", 404)
    uids = sorted(_linked_uids(con, uid))
    marks = ",".join("?" for _ in uids)
    con.execute(f"UPDATE INOUTCOME SET IS_DEL = 1, UTIME = ? WHERE uid IN ({marks})", [_now_ms(), *uids])
    return uids


# ---------------------------------------------------------------------------
# Accounts and categories
# ---------------------------------------------------------------------------

def update_account(con, uid, changes):
    """Change any of ACCOUNT_FIELDS of an account: name, group (an
    ASSETGROUP uid), order (ORDERSEQ, within its group) and status (one of
    ACCOUNT_STATUSES)."""
    _check_fields(changes, ACCOUNT_FIELDS)
    if not _rows(con, "SELECT 1 FROM ASSETS WHERE uid = ?", (uid,)):
        raise EditError("not found", 404)
    cols = {}
    if "name" in changes:
        cols["NIC_NAME"] = _text_arg(changes["name"], "name")
    if "group" in changes:
        if not _find(con, "SELECT 1 FROM ASSETGROUP WHERE uid = ?", changes["group"]):
            raise EditError("unknown group")
        cols["groupUid"] = changes["group"]
    if "order" in changes:
        cols["ORDERSEQ"] = _order_arg(changes["order"])
    if "status" in changes:
        status = changes["status"]
        if isinstance(status, int) and not isinstance(status, bool):
            status = str(status)
        if status not in ACCOUNT_STATUSES:
            raise EditError(f"status must be one of {', '.join(sorted(ACCOUNT_STATUSES))}")
        cols["ZDATA"] = status
    _update(con, "ASSETS", {**cols, "A_UTIME": _now_ms()}, {"uid": uid})


def update_category(con, uid, tree, changes):
    """Change any of CATEGORY_FIELDS of a category in one tree (0 income,
    1 expense; the same uid can be in both): name, unique among its live
    siblings as the app needs, and order (ORDERSEQ, among its siblings)."""
    _check_fields(changes, CATEGORY_FIELDS)
    rows = _rows(con, "SELECT STATUS, pUid FROM ZCATEGORY WHERE uid = ? AND TYPE = ?", (uid, tree))
    if not rows:
        raise EditError("not found", 404)
    cat = rows[0]
    cols = {}
    if "name" in changes:
        name = _text_arg(changes["name"], "name")
        # Roots (STATUS 0) are siblings of each other; children (2) of the
        # others under the same parent.
        sql = f"""SELECT 1 FROM ZCATEGORY WHERE TYPE = ? AND STATUS = ? AND NAME = ? AND uid <> ?
                  AND {LIVE_CATEGORY}"""
        params = [tree, cat["STATUS"], name, uid]
        if cat["STATUS"] != 0:
            sql += " AND pUid = ?"
            params.append(cat["pUid"])
        if _rows(con, sql, params):
            raise EditError(f"there is already a category called {name} here")
        cols["NAME"] = name
    if "order" in changes:
        cols["ORDERSEQ"] = _order_arg(changes["order"])
    _update(con, "ZCATEGORY", {**cols, "C_UTIME": _now_ms()}, {"uid": uid, "TYPE": tree})
