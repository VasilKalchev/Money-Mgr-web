"""Syncing end to end (dbsync.py and its routes): merging uploaded exports,
the review page, downloads, and Google Drive against a fake Drive."""
import io
import os
import re
import shutil

import pytest

import dbstore
import dbsync
import gdrive
from fakedrive import FakeDrive
from mmbak import add_tx, execute, query


@pytest.fixture
def store(client):
    return dbstore.store_for("alice")


@pytest.fixture
def export(mmbak_file, tmp_path):
    """A fresh copy of the export the client fixture installed, to edit
    into "what the app has now"."""
    counter = iter(range(100))

    def make():
        path = str(tmp_path / f"app{next(counter)}.mmbak")
        shutil.copy(mmbak_file, path)
        return path

    return make


def patch(client, uid, field, value):
    r = client.patch(f"/api/transactions/{uid}", json={"field": field, "value": value}, headers=client.csrf)
    assert r.status_code == 200


def add_here(client, **over):
    body = {"type": "1", "account": "a1", "amount": "12.5", "date": "2024-04-01", "time": "10:00",
            "category": "c-fun", "note": "cinema"}
    body.update(over)
    r = client.post("/api/transactions", json=body, headers=client.csrf)
    assert r.status_code == 200
    return r.get_json()["uid"]


def upload(client, path, name="MM_new.mmbak"):
    with open(path, "rb") as f:
        data = f.read()
    return client.post("/db/sync/upload", data={"db_file": (io.BytesIO(data), name), "csrf_token": "t"},
                       content_type="multipart/form-data")


def local(store, uid, col="*"):
    rows = query(store.db_path, f"SELECT {col} FROM INOUTCOME WHERE uid = ?", (uid,))
    return rows[0] if rows else None


def flashes(client):
    """The messages flashed since the last call (consumed, like a page would)."""
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.pop("_flashes", []))


def review_form(client):
    """The review page's hidden fields and the ids of its choices."""
    html = client.get("/sync").get_data(as_text=True)
    fp = re.search(r'name="fingerprint" value="([^"]+)"', html).group(1)
    ids = sorted(set(re.findall(r'name="choice_([cd][0-9a-f]+)"', html)))
    return html, fp, ids


def apply(client, fp, **choices):
    data = {"csrf_token": "t", "fingerprint": fp, **{f"choice_{k}": v for k, v in choices.items()}}
    return client.post("/sync/apply", data=data)


def accept(client):
    """Apply a pending merge that has nothing to decide, as listed."""
    _, fp, ids = review_form(client)
    assert ids == []
    r = apply(client, fp)
    assert r.status_code == 302
    return r


# -- merging an uploaded export ---------------------------------------------------

def test_clean_merge_shows_its_changes_first(client, store, export):
    patch(client, "t1", "ZCONTENT", "edited here")
    app = export()
    add_tx(app, "app1", note="from the app")
    r = upload(client, app)
    assert r.location.endswith("/sync") and flashes(client) == ""
    assert local(store, "app1") is None  # nothing applied yet
    html, _, _ = review_form(client)
    assert "Nothing conflicts" in html and "From the app: 1 new transaction" in html
    assert "Changed here: 1 changed transaction" in html and "edited here" in html
    r = accept(client)
    assert r.location.endswith("/settings")
    assert "Merged MM_new.mmbak: 1 new transaction from the app" in flashes(client)
    assert local(store, "t1")["ZCONTENT"] == "edited here"
    assert local(store, "app1")["ZCONTENT"] == "from the app"
    assert dbstore.file_md5(store.base_path) == dbstore.file_md5(app)  # the app's file is the new base
    assert os.path.isfile(os.path.join(store.backup_dir, store.latest_backup(), dbstore.INCOMING_NAME))
    assert store.unsynced() and store.load_pending() is None
    assert store.db_info()["name"] == "MM_new.mmbak"


def test_without_local_changes_the_file_is_installed_as_is(client, store, export):
    app = export()
    add_tx(app, "app1")
    upload(client, app)
    accept(client)
    assert "Updated to MM_new.mmbak" in flashes(client)
    assert dbstore.file_md5(store.db_path) == dbstore.file_md5(app)
    assert not store.unsynced()


