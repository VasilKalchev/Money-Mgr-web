"""merge.py: the three-way merge, duplicate detection and reference checks,
on small synthetic databases (see mmbak.py)."""
import shutil

import pytest

import merge
from mmbak import add_tx, execute, query


@pytest.fixture
def dbs(make_mmbak, tmp_path):
    """base, local and remote copies of the same export; edit local/remote,
    then call .plan()."""
    class Dbs:
        def plan(self, base=True):
            return merge.build_plan([merge.Snapshot(self.base)] if base else [],
                                    merge.Snapshot(self.local), merge.Snapshot(self.remote))

        def merged(self, p, decisions=None):
            res = p.resolve(decisions or {})
            out = str(tmp_path / f"merged{len(list(tmp_path.glob('merged*')))}.mmbak")
            p.write(res, out)
            return out

    d = Dbs()
    d.base = make_mmbak()
    d.local = str(tmp_path / "local.mmbak")
    d.remote = str(tmp_path / "remote.mmbak")
    shutil.copy(d.base, d.local)
    shutil.copy(d.base, d.remote)
    return d


def tx(path, uid, col="*"):
    rows = query(path, f"SELECT {col} FROM INOUTCOME WHERE uid = ?", (uid,))
    return rows[0] if rows else None


def uids(path):
    return {r["uid"] for r in query(path, "SELECT uid FROM INOUTCOME")}


# -- the merge ------------------------------------------------------------------

def test_identical_databases_need_nothing(dbs):
    p = dbs.plan()
    assert not p.needs_review() and not p.conflicts and not p.inserts
    assert p.matches_remote(p.resolve({}))


def test_app_additions_come_in_and_local_edits_stay(dbs):
    add_tx(dbs.remote, "new1", note="from the app")
    execute(dbs.local, "UPDATE INOUTCOME SET ZCONTENT = 'edited here' WHERE uid = 't1'")
    execute(dbs.local, "UPDATE ZCATEGORY SET NAME = 'Groceries' WHERE uid = 'c-food'")
    p = dbs.plan()
    assert not p.needs_review()
    assert merge.TX in p.changes and p.changes[merge.TX]["remote"]["added"] == [("new1",)]
    res = p.resolve({})
    assert not p.problems(res) and not p.matches_remote(res)
    out = dbs.merged(p)
    assert tx(out, "new1")["ZCONTENT"] == "from the app"
    assert tx(out, "t1")["ZCONTENT"] == "edited here"
    assert query(out, "SELECT NAME FROM ZCATEGORY WHERE uid = 'c-food'")[0]["NAME"] == "Groceries"


def test_only_app_changes_match_remote(dbs):
    add_tx(dbs.remote, "new1")
    execute(dbs.remote, "UPDATE ASSETS SET NIC_NAME = 'Purse' WHERE uid = 'a1'")
    p = dbs.plan()
    assert p.matches_remote(p.resolve({}))


def test_different_columns_of_one_row_merge(dbs):
    execute(dbs.local, "UPDATE INOUTCOME SET ZCONTENT = 'mine' WHERE uid = 't1'")
    execute(dbs.remote, "UPDATE INOUTCOME SET ctgUid = 'c-fun' WHERE uid = 't1'")
    p = dbs.plan()
    assert not p.conflicts
    row = tx(dbs.merged(p), "t1")
    assert (row["ZCONTENT"], row["ctgUid"]) == ("mine", "c-fun")


def test_same_column_changed_both_sides_is_a_conflict(dbs):
    execute(dbs.local, "UPDATE INOUTCOME SET ZCONTENT = 'mine', ZDATA = 'desc here' WHERE uid = 't1'")
    execute(dbs.remote, "UPDATE INOUTCOME SET ZCONTENT = 'theirs', ctgUid = 'c-fun' WHERE uid = 't1'")
    p = dbs.plan()
    assert p.needs_review()
    (c,) = p.conflicts
    assert (c.table, c.key, c.kind) == ("INOUTCOME", ("t1",), "edit")
    assert c.fields == [("ZCONTENT", "lunch", "mine", "theirs")]
    assert p.resolve({}).unresolved == [c]
    local = tx(dbs.merged(p, {c.id: "local"}), "t1")
    remote = tx(dbs.merged(p, {c.id: "remote"}), "t1")
    # the non-conflicting changes from both sides merge either way
    assert (local["ZCONTENT"], local["ZDATA"], local["ctgUid"]) == ("mine", "desc here", "c-fun")
    assert (remote["ZCONTENT"], remote["ZDATA"], remote["ctgUid"]) == ("theirs", "desc here", "c-fun")


