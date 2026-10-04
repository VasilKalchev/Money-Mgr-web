"""The write routes and the edits module behind them: allowlists, amount
restating, transfers, accounts and categories, first-write backup."""
import io
import sqlite3

import pytest

import dbstore
import mmbak


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


@pytest.mark.parametrize("field", ["uid", "IS_DEL", "DO_TYPE", "WDATE", "ZCONTENT = 'x', IS_DEL", "1; DROP TABLE INOUTCOME", None, ""])
def test_edit_disallowed_field_is_rejected(client, field):
    before = db_row(client, "t1")["IS_DEL"]
    r = patch(client, "/api/transactions/t1", field=field, value="9")
    assert r.status_code == 400
    assert db_row(client, "t1")["IS_DEL"] == before
    assert db_rows("SELECT COUNT(*) AS n FROM INOUTCOME")[0]["n"] == 6


def test_page_edits_bump_the_last_write_time(client):
    patch(client, "/api/transactions/t1", field="ZCONTENT", value="x")
    patch(client, "/api/transactions/t2/datetime", date="2024-02-12", time="10:00")
    patch(client, "/api/transactions/t3", field="ZDATA", value="y")
    assert all(db_row(client, u)["UTIME"] > 1000 for u in ("t1", "t2", "t3"))
    assert db_row(client, "t4")["UTIME"] == 3


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
    assert d["ok"]
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


# -- the same rules as the API ------------------------------------------------------

def add_transfer(path):
    mmbak.add_tx(path, "x3", do_type="3", asset="a1", ctg="", note="rent", txUidTrans="tr", toAssetUid="a2")
    mmbak.add_tx(path, "x4", do_type="4", asset="a2", ctg="", note="rent", txUidTrans="tr", toAssetUid="a1")


def test_page_edit_of_a_transfer_carries_over_to_its_other_row(client):
    add_transfer(dbstore.store_for("alice").db_path)
    assert patch(client, "/api/transactions/x4", field="ZCONTENT", value="flat").status_code == 200
    patch(client, "/api/transactions/x3/datetime", date="2025-05-06", time="07:08:09")
    a, b = db_row(client, "x3"), db_row(client, "x4")
    assert a["ZCONTENT"] == b["ZCONTENT"] == "flat"
    assert (a["WDATE"], a["ZDATE"]) == (b["WDATE"], b["ZDATE"]) == ("2025-05-06", a["ZDATE"])


def test_page_cannot_move_a_transfer_leg_to_another_account(client):
    add_transfer(dbstore.store_for("alice").db_path)
    assert patch(client, "/api/transactions/x4", field="assetUid", value="a1").status_code == 400
    assert db_row(client, "x4")["assetUid"] == "a2"


def test_page_category_must_fit_the_row(client):
    assert patch(client, "/api/transactions/t1", field="ctgUid", value="c-salary").status_code == 400  # income tree
    assert patch(client, "/api/transactions/t1", field="ctgUid", value="nope").status_code == 400
    assert patch(client, "/api/transactions/t1", field="ctgUid", value="c-fun").status_code == 200
    assert db_row(client, "t1")["ctgUid"] == "c-fun"


def test_refused_edit_writes_nothing(client):
    before = dict(db_row(client, "t1"))
    r = client.patch("/api/v1/transactions/t1", json={"note": "x", "amount": "abc"}, headers=client.csrf)
    assert r.status_code == 400
    assert dict(db_row(client, "t1")) == before


# -- accounts and categories ------------------------------------------------------

def test_account_edits(client):
    assert patch(client, "/api/accounts/a1", field="ZDATA", value="3").status_code == 200
    assert patch(client, "/api/accounts/a1", field="ORDERSEQ", value="20").status_code == 200
    assert patch(client, "/api/accounts/a1", field="NIC_NAME", value="  Purse ").status_code == 200
    row = db_row(client, "a1", "ASSETS")
    assert (row["ZDATA"], row["ORDERSEQ"], row["NIC_NAME"]) == ("3", 20, "Purse")
    assert row["A_UTIME"] > 1000


