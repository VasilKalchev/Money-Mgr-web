"""The public API (/api/v1/): token auth, reads and writes."""
import sqlite3
from datetime import datetime

import pytest

import dbstore
import mmbak
import users


@pytest.fixture
def api(client):
    """A cookie-less client with a read-write token for alice. Call
    api.get/post/... with the path after /api/v1/."""
    from app import app

    class Api:
        def __init__(self, token):
            self.c, self.token = app.test_client(), token

        def call(self, method, path, token=None, **kw):
            headers = {"Authorization": f"Bearer {token or self.token}"}
            return getattr(self.c, method)(f"/api/v1/{path}", headers=headers, **kw)

        def get(self, path, **kw):
            return self.call("get", path, **kw)

        def post(self, path, body, **kw):
            return self.call("post", path, json=body, **kw)

        def patch(self, path, body, **kw):
            return self.call("patch", path, json=body, **kw)

        def delete(self, path, **kw):
            return self.call("delete", path, **kw)

    return Api(users.create_token("alice", "script", write=True))


def db_path():
    return dbstore.store_for("alice").db_path


def row(uid):
    return mmbak.query(db_path(), "SELECT * FROM INOUTCOME WHERE uid = ?", (uid,))[0]


def count():
    return mmbak.query(db_path(), "SELECT COUNT(*) AS n FROM INOUTCOME")[0]["n"]


# -- tokens and auth ----------------------------------------------------------------

def test_settings_creates_token_and_shows_it_once(client):
    r = client.post("/settings/api-tokens", data={"name": "Home <b>", "write": "1", "csrf_token": "t"})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    token = html.split('id="new-token" value="')[1].split('"')[0]
    assert token.startswith("mmw_")
    assert "Home &lt;b&gt;" in html and "Read and write" in html
    assert token not in client.get("/settings").get_data(as_text=True)
    with open(dbstore.APP_CONFIG_PATH) as f:
        assert token not in f.read()  # only its hash is kept
    (tid, t), = users.tokens("alice").items()
    assert t["name"] == "Home <b>" and t["write"] and "hash" not in t


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_token_needs_a_name(client, name):
    client.post("/settings/api-tokens", data={"name": name, "csrf_token": "t"})
    assert users.tokens("alice") == {}


def test_token_creation_needs_csrf(client):
    client.post("/settings/api-tokens", data={"name": "x"})
    assert users.tokens("alice") == {}


def test_token_authenticates_without_a_session(api):
    r = api.get("status")
    assert r.status_code == 200
    d = r.get_json()
    assert (d["user"], d["auth"], d["can_write"]) == ("alice", "token", True)
    assert d["database"]["name"] == "export.mmbak"


