"""Builds a tiny synthetic Money Manager export (user_version 19) for tests.

Only the tables the app's queries touch are created. INOUTCOME keeps the
real column list; the other tables keep just the columns the queries use.
"""
import sqlite3

SCHEMA = """
CREATE TABLE INOUTCOME (AID INTEGER PRIMARY KEY, ASSET_GROUP INTEGER, ASSET_ID INTEGER,
  ASSET_NIC VARCHAR, ASSET_NAME VARCHAR, CARDDIVIDID VARCHAR, CARDDIVIDMONTH VARCHAR,
  CATEGORY_ID INTEGER, CATEGORY_NAME VARCHAR, ZCONTENT VARCHAR, ZDATE VARCHAR, WDATE VARCHAR,
  DO_TYPE VARCHAR, ZMONEY VARCHAR, OPPOSITEAID INTEGER, ZDATA VARCHAR, ZDATA1 VARCHAR,
  ZDATA2 VARCHAR, SMS_RDATE VARCHAR, IN_ZMONEY VARCHAR, CARD_DIVIDE_CID INTEGER,
  CARD_DIVIDE_MONTH_STR VARCHAR, CARD_TIME_STAMP_STR VARCHAR, IMPORTANT INTEGER, FEE_ID INTEGER,
  SMS_ORIGIN VARCHAR, SMS_PARSE_CONTENT VARCHAR, IS_DEL INTEGER, SYNC_CHECK INTEGER,
  UTIME INTEGER, CURRENCY_ID INTEGER, AMOUNT_ACCOUNT REAL, TX_UID TEXT, txUidFee TEXT,
  cardDivideUid TEXT, uid TEXT, currencyUid TEXT, assetUid TEXT, categoryUid TEXT,
  txUidTrans TEXT, MARK INTEGER, syncTime REAL, syncVersion INTEGER, ctgUid TEXT,
  toAssetUid TEXT, isSynced integer, lat TEXT, lng TEXT, gstd TEXT, wtime TEXT, paid TEXT);
CREATE UNIQUE INDEX UNIQUE_IDX_INOUTCOME_UID ON INOUTCOME (uid);
CREATE TABLE ASSETS (ID integer primary key autoincrement, NIC_NAME varchar, ORDERSEQ integer,
  TYPE integer, ZDATA varchar, AMOUNT varchar, CARD_ACCOUNT_NAME varchar, A_UTIME INTEGER, uid TEXT,
  currencyUid TEXT, groupUid TEXT);
CREATE TABLE ASSETGROUP (DEVICE_ID INTEGER PRIMARY KEY autoincrement, ACC_GROUP_NAME VARCHAR,
  ORDERSEQ INTEGER, TYPE INTEGER, uid TEXT);
CREATE TABLE CURRENCY (ID integer primary key autoincrement, ISO varchar, RATE real,
  DECIMAL_POINT integer, SYMBOL varchar, IS_MAIN_CURRENCY integer, ORDER_SEQ integer, uid TEXT);
CREATE TABLE ZCATEGORY (ID integer primary key autoincrement, C_IS_DEL integer, C_UTIME INTEGER,
  NAME varchar, ORDERSEQ integer, TYPE integer, STATUS integer, uid TEXT, pUid TEXT);
CREATE TABLE ZETC (dataTypeKey TEXT, ZDATA TEXT);
CREATE TABLE FAVTRANSACTION (DEVICE_ID INTEGER PRIMARY KEY, IS_DEL INTEGER, USETIME INTEGER, DO_TYPE INTEGER,
  AMOUNT_SUB REAL, MEMO VARCHAR, PAYEE VARCHAR, ORDERSEQ INTEGER, uid TEXT, currencyUid TEXT, assetUid TEXT,
  toAssetUid TEXT, ctgUid TEXT);
CREATE TABLE BUDGET (ID INTEGER PRIMARY KEY, DO_TYPE INTEGER, PERIOD_TYPE INTEGER, IS_TOTAL INTEGER,
  IS_DEL INTEGER, ORDER_SEQ INTEGER, uid TEXT, targetUid TEXT);
CREATE TABLE BUDGET_AMOUNT (ID INTEGER PRIMARY KEY, IS_DEL INTEGER, AMOUNT REAL, BUDGET_PERIOD INTEGER,
  budgetUid TEXT);
"""

