"""The write routes: allowlists, amount restating, transfers, first-write backup."""
import io
import sqlite3

import pytest

import dbstore


def db_row(client, uid, table="INOUTCOME"):
    con = sqlite3.connect(dbstore.store_for("alice").db_path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(f"SELECT * FROM {table} WHERE uid = ?", (uid,)).fetchone()
    finally:
        con.close()


def db_rows(sql, params=()):
    con = sqlite3.connect(dbstore.store_for("alice").db_path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def patch(client, url, **body):
    return client.patch(url, json=body, headers=client.csrf)


def post(client, url, **body):
    return client.post(url, json=body, headers=client.csrf)


# -- field allowlists -----------------------------------------------------------

def test_edit_allowed_field(client):
    r = patch(client, "/api/transactions/t1", field="ZCONTENT", value="changed")
    assert r.status_code == 200 and r.get_json()["ok"]
    assert db_row(client, "t1")["ZCONTENT"] == "changed"
    assert db_row(client, "t2")["ZCONTENT"] == "dinner"  # only the one row


@pytest.mark.parametrize("field", ["uid", "IS_DEL", "DO_TYPE", "ZCONTENT = 'x', IS_DEL", "1; DROP TABLE INOUTCOME", None, ""])
def test_edit_disallowed_field_is_rejected(client, field):
    before = db_row(client, "t1")["IS_DEL"]
    r = patch(client, "/api/transactions/t1", field=field, value="9")
    assert r.status_code == 400
    assert db_row(client, "t1")["IS_DEL"] == before
    assert db_rows("SELECT COUNT(*) AS n FROM INOUTCOME")[0]["n"] == 6


def test_edit_value_is_bound_not_interpolated(client):
    patch(client, "/api/transactions/t1", field="ZCONTENT", value="x' WHERE 1=1 --")
    assert db_row(client, "t1")["ZCONTENT"] == "x' WHERE 1=1 --"
    assert db_row(client, "t2")["ZCONTENT"] == "dinner"


def test_account_and_category_allowlists(client):
    assert patch(client, "/api/accounts/a1", field="NIC_NAME", value="Purse").status_code == 200
    assert db_row(client, "a1", "ASSETS")["NIC_NAME"] == "Purse"
    assert patch(client, "/api/accounts/a1", field="currencyUid", value="cur-usd").status_code == 400
    assert patch(client, "/api/categories/c-fun/1", field="NAME", value="Games").status_code == 200
    assert patch(client, "/api/categories/c-fun/1", field="STATUS", value="2").status_code == 400


def test_category_edit_is_scoped_to_its_tree(client):
    # the same uid can exist in both trees, so TYPE must narrow the update
    con = sqlite3.connect(dbstore.store_for("alice").db_path)
    con.execute("INSERT INTO ZCATEGORY (C_IS_DEL, NAME, ORDERSEQ, TYPE, STATUS, uid) VALUES (0, 'Twin', 9, 0, 0, 'c-fun')")
    con.commit()
    con.close()
    patch(client, "/api/categories/c-fun/0", field="NAME", value="Renamed")
    names = {r["TYPE"]: r["NAME"] for r in db_rows("SELECT TYPE, NAME FROM ZCATEGORY WHERE uid = 'c-fun'")}
    assert names == {0: "Renamed", 1: "Fun"}


# -- amount edit ----------------------------------------------------------------

def test_amount_edit_restates_money_columns(client):
    r = patch(client, "/api/transactions/t3", field="AMOUNT_ACCOUNT", value="500")  # t3 is in USD, rate 0.9
    d = r.get_json()
    assert r.status_code == 200
    row = db_row(client, "t3")
    assert row["AMOUNT_ACCOUNT"] == 500
    assert float(row["IN_ZMONEY"]) == 500  # keeps the entered/account ratio (1:1 here)
    assert float(row["ZMONEY"]) == pytest.approx(450.0)
    assert float(d["zmoney"]) == pytest.approx(450.0)
    assert row["UTIME"] > 1000


def test_amount_edit_scales_entered_amount_by_existing_ratio(client):
    con = sqlite3.connect(dbstore.store_for("alice").db_path)
    con.execute("UPDATE INOUTCOME SET IN_ZMONEY = '20' WHERE uid = 't1'")  # entered 20, account 10
    con.commit()
    con.close()
    patch(client, "/api/transactions/t1", field="AMOUNT_ACCOUNT", value="15")
    assert float(db_row(client, "t1")["IN_ZMONEY"]) == pytest.approx(30)


@pytest.mark.parametrize("value, status", [("abc", 400), ("", 400), (None, 400), ("-1", 400)])
def test_amount_edit_rejects_bad_values(client, value, status):
    r = patch(client, "/api/transactions/t1", field="AMOUNT_ACCOUNT", value=value)
    assert r.status_code == status
    assert db_row(client, "t1")["AMOUNT_ACCOUNT"] == 10


def test_amount_edit_unknown_row(client):
    assert patch(client, "/api/transactions/nope", field="AMOUNT_ACCOUNT", value="5").status_code == 404


# -- bulk -----------------------------------------------------------------------

def test_bulk_edit(client):
    r = patch(client, "/api/transactions/bulk", field="ctgUid", value="c-fun", uids=["t1", "t2"])
    assert r.get_json()["updated"] == 2
    assert db_row(client, "t1")["ctgUid"] == "c-fun" and db_row(client, "t2")["ctgUid"] == "c-fun"
    assert db_row(client, "t3")["ctgUid"] == "c-salary"


@pytest.mark.parametrize("body", [
    {"field": "AMOUNT_ACCOUNT", "value": "1", "uids": ["t1"]},  # allowed singly, not in bulk
    {"field": "ctgUid", "value": "x", "uids": []},
    {"field": "ctgUid", "value": "x", "uids": "t1"},
    {"field": "ctgUid", "value": "x"},
])
def test_bulk_edit_rejects(client, body):
    assert patch(client, "/api/transactions/bulk", **body).status_code == 400
    assert db_row(client, "t1")["ctgUid"] == "c-food"


# -- datetime -------------------------------------------------------------------

def test_datetime_edit(client):
    r = patch(client, "/api/transactions/t1/datetime", date="2025-06-07", time="08:30")
    assert r.status_code == 200
    row = db_row(client, "t1")
    assert row["WDATE"] == "2025-06-07"
    from datetime import datetime
    assert int(row["ZDATE"]) == int(datetime(2025, 6, 7, 8, 30).timestamp() * 1000)


@pytest.mark.parametrize("date, time", [("2025-13-01", ""), ("junk", ""), ("2025-01-01", "25:00"), ("", "")])
def test_datetime_edit_rejects(client, date, time):
    assert patch(client, "/api/transactions/t1/datetime", date=date, time=time).status_code == 400
    assert db_row(client, "t1")["WDATE"] == "2024-01-05"


# -- add transaction --------------------------------------------------------------

def add(client, **over):
    body = {"type": "1", "account": "a1", "amount": "12.5", "date": "2025-01-02", "category": "c-fun", "note": "n"}
    body.update(over)
    return post(client, "/api/transactions", **body)


def test_add_expense(client):
    r = add(client)
    assert r.status_code == 200
    row = db_row(client, r.get_json()["uid"])
    assert (row["DO_TYPE"], row["AMOUNT_ACCOUNT"], row["assetUid"], row["ctgUid"]) == ("1", 12.5, "a1", "c-fun")
    assert row["currencyUid"] == "cur-eur"  # the account's currency
    assert row["WDATE"] == "2025-01-02" and row["IS_DEL"] == 0


@pytest.mark.parametrize("over", [
    {"type": "9"}, {"type": ""}, {"account": "nope"}, {"amount": "abc"}, {"amount": "0"}, {"amount": "-3"},
    {"date": "2025-02-30"}, {"date": ""}, {"category": ""},
])
def test_add_rejects_bad_input(client, over):
    assert add(client, **over).status_code == 400
    assert db_rows("SELECT COUNT(*) AS n FROM INOUTCOME")[0]["n"] == 6


def test_add_adjustment_gets_fixed_category_and_default_note(client):
    r = add(client, type="7", category="", note="")
    row = db_row(client, r.get_json()["uid"])
    assert row["ctgUid"] == "-4" and row["ZCONTENT"] == "Difference"


def test_add_transfer_creates_linked_pair_with_conversion(client):
    r = add(client, type="3", account="a1", to_account="a2", amount="90", category="")  # EUR 1.0 -> USD 0.9
    assert r.status_code == 200
    out = db_row(client, r.get_json()["uid"])
    pair = db_rows("SELECT * FROM INOUTCOME WHERE txUidTrans = ?", (out["txUidTrans"],))
    assert sorted(p["DO_TYPE"] for p in pair) == ["3", "4"]
    mirror = next(p for p in pair if p["DO_TYPE"] == "4")
    assert out["AMOUNT_ACCOUNT"] == 90
    assert mirror["AMOUNT_ACCOUNT"] == pytest.approx(100.0)
    assert (mirror["assetUid"], mirror["toAssetUid"]) == ("a2", "a1")
    assert mirror["currencyUid"] == "cur-usd"


@pytest.mark.parametrize("over", [{"to_account": ""}, {"to_account": "a1"}, {"to_account": "nope"}])
def test_add_transfer_rejects_bad_destination(client, over):
    assert add(client, type="3", category="", **over).status_code == 400
    assert db_rows("SELECT COUNT(*) AS n FROM INOUTCOME")[0]["n"] == 6


# -- backups on first write -----------------------------------------------------

def test_first_write_takes_one_backup(client):
    store = dbstore.store_for("alice")
    assert store.backup_count() == 0
    client.get("/transactions")  # reads don't back up
    assert store.backup_count() == 0
    patch(client, "/api/transactions/t1", field="ZCONTENT", value="a")
    patch(client, "/api/transactions/t1", field="ZCONTENT", value="b")
    assert store.backup_count() == 1
    # the snapshot holds the pre-edit data
    snap = f"{store.backup_dir}/{store.latest_backup()}/current.mmbak"
    con = sqlite3.connect(snap)
    assert con.execute("SELECT ZCONTENT FROM INOUTCOME WHERE uid='t1'").fetchone()[0] == "lunch"
    con.close()


def test_edit_marks_local_modified(client):
    assert not dbstore.store_for("alice").local_modified()
    patch(client, "/api/transactions/t1", field="ZCONTENT", value="a")
    assert dbstore.store_for("alice").local_modified()


# -- upload ---------------------------------------------------------------------

def upload(client, data, name="MM_new.mmbak"):
    return client.post("/db/upload", data={"db_file": (io.BytesIO(data), name), "csrf_token": "t"},
                       content_type="multipart/form-data")


def test_upload_replaces_database_and_backs_up_old(client, make_mmbak):
    store = dbstore.store_for("alice")
    new = open(make_mmbak(), "rb").read()
    r = upload(client, new, name="../../etc/MM_new.mmbak")
    assert r.status_code == 302
    assert store.db_info()["name"] == "MM_new.mmbak"  # path components stripped
    assert store.backup_count() == 1


def test_upload_unsupported_keeps_current(client, make_mmbak):
    store = dbstore.store_for("alice")
    before = dbstore.file_md5(store.db_path)
    upload(client, open(make_mmbak(18), "rb").read())
    assert dbstore.file_md5(store.db_path) == before
    upload(client, b"not a database at all")
    assert dbstore.file_md5(store.db_path) == before
    assert store.db_info()["name"] == "export.mmbak"


def test_upload_without_file(client):
    r = client.post("/db/upload", data={"csrf_token": "t"})
    assert r.status_code == 302