@pytest.mark.parametrize("token", ["nope", "mmw_", "mmw_deadbeef_x", ""])
def test_bad_token_is_401(api, token):
    r = api.c.get("/api/v1/accounts", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.get_json()["ok"] is False


def test_wrong_secret_for_a_real_token_id_is_401(api):
    assert api.get("accounts", token=api.token[:-4] + "AAAA").status_code == 401


def test_token_only_works_on_v1(api):
    r = api.c.get("/api/transactions/uids", headers={"Authorization": f"Bearer {api.token}"})
    assert r.status_code == 401
    r = api.c.get("/editor/transactions", headers={"Authorization": f"Bearer {api.token}"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_read_only_token_cannot_write(api):
    ro = users.create_token("alice", "ro")
    assert api.get("transactions", token=ro).status_code == 200
    r = api.post("transactions", {"type": "expense", "account": "a1", "amount": 1, "category": "c-fun"}, token=ro)
    assert r.status_code == 403
    assert api.delete("transactions/t1", token=ro).status_code == 403
    assert count() == 6 and row("t1")["IS_DEL"] == 0


def test_token_writes_need_no_csrf(api):
    r = api.post("transactions", {"type": "expense", "account": "a1", "amount": 1, "category": "c-fun"})
    assert r.status_code == 201


def test_session_writes_to_v1_still_need_csrf(client):
    body = {"type": "expense", "account": "a1", "amount": 1, "category": "c-fun"}
    assert client.post("/api/v1/transactions", json=body).status_code == 400
    assert client.post("/api/v1/transactions", json=body, headers=client.csrf).status_code == 201


def test_session_works_on_v1_alongside_basic_auth(client):
    # a proxy's Basic auth header must not be mistaken for a token
    r = client.get("/api/v1/status", headers={"Authorization": "Basic YTpi"})
    assert r.status_code == 200 and r.get_json()["auth"] == "session"


def test_revoked_token_stops_working(api, client):
    tid = next(iter(users.tokens("alice")))
    client.post(f"/settings/api-tokens/{tid}/revoke", data={"csrf_token": "t"})
    assert users.tokens("alice") == {}
    assert api.get("status").status_code == 401


def test_removed_users_tokens_stop_working(api):
    users.create("bob", "correct horse")
    bob = users.create_token("bob", "b")
    assert api.get("status", token=bob).get_json()["user"] == "bob"
    users.remove("bob")
    assert api.get("status", token=bob).status_code == 401


def test_cannot_revoke_someone_elses_token(client):
    users.create("bob", "correct horse")
    users.create_token("bob", "b")
    tid = next(iter(users.tokens("bob")))
    client.post(f"/settings/api-tokens/{tid}/revoke", data={"csrf_token": "t"})
    assert tid in users.tokens("bob")


def test_last_used_is_recorded(api):
    assert next(iter(users.tokens("alice").values()))["last_used"] == ""
    api.get("status")
    assert next(iter(users.tokens("alice").values()))["last_used"]


def test_no_database_yet(data_dir):
    from app import app
    users.create("carol", "correct horse")
    token = users.create_token("carol", "c")
    c = app.test_client()
    h = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/v1/status", headers=h).get_json()["database"] is None
    assert c.get("/api/v1/transactions", headers=h).status_code == 409


def test_unknown_route_and_method_are_json(api):
    r = api.get("nope")
    assert r.status_code == 404 and r.get_json()["ok"] is False
    r = api.call("put", "transactions/t1")
    assert r.status_code == 405 and "PATCH" in r.headers["Allow"]


# -- reads ------------------------------------------------------------------------

def test_list_hides_deleted_and_mirror_rows_by_default(api):
    d = api.get("transactions").get_json()
    assert d["total"] == 4
    assert [t["uid"] for t in d["transactions"]] == ["t4", "t3", "t2", "t1"]  # newest first


def test_list_uses_the_page_filters(api):
    d = api.get("transactions?account=a1&show_deleted=show").get_json()
    assert sorted(t["uid"] for t in d["transactions"]) == ["t1", "t2", "t5"]
    d = api.get("transactions?category=c-food").get_json()  # a root pulls in its children
    assert sorted(t["uid"] for t in d["transactions"]) == ["t1", "t2"]
    d = api.get("transactions?from=2024-02-01&to=2024-02-28&type=1").get_json()
    assert [t["uid"] for t in d["transactions"]] == ["t2"]


def test_list_paging_and_sort(api):
    d = api.get("transactions?limit=2&offset=1&sort=amount&dir=asc").get_json()
    assert (d["total"], d["limit"], d["offset"]) == (4, 2, 1)
    assert [t["uid"] for t in d["transactions"]] == ["t1", "t2"]  # 7, [10, 25.5], 1000


@pytest.mark.parametrize("qs", ["limit=0", "limit=1001", "limit=x", "offset=-1", "sort=uid", "dir=up"])
def test_list_rejects_bad_paging(api, qs):
    assert api.get(f"transactions?{qs}").status_code == 400


def test_transaction_fields(api):
    t = api.get("transactions/t2").get_json()["transaction"]
    assert t == {
        "uid": "t2", "type": "expense", "date": "2024-02-10", "time": t["time"],
        "timestamp_ms": 1704067201000, "amount": 25.5, "currency": "EUR",
        "entered_amount": 25.5, "entered_currency": "EUR",
        "account_uid": "a1", "account": "Wallet", "to_account_uid": None, "to_account": None,
        "category_uid": "c-food-out", "category": "Food > Eating out",
        "note": "dinner", "description": "", "transfer_id": None, "deleted": False, "updated_ms": 1,
        "app_updated_ms": 1,
    }
    assert t["time"] == datetime.fromtimestamp(1704067201).strftime("%H:%M:%S")


def test_get_deleted_and_missing(api):
    assert api.get("transactions/t5").get_json()["transaction"]["deleted"] is True
    r = api.get("transactions/nope")
    assert r.status_code == 404 and r.get_json() == {"ok": False, "error": "not found"}


def test_accounts(api):
    accts = {a["uid"]: a for a in api.get("accounts").get_json()["accounts"]}
    assert accts["a1"]["name"] == "Wallet" and accts["a1"]["currency"] == "EUR"
    assert accts["a1"]["group"] == "Cash" and accts["a1"]["status"] == "normal"
    # t1 and t2 are expenses, t6 an incoming transfer, t5 is deleted
    assert accts["a1"]["balance"] == pytest.approx(-10 - 25.5 + 50)
    assert accts["a2"]["balance"] == pytest.approx(1000 - 7)


def test_categories_and_currencies(api):
    d = api.get("categories").get_json()
    food = next(c for c in d["expense"] if c["uid"] == "c-food")
    assert food["deleted"] is False
    assert food["children"] == [{"uid": "c-food-out", "name": "Eating out", "deleted": False}]
    assert [c["uid"] for c in d["income"]] == ["c-salary"]
    cur = api.get("currencies").get_json()["currencies"]
    assert {c["iso"]: c["rate"] for c in cur} == {"EUR": 1.0, "USD": 0.9}


def test_categories_show_deleted(api):
    mmbak.execute(db_path(), "INSERT INTO ZCATEGORY (C_IS_DEL, NAME, ORDERSEQ, TYPE, STATUS, uid, pUid) VALUES "
                             "(1, 'Old', 3, 1, 2, 'c-old', 'c-food'), (1, 'Gone', 4, 1, 0, 'c-gone', NULL), "
                             "(1, 'Gone kid', 1, 1, 2, 'c-gone-kid', 'c-gone'), ('', 'Bonus', 2, 0, 0, 'c-bonus', NULL)")
    tree = lambda d: {c["uid"]: (c["deleted"], [(k["uid"], k["deleted"]) for k in c["children"]])
                      for t in ("income", "expense") for c in d[t]}
    hidden = tree(api.get("categories").get_json())
    assert hidden == tree(api.get("categories?show_deleted=hide").get_json())
    assert hidden == {"c-food": (False, [("c-food-out", False)]), "c-fun": (False, []),
                      "c-salary": (False, []), "c-bonus": (False, [])}  # '' is live, as the app writes it
    assert tree(api.get("categories?show_deleted=show").get_json()) == {
        **hidden, "c-food": (False, [("c-food-out", False), ("c-old", True)]),
        "c-gone": (True, [("c-gone-kid", True)])}
    assert api.get("categories?show_deleted=only").status_code == 400


# -- writes -----------------------------------------------------------------------

def test_add_by_type_name(api):
    r = api.post("transactions", {"type": "expense", "account": "a2", "amount": 3.5, "category": "c-fun",
                                  "date": "2025-05-06", "time": "07:08", "note": "coffee"})
    assert r.status_code == 201
    t = r.get_json()["transaction"]
    assert (t["type"], t["amount"], t["currency"], t["date"], t["time"]) == ("expense", 3.5, "USD", "2025-05-06", "07:08:00")
    assert (t["category"], t["note"], t["account"]) == ("Fun", "coffee", "Bank")


def test_add_without_date_means_now(api):
    before = datetime.now().replace(microsecond=0)
    t = api.post("transactions", {"type": "income", "account": "a1", "amount": 5, "category": "c-salary"}).get_json()["transaction"]
    assert before.timestamp() * 1000 <= t["timestamp_ms"] <= datetime.now().timestamp() * 1000 + 1000


def test_add_transfer(api):
    d = api.post("transactions", {"type": "transfer", "account": "a1", "to_account": "a2", "amount": 9}).get_json()
    t, m = d["transaction"], d["mirror"]
    assert (t["type"], t["to_account"]) == ("transfer", "Bank")
    assert (m["type"], m["account_uid"], m["transfer_id"]) == ("transfer_mirror", "a2", t["transfer_id"])
    assert count() == 8
    d = api.post("transactions", {"type": "expense", "account": "a1", "amount": 1, "category": "c-fun"}).get_json()
    assert "mirror" not in d


@pytest.mark.parametrize("body", [
    {"type": "transfer_mirror", "account": "a1", "to_account": "a2", "amount": 1},
    {"type": "expense", "account": "a1", "amount": 1, "category": "c-salary"},  # an income category
    {"type": "expense", "account": "a1", "amount": 1, "category": "nope"},
    {"type": "expense", "account": "a1", "amount": 1, "category": "c-fun", "note": 5},
    {"type": "gift", "account": "a1", "amount": 1},
])
def test_add_rejects(api, body):
    assert api.post("transactions", body).status_code == 400
    assert count() == 6


def test_add_rejects_non_object(api):
    assert api.post("transactions", ["x"]).status_code == 400
    assert api.call("post", "transactions", data="not json", content_type="application/json").status_code == 400


def test_patch_several_fields(api):
    r = api.patch("transactions/t1", {"note": "n", "description": "d", "category": "c-fun", "account": "a2", "amount": "12"})
    assert r.status_code == 200
    t = r.get_json()["transaction"]
    assert (t["note"], t["description"], t["category_uid"], t["account_uid"], t["amount"]) == ("n", "d", "c-fun", "a2", 12)
    db = row("t1")
    assert float(db["ZMONEY"]) == pytest.approx(12 * 0.9)  # restated in the new account's rate
    assert db["UTIME"] > 1000


def test_patch_time_keeps_date_and_date_keeps_time(api):
    api.patch("transactions/t1", {"time": "13:14"})
    t = api.get("transactions/t1").get_json()["transaction"]
    assert (t["date"], t["time"]) == ("2024-01-05", "13:14:00")
    api.patch("transactions/t1", {"date": "2025-02-03"})
    t = api.get("transactions/t1").get_json()["transaction"]
    assert (t["date"], t["time"]) == ("2025-02-03", "13:14:00")
    assert t["timestamp_ms"] == int(datetime(2025, 2, 3, 13, 14).timestamp() * 1000)


@pytest.mark.parametrize("body", [
    {}, {"IS_DEL": 1}, {"ZCONTENT": "x"}, {"amount": "abc"}, {"amount": -1}, {"date": "2025-02-30"},
    {"category": "c-salary"}, {"account": "nope"}, {"note": None},
])
def test_patch_rejects(api, body):
    before = dict(row("t1"))
    assert api.patch("transactions/t1", body).status_code == 400
    assert dict(row("t1")) == before


def test_patch_transfer_keeps_accounts_and_has_no_category(api):
    t = api.post("transactions", {"type": "transfer", "account": "a1", "to_account": "a2", "amount": 9}).get_json()["transaction"]
    assert api.patch(f"transactions/{t['uid']}", {"account": "a2"}).status_code == 400
    assert api.patch(f"transactions/{t['uid']}", {"category": "c-fun"}).status_code == 400
    assert api.patch(f"transactions/{t['uid']}", {"note": "rent"}).status_code == 200


def test_patch_missing(api):
    assert api.patch("transactions/nope", {"note": "x"}).status_code == 404


def test_add_with_entered_amount_and_currency(api):
    body = {"type": "expense", "account": "a1", "amount": 5.11, "category": "c-fun", "date": "2001-05-06"}
    t = api.post("transactions", {**body, "entered_amount": 10, "entered_currency": "USD"}).get_json()["transaction"]
    assert (t["amount"], t["currency"], t["entered_amount"], t["entered_currency"]) == (5.11, "EUR", 10, "USD")
    t = api.post("transactions", {**body, "entered_amount": 10, "entered_currency": "cur-usd"}).get_json()["transaction"]
    assert t["entered_currency"] == "USD"  # a uid works too
    t = api.post("transactions", body).get_json()["transaction"]
    assert (t["entered_amount"], t["entered_currency"]) == (5.11, "EUR")  # without them: the amount


@pytest.mark.parametrize("extra", [
    {"entered_amount": 10}, {"entered_currency": "USD"}, {"entered_amount": 10, "entered_currency": "XXX"},
    {"entered_amount": 0, "entered_currency": "USD"}, {"entered_amount": "nan", "entered_currency": "USD"},
    {"to_amount": 3},
])
def test_add_rejects_bad_entered_values(api, extra):
    body = {"type": "expense", "account": "a1", "amount": 1, "category": "c-fun", **extra}
    assert api.post("transactions", body).status_code == 400
    assert count() == 6


def test_add_transfer_with_the_amount_that_arrived(api):
    t = api.post("transactions", {"type": "transfer", "account": "a1", "to_account": "a2", "amount": 100,
                                  "to_amount": 107.5}).get_json()["transaction"]
    mirror = api.get("transactions?show_mirror=only&account=a2").get_json()["transactions"]
    assert [(m["transfer_id"], m["amount"], m["entered_amount"]) for m in mirror] == [(t["transfer_id"], 107.5, 107.5)]


def test_patch_entered_values_are_written_as_is(api):
    # restating a pre-euro row: the entered BGN must stay exact
    t = api.patch("transactions/t1", {"amount": 5.11, "entered_amount": 10, "entered_currency": "USD"}).get_json()["transaction"]
    assert (t["amount"], t["entered_amount"], t["entered_currency"]) == (5.11, 10, "USD")
    t = api.patch("transactions/t1", {"entered_amount": 9.99}).get_json()["transaction"]
    assert (t["amount"], t["entered_amount"], t["entered_currency"]) == (5.11, 9.99, "USD")
    # the amount alone still rescales what was entered
    t = api.patch("transactions/t2", {"amount": 51}).get_json()["transaction"]
    assert t["entered_amount"] == 51
    assert api.patch("transactions/t1", {"entered_currency": "XXX"}).status_code == 400


def test_patch_type_between_income_and_expense_and_between_adjustments(api):
    t = api.patch("transactions/t1", {"type": "income", "category": "c-salary"}).get_json()["transaction"]
    assert (t["uid"], t["type"], t["category_uid"]) == ("t1", "income", "c-salary")
    assert row("t1")["DO_TYPE"] == "0"
    adj = api.post("transactions", {"type": "balance_increase", "account": "a1", "amount": 1}).get_json()["transaction"]
    t = api.patch(f"transactions/{adj['uid']}", {"type": "8"}).get_json()["transaction"]
    assert (t["type"], t["category_uid"]) == ("balance_decrease", "-4")
    assert api.patch("transactions/t2", {"type": "expense", "note": "same type is fine"}).status_code == 200


@pytest.mark.parametrize("body", [
    {"type": "income"},  # needs a category from the income tree
    {"type": "income", "category": "c-fun"},  # an expense category
    {"type": "transfer"}, {"type": "balance_increase"}, {"type": "gift"}, {"type": 1},
])
def test_patch_type_rejects(api, body):
    before = dict(row("t1"))
    assert api.patch("transactions/t1", body).status_code == 400
    assert dict(row("t1")) == before


def test_patch_transfer_carries_over_to_its_other_row(api):
    mmbak.execute(db_path(), "INSERT INTO ASSETS (NIC_NAME, ORDERSEQ, ZDATA, uid, currencyUid, groupUid) "
                             "VALUES ('Savings', 3, '0', 'a3', 'cur-eur', 'g1')")
    same = api.post("transactions", {"type": "transfer", "account": "a1", "to_account": "a3", "amount": 20}).get_json()["transaction"]
    r = api.patch(f"transactions/{same['uid']}", {"amount": 25, "note": "rent", "date": "2025-03-04", "time": "09:00"}).get_json()
    m = r["mirror"]
    assert (m["type"], m["transfer_id"], m["account_uid"]) == ("transfer_mirror", same["transfer_id"], "a3")
    assert (m["amount"], m["note"], m["date"], m["time"]) == (25, "rent", "2025-03-04", "09:00:00")
    # and the other way round, from the mirror
    m = api.patch(f"transactions/{m['uid']}", {"description": "d"}).get_json()["mirror"]
    assert (m["uid"], m["description"]) == (same["uid"], "d")

    cross = api.post("transactions", {"type": "transfer", "account": "a1", "to_account": "a2", "amount": 10,
                                      "to_amount": 11}).get_json()["transaction"]
    m = api.patch(f"transactions/{cross['uid']}", {"amount": 12}).get_json()["mirror"]
    assert m["amount"] == 11  # another currency: its amount is left alone
    m = api.patch(f"transactions/{cross['uid']}", {"to_amount": 13.2}).get_json()["mirror"]
    assert m["amount"] == 13.2
    assert api.patch(f"transactions/{m['uid']}", {"to_amount": 1}).status_code == 400  # not on the mirror
    assert api.patch("transactions/t1", {"to_amount": 1}).status_code == 400
    assert "mirror" not in api.patch("transactions/t1", {"note": "x"}).get_json()


def test_list_updated_since(api):
    assert api.get("transactions?updated_since=0").get_json()["total"] == 4
    since = int(datetime.now().timestamp() * 1000)
    api.patch("transactions/t2", {"note": "changed"})
    d = api.get(f"transactions?updated_since={since}").get_json()
    assert [t["uid"] for t in d["transactions"]] == ["t2"] and d["total"] == 1
    assert d["transactions"][0]["updated_ms"] >= since
    assert api.get("transactions?updated_since=x").status_code == 400


def stamped():
    con = sqlite3.connect(dbstore.store_for("alice").changes_path)
    try:
        return dict(con.execute(f"SELECT uid, changed FROM {dbstore.CHANGES_TABLE}"))
    finally:
        con.close()


def test_a_refused_edit_stamps_nothing(api):
    assert api.patch("transactions/t2", {"amount": -1}).status_code == 400
    assert "t2" not in stamped()


def install_changed_copy(tmp_path, sql, params=()):
    """Install a copy of the working db with sql applied, as a sync from
    the app would (the copy keeps the app's own UTIME)."""
    store = dbstore.store_for("alice")
    staged = store.staging_path()
    dbstore.copy_db(store.db_path, staged)
    mmbak.execute(staged, sql, params)
    store.install_db(staged, "gdrive", "MM.mmbak")


def test_rows_synced_from_the_app_count_from_when_mmw_got_them(api, tmp_path):
    # Changed in the app long before the sync: UTIME 5, older than any read.
    since = int(datetime.now().timestamp() * 1000)
    install_changed_copy(tmp_path, "UPDATE INOUTCOME SET ZCONTENT = 'from the phone', UTIME = 5 WHERE uid = 't3'")
    d = api.get(f"transactions?updated_since={since}").get_json()
    assert [t["uid"] for t in d["transactions"]] == ["t3"]
    t = d["transactions"][0]
    assert t["updated_ms"] >= since and t["app_updated_ms"] == 5
    # sort=updated goes by it too: t3 comes first despite its old UTIME.
    first = api.get("transactions?sort=updated&dir=desc").get_json()["transactions"][0]
    assert first["uid"] == "t3"


def test_rows_deleted_in_the_app_show_up_as_changed(api, tmp_path):
    since = int(datetime.now().timestamp() * 1000)
    install_changed_copy(tmp_path, "UPDATE INOUTCOME SET IS_DEL = 1 WHERE uid = 't1'")
    d = api.get(f"transactions?updated_since={since}&show_deleted=show").get_json()
    assert [(t["uid"], t["deleted"]) for t in d["transactions"]] == [("t1", True)]


def test_status_has_the_time_zone(api, monkeypatch):
    tz = api.get("status").get_json()["time_zone"]
    assert tz["name"] and isinstance(tz["utc_offset_minutes"], int)
    monkeypatch.setenv("TZ", "Europe/Sofia")
    assert api.get("status").get_json()["time_zone"]["name"] == "Europe/Sofia"


def test_delete_is_soft(api):
    r = api.delete("transactions/t1")
    assert r.get_json() == {"ok": True, "deleted": ["t1"]}
    assert row("t1")["IS_DEL"] == 1 and row("t1")["UTIME"] > 1000
    assert count() == 6
    assert "t1" not in [t["uid"] for t in api.get("transactions").get_json()["transactions"]]
    assert api.delete("transactions/nope").status_code == 404


def test_delete_transfer_takes_its_other_leg_and_fee(api):
    for uid, do_type, asset, link in (("x3", "3", "a1", "tr"), ("x4", "4", "a2", "tr"), ("fee", "1", "a1", "")):
        mmbak.add_tx(db_path(), uid, do_type=do_type, asset=asset, txUidTrans=link, txUidFee="f1")
    r = api.delete("transactions/fee")
    assert sorted(r.get_json()["deleted"]) == ["fee", "x3", "x4"]
    assert {row(u)["IS_DEL"] for u in ("x3", "x4", "fee")} == {1}
    assert row("t1")["IS_DEL"] == 0


def test_moving_account_restates_main_currency_amount(api):
    api.patch("transactions/t1", {"account": "a2"})  # EUR 1.0 -> USD 0.9, amount 10
    db = row("t1")
    assert db["AMOUNT_ACCOUNT"] == 10 and float(db["ZMONEY"]) == pytest.approx(9.0)


def test_writes_take_the_first_write_backup(api):
    api.patch("transactions/t1", {"note": "x"})
    assert dbstore.store_for("alice").backup_count() == 1
    assert dbstore.store_for("alice").local_modified()


# -- expect and batch PATCH ----------------------------------------------------------

def test_patch_expect(api):
    ok = api.patch("transactions/t1", {"note": "x", "expect": {"type": "expense", "amount": 10, "note": "lunch"}})
    assert ok.status_code == 200 and row("t1")["ZCONTENT"] == "x"
    r = api.patch("transactions/t1", {"amount": 11, "expect": {"amount": 10.5, "note": "x"}})
    assert r.status_code == 412 and r.get_json()["error"] == "expect not met: amount is 10.0, not 10.5"
    assert row("t1")["AMOUNT_ACCOUNT"] == 10
    assert api.patch("transactions/t1", {"note": "y", "expect": {"bogus": 1}}).status_code == 400
    assert api.patch("transactions/t1", {"note": "y", "expect": [1]}).status_code == 400
    assert api.patch("transactions/nope", {"note": "y", "expect": {"note": "x"}}).status_code == 404
    assert row("t1")["ZCONTENT"] == "x"


def test_batch_patch_applies_in_order(api):
    mmbak.execute(db_path(), "INSERT INTO ASSETS (NIC_NAME, ORDERSEQ, ZDATA, uid, currencyUid, groupUid) "
                             "VALUES ('Savings', 3, '0', 'a3', 'cur-eur', 'g1')")
    tr = api.post("transactions", {"type": "transfer", "account": "a1", "to_account": "a3", "amount": 20}).get_json()
    sending = tr["transaction"]["uid"]
    mirror = api.patch(f"transactions/{sending}", {"note": "rent"}).get_json()["mirror"]["uid"]
    r = api.patch("transactions", {"changes": [
        {"uid": "t1", "amount": 20, "entered_amount": 39.12, "entered_currency": "USD"},
        {"uid": "t1", "note": "second", "expect": {"amount": 20, "entered_amount": 39.12}},  # sees the first
        {"uid": sending, "amount": 25},
        {"uid": mirror, "description": "d", "expect": {"amount": 25}},  # the sending row's change carried over
    ]})
    assert r.status_code == 200, r.get_json()
    res = r.get_json()["results"]
    assert [x["transaction"]["uid"] for x in res] == ["t1", "t1", sending, mirror]
    assert "mirror" not in res[0] and res[2]["mirror"]["uid"] == mirror
    assert res[0]["transaction"]["note"] == "second"  # rows as the whole batch left them
    assert (res[3]["transaction"]["amount"], res[3]["transaction"]["description"]) == (25, "d")
    assert (row("t1")["AMOUNT_ACCOUNT"], float(row("t1")["IN_ZMONEY"]), row("t1")["currencyUid"]) == (20, 39.12, "cur-usd")


def test_batch_patch_is_all_or_nothing(api):
    before = {u: dict(row(u)) for u in ("t1", "t2", "t3")}
    r = api.patch("transactions", {"changes": [
        {"uid": "t1", "note": "fine"},
        {"uid": "nope", "note": "x"},
        {"uid": "t2", "amount": "abc"},
        {"uid": "t3", "note": "x", "expect": {"note": "not this"}},
        {"note": "no uid"},
        "junk",
    ]})
    assert r.status_code == 400
    d = r.get_json()
    assert d["error"] == "5 of 6 changes failed; nothing was written"
    assert [(e["index"], e["uid"], e["status"]) for e in d["errors"]] == [
        (1, "nope", 404), (2, "t2", 400), (3, "t3", 412), (4, None, 400), (5, None, 400)]
    assert d["errors"][1]["error"] == "invalid amount"
    assert {u: dict(row(u)) for u in before} == before


@pytest.mark.parametrize("body", [{}, {"changes": []}, {"changes": {"uid": "t1"}}, {"changes": "t1"}])
def test_batch_patch_rejects(api, body):
    assert api.patch("transactions", body).status_code == 400


def test_batch_patch_limit(api):
    assert api.patch("transactions", {"changes": [{"uid": "t1", "note": "x"}] * 1001}).status_code == 400
    r = api.patch("transactions", {"changes": [{"uid": "t1", "note": str(i)} for i in range(1000)]})
    assert r.status_code == 200 and row("t1")["ZCONTENT"] == "999"


def test_batch_post_adds_in_order(api):
    r = api.post("transactions", {"transactions": [
        {"type": "expense", "account": "a1", "amount": 30, "category": "c-food", "date": "2026-10-02",
         "time": "18:05", "note": "groceries"},
        {"type": "expense", "account": "a1", "amount": 12.4, "category": "c-fun", "date": "2026-10-02",
         "time": "18:05", "note": "groceries", "entered_amount": 13.8, "entered_currency": "USD"},
        {"type": "transfer", "account": "a1", "to_account": "a2", "amount": 9},
    ]})
    assert r.status_code == 201, r.get_json()
    res = r.get_json()["results"]
    assert [(x["transaction"]["type"], x["transaction"]["amount"]) for x in res] == [
        ("expense", 30), ("expense", 12.4), ("transfer", 9)]
    assert "mirror" not in res[0] and res[2]["mirror"]["type"] == "transfer_mirror"
    assert (res[1]["transaction"]["entered_amount"], res[1]["transaction"]["entered_currency"]) == (13.8, "USD")
    assert count() == 6 + 4
    assert row(res[0]["transaction"]["uid"])["ZCONTENT"] == "groceries"


def test_batch_post_is_all_or_nothing(api):
    r = api.post("transactions", {"transactions": [
        {"type": "expense", "account": "a1", "amount": 30, "category": "c-food"},
        {"type": "expense", "account": "a1", "amount": 1, "category": "c-salary"},
        {"type": "transfer_mirror", "account": "a1", "to_account": "a2", "amount": 1},
        {"type": "expense", "account": "nope", "amount": 1, "category": "c-food", "uid": "mine"},
        "junk",
    ]})
    assert r.status_code == 400
    d = r.get_json()
    assert d["error"] == "4 of 5 transactions failed; nothing was written"
    assert [(e["index"], e["uid"], e["status"]) for e in d["errors"]] == [
        (1, None, 400), (2, None, 400), (3, None, 400), (4, None, 400)]
    assert d["errors"][2]["error"] == "unknown account"
    assert count() == 6


@pytest.mark.parametrize("body", [{"transactions": []}, {"transactions": {"type": "expense"}}, {"transactions": None}])
def test_batch_post_rejects(api, body):
    assert api.post("transactions", body).status_code == 400
    assert count() == 6


def test_batch_post_limit_and_token(api):
    item = {"type": "expense", "account": "a1", "amount": 1, "category": "c-fun"}
    assert api.post("transactions", {"transactions": [item] * 1001}).status_code == 400
    ro = users.create_token("alice", "ro")
    assert api.post("transactions", {"transactions": [item]}, token=ro).status_code == 403
    assert count() == 6


def test_batch_patch_needs_a_write_token(api):
    ro = users.create_token("alice", "ro")
    assert api.patch("transactions", {"changes": [{"uid": "t1", "note": "x"}]}, token=ro).status_code == 403