def test_timestamps_keep_the_later_value(dbs):
    execute(dbs.local, "UPDATE INOUTCOME SET UTIME = 500, ZCONTENT = 'x' WHERE uid = 't1'")
    execute(dbs.remote, "UPDATE INOUTCOME SET UTIME = 900, ctgUid = 'c-fun' WHERE uid = 't1'")
    execute(dbs.local, "UPDATE INOUTCOME SET UTIME = 950 WHERE uid = 't2'")
    execute(dbs.remote, "UPDATE INOUTCOME SET UTIME = 700 WHERE uid = 't2'")
    p = dbs.plan()
    assert not p.conflicts
    out = dbs.merged(p)
    assert tx(out, "t1")["UTIME"] == 900 and tx(out, "t2")["UTIME"] == 950


def test_app_deleting_an_untouched_row_deletes_it(dbs):
    execute(dbs.remote, "DELETE FROM INOUTCOME WHERE uid = 't2'")
    p = dbs.plan()
    assert not p.needs_review() and p.changes[merge.TX]["remote"]["deleted"] == [("t2",)]
    assert tx(dbs.merged(p), "t2") is None


def test_app_deleting_a_row_edited_here_is_a_conflict(dbs):
    execute(dbs.remote, "DELETE FROM INOUTCOME WHERE uid = 't2'")
    execute(dbs.local, "UPDATE INOUTCOME SET ZCONTENT = 'keep me' WHERE uid = 't2'")
    p = dbs.plan()
    (c,) = p.conflicts
    assert c.kind == "edit_delete" and c.fields == [("ZCONTENT", "dinner", "keep me", None)]
    # Both sides' changes are listed, conflicting or not.
    assert p.changes[merge.TX]["remote"]["deleted"] == p.changes[merge.TX]["local"]["changed"] == [("t2",)]
    assert tx(dbs.merged(p, {c.id: "local"}), "t2")["ZCONTENT"] == "keep me"
    assert tx(dbs.merged(p, {c.id: "remote"}), "t2") is None


def test_row_deleted_here_but_edited_in_app_is_a_conflict(dbs):
    execute(dbs.local, "DELETE FROM INOUTCOME WHERE uid = 't2'")
    execute(dbs.remote, "UPDATE INOUTCOME SET ZCONTENT = 'changed in app' WHERE uid = 't2'")
    p = dbs.plan()
    (c,) = p.conflicts
    assert c.kind == "delete_edit"
    assert p.changes[merge.TX]["remote"]["changed"] == p.changes[merge.TX]["local"]["deleted"] == [("t2",)]
    assert tx(dbs.merged(p, {c.id: "local"}), "t2") is None
    assert tx(dbs.merged(p, {c.id: "remote"}), "t2")["ZCONTENT"] == "changed in app"


def test_row_deleted_here_and_untouched_in_app_stays_deleted(dbs):
    execute(dbs.local, "DELETE FROM INOUTCOME WHERE uid = 't2'")
    p = dbs.plan()
    assert not p.needs_review() and tx(dbs.merged(p), "t2") is None


def test_category_rows_are_keyed_by_uid_and_tree(dbs):
    # one uid in both trees (docs/MM_DB_SCHEMA.md rule 3)
    for path in (dbs.base, dbs.local, dbs.remote):
        execute(path, "INSERT INTO ZCATEGORY (C_IS_DEL, NAME, ORDERSEQ, TYPE, STATUS, uid) VALUES (0, 'Twin', 9, 0, 0, 'c-fun')")
    execute(dbs.local, "UPDATE ZCATEGORY SET NAME = 'Games' WHERE uid = 'c-fun' AND TYPE = 1")
    execute(dbs.remote, "UPDATE ZCATEGORY SET NAME = 'Gifts' WHERE uid = 'c-fun' AND TYPE = 0")
    p = dbs.plan()
    assert not p.conflicts
    names = {r["TYPE"]: r["NAME"] for r in query(dbs.merged(p), "SELECT TYPE, NAME FROM ZCATEGORY WHERE uid = 'c-fun'")}
    assert names == {0: "Gifts", 1: "Games"}


