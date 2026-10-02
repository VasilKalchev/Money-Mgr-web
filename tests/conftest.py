import os
import shutil
import tempfile

# dbstore reads MMW_DATA_DIR at import time, so point it at a scratch dir
# before anything imports app. Tests then retarget it per test (see data_dir).
os.environ["MMW_DATA_DIR"] = tempfile.mkdtemp(prefix="mmw-test-import-")
os.environ.pop("MMW_AUTH_HEADER", None)
os.environ.pop("MMW_TRUSTED_PROXIES", None)

import pytest

import dbstore
import mmbak
import users


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """A fresh, empty DATA_DIR for every test."""
    monkeypatch.setattr(dbstore, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dbstore, "APP_CONFIG_PATH", str(tmp_path / "app.json"))
    monkeypatch.setattr(dbstore, "USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setattr(dbstore, "_stores", {})
    monkeypatch.setattr(users, "_failures", {})
    return tmp_path


@pytest.fixture
def mmbak_file(tmp_path):
    """Path to a fresh supported .mmbak outside any user's folder."""
    return mmbak.build(str(tmp_path / "export.mmbak"))


@pytest.fixture
def make_mmbak(tmp_path):
    counter = iter(range(1000))

    def make(user_version=19):
        return mmbak.build(str(tmp_path / f"staged{next(counter)}.mmbak"), user_version=user_version)

    return make


@pytest.fixture
def client(data_dir, mmbak_file):
    """Test client logged in as admin "alice" with the fixture database installed."""
    from app import app

    users.create("alice", "correct horse")
    store = dbstore.store_for("alice")
    staged = store.staging_path()
    shutil.copy(mmbak_file, staged)
    store.install_db(staged, "manual", "export.mmbak")
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(user="alice", ver=users.get("alice")["session_version"], csrf="t")
    c.csrf = {"X-CSRF-Token": "t"}
    return c