@pytest.mark.parametrize("field, value", [
    ("NIC_NAME", ""), ("NIC_NAME", "  "), ("NIC_NAME", None), ("ZDATA", "5"), ("ZDATA", ""),
    ("ORDERSEQ", "x"), ("ORDERSEQ", "-1"), ("ORDERSEQ", "1.5"), ("groupUid", "nope"),
])
def test_account_edit_rejects(client, field, value):
    before = dict(db_row(client, "a1", "ASSETS"))
    assert patch(client, "/api/accounts/a1", field=field, value=value).status_code == 400
    assert dict(db_row(client, "a1", "ASSETS")) == before


def test_account_edit_unknown(client):
    assert patch(client, "/api/accounts/nope", field="NIC_NAME", value="x").status_code == 404


def test_category_edits(client):
    assert patch(client, "/api/categories/c-food-out/1", field="NAME", value="Fun").status_code == 200  # a root's name
    assert patch(client, "/api/categories/c-food-out/1", field="ORDERSEQ", value="10").status_code == 200
    row = db_row(client, "c-food-out", "ZCATEGORY")
    assert (row["NAME"], row["ORDERSEQ"]) == ("Fun", 10) and row["C_UTIME"] > 1000


def test_category_name_must_be_unique_among_its_siblings(client):
    path = dbstore.store_for("alice").db_path
    mmbak.execute(path, "INSERT INTO ZCATEGORY (NAME, ORDERSEQ, TYPE, STATUS, uid, pUid) "
                        "VALUES ('Groceries', 3, 1, 2, 'c-groc', 'c-food')")
    mmbak.execute(path, "INSERT INTO ZCATEGORY (C_IS_DEL, NAME, ORDERSEQ, TYPE, STATUS, uid) "
                        "VALUES (1, 'Old', 4, 1, 0, 'c-old')")
    assert patch(client, "/api/categories/c-fun/1", field="NAME", value="Food").status_code == 400
    assert patch(client, "/api/categories/c-groc/1", field="NAME", value="Eating out").status_code == 400
    assert patch(client, "/api/categories/c-fun/1", field="NAME", value="Salary").status_code == 200  # other tree
    assert patch(client, "/api/categories/c-fun/1", field="NAME", value="Old").status_code == 200  # deleted one
    assert db_row(client, "c-groc", "ZCATEGORY")["NAME"] == "Groceries"


@pytest.mark.parametrize("url, field, value, status", [
    ("/api/categories/c-fun/1", "NAME", "", 400),
    ("/api/categories/c-fun/1", "ORDERSEQ", "x", 400),
    ("/api/categories/c-fun/0", "NAME", "x", 404),  # c-fun is in the expense tree
    ("/api/categories/nope/1", "NAME", "x", 404),
])
def test_category_edit_rejects(client, url, field, value, status):
    assert patch(client, url, field=field, value=value).status_code == status
    assert db_row(client, "c-fun", "ZCATEGORY")["NAME"] == "Fun"


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

def delete(client, uid):
    return client.delete(f"/api/transactions/{uid}", headers=client.csrf)


def test_delete_is_soft(client):
    r = delete(client, "t1")
    assert r.status_code == 200 and r.get_json()["deleted"] == ["t1"]
    assert db_row(client, "t1")["IS_DEL"] == 1 and db_row(client, "t1")["UTIME"] > 1000
    assert db_row(client, "t2")["IS_DEL"] == 0
    assert db_rows("SELECT COUNT(*) AS n FROM INOUTCOME")[0]["n"] == 6  # rows stay


def test_delete_transfer_takes_its_other_leg(client):
    mmbak.add_tx(dbstore.store_for("alice").db_path, "x3", do_type="3", asset="a1", txUidTrans="tr")
    mmbak.add_tx(dbstore.store_for("alice").db_path, "x4", do_type="4", asset="a2", txUidTrans="tr")
    assert delete(client, "x3").get_json()["deleted"] == ["x3", "x4"]
    assert db_row(client, "t1")["IS_DEL"] == 0


def test_delete_unknown_uid_changes_nothing(client):
    r = delete(client, "nope")
    assert r.status_code == 404 and not r.get_json()["ok"]
    assert db_rows("SELECT COUNT(*) AS n FROM INOUTCOME WHERE IS_DEL = 1")[0]["n"] == 1  # just the seeded t5


def test_delete_needs_csrf(client):
    assert client.delete("/api/transactions/t1").status_code == 400
    assert db_row(client, "t1")["IS_DEL"] == 0


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
