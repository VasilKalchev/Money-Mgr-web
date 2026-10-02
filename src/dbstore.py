"""Where everything lives on disk: the app-wide config and, per user, their
settings, managed database and backups.

Everything lives under DATA_DIR (the docker volume, /config in the image):

    app.json                     app-wide: session key, user accounts
    users/<name>/config.json     the user's settings, db provenance, Google
                                 credentials
    users/<name>/db/current.mmbak
                                 the working database every route reads/writes
    users/<name>/backups/<stamp>/
                                 a copy of the working db + diff.txt, taken
                                 before the first write after startup and
                                 before a replacement overwrites it

Installs from before multi-user support kept config.json, db/ and backups/
directly in DATA_DIR; adopt_legacy_data() moves them to the first user.
"""
import difflib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("MMW_DATA_DIR") or os.path.join(BASE_DIR, "data")
APP_CONFIG_PATH = os.path.join(DATA_DIR, "app.json")
USERS_DIR = os.path.join(DATA_DIR, "users")
# Defaults for the "backups" settings in a user's config.json (editable in
# Settings).
BACKUP_DEFAULTS = {
    "keep": int(os.environ.get("MMW_BACKUP_KEEP", "30")),  # 0 = keep all
    "on_first_write": True,  # snapshot before the first edit after startup/install
    "diff": True,  # write diff.txt against the previous snapshot
}

# PRAGMA user_version values of the .mmbak files this app's SQL and INSERTs
# were written and verified against (see docs/MM_DB_SCHEMA.md). Anything
# else is refused rather than risk wrong joins or malformed writes - add a
# version here only after checking its schema against the notes.
SUPPORTED_USER_VERSIONS = {19}

_app_lock = threading.RLock()


# ---------------------------------------------------------------------------
# JSON config files
# ---------------------------------------------------------------------------

def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_json(path, data):
    """Write atomically and owner-only (these hold password hashes and
    Google credentials)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)  # mkstemp already created it 0600


def load_app_config():
    return _load_json(APP_CONFIG_PATH)


def save_app_config(partial):
    """Shallow-merge `partial` into app.json."""
    with _app_lock:
        cfg = load_app_config()
        cfg.update(partial)
        _save_json(APP_CONFIG_PATH, cfg)


def update_app_config(fn):
    """Read-modify-write app.json under the lock: fn(cfg) edits it in place."""
    with _app_lock:
        cfg = load_app_config()
        fn(cfg)
        _save_json(APP_CONFIG_PATH, cfg)


def secret_key():
    """Flask session key, generated once and kept in app.json so sessions
    (and an in-flight Google sign-in) survive a restart."""
    with _app_lock:
        cfg = load_app_config()
        if not cfg.get("secret_key"):
            save_app_config({"secret_key": secrets.token_hex(32)})
            cfg = load_app_config()
    return cfg["secret_key"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class UnsupportedDatabase(Exception):
    pass


def db_user_version(path):
    """(user_version, error) for the .mmbak at path. user_version is None
    when the file can't be read as SQLite; error then says why."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0], None
        finally:
            con.close()
    except sqlite3.Error as e:
        return None, str(e)