def test_nothing_changed_in_the_app_applies_right_away(client, store, export):
    """A backup whose file differs but whose rows don't has nothing to show."""
    patch(client, "t1", "ZCONTENT", "edited here")
    app = export()
    add_tx(app, "gone")
    execute(app, "DELETE FROM INOUTCOME WHERE uid = 'gone'")
    assert dbstore.file_md5(app) not in {dbstore.file_md5(p) for p in store.base_candidates()}
    r = upload(client, app)
    assert r.location.endswith("/settings") and "no changes from the app" in flashes(client)
    assert local(store, "t1")["ZCONTENT"] == "edited here" and store.load_pending() is None
    assert dbstore.file_md5(store.base_path) == dbstore.file_md5(app)


def test_review_lists_what_each_side_changed(client, store, export):
    patch(client, "t1", "ZCONTENT", "edited here")
    execute(store.db_path, "UPDATE ZCATEGORY SET NAME = 'Fun stuff' WHERE uid = 'c-fun'")
    app = export()
    add_tx(app, "app1", note="from the app")
    add_tx(app, "tr-out", do_type="3", asset="a1", toAssetUid="a2", ctg="", txUidTrans="x1")
    add_tx(app, "tr-in", do_type="4", asset="a2", toAssetUid="a1", ctg="", txUidTrans="x1")
    execute(app, "UPDATE INOUTCOME SET AMOUNT_ACCOUNT = 30, IN_ZMONEY = '30', UTIME = 500 WHERE uid = 't2'")
    execute(app, "UPDATE INOUTCOME SET UTIME = 500, ZMONEY = '2' WHERE uid = 't3'")  # bookkeeping only
    execute(app, "UPDATE INOUTCOME SET IS_DEL = 1 WHERE uid = 't4'")
    execute(app, "UPDATE ASSETS SET NIC_NAME = 'Purse' WHERE uid = 'a1'")
    upload(client, app)
    v = dbsync.review(store)
    remote, here = v["changes"]["remote"], v["changes"]["local"]
    txs = {t["uid"]: t for t in remote["transactions"]}
    assert set(txs) == {"app1", "tr-out", "t2", "t4"}  # the transfer once, t3 only counted
    assert [t["what"] for t in remote["transactions"]] == ["added", "added", "changed", "deleted"]
    assert txs["tr-out"]["account"] == "Purse → Bank"
    assert txs["t2"]["was"] == {"amount": "25.50 EUR"} and txs["t2"]["amount"] == "30.00"
    assert txs["t2"]["other"] == [("Entered amount", "25.5", "30")]
    assert remote["quiet"] == 1
    assert remote["rows"] == [{"table": "ASSETS", "what": "changed", "word": "changed", "title": "Account “Purse”",
                               "fields": [("Name", "Wallet", "Purse")], "mark": None}]
    # Counted as listed: the transfer once, the soft delete as a deletion.
    assert remote["summary"] == ("2 new transactions, 2 changed transactions, 1 deleted transaction, "
                                 "1 changed account")
    assert [(t["uid"], t["was"]) for t in here["transactions"]] == [("t1", {"note": "lunch"})]
    assert here["rows"][0]["fields"] == [("Name", "Fun", "Fun stuff")]
    html = client.get("/sync").get_data(as_text=True)
    assert '<span class="was">25.50 EUR</span>30.00 EUR' in html
    assert "Name: <span class=\"was-inline\">Wallet</span> → Purse" in html
    assert "1 more row changed only in bookkeeping fields" in html
    accept(client)
    assert local(store, "t2")["AMOUNT_ACCOUNT"] == 30 and local(store, "t4")["IS_DEL"] == 1


def test_changes_point_at_conflicts_and_duplicates(client, store, export):
    patch(client, "t1", "ZCONTENT", "mine")
    add_here(client)
    app = export()
    execute(app, "UPDATE INOUTCOME SET ZCONTENT = 'theirs' WHERE uid = 't1'")
    add_tx(app, "app1", amount=12.5, asset="a1", ctg="c-fun", note="Cinema")
    upload(client, app)
    v = dbsync.review(store)
    marks = {(side, t["uid"]): t["mark"] for side in ("remote", "local") for t in v["changes"][side]["transactions"]}
    assert marks[("remote", "t1")] == marks[("local", "t1")] == "conflict"
    assert marks[("remote", "app1")] == "duplicate"
    html = client.get("/sync").get_data(as_text=True)
    assert "see Conflicts" in html and "see Possible duplicates" in html
    assert "Nothing conflicts" not in html