def test_primary_keys_kept_when_free(dbs):
    add_tx(dbs.remote, "app1")  # gets AID 7 in the app
    app_aid = tx(dbs.remote, "app1")["AID"]
    out = dbs.merged(dbs.plan())
    assert tx(out, "app1")["AID"] == app_aid
    add_tx(dbs.local, "mine", wdate="2024-08-01", amount=1.0)  # takes AID 7 here first
    out = dbs.merged(dbs.plan())
    assert tx(out, "mine")["AID"] == app_aid and tx(out, "app1")["AID"] != app_aid


def test_unkeyed_table_taken_whole_from_the_app(dbs):
    for path in (dbs.base, dbs.local, dbs.remote):
        execute(path, "CREATE TABLE NOTES (txt TEXT)")
        execute(path, "INSERT INTO NOTES VALUES ('a')")
    execute(dbs.remote, "INSERT INTO NOTES VALUES ('b')")
    p = dbs.plan()
    assert "NOTES" in p.replace and not p.conflicts
    assert sorted(r["txt"] for r in query(dbs.merged(p), "SELECT txt FROM NOTES")) == ["a", "b"]
    execute(dbs.local, "INSERT INTO NOTES VALUES ('c')")
    (c,) = dbs.plan().conflicts
    assert c.kind == "table"


# -- duplicates -----------------------------------------------------------------

def test_same_purchase_entered_on_both_sides_is_a_duplicate(dbs):
    add_tx(dbs.local, "here", amount=3.5, note="Coffee", zdate=1711929600000)
    add_tx(dbs.remote, "app", amount=3.5, note="coffee ", zdate=1711929600000 + 5 * 60 * 1000)
    p = dbs.plan()
    (d,) = p.duplicates
    assert (d.local, d.remote) == ([("here",)], [("app",)]) and d.score >= merge.DUPLICATE_THRESHOLD
    assert p.needs_review()
    assert uids(dbs.merged(p, {d.id: "remote"})) >= {"app"} and "here" not in uids(dbs.merged(p, {d.id: "remote"}))
    assert "app" not in uids(dbs.merged(p, {d.id: "local"})) and "here" in uids(dbs.merged(p, {d.id: "local"}))
    assert {"here", "app"} <= uids(dbs.merged(p, {d.id: "both"}))
    assert {"here", "app"} & uids(dbs.merged(p)) == {"app"}  # keeping the app's is the default


@pytest.mark.parametrize("app_side", [
    {"wdate": "2024-04-02", "amount": 3.45},             # a day later, amount mistyped
    {"amount": 3.5, "asset": "a2", "note": "coffee"},    # same, but picked another account
])
def test_near_misses_still_flagged(dbs, app_side):
    add_tx(dbs.local, "here", amount=3.5, note="coffee")
    add_tx(dbs.remote, "app", **{"note": "coffee", **app_side})
    assert len(dbs.plan().duplicates) == 1


@pytest.mark.parametrize("app_side", [
    {"wdate": "2024-04-09"},                              # a week apart: a recurring payment
    {"amount": 40.0, "ctg": "c-fun", "note": "cinema"},   # a different purchase that day
    {"do_type": "0", "ctg": "c-salary"},                  # income, not an expense
])
def test_different_transactions_are_not_flagged(dbs, app_side):
    add_tx(dbs.local, "here", amount=3.5, note="coffee")
    add_tx(dbs.remote, "app", **{"amount": 3.5, "note": "coffee", **app_side})
    p = dbs.plan()
    assert not p.duplicates and not p.needs_review()
    assert {"here", "app"} <= uids(dbs.merged(p))


