import os
import sqlite3

import pytest

import dbstore
from dbstore import Store, UnsupportedDatabase


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "u"))
    os.makedirs(s.db_dir)  # the app creates this via staging_path() before any install
    return s


def test_check_db_supported(make_mmbak, tmp_path):
    dbstore.check_db_supported(make_mmbak(19))
    with pytest.raises(UnsupportedDatabase, match="isn't supported"):
        dbstore.check_db_supported(make_mmbak(18))
    junk = tmp_path / "junk.mmbak"
    junk.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(UnsupportedDatabase, match="Can't read"):
        dbstore.check_db_supported(str(junk))


def test_install_db_first_time(store, make_mmbak):
    staged = make_mmbak()
    store.install_db(staged, "manual", "/some/dir/MM_export.mmbak")
    assert store.db_exists()
    assert not os.path.exists(staged)
    info = store.db_info()
    assert info["name"] == "MM_export.mmbak"  # basename only
    assert info["installed_md5"] == dbstore.file_md5(store.db_path)
    assert store.load_config()["db_source"] == "manual"
    assert store.backup_count() == 0  # nothing to back up yet
    assert not store.local_modified()


def test_install_db_backs_up_replaced_file_and_stores_extra(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    store.install_db(make_mmbak(), "gdrive", "b.mmbak", drive_id="abc")
    assert store.backup_count() == 1
    assert store.db_info()["drive_id"] == "abc"
    assert store.load_config()["db_source"] == "gdrive"


def test_install_db_rejects_unsupported_and_keeps_current(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    md5 = dbstore.file_md5(store.db_path)
    bad = make_mmbak(18)
    with pytest.raises(UnsupportedDatabase):
        store.install_db(bad, "manual", "b.mmbak")
    assert dbstore.file_md5(store.db_path) == md5
    assert not os.path.exists(bad)  # staged file consumed either way
    assert store.db_info()["name"] == "a.mmbak"


def test_install_db_removes_stale_sidecars(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    for suffix in ("-wal", "-shm", "-journal"):
        open(store.db_path + suffix, "w").close()
    store.install_db(make_mmbak(), "manual", "b.mmbak")
    for suffix in ("-wal", "-shm", "-journal"):
        assert not os.path.exists(store.db_path + suffix)


def test_local_modified(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    assert not store.local_modified()
    con = sqlite3.connect(store.db_path)
    con.execute("UPDATE INOUTCOME SET ZCONTENT = 'edited' WHERE uid = 't1'")
    con.commit()
    con.close()
    assert store.local_modified()


def test_backup_contents_and_diff(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    first = store.backup()
    assert os.path.isfile(os.path.join(first, "current.mmbak"))
    assert "Initial backup" in open(os.path.join(first, "diff.txt")).read()

    con = sqlite3.connect(store.db_path)
    con.execute("UPDATE INOUTCOME SET ZCONTENT = 'edited' WHERE uid = 't1'")
    con.commit()
    con.close()
    second = store.backup()
    assert second != first  # same-second stamp gets a suffix
    diff = open(os.path.join(second, "diff.txt")).read()
    assert "edited" in diff and diff.startswith("---")
    assert store.latest_backup() == os.path.basename(second)


def test_backup_diff_can_be_disabled(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    store.save_backup_settings(keep=0, on_first_write=True, diff=False)
    folder = store.backup()
    assert not os.path.exists(os.path.join(folder, "diff.txt"))


def test_backup_pruning_keeps_newest(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    store.save_backup_settings(keep=2, on_first_write=True, diff=False)
    made = [os.path.basename(store.backup()) for _ in range(4)]
    assert store.backup_count() == 2
    assert store._backup_folders() == sorted(made)[-2:]


def test_backup_keep_zero_keeps_all(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    store.save_backup_settings(keep=0, on_first_write=True, diff=False)
    for _ in range(4):
        store.backup()
    assert store.backup_count() == 4


def test_backup_once_runs_once_until_reinstall(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    store.backup_once()
    store.backup_once()
    assert store.backup_count() == 1
    store.install_db(make_mmbak(), "manual", "b.mmbak")  # +1 for the replaced file
    assert store.backup_count() == 2
    store.backup_once()  # flag was reset by the install
    assert store.backup_count() == 3


def test_backup_once_respects_setting(store, make_mmbak):
    store.install_db(make_mmbak(), "manual", "a.mmbak")
    store.save_backup_settings(keep=5, on_first_write=False, diff=True)
    store.backup_once()
    assert store.backup_count() == 0


def test_backup_settings_defaults_and_override(store):
    assert store.backup_settings() == dbstore.BACKUP_DEFAULTS
    store.save_config({"backups": {"keep": 3}})
    s = store.backup_settings()
    assert s["keep"] == 3 and s["on_first_write"] is True


def test_save_config_merges_shallowly(store):
    store.save_config({"a": 1})
    store.save_config({"b": 2})
    assert store.load_config() == {"a": 1, "b": 2}


def test_config_files_are_owner_only(store):
    store.save_config({"a": 1})
    assert os.stat(store.config_path).st_mode & 0o077 == 0


def test_secret_key_is_generated_once():
    assert dbstore.secret_key() == dbstore.secret_key()
    assert len(dbstore.secret_key()) == 64


def test_adopt_legacy_data(data_dir):
    (data_dir / "db").mkdir()
    (data_dir / "db" / "current.mmbak").write_bytes(b"x")
    (data_dir / "config.json").write_text('{"secret_key": "k", "db_dir": "/x", "keep": 1}')
    assert dbstore.adopt_legacy_data("alice") is True
    dest = dbstore.user_dir("alice")
    assert os.path.isfile(os.path.join(dest, "db", "current.mmbak"))
    assert not (data_dir / "config.json").exists()
    cfg = dbstore.store_for("alice").load_config()
    assert cfg == {"keep": 1}  # app-wide / dead keys dropped


def test_adopt_legacy_data_noops(data_dir):
    assert dbstore.adopt_legacy_data("alice") is False  # nothing to move
    (data_dir / "config.json").write_text("{}")
    os.makedirs(dbstore.user_dir("alice"))
    open(os.path.join(dbstore.user_dir("alice"), "config.json"), "w").write("{}")
    assert dbstore.adopt_legacy_data("alice") is False  # user already has data
    assert (data_dir / "config.json").exists()
