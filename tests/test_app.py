"""The app at / and its JSON under /api/app/, against the synthetic export
in mmbak.py (Wallet a1 in EUR, Bank a2 in USD at 0.9 to the EUR)."""
import pytest

import dbstore
import mmbak
import users


def db_path():
    return dbstore.store_for("alice").db_path


def get(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


# -- the page --------------------------------------------------------------------

def test_index_is_the_app(client):
    html = client.get("/").get_data(as_text=True)
    assert 'src="/static/app/app.js' in html and 'rel="manifest"' in html
    assert client.get("/static/app/app.js").status_code == 200
    assert client.get("/static/app/app.css").status_code == 200


def test_index_without_a_database_goes_to_setup(data_dir):
    from app import app
    users.create("bob", "correct horse")
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(user="bob", ver=users.get("bob")["session_version"])
    assert "/setup" in c.get("/").headers["Location"]


@pytest.mark.parametrize("old", ["/transactions?account=a1&page=2", "/accounts", "/categories"])
def test_the_editor_moved_under_editor(client, old):
    r = client.get(old)
    assert r.status_code == 301 and r.headers["Location"] == "/editor" + old
    assert client.get("/editor").headers["Location"] == "/editor/transactions"
    assert client.get("/editor" + old).status_code == 200


def test_editor_links_back_to_the_app(client):
    assert '<a href="/">App</a>' in client.get("/editor/transactions").get_data(as_text=True)


# -- reads -------------------------------------------------------------------------

def test_meta(client):
    d = get(client, "/api/app/meta")
    assert d["main_currency"] == "cur-eur"
    assert d["currencies"]["cur-usd"] == {"iso": "USD", "symbol": "US$", "decimals": 2, "rate": 0.9, "order": 101}
    assert [(a["uid"], a["currency"], a["hidden"]) for a in d["accounts"]] == [("a1", "cur-eur", False),
                                                                               ("a2", "cur-usd", False)]
    food = next(c for c in d["categories"]["expense"] if c["uid"] == "c-food")
    assert food["children"] == [{"uid": "c-food-out", "name": "Eating out"}]
    assert [c["uid"] for c in d["categories"]["income"]] == ["c-salary"]


def test_days_group_rows_newest_first_with_day_totals(client):
    days = get(client, "/api/app/days?from=2024-02-01&to=2024-02-29")["days"]
    assert [d["date"] for d in days] == ["2024-02-11", "2024-02-10"]
    # Bank's 1000 USD income counts as 900 in the main currency.
    assert (days[0]["income"], days[0]["expense"]) == (900.0, 0.0)
    assert (days[1]["income"], days[1]["expense"]) == (0.0, 25.5)
    row = days[1]["rows"][0]
    assert {k: row[k] for k in ("uid", "type", "category", "subcategory", "account", "note", "amount", "currency")} == {
        "uid": "t2", "type": "1", "category": "Food", "subcategory": "Eating out", "account": "Wallet",
        "note": "dinner", "amount": 25.5, "currency": "cur-eur"}


def test_days_leave_out_deleted_and_mirror_rows_and_adjustments_from_totals(client):
    mmbak.add_tx(db_path(), "adj", wdate="2024-03-01", do_type="7", amount=5, ctg="-4")
    days = get(client, "/api/app/days?from=2024-03-01&to=2024-03-31")["days"]
    assert [r["uid"] for d in days for r in d["rows"]] == ["adj", "t4"]  # t5 deleted, t6 a mirror
    assert (days[0]["income"], days[0]["expense"]) == (0.0, 6.3)  # t4: 7 USD


def test_days_need_dates(client):
    r = client.get("/api/app/days?from=2024-02-01&to=soon")
    assert r.status_code == 400 and "to must be a date" in r.get_json()["error"]


def test_sums_per_day(client):
    days = get(client, "/api/app/sums?from=2024-01-01&to=2024-03-31")["days"]
    assert [(d["date"], d["income"], d["expense"]) for d in days] == [
        ("2024-01-05", 0.0, 10.0), ("2024-02-10", 0.0, 25.5), ("2024-02-11", 900.0, 0.0), ("2024-03-01", 0.0, 6.3)]


def test_sums_round_each_row_like_the_app(client):
    # Restated amounts carry fractions of a cent: each row counts rounded,
    # so three of 0.004 add up to 0, not 0.01.
    for uid in ("x1", "x2", "x3"):
        mmbak.add_tx(db_path(), uid, wdate="2024-05-01", amount=0.004)
    assert get(client, "/api/app/sums?from=2024-05-01&to=2024-05-31")["days"] == [
        {"date": "2024-05-01", "income": 0.0, "expense": 0.0}]
    assert get(client, "/api/app/days?from=2024-05-01&to=2024-05-31")["days"][0]["expense"] == 0.0


def test_stats_per_category(client):
    d = get(client, "/api/app/stats?from=2024-01-01&to=2024-03-31")
    assert d["income"]["total"] == 900.0
    assert d["expense"]["total"] == 41.8
    food, fun = d["expense"]["categories"]
    assert (food["uid"], food["name"], food["amount"]) == ("c-food", "Food", 35.5)
    # t1 sits on the root itself: the app calls that Other.
    assert [(c["name"], c["amount"]) for c in food["children"]] == [("Eating out", 25.5), ("Other", 10.0)]
    assert (fun["name"], fun["amount"]) == ("Fun", 6.3)  # 7 USD


def test_accounts_with_balances_and_totals(client):
    d = get(client, "/api/app/accounts")
    # Wallet: -10 -25.5 +50 (t6, a receiving leg); Bank: +1000 -7 USD.
    (group,) = d["groups"]
    assert [(a["name"], a["balance"], a["currency"]) for a in group["accounts"]] == [
        ("Wallet", 14.5, "cur-eur"), ("Bank", 993.0, "cur-usd")]
    assert group["total"] == 908.2 and d["assets"] == 908.2 and d["liabilities"] == 0


def test_hidden_accounts_count_but_deleted_ones_dont(client):
    mmbak.execute(db_path(), "UPDATE ASSETS SET ZDATA = '3' WHERE uid = 'a2'")
    d = get(client, "/api/app/accounts")
    assert [a["uid"] for a in d["groups"][0]["accounts"]] == ["a1"]
    assert d["assets"] == 908.2
    mmbak.execute(db_path(), "UPDATE ASSETS SET ZDATA = '1' WHERE uid = 'a2'")
    assert get(client, "/api/app/accounts")["assets"] == 14.5


def test_account_statement_with_running_balance(client):
    d = get(client, "/api/app/accounts/a1?from=2024-02-01&to=2024-03-31")
    assert (d["name"], d["opening"], d["closing"]) == ("Wallet", -10.0, 14.5)
    assert [(day["date"], day["deposit"], day["withdrawal"]) for day in d["days"]] == [
        ("2024-03-03", 50.0, 0.0), ("2024-02-10", 0.0, 25.5)]
    assert [(r["uid"], r["amount"], r["balance"]) for day in d["days"] for r in day["rows"]] == [
        ("t6", 50.0, 14.5), ("t2", 25.5, -35.5)]
    sums = get(client, "/api/app/accounts/a1?from=2024-02-01&to=2024-03-31&sums=1")
    assert sums["closing"] == 14.5 and "rows" not in sums["days"][0]
    assert client.get("/api/app/accounts/nope?from=2024-02-01&to=2024-03-31").status_code == 404


# -- writes ------------------------------------------------------------------------

def test_add_edit_and_delete(client):
    r = client.post("/api/app/transactions", headers=client.csrf, json={
        "type": "1", "account": "a1", "category": "c-fun", "amount": 3.5, "date": "2024-04-01", "time": "10:30",
        "note": "n"})
    assert r.status_code == 200
    uid = r.get_json()["uid"]
    t = get(client, f"/api/app/transactions/{uid}")["transaction"]
    assert {k: t[k] for k in ("type", "date", "time", "account", "category", "amount", "currency", "note")} == {
        "type": "1", "date": "2024-04-01", "time": "10:30", "account": "a1", "category": "c-fun", "amount": 3.5,
        "currency": "cur-eur", "note": "n"}
    r = client.patch(f"/api/app/transactions/{uid}", headers=client.csrf, json={"amount": 4, "note": "m"})
    assert r.status_code == 200
    t = get(client, f"/api/app/transactions/{uid}")["transaction"]
    assert (t["amount"], t["note"], t["time"]) == (4.0, "m", "10:30")
    assert client.delete(f"/api/app/transactions/{uid}", headers=client.csrf).status_code == 200
    assert get(client, "/api/app/days?from=2024-04-01&to=2024-04-30")["days"] == []


def test_refused_edits_answer_with_the_reason(client):
    r = client.post("/api/app/transactions", headers=client.csrf, json={"type": "1", "account": "a1", "amount": 1,
                                                                        "date": "2024-04-01"})
    assert r.status_code == 400 and r.get_json()["error"] == "category required"
    assert client.get("/api/app/transactions/nope").status_code == 404


@pytest.mark.parametrize("method, url", [
    ("post", "/api/app/transactions"), ("patch", "/api/app/transactions/t1"), ("delete", "/api/app/transactions/t1")])
def test_writes_need_the_csrf_token(client, method, url):
    r = getattr(client, method)(url, json={"note": "x"})
    assert r.status_code == 400 and "form token" in r.get_json()["error"]


# -- settings, suggestions, search, budgets ------------------------------------

def test_meta_carries_the_week_start(client):
    assert get(client, "/api/app/meta")["week_start"] == 1
    mmbak.execute(db_path(), "UPDATE ZETC SET ZDATA = '0' WHERE dataTypeKey = 'week_start_day'")
    assert get(client, "/api/app/meta")["week_start"] == 0
    mmbak.execute(db_path(), "UPDATE ZETC SET ZDATA = 'x' WHERE dataTypeKey = 'week_start_day'")
    assert get(client, "/api/app/meta")["week_start"] == 1


def test_suggestions_are_live_notes(client):
    assert get(client, "/api/app/suggestions")["notes"] == ["mirror", "50% off_sale", "dinner", "lunch"]


def test_search_matches_note_or_description(client):
    d = get(client, "/api/app/search?q=work")  # t1's description
    assert [r["uid"] for r in d["rows"]] == ["t1"] and d["count"] == 1
    d = get(client, "/api/app/search?q=dinner")
    assert [r["uid"] for r in d["rows"]] == ["t2"] and d["rows"][0]["date"] == "2024-02-10"
    assert d["expense"] == 25.5 and d["income"] == 0


def test_search_takes_the_filters(client):
    d = get(client, "/api/app/search?account=a2")
    assert [r["uid"] for r in d["rows"]] == ["t4", "t3"]
    assert (d["income"], d["expense"]) == (900.0, 6.3)
    assert get(client, "/api/app/search?q=!lunch&account=a1")["count"] == 1  # t2


def add_budget(uid, category, amounts, is_total=0, order=1):
    mmbak.execute(db_path(), "INSERT INTO BUDGET (DO_TYPE, PERIOD_TYPE, IS_TOTAL, IS_DEL, ORDER_SEQ, uid, targetUid)"
                             " VALUES (1, 6, ?, 0, ?, ?, ?)", (is_total, order, uid, category))
    for period, amount in amounts:
        mmbak.execute(db_path(), "INSERT INTO BUDGET_AMOUNT (IS_DEL, AMOUNT, BUDGET_PERIOD, budgetUid)"
                                 " VALUES (0, ?, ?, ?)", (amount, period, uid))


def test_total_budgets_in_force_with_spending(client):
    add_budget("b-total", "", [(0, 100), (202402, 50)], is_total=1, order=3)
    add_budget("b-fun", "c-fun", [(0, 5)], order=1)
    add_budget("b-food", "c-food", [(0, 30), (202403, 40)], order=2)
    add_budget("b-out", "c-food-out", [(0, 20)], order=4)
    d = get(client, "/api/app/total?month=2024-02")
    total, food, fun = d["budgets"]
    # Feb: the total's 202402 amount, Food's default; t2 (Eating out) 25.5.
    assert (total["name"], total["amount"], total["spent"]) == ("Total Budget", 50, 25.5)
    # In the category tree's order, the child's budget under its root's.
    assert (food["name"], food["amount"], food["spent"]) == ("Food", 30, 25.5)
    assert [(c["name"], c["amount"], c["spent"]) for c in food["children"]] == [("Eating out", 20, 25.5)]
    assert (fun["name"], fun["spent"]) == ("Fun", 0.0)
    assert get(client, "/api/app/total?month=2024-03")["budgets"][1]["amount"] == 40


def test_total_compares_months_and_counts_cash(client):
    d = get(client, "/api/app/total?month=2024-02")
    # Jan: t1 10; Feb: t2 25.5, all from Wallet, in the Cash group (TYPE 11).
    assert (d["expenses"], d["previous"], d["cash"]) == (25.5, 10.0, 25.5)
    mmbak.execute(db_path(), "UPDATE ASSETGROUP SET TYPE = 0")
    assert get(client, "/api/app/total?month=2024-02")["cash"] == 0
    assert client.get("/api/app/total?month=feb").status_code == 400


def test_edit_form_shows_the_entered_amount(client):
    r = client.post("/api/app/transactions", headers=client.csrf, json={
        "type": "1", "account": "a1", "category": "c-fun", "amount": 9, "entered_amount": 10,
        "entered_currency": "cur-usd", "date": "2024-04-01"})
    t = get(client, f"/api/app/transactions/{r.get_json()['uid']}")["transaction"]
    assert (t["amount"], t["currency"], t["entered_amount"], t["entered_currency"]) == (9.0, "cur-eur", 10.0, "cur-usd")


# -- fees, accounts, categories, bookmarks -------------------------------------

def post(client, url, **body):
    return client.post(url, json=body, headers=client.csrf)


def patch(client, url, **body):
    return client.patch(url, json=body, headers=client.csrf)


def test_a_transfer_with_a_fee(client):
    r = post(client, "/api/app/transactions", type="3", account="a1", to_account="a2", amount=100,
             date="2024-04-02", time="09:00", fee=1.5, fee_category="c-fun", fee_note="bank fee")
    assert r.status_code == 200
    rows = mmbak.query(db_path(), "SELECT DO_TYPE, assetUid, AMOUNT_ACCOUNT, ctgUid, ZCONTENT, ZDATE, txUidFee "
                                  "FROM INOUTCOME WHERE WDATE = '2024-04-02' ORDER BY DO_TYPE")
    fee, leg, mirror = rows
    assert (fee["DO_TYPE"], fee["assetUid"], fee["AMOUNT_ACCOUNT"], fee["ctgUid"], fee["ZCONTENT"]) == (
        "1", "a1", 1.5, "c-fun", "bank fee")
    # Tied to both legs, a millisecond after them, as the app does it.
    assert fee["txUidFee"] and fee["txUidFee"] == leg["txUidFee"] == mirror["txUidFee"]
    assert int(fee["ZDATE"]) == int(leg["ZDATE"]) + 1
    # Deleting the transfer takes the fee with it.
    client.delete(f"/api/app/transactions/{r.get_json()['uid']}", headers=client.csrf)
    assert get(client, "/api/app/days?from=2024-04-02&to=2024-04-02")["days"] == []


def test_a_fee_is_only_for_transfers(client):
    r = post(client, "/api/app/transactions", type="1", account="a1", category="c-fun", amount=1, date="2024-04-02",
             fee=1, fee_category="c-fun")
    assert r.status_code == 400 and "only for transfers" in r.get_json()["error"]


def test_add_move_hide_and_delete_accounts(client):
    r = post(client, "/api/app/accounts", name="Savings", group="g1", currency="USD")
    uid = r.get_json()["uid"]
    meta = get(client, "/api/app/meta")
    assert meta["groups"] == [{"uid": "g1", "name": "Cash"}]
    new = next(a for a in meta["accounts"] if a["uid"] == uid)
    assert (new["name"], new["currency"], new["group_uid"], new["status"]) == ("Savings", "cur-usd", "g1", "0")
    assert [a["uid"] for a in meta["accounts"]] == ["a1", "a2", uid]
    assert patch(client, f"/api/app/accounts/{uid}", move=-1).status_code == 200
    assert [a["uid"] for a in get(client, "/api/app/meta")["accounts"]] == ["a1", uid, "a2"]
    patch(client, f"/api/app/accounts/{uid}", name="Rainy day", status="3")
    new = next(a for a in get(client, "/api/app/meta")["accounts"] if a["uid"] == uid)
    assert (new["name"], new["hidden"]) == ("Rainy day", True)
    assert post(client, "/api/app/accounts", name=" ", group="g1").status_code == 400
    assert post(client, "/api/app/accounts", name="X", group="nope").status_code == 400


def test_add_move_and_delete_categories(client):
    r = post(client, "/api/app/categories/1", name="Travel")
    root = r.get_json()["uid"]
    child = post(client, "/api/app/categories/1", name="Trains", parent=root).get_json()["uid"]
    tree = get(client, "/api/app/meta")["categories"]["expense"]
    assert [c["name"] for c in tree] == ["Food", "Fun", "Travel"]
    assert tree[2]["children"] == [{"uid": child, "name": "Trains"}]
    assert post(client, "/api/app/categories/1", name="Food").status_code == 400  # a sibling has it
    patch(client, f"/api/app/categories/1/{root}", move=-1)
    assert [c["name"] for c in get(client, "/api/app/meta")["categories"]["expense"]] == ["Food", "Travel", "Fun"]
    # A root with children, or a category in use, stays.
    assert "subcategories" in client.delete(f"/api/app/categories/1/{root}", headers=client.csrf).get_json()["error"]
    assert "transactions" in client.delete("/api/app/categories/1/c-fun", headers=client.csrf).get_json()["error"]
    assert client.delete(f"/api/app/categories/1/{child}", headers=client.csrf).status_code == 200
    assert client.delete(f"/api/app/categories/1/{root}", headers=client.csrf).status_code == 200
    assert [c["name"] for c in get(client, "/api/app/meta")["categories"]["expense"]] == ["Food", "Fun"]


def test_bookmarks(client):
    r = post(client, "/api/app/bookmarks", type="1", account="a1", category="c-food-out", amount=4.5,
             currency="cur-eur", note="coffee", description="")
    uid = r.get_json()["uid"]
    (b,) = get(client, "/api/app/bookmarks")["bookmarks"]
    assert b == {"uid": uid, "type": "1", "account": "a1", "to_account": "", "category": "c-food-out", "amount": 4.5,
                 "currency": "cur-eur", "note": "coffee", "description": ""}
    assert client.delete(f"/api/app/bookmarks/{uid}", headers=client.csrf).status_code == 200
    assert get(client, "/api/app/bookmarks")["bookmarks"] == []
    assert post(client, "/api/app/bookmarks", type="1", category="c-salary").status_code == 400  # income tree


def test_networth(client):
    # Before Feb: t1 -10 (Wallet). In Feb: t2 -25.5 (Wallet), t3 +1000 USD = +900.
    d = get(client, "/api/app/networth?from=2024-02-01&to=2024-02-29")
    assert d["opening"] == -10.0
    assert d["days"] == [{"date": "2024-02-10", "change": -25.5}, {"date": "2024-02-11", "change": 900.0}]