def test_rows_already_in_the_base_are_never_duplicates(dbs):
    add_tx(dbs.remote, "app", wdate="2024-01-05", amount=10.0, note="lunch", ctg="c-food")  # same as t1
    assert not dbs.plan().duplicates


def test_each_transaction_pairs_at_most_once(dbs):
    add_tx(dbs.local, "here1", amount=3.5, note="coffee")
    add_tx(dbs.local, "here2", amount=3.5, note="coffee")
    add_tx(dbs.remote, "app1", amount=3.5, note="coffee")
    (d,) = dbs.plan().duplicates
    assert d.remote == [("app1",)]


def add_transfer(path, prefix, amount=50.0, fee=None):
    add_tx(path, f"{prefix}-out", do_type="3", amount=amount, asset="a1", toAssetUid="a2", ctg="",
           txUidTrans=f"{prefix}-pair", txUidFee=f"{prefix}-fee" if fee else "")
    add_tx(path, f"{prefix}-in", do_type="4", amount=amount, asset="a2", toAssetUid="a1", ctg="",
           txUidTrans=f"{prefix}-pair", txUidFee=f"{prefix}-fee" if fee else "")
    if fee:
        add_tx(path, f"{prefix}-fee-row", amount=fee, asset="a1", ctg="c-fun", txUidFee=f"{prefix}-fee")


def test_transfer_legs_and_fee_are_one_duplicate(dbs):
    add_transfer(dbs.local, "here", fee=1.0)
    add_transfer(dbs.remote, "app", fee=1.0)
    p = dbs.plan()
    (d,) = p.duplicates
    assert len(d.local) == 3 and len(d.remote) == 3
    out = dbs.merged(p, {d.id: "remote"})
    assert not {"here-out", "here-in", "here-fee-row"} & uids(out)
    assert {"app-out", "app-in", "app-fee-row"} <= uids(out)
    assert not p.problems(p.resolve({d.id: "remote"}))


def test_transfer_does_not_match_an_expense(dbs):
    add_transfer(dbs.local, "here")
    add_tx(dbs.remote, "app", amount=50.0, ctg="")
    assert not dbs.plan().duplicates


# -- problems -------------------------------------------------------------------

def test_new_transaction_in_a_category_the_app_deleted(dbs):
    execute(dbs.remote, "UPDATE INOUTCOME SET ctgUid = 'c-food' WHERE ctgUid = 'c-fun'")
    execute(dbs.remote, "DELETE FROM ZCATEGORY WHERE uid = 'c-fun'")
    add_tx(dbs.local, "mine", ctg="c-fun")
    p = dbs.plan()
    res = p.resolve({})
    assert p.problems(res) == [("category", "INOUTCOME", ("mine",), "c-fun")]
    # fixed here: the transaction moves to another category
    execute(dbs.local, "UPDATE INOUTCOME SET ctgUid = 'c-food' WHERE uid = 'mine'")
    p = dbs.plan()
    assert not p.problems(p.resolve({}))


def test_app_transaction_in_an_account_deleted_here(dbs):
    execute(dbs.local, "DELETE FROM ASSETS WHERE uid = 'a2'")
    execute(dbs.local, "UPDATE INOUTCOME SET assetUid = 'a1' WHERE assetUid = 'a2'")
    add_tx(dbs.remote, "app", asset="a2")
    p = dbs.plan()
    assert ("account", "INOUTCOME", ("app",), "a2") in p.problems(p.resolve({}))


def test_transfer_left_with_one_leg(dbs):
    add_transfer(dbs.base, "tr")
    add_transfer(dbs.local, "tr")
    add_transfer(dbs.remote, "tr")
    execute(dbs.remote, "DELETE FROM INOUTCOME WHERE uid = 'tr-in'")
    execute(dbs.remote, "DELETE FROM INOUTCOME WHERE uid = 'tr-out'")
    execute(dbs.local, "UPDATE INOUTCOME SET ZCONTENT = 'edited' WHERE uid = 'tr-out'")
    p = dbs.plan()
    (c,) = p.conflicts  # tr-out: edited here, deleted in the app; tr-in just goes
    assert p.problems(p.resolve({c.id: "local"})) == [("transfer", "INOUTCOME", ("tr-pair",), ("3",))]
    assert not p.problems(p.resolve({c.id: "remote"}))