def test_nothing_new(client, store, mmbak_file):
    upload(client, mmbak_file)
    assert "Already up to date" in flashes(client)
    patch(client, "t1", "ZCONTENT", "edited here")
    upload(client, mmbak_file)
    assert "nothing new" in flashes(client)
    assert local(store, "t1")["ZCONTENT"] == "edited here"


def test_unsupported_upload_is_refused(client, store, make_mmbak):
    before = dbstore.file_md5(store.db_path)
    upload(client, make_mmbak(18))
    assert "Sync failed" in flashes(client)
    assert dbstore.file_md5(store.db_path) == before and store.load_pending() is None
    assert [f for f in os.listdir(store.db_dir) if f.endswith(".part")] == []


def test_first_upload_installs(data_dir, mmbak_file):
    import users
    from app import app
    users.create("bob", "correct horse")
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(user="bob", ver=users.get("bob")["session_version"], csrf="t")
    upload(c, mmbak_file)
    assert dbstore.store_for("bob").db_exists()


# -- review -----------------------------------------------------------------------

def test_duplicate_is_reviewed(client, store, export):
    mine = add_here(client)
    app = export()
    add_tx(app, "app1", amount=12.5, asset="a1", ctg="c-fun", note="Cinema")
    r = upload(client, app)
    assert r.location.endswith("/sync") and "needs your review" in flashes(client)
    assert local(store, "app1") is None  # nothing applied yet
    html, fp, ids = review_form(client)
    assert "Possible duplicates (1)" in html and "Cinema" in html
    assert "A sync is waiting" not in html  # the banner shows on other pages only
    assert "A sync is waiting" in client.get("/transactions").get_data(as_text=True)
    r = apply(client, fp, **{ids[0]: "local"})
    assert r.status_code == 302 and r.location.endswith("/settings")
    assert local(store, mine) is not None and local(store, "app1") is None
    assert store.load_pending() is None


def test_conflict_needs_a_choice(client, store, export):
    patch(client, "t1", "ZCONTENT", "mine")
    app = export()
    execute(app, "UPDATE INOUTCOME SET ZCONTENT = 'theirs' WHERE uid = 't1'")
    upload(client, app)
    html, fp, ids = review_form(client)
    assert "Conflicts (1)" in html and "mine" in html and "theirs" in html
    r = apply(client, fp)
    assert r.status_code == 409 and "for every conflict" in r.get_data(as_text=True)
    assert local(store, "t1")["ZCONTENT"] == "mine"
    apply(client, fp, **{ids[0]: "remote"})
    assert local(store, "t1")["ZCONTENT"] == "theirs"


def test_review_submitted_after_the_db_changed_is_refused(client, store, export):
    patch(client, "t1", "ZCONTENT", "mine")
    app = export()
    execute(app, "UPDATE INOUTCOME SET ZCONTENT = 'theirs' WHERE uid = 't1'")
    upload(client, app)
    _, fp, ids = review_form(client)
    patch(client, "t2", "ZCONTENT", "meanwhile")
    r = apply(client, fp, **{ids[0]: "remote"})
    assert r.location.endswith("/sync") and "changed while you were reviewing" in flashes(client)
    assert local(store, "t1")["ZCONTENT"] == "mine" and store.load_pending() is not None


def test_problem_blocks_until_fixed(client, store, export):
    mine = add_here(client, category="c-fun")
    app = export()
    execute(app, "UPDATE INOUTCOME SET ctgUid = 'c-food' WHERE ctgUid = 'c-fun'")
    execute(app, "DELETE FROM ZCATEGORY WHERE uid = 'c-fun'")
    upload(client, app)
    html, fp, _ = review_form(client)
    assert "Fix these first (1)" in html and "which was deleted in the app" in html
    assert re.search(r'<button type="submit" class="btn-primary"\s+disabled', html)
    r = apply(client, fp)
    assert r.status_code == 409 and "Fix the problems" in r.get_data(as_text=True)
    patch(client, mine, "ctgUid", "c-food")  # fixed here
    html, fp, _ = review_form(client)
    assert "Fix these first" not in html
    apply(client, fp)
    assert local(store, mine)["ctgUid"] == "c-food" and store.load_pending() is None
    assert not query(store.db_path, "SELECT 1 FROM ZCATEGORY WHERE uid = 'c-fun'")


