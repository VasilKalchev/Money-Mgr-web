"""reads.py on a plain connection to the synthetic export in mmbak.py."""
import sqlite3
import subprocess
import sys

import pytest
from werkzeug.datastructures import MultiDict

import dbstore
import reads


@pytest.fixture
def con(mmbak_file):
    con = sqlite3.connect(f"file:{mmbak_file}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # An empty change log, as app.get_db() attaches the user's.
    con.execute("ATTACH DATABASE ':memory:' AS mmw")
    con.execute(f"CREATE TABLE mmw.{dbstore.CHANGES_TABLE} (uid TEXT PRIMARY KEY, changed INTEGER NOT NULL)")
    yield con
    con.close()


def filters(con, *pairs):
    return reads.parse_transaction_filters(con, MultiDict(pairs))


def test_data_modules_dont_import_flask():
    out = subprocess.run([sys.executable, "-c", "import sys, edits, reads; print('flask' in sys.modules)"],
                         capture_output=True, text=True, check=True, cwd=reads.__file__.rsplit("/", 1)[0])
    assert out.stdout.strip() == "False"


def test_build_where_exclude(con):
    f = filters(con, ("account", "a1"), ("show_deleted", "only"))
    where, params = f["build_where"]()
    assert "i.IS_DEL <> 0" in where and "assetUid" in where and params == ["a1"]
    where, params = f["build_where"](exclude="show_deleted")
    assert "IS_DEL" not in where and "assetUid" in where
    where, params = f["build_where"](exclude="account")
    assert "assetUid" not in where and params == []


def test_empty_where_is_valid_sql(con):
    f = filters(con, ("show_deleted", "show"), ("show_mirror", "show"))
    assert f["build_where"]() == ("1=1", [])


def test_text_search_clause():
    clause, param = reads._text_search_clause("c", "a%b_c\\d")
    assert "LIKE" in clause and "NOT" not in clause
    assert param == "%a\\%b\\_c\\\\d%"
    clause, param = reads._text_search_clause("c", "!foo")
    assert "NOT LIKE" in clause and param == "%foo%"


def test_category_tree(con):
    by_uid = {r["uid"]: r for r in reads.build_category_tree(con, 1)}
    assert [c["uid"] for c in by_uid["c-food"]["children"]] == ["c-food-out"]
    assert by_uid["c-fun"]["children"] == []
    assert "c-food-out" not in by_uid  # children aren't roots
    assert [r["uid"] for r in reads.build_category_tree(con, 0)] == ["c-salary"]


def test_category_path(con):
    lookups = reads.category_lookups(con)
    assert reads.category_path(lookups[1], "c-food-out") == ("c-food", "Food > Eating out")
    assert reads.category_path(lookups[1], "c-food") == ("c-food", "Food")
    assert reads.category_path(lookups[0], "c-food") == ("", "")  # not in the income tree


def test_facets_leave_out_their_own_filter(con):
    facets = reads.transaction_facets(con, filters(con))
    assert facets["deleted"] == 1 and facets["mirror"] == 1  # t5, t6
    assert facets["note"] == {"yes": 3, "no": 1}
    assert facets["description"] == {"yes": 2, "no": 2}
    assert facets["account"] == {"a1": 2, "a2": 2}
    assert facets["type"] == {"0": 1, "1": 3}
    assert facets["category_own"] == {"c-food": 1, "c-food-out": 1, "c-salary": 1, "c-fun": 1}
    assert facets["category"]["c-food"] == 2  # a root counts its children's rows
    # The account filter narrows every count but the account facet's own.
    facets = reads.transaction_facets(con, filters(con, ("account", "a1")))
    assert facets["account"] == {"a1": 2, "a2": 2}
    assert facets["type"] == {"1": 2}


def test_list_and_count_transactions(con):
    f = filters(con)
    assert reads.count_transactions(con, f) == 4
    rows = reads.list_transactions(con, f, "amount", "asc", limit=2, offset=1)
    assert [r["uid"] for r in rows] == ["t1", "t2"]  # 7.0 (t4) skipped, then 10.0, 25.5
    assert rows[0]["account_name"] == "Wallet" and rows[0]["account_currency_iso"] == "EUR"
    # UTIME is the row's index in mmbak.TRANSACTIONS.
    assert reads.count_transactions(con, f, updated_since=3) == 1
    assert [r["uid"] for r in reads.list_transactions(con, f, updated_since=3)] == ["t4"]


@pytest.mark.parametrize("sort, direction", [("bogus", "asc"), ("date", "sideways")])
def test_list_transactions_refuses_unknown_order(con, sort, direction):
    with pytest.raises(ValueError):
        reads.list_transactions(con, filters(con), sort, direction)


def test_get_transactions(con):
    assert reads.get_transaction(con, "t3")["DO_TYPE"] == "0"
    assert reads.get_transaction(con, "nope") is None
    assert sorted(reads.get_transactions(con, ["t1", "t6", "nope"])) == ["t1", "t6"]


def test_suggestions_most_recent_first(con):
    # t5's note is left out: it's deleted.
    assert reads.get_suggestions(con, "note") == ["mirror", "50% off_sale", "dinner", "lunch"]
    assert reads.get_suggestions(con, "description", limit=1) == ["pay"]
    with pytest.raises(KeyError):
        reads.get_suggestions(con, "ZCONTENT")  # fields, not columns