# uid, WDATE, DO_TYPE (0 income, 1 expense, 4 mirror), amount, account, category,
# note, description, IS_DEL
TRANSACTIONS = [
    ("t1", "2024-01-05", "1", 10.0, "a1", "c-food", "lunch", "work", 0),
    ("t2", "2024-02-10", "1", 25.5, "a1", "c-food-out", "dinner", "", 0),
    ("t3", "2024-02-11", "0", 1000.0, "a2", "c-salary", "", "pay", 0),
    ("t4", "2024-03-01", "1", 7.0, "a2", "c-fun", "50% off_sale", "", 0),
    ("t5", "2024-03-02", "1", 99.0, "a1", "c-food", "deleted one", "", 1),
    ("t6", "2024-03-03", "4", 50.0, "a1", "c-food", "mirror", "", 0),
]


def build(path, user_version=19):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    # RATE is the rate to the main currency (EUR), used to restate ZMONEY.
    con.executemany("INSERT INTO CURRENCY (ISO, RATE, DECIMAL_POINT, SYMBOL, IS_MAIN_CURRENCY, ORDER_SEQ, uid)"
                    " VALUES (?, ?, 2, ?, ?, ?, ?)",
                    [("EUR", 1.0, "€", 1, 100, "cur-eur"), ("USD", 0.9, "US$", 0, 101, "cur-usd")])
    con.execute("INSERT INTO ASSETGROUP (ACC_GROUP_NAME, ORDERSEQ, TYPE, uid) VALUES ('Cash', 1, 11, 'g1')")
    con.executemany("INSERT INTO ZETC VALUES (?, ?)", [("week_start_day", "1"), ("start_day", "1")])
    con.executemany(
        "INSERT INTO ASSETS (NIC_NAME, ORDERSEQ, ZDATA, uid, currencyUid, groupUid) VALUES (?, ?, '0', ?, ?, 'g1')",
        [("Wallet", 1, "a1", "cur-eur"), ("Bank", 2, "a2", "cur-usd")],
    )
    con.executemany(
        "INSERT INTO ZCATEGORY (C_IS_DEL, NAME, ORDERSEQ, TYPE, STATUS, uid, pUid) VALUES (0, ?, ?, ?, ?, ?, ?)",
        [
            ("Food", 1, 1, 0, "c-food", None),
            ("Eating out", 2, 1, 2, "c-food-out", "c-food"),
            ("Fun", 3, 1, 0, "c-fun", None),
            ("Salary", 1, 0, 0, "c-salary", None),
        ],
    )
    for i, (uid, wdate, do_type, amount, asset, ctg, note, desc, is_del) in enumerate(TRANSACTIONS):
        con.execute(
            "INSERT INTO INOUTCOME (uid, WDATE, ZDATE, DO_TYPE, AMOUNT_ACCOUNT, IN_ZMONEY, ZMONEY, assetUid, ctgUid,"
            " ZCONTENT, ZDATA, IS_DEL, currencyUid, UTIME) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cur-eur', ?)",
            (uid, wdate, str(1704067200000 + i * 1000), do_type, amount, str(amount), str(amount),
             asset, ctg, note, desc, is_del, i),
        )
    con.execute(f"PRAGMA user_version = {int(user_version)}")
    con.commit()
    con.close()
    return path


def execute(path, sql, params=()):
    con = sqlite3.connect(path)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def query(path, sql, params=()):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def add_tx(path, uid, wdate="2024-04-01", do_type="1", amount=12.5, asset="a1", ctg="c-food", note="", desc="",
           zdate=None, **extra):
    """Insert a transaction the way the app does (no AID: SQLite picks one)."""
    cols = {"uid": uid, "WDATE": wdate, "ZDATE": str(zdate or 1711929600000), "DO_TYPE": do_type,
            "AMOUNT_ACCOUNT": amount, "IN_ZMONEY": str(amount), "ZMONEY": str(amount), "assetUid": asset,
            "ctgUid": ctg, "ZCONTENT": note, "ZDATA": desc, "IS_DEL": 0, "currencyUid": "cur-eur",
            "toAssetUid": "", "txUidTrans": "", "UTIME": 100, **extra}
    execute(path, f"INSERT INTO INOUTCOME ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            list(cols.values()))