def test_cancel_and_replace(client, store, export):
    patch(client, "t1", "ZCONTENT", "mine")
    app = export()
    execute(app, "UPDATE INOUTCOME SET ZCONTENT = 'theirs' WHERE uid = 't1'")
    upload(client, app)
    client.post("/sync/cancel", data={"csrf_token": "t"})
    assert store.load_pending() is None and local(store, "t1")["ZCONTENT"] == "mine"
    assert client.get("/sync").location.endswith("/settings")
    upload(client, app)
    backups = store.backup_count()
    client.post("/sync/replace", data={"csrf_token": "t"})
    assert dbstore.file_md5(store.db_path) == dbstore.file_md5(app)
    assert store.backup_count() == backups + 1 and store.load_pending() is None


def test_replacing_upload_drops_a_pending_sync(client, store, export, make_mmbak):
    patch(client, "t1", "ZCONTENT", "mine")
    app = export()
    execute(app, "UPDATE INOUTCOME SET ZCONTENT = 'theirs' WHERE uid = 't1'")
    upload(client, app)
    with open(make_mmbak(), "rb") as f:
        client.post("/db/upload", data={"db_file": (io.BytesIO(f.read()), "MM_x.mmbak"), "csrf_token": "t"},
                    content_type="multipart/form-data")
    assert store.load_pending() is None


# -- downloads and the bases they leave ---------------------------------------------

def test_download(client, store):
    store.update_db_info(name="MMGF(1-2-26-101010).mmbak")
    patch(client, "t1", "ZCONTENT", "edited here")
    r = client.get("/db/download")
    assert r.status_code == 200
    name = re.search(r'filename="?([^";]+)', r.headers["Content-Disposition"]).group(1)
    assert re.fullmatch(r"MMGF\(\d+-\d+-\d\d-\d{6}\)\.mmbak", name) and name != "MMGF(1-2-26-101010).mmbak"
    r.close()
    assert len(store.base_candidates()) == 2  # the base and the downloaded state
    assert not store.unsynced()
    assert [f for f in os.listdir(store.db_dir) if f.endswith(".part")] == []


def test_backup_restored_from_a_download_merges_cleanly(client, store, tmp_path):
    """Download, restore it in the app, keep working on both sides: the
    app's next backup descends from the download, so edits made here
    before it don't conflict with it."""
    patch(client, "t1", "ZCONTENT", "first edit")
    r = client.get("/db/download")
    restored = str(tmp_path / "restored.mmbak")
    with open(restored, "wb") as f:
        f.write(r.get_data())
    r.close()
    add_tx(restored, "app1")  # the app carries on from the download
    patch(client, "t1", "ZCONTENT", "second edit")  # and so does this side
    upload(client, restored)
    accept(client)
    assert "Merged" in flashes(client)
    assert local(store, "t1")["ZCONTENT"] == "second edit" and local(store, "app1") is not None


def test_backup_not_restored_after_a_download_keeps_local_changes(client, store, export):
    patch(client, "t1", "ZCONTENT", "edited here")
    client.get("/db/download").close()
    app = export()  # the app never restored the download
    add_tx(app, "app1")
    upload(client, app)
    accept(client)
    assert local(store, "t1")["ZCONTENT"] == "edited here" and local(store, "app1") is not None


# -- installs from before sync existed -----------------------------------------------

def forget_base(store):
    shutil.rmtree(store.sync_dir)


def test_unedited_legacy_install_uses_itself_as_base(client, store, export):
    forget_base(store)
    app = export()
    add_tx(app, "app1")
    upload(client, app)
    accept(client)
    assert "Updated to" in flashes(client)
    assert dbstore.file_md5(store.db_path) == dbstore.file_md5(app)


def test_edited_legacy_install_merges_two_way(client, store, export):
    forget_base(store)
    patch(client, "t1", "ZCONTENT", "edited here")
    app = export()
    add_tx(app, "app1")
    upload(client, app)
    html, fp, ids = review_form(client)
    assert "no snapshot from an earlier sync" in html and len(ids) == 1
    apply(client, fp, **{ids[0]: "local"})
    assert local(store, "t1")["ZCONTENT"] == "edited here" and local(store, "app1") is not None


# -- Google Drive ---------------------------------------------------------------------

DRIVE_NAME = "MMGF(1-1-26-101010).mmbak"