def check_db_supported(path):
    """Raise UnsupportedDatabase unless path is a SQLite file whose
    user_version is in SUPPORTED_USER_VERSIONS."""
    version, err = db_user_version(path)
    if version is None:
        raise UnsupportedDatabase(f"Can't read this file as a SQLite database ({err}).")
    if version not in SUPPORTED_USER_VERSIONS:
        raise UnsupportedDatabase(
            f"Database schema version {version} isn't supported "
            f"(supported: {', '.join(str(v) for v in sorted(SUPPORTED_USER_VERSIONS))})."
        )


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump_lines(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [line + "\n" for line in con.iterdump()]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# A user's data
# ---------------------------------------------------------------------------

class Store:
    """One user's settings, working database and backups, all under `root`.

    Get instances from store_for(), not the constructor: the instance also
    carries the per-process "already backed up since startup" flag."""

    def __init__(self, root):
        self.root = root
        self.config_path = os.path.join(root, "config.json")
        self.db_dir = os.path.join(root, "db")
        self.db_path = os.path.join(self.db_dir, "current.mmbak")
        self.backup_dir = os.path.join(root, "backups")
        self._lock = threading.RLock()
        self._backed_up = False

    # -- config --------------------------------------------------------------

    def load_config(self):
        return _load_json(self.config_path)

    def save_config(self, partial):
        """Shallow-merge `partial` into the user's config.json."""
        with self._lock:
            cfg = self.load_config()
            cfg.update(partial)
            _save_json(self.config_path, cfg)

    # -- the managed database ------------------------------------------------

    def db_exists(self):
        return os.path.isfile(self.db_path)

    def staging_path(self):
        """A fresh path next to the live db to write an incoming file to
        (same filesystem, so install_db can swap it in with an atomic
        rename)."""
        os.makedirs(self.db_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=self.db_dir, suffix=".part")
        os.close(fd)
        return path

    def install_db(self, staged, source, name, **extra):
        """Validate the staged file and make it the working database, backing
        up the one it replaces. `source` is "manual" or "gdrive"; `name` is
        the original file name, shown in the UI. Extra keyword args (e.g. the
        Drive file id/md5) are stored with the provenance. The staged file is
        consumed either way. Raises UnsupportedDatabase if it isn't usable."""
        try:
            check_db_supported(staged)
            with self._lock:
                if self.db_exists():
                    self.backup()
                for suffix in ("-journal", "-wal", "-shm"):
                    try:
                        os.remove(self.db_path + suffix)
                    except FileNotFoundError:
                        pass
                md5 = file_md5(staged)
                os.replace(staged, self.db_path)
                self.save_config({
                    "db_source": source,
                    "db_info": {
                        "name": os.path.basename(name),
                        "installed_at": datetime.now().isoformat(timespec="seconds"),
                        "installed_md5": md5,
                        **extra,
                    },
                })
                self._backed_up = False
        finally:
            if os.path.exists(staged):
                os.remove(staged)

    def db_info(self):
        return self.load_config().get("db_info") or {}

    def local_modified(self):
        """True when the working db has been edited since it was installed."""
        md5 = self.db_info().get("installed_md5")
        return bool(md5) and self.db_exists() and file_md5(self.db_path) != md5

    # -- backups -------------------------------------------------------------

    def backup_settings(self):
        return {**BACKUP_DEFAULTS, **(self.load_config().get("backups") or {})}

    def save_backup_settings(self, keep, on_first_write, diff):
        self.save_config({"backups": {"keep": keep, "on_first_write": on_first_write, "diff": diff}})
        with self._lock:
            self._prune_backups()

    def _backup_folders(self):
        if not os.path.isdir(self.backup_dir):
            return []
        return sorted(d for d in os.listdir(self.backup_dir) if os.path.isdir(os.path.join(self.backup_dir, d)))

    def _write_diff(self, diff_path, prev_dir, new_db_path):
        """diff.txt: a unified diff of the SQL dumps against the previous
        backup, so it's easy to see what changed without per-query logging."""
        prev_db = None
        if prev_dir:
            prev_db = next((os.path.join(prev_dir, f) for f in os.listdir(prev_dir) if f.endswith(".mmbak")), None)
        if prev_db is None:
            text = "Initial backup: no prior snapshot to diff against.\n"
        else:
            try:
                diff = list(difflib.unified_diff(
                    _dump_lines(prev_db), _dump_lines(new_db_path),
                    fromfile=os.path.relpath(prev_db, self.backup_dir),
                    tofile=os.path.relpath(new_db_path, self.backup_dir),
                ))
                text = "".join(diff) or f"No changes since {os.path.basename(prev_dir)}.\n"
            except sqlite3.Error as e:
                text = f"Diff unavailable ({e}).\n"
        with open(diff_path, "w") as f:
            f.write(text)

    def _prune_backups(self):
        keep = self.backup_settings()["keep"]
        folders = self._backup_folders()
        for old in folders[:-keep] if keep > 0 else []:
            shutil.rmtree(os.path.join(self.backup_dir, old), ignore_errors=True)

    def backup(self):
        """Snapshot the working db into backups/<stamp>/ (with a diff.txt
        against the previous snapshot, unless turned off in settings);
        returns the new folder. Uses SQLite's online backup API so the copy
        is consistent even if the file is open elsewhere."""
        with self._lock:
            folders = self._backup_folders()
            prev_dir = os.path.join(self.backup_dir, folders[-1]) if folders else None
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_dir = os.path.join(self.backup_dir, stamp)
            suffix = 2
            while os.path.exists(archive_dir):
                archive_dir = os.path.join(self.backup_dir, f"{stamp}_{suffix}")
                suffix += 1
            os.makedirs(archive_dir)

            dest = os.path.join(archive_dir, os.path.basename(self.db_path))
            src_con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            dst_con = sqlite3.connect(dest)
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
                src_con.close()
            if self.backup_settings()["diff"]:
                self._write_diff(os.path.join(archive_dir, "diff.txt"), prev_dir, dest)
            self._prune_backups()
            return archive_dir

    def backup_once(self):
        """Back up the working db the first time it's about to be written
        after startup or after a new file was installed (unless turned off)."""
        with self._lock:
            if not self._backed_up and self.backup_settings()["on_first_write"]:
                self.backup()
                self._backed_up = True

    def backup_count(self):
        return len(self._backup_folders())

    def latest_backup(self):
        """Folder name (its timestamp) of the newest backup, or ""."""
        folders = self._backup_folders()
        return folders[-1] if folders else ""


_stores = {}


def user_dir(username):
    return os.path.join(USERS_DIR, username)


def store_for(username):
    """The Store for a user (usernames are validated by users.py before they
    ever reach a path)."""
    with _app_lock:
        if username not in _stores:
            _stores[username] = Store(user_dir(username))
        return _stores[username]


def forget_store(username):
    with _app_lock:
        _stores.pop(username, None)


def adopt_legacy_data(username):
    """Move a pre-multi-user install's config.json, db/ and backups/ from the
    top of DATA_DIR into this user's folder. No-op when there's nothing to
    move or the user already has data."""
    dest = user_dir(username)
    legacy = [n for n in ("config.json", "db", "backups") if os.path.exists(os.path.join(DATA_DIR, n))]
    if not legacy or os.path.exists(os.path.join(dest, "config.json")):
        return False
    os.makedirs(dest, exist_ok=True)
    for name in legacy:
        os.replace(os.path.join(DATA_DIR, name), os.path.join(dest, name))
    store = store_for(username)
    cfg = store.load_config()
    for key in ("secret_key", "db_dir"):  # app-wide now / long unused
        cfg.pop(key, None)
    _save_json(store.config_path, cfg)
    return True