def test_broken_references_both_sides_already_had_are_ignored(dbs):
    # t6 is a transfer mirror leg without its outgoing leg, in every copy
    add_tx(dbs.remote, "app")
    p = dbs.plan()
    assert not p.problems(p.resolve({}))


def test_schema_mismatch_blocks_the_merge(dbs, make_mmbak):
    execute(dbs.remote, "ALTER TABLE INOUTCOME ADD COLUMN newcol TEXT")
    p = dbs.plan()
    assert p.needs_review() and any("INOUTCOME" in s for s in p.schema_problems)
    assert not p.conflicts and not p.inserts


# -- base selection and two-way --------------------------------------------------

def test_two_way_without_base(dbs):
    add_tx(dbs.remote, "app")
    add_tx(dbs.local, "mine", wdate="2024-06-01", amount=99.0)
    execute(dbs.local, "UPDATE INOUTCOME SET ZCONTENT = 'mine' WHERE uid = 't1'")
    p = dbs.plan(base=False)
    assert p.two_way
    (c,) = p.conflicts  # without a base every difference is a conflict
    assert c.key == ("t1",) and c.fields == [("ZCONTENT", None, "mine", "lunch")]
    out = dbs.merged(p, {c.id: "local"})
    assert {"app", "mine"} <= uids(out) and tx(out, "t1")["ZCONTENT"] == "mine"


def test_choose_base_picks_what_the_app_restored(dbs, tmp_path):
    """After a push: if the app restored the pushed copy, its next backup
    descends from that; otherwise from the old base."""
    execute(dbs.local, "UPDATE INOUTCOME SET ZCONTENT = 'pushed edit' WHERE uid = 't1'")
    pushed = str(tmp_path / "pushed.mmbak")
    shutil.copy(dbs.local, pushed)
    base, pushed_snap = merge.Snapshot(dbs.base), merge.Snapshot(pushed)

    restored = str(tmp_path / "restored.mmbak")
    shutil.copy(pushed, restored)
    add_tx(restored, "app")
    assert merge.choose_base([base, pushed_snap], merge.Snapshot(restored)) is pushed_snap

    not_restored = str(tmp_path / "not_restored.mmbak")
    shutil.copy(dbs.base, not_restored)
    add_tx(not_restored, "app")
    assert merge.choose_base([base, pushed_snap], merge.Snapshot(not_restored)) is base

    # a tie goes to the base, which can't lose local changes
    assert merge.choose_base([base, merge.Snapshot(dbs.base)], merge.Snapshot(not_restored)) is base


def test_not_restored_push_keeps_local_changes(dbs, tmp_path):
    """The dangerous case: the pushed copy was never restored in the app.
    Merging on the wrong base would read the app's backup as reverting
    the pushed edits."""
    execute(dbs.local, "UPDATE INOUTCOME SET ZCONTENT = 'pushed edit' WHERE uid = 't1'")
    add_tx(dbs.local, "mine", wdate="2024-06-01")
    pushed = str(tmp_path / "pushed.mmbak")
    shutil.copy(dbs.local, pushed)
    add_tx(dbs.remote, "app")
    p = merge.build_plan([merge.Snapshot(dbs.base), merge.Snapshot(pushed)],
                         merge.Snapshot(dbs.local), merge.Snapshot(dbs.remote))
    out = dbs.merged(p)
    assert tx(out, "t1")["ZCONTENT"] == "pushed edit" and {"mine", "app"} <= uids(out)


def test_distance(dbs):
    a = merge.Snapshot(dbs.base)
    execute(dbs.remote, "UPDATE INOUTCOME SET ZCONTENT = 'x', ZDATA = 'y' WHERE uid = 't1'")
    add_tx(dbs.remote, "new")
    assert merge.distance(a, merge.Snapshot(dbs.remote)) == 3
    execute(dbs.local, "ALTER TABLE ASSETS ADD COLUMN extra TEXT")
    assert merge.distance(a, merge.Snapshot(dbs.local)) is None