@pytest.fixture
def drive(client, store, monkeypatch, mmbak_file):
    """alice connected to Drive with write access, her db installed from
    the folder's only backup (f1)."""
    store.save_config({"gdrive": {"client_id": "cid", "client_secret": "s", "refresh_token": "rt",
                                  "scope": gdrive.SCOPE_WRITE, "write": True}})
    fake = FakeDrive(monkeypatch, store)
    fake.add("f1", DRIVE_NAME, mmbak_file)
    dbsync.sync_gdrive(store, replace=True)
    fake.uploads.clear()
    return fake


def sync(client, **form):
    return client.post("/db/sync", data={"csrf_token": "t", **form})


def drive_db(fake, fid, tmp_path):
    path = str(tmp_path / f"drive-{fid}-{len(fake.uploads)}.mmbak")
    with open(path, "wb") as f:
        f.write(fake.data(fid))
    return path


def test_drive_up_to_date(client, store, drive):
    sync(client)
    assert "Already up to date" in flashes(client) and not drive.uploads


def test_drive_local_changes_are_written_back(client, store, drive, tmp_path):
    patch(client, "t1", "ZCONTENT", "edited here")
    sync(client)
    assert "Nothing new on Drive. Uploaded the result to " + DRIVE_NAME in flashes(client)
    assert drive.uploads == ["f1"] and drive.files["f1"]["name"] == DRIVE_NAME
    assert query(drive_db(drive, "f1", tmp_path), "SELECT ZCONTENT FROM INOUTCOME WHERE uid = 't1'")[0][0] == "edited here"
    assert store.db_info()["remote_md5"] == drive.meta("f1")["md5Checksum"] and not store.unsynced()
    sync(client)
    assert "Already up to date" in flashes(client) and drive.uploads == ["f1"]


def test_drive_new_backup_merges_and_is_replaced(client, store, drive, export, tmp_path):
    patch(client, "t1", "ZCONTENT", "edited here")
    app = export()
    add_tx(app, "app1")
    drive.add("f2", "MMGF(1-2-26-101010).mmbak", app)
    assert sync(client).location.endswith("/sync") and not drive.uploads
    accept(client)
    msg = flashes(client)
    assert "Merged MMGF(1-2-26-101010).mmbak" in msg and "Uploaded the result" in msg
    assert drive.uploads == ["f2"]  # the backup it came from, not the older one
    on_drive = drive_db(drive, "f2", tmp_path)
    assert query(on_drive, "SELECT COUNT(*) FROM INOUTCOME WHERE uid IN ('app1') OR ZCONTENT = 'edited here'")[0][0] == 2
    assert local(store, "app1") is not None and local(store, "t1")["ZCONTENT"] == "edited here"
    assert dbstore.file_md5(store.base_path) == dbstore.file_md5(app)
    assert not store.unsynced() and store.db_info()["remote_id"] == "f2"
    sync(client)
    assert "Already up to date" in flashes(client)


def test_drive_new_backup_without_local_changes_is_installed(client, store, drive, export):
    app = export()
    add_tx(app, "app1")
    drive.add("f2", "MMGF(1-2-26-101010).mmbak", app)
    sync(client)
    accept(client)
    assert "Updated to" in flashes(client) and not drive.uploads
    assert dbstore.file_md5(store.db_path) == dbstore.file_md5(app)


def test_drive_restored_backup_after_a_push(client, store, drive, tmp_path):
    """Push, the app restores it and adds a transaction, meanwhile more
    edits here: merges on the pushed state, no conflicts."""
    patch(client, "t1", "ZCONTENT", "first edit")
    sync(client)
    restored = drive_db(drive, "f1", tmp_path)
    add_tx(restored, "app1")
    drive.add("f2", "MMGF(1-3-26-101010).mmbak", restored)
    patch(client, "t1", "ZCONTENT", "second edit")
    sync(client)
    accept(client)
    assert "Merged" in flashes(client)
    assert local(store, "t1")["ZCONTENT"] == "second edit" and local(store, "app1") is not None


def test_drive_read_only_merges_without_writing(client, store, drive, export):
    store.save_config({"gdrive": {**gdrive.settings(store), "scope": gdrive.SCOPE_READ}})
    patch(client, "t1", "ZCONTENT", "edited here")
    sync(client)
    assert "connected read-only" in flashes(client) and not drive.uploads
    app = export()
    add_tx(app, "app1")
    drive.add("f2", "MMGF(1-2-26-101010).mmbak", app)
    sync(client)
    accept(client)
    assert "Merged" in flashes(client) and not drive.uploads
    assert local(store, "app1") is not None and store.unsynced()
    assert "can only read Drive" in client.get("/settings").get_data(as_text=True)


def test_drive_review_then_write_back(client, store, drive, export):
    patch(client, "t1", "ZCONTENT", "mine")
    app = export()
    execute(app, "UPDATE INOUTCOME SET ZCONTENT = 'theirs' WHERE uid = 't1'")
    drive.add("f2", "MMGF(1-2-26-101010).mmbak", app)
    r = sync(client)
    assert r.location.endswith("/sync") and not drive.uploads
    _, fp, ids = review_form(client)
    apply(client, fp, **{ids[0]: "local"})
    assert drive.uploads == ["f2"] and local(store, "t1")["ZCONTENT"] == "mine"


def test_drive_newer_backup_during_review_is_not_overwritten(client, store, drive, export):
    patch(client, "t1", "ZCONTENT", "mine")
    app = export()
    execute(app, "UPDATE INOUTCOME SET ZCONTENT = 'theirs' WHERE uid = 't1'")
    drive.add("f2", "MMGF(1-2-26-101010).mmbak", app)
    sync(client)
    _, fp, ids = review_form(client)
    drive.add("f3", "MMGF(1-3-26-101010).mmbak", export())  # the app backed up again meanwhile
    apply(client, fp, **{ids[0]: "local"})
    assert "A newer backup appeared on Drive" in flashes(client)
    assert not drive.uploads and local(store, "t1")["ZCONTENT"] == "mine" and store.unsynced()


def test_drive_replace(client, store, drive, export):
    patch(client, "t1", "ZCONTENT", "mine")
    app = export()
    add_tx(app, "app1")
    drive.add("f2", "MMGF(1-2-26-101010).mmbak", app)
    sync(client, replace="1")
    assert dbstore.file_md5(store.db_path) == dbstore.file_md5(app) and not drive.uploads


def test_drive_upload_failure_keeps_the_merge(client, store, drive, export, monkeypatch):
    patch(client, "t1", "ZCONTENT", "edited here")
    app = export()
    add_tx(app, "app1")
    drive.add("f2", "MMGF(1-2-26-101010).mmbak", app)

    def refuse(*a, **k):
        raise gdrive.GDriveError("Google Drive error (403): Request had insufficient authentication scopes.")

    monkeypatch.setattr(gdrive, "upload", refuse)
    sync(client)
    accept(client)
    assert "Uploading to Drive failed" in flashes(client)
    assert local(store, "app1") is not None and store.unsynced()


def test_drive_errors(client, store, drive, make_mmbak, monkeypatch):
    before = dbstore.file_md5(store.db_path)
    drive.add("bad", "MMGF(1-9-26-101010).mmbak", make_mmbak(18))
    sync(client)
    assert "Sync failed" in flashes(client) and dbstore.file_md5(store.db_path) == before
    del drive.files["bad"]

    drive.add("f2", "MMGF(1-8-26-101010).mmbak", b"x")
    monkeypatch.setattr(gdrive, "download", lambda s, f, dest: open(dest, "wb").write(b"corrupt"))
    sync(client)
    assert "corrupted" in flashes(client) and store.load_pending() is None
    assert [f for f in os.listdir(store.db_dir) if f.endswith(".part")] == []

    drive.files.clear()
    sync(client)
    assert "No MM*.mmbak" in flashes(client)


def test_drive_legacy_install_fetches_its_base(client, store, drive, export):
    forget_base(store)
    patch(client, "t1", "ZCONTENT", "edited here")
    app = export()
    add_tx(app, "app1")
    drive.add("f2", "MMGF(1-2-26-101010).mmbak", app)
    sync(client)
    # f1 (what was installed) is still on Drive, so it's a real three-way merge
    accept(client)
    assert "Merged" in flashes(client) and store.load_pending() is None
    assert local(store, "t1")["ZCONTENT"] == "edited here"


def test_drive_setup_can_ask_for_read_only(client, store):
    r = client.post("/setup/gdrive", data={"csrf_token": "t", "client_id": "cid", "client_secret": "s",
                                           "folder_name": ""})
    assert "drive.readonly" in r.location and not gdrive.wants_write(store)
    r = client.post("/setup/gdrive", data={"csrf_token": "t", "client_id": "cid", "client_secret": "s",
                                           "folder_name": "", "write": "1"})
    assert "auth%2Fdrive&" in r.location and gdrive.wants_write(store)
