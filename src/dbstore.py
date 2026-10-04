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
                                 before a replacement overwrites it (plus
                                 incoming.mmbak, the app's snapshot, when a
                                 sync merge replaced it)
    users/<name>/sync/           sync state (see dbsync.py): base.mmbak, the
                                 last snapshot taken from the app; pushed/,
                                 states handed back to it since; and
                                 incoming.mmbak + pending.json while a merge
                                 waits for review

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
import time
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

# States handed back to the app (uploaded to Drive or downloaded) kept as
# candidate bases for the next sync; see Store.base_candidates().
PUSHED_KEEP = 3
INCOMING_NAME = "incoming.mmbak"
# MMW's own change time per transaction (see Store.stamp_changes), in
# changes.sqlite beside the working db. The name is unique so that the
# connections it's attached to (as "mmw") can name it unqualified, as
# triggers must.
CHANGES_TABLE = "mmw_changes"

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


def copy_db(src, dest):
    """Copy a SQLite database with the online backup API, so the copy is
    consistent even if src is being written."""
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(dest)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()


def _copy_atomic(src, dest):
    """Copy a file to dest through a temp file next to it, so dest is never
    half-written."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".part")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        os.remove(tmp)
        raise


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
        self.sync_dir = os.path.join(root, "sync")
        self.base_path = os.path.join(self.sync_dir, "base.mmbak")
        self.pushed_dir = os.path.join(self.sync_dir, "pushed")
        self.incoming_path = os.path.join(self.sync_dir, INCOMING_NAME)
        self.pending_path = os.path.join(self.sync_dir, "pending.json")
        self.changes_path = os.path.join(root, "changes.sqlite")
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

    # -- MMW's change times -------------------------------------------------

    def ensure_changes(self):
        """Create the change log if it isn't there yet."""
        if os.path.isfile(self.changes_path):
            return
        os.makedirs(self.root, exist_ok=True)
        con = sqlite3.connect(self.changes_path)
        try:
            con.execute(f"CREATE TABLE IF NOT EXISTS {CHANGES_TABLE} (uid TEXT PRIMARY KEY, changed INTEGER NOT NULL)")
            con.commit()
        finally:
            con.close()

    def stamp_changes(self, old_db, new_db):
        """Record now (Unix ms, server clock) as the change time of every
        transaction that differs between old_db and new_db: added, changed
        or gone. A row synced from the app keeps the app's UTIME, which can
        be from before the sync, so clients reading incrementally go by
        this instead; API writes stamp their rows themselves (app.write_db).
        Rows never stamped go by their UTIME."""
        old, new = _transactions(old_db), _transactions(new_db)
        changed = [uid for uid in old.keys() | new.keys() if old.get(uid) != new.get(uid)]
        if not changed:
            return
        self.ensure_changes()
        now = int(time.time() * 1000)
        con = sqlite3.connect(self.changes_path)
        try:
            con.executemany(f"INSERT OR REPLACE INTO {CHANGES_TABLE} (uid, changed) VALUES (?, ?)",
                            [(uid, now) for uid in changed])
            con.commit()
        finally:
            con.close()

    def _swap_in(self, staged):
        if self.db_exists():
            self.stamp_changes(self.db_path, staged)
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except FileNotFoundError:
                pass
        os.replace(staged, self.db_path)
        self._backed_up = False

    def install_db(self, staged, source, name, **extra):
        """Validate the staged file and make it the working database, backing
        up the one it replaces. `source` is "manual" or "gdrive"; `name` is
        the original file name, shown in the UI. Extra keyword args (e.g. the
        Drive file id/md5) are stored with the provenance. The file also
        becomes the sync base (it's what the app has), and a pending sync
        is dropped. The staged file is consumed either way. Raises
        UnsupportedDatabase if it isn't usable."""
        try:
            check_db_supported(staged)
            with self._lock:
                if self.db_exists():
                    self.backup()
                md5 = file_md5(staged)
                self.set_base(staged)
                self._swap_in(staged)
                now = datetime.now().isoformat(timespec="seconds")
                self.save_config({
                    "db_source": source,
                    "db_info": {
                        "name": os.path.basename(name),
                        "installed_at": now,
                        "synced_at": now,
                        "installed_md5": md5,
                        **extra,
                    },
                })
                self.clear_pending()
        finally:
            if os.path.exists(staged):
                os.remove(staged)

    def install_merged(self, merged, incoming, source, name, **extra):
        """Make a sync's merged database the working one. The one it replaces
        is backed up with the incoming snapshot alongside, and `incoming`
        becomes the new sync base. Until it's handed back to the app, the
        working db has changes the app lacks ("unsynced"). Consumes
        `merged`; `incoming` is left for the caller."""
        try:
            check_db_supported(merged)
            with self._lock:
                self.backup(extra={INCOMING_NAME: incoming})
                md5 = file_md5(merged)
                self.set_base(incoming)
                self._swap_in(merged)
                now = datetime.now().isoformat(timespec="seconds")
                self.save_config({
                    "db_source": source,
                    "db_info": {
                        "name": os.path.basename(name),
                        "installed_at": now,
                        "synced_at": now,
                        "installed_md5": md5,
                        "unsynced": True,
                        **extra,
                    },
                })
        finally:
            if os.path.exists(merged):
                os.remove(merged)

    def db_info(self):
        return self.load_config().get("db_info") or {}

    def update_db_info(self, **fields):
        with self._lock:
            self.save_config({"db_info": {**self.db_info(), **fields}})

    def local_modified(self):
        """True when the working db has been edited since it was installed
        or last synced."""
        md5 = self.db_info().get("installed_md5")
        return bool(md5) and self.db_exists() and file_md5(self.db_path) != md5

    def unsynced(self):
        """True when the working db has changes the app (for a Drive sync:
        the file on Drive) doesn't have yet."""
        return bool(self.db_info().get("unsynced")) or self.local_modified()

    # -- sync state (see dbsync.py) -------------------------------------------

    def set_base(self, src):
        """Record src as the snapshot this db and the app last had in
        common, and forget the states handed back to the app before it."""
        with self._lock:
            _copy_atomic(src, self.base_path)
            shutil.rmtree(self.pushed_dir, ignore_errors=True)

    def add_pushed(self, src):
        """Remember src as a state handed back to the app (uploaded to Drive
        or downloaded), which its next backup may descend from."""
        with self._lock:
            name = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".mmbak"
            _copy_atomic(src, os.path.join(self.pushed_dir, name))
            for old in self._pushed()[PUSHED_KEEP:]:
                os.remove(old)

    def _pushed(self):
        if not os.path.isdir(self.pushed_dir):
            return []
        names = sorted((n for n in os.listdir(self.pushed_dir) if n.endswith(".mmbak")), reverse=True)
        return [os.path.join(self.pushed_dir, n) for n in names]

    def base_candidates(self):
        """Snapshots the app's next backup may descend from, safest first:
        the last one taken from the app, then states handed back to it,
        newest first."""
        return ([self.base_path] if os.path.isfile(self.base_path) else []) + self._pushed()

    def load_pending(self):
        """The sync waiting for review ({source, name, remote, started_at}),
        or None."""
        if not os.path.isfile(self.incoming_path):
            return None
        return _load_json(self.pending_path) or None

    def save_pending(self, staged, info):
        """Park a staged incoming snapshot until its merge is reviewed
        (replacing any earlier one)."""
        with self._lock:
            os.makedirs(self.sync_dir, exist_ok=True)
            os.replace(staged, self.incoming_path)
            _save_json(self.pending_path, {**info, "started_at": datetime.now().isoformat(timespec="seconds")})

    def clear_pending(self):
        with self._lock:
            for path in (self.pending_path, self.incoming_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

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
            names = sorted(f for f in os.listdir(prev_dir) if f.endswith(".mmbak") and f != INCOMING_NAME)
            own = os.path.basename(self.db_path)
            if names:
                prev_db = os.path.join(prev_dir, own if own in names else names[0])
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

    def backup(self, extra=None):
        """Snapshot the working db into backups/<stamp>/ (with a diff.txt
        against the previous snapshot, unless turned off in settings);
        returns the new folder. Uses SQLite's online backup API so the copy
        is consistent even if the file is open elsewhere. `extra`
        ({file name: path}) is copied in alongside."""
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
            copy_db(self.db_path, dest)
            for name, path in (extra or {}).items():
                shutil.copyfile(path, os.path.join(archive_dir, name))
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


def _transactions(path):
    """{uid: {column: value}} of a database's INOUTCOME rows."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = con.execute("SELECT * FROM INOUTCOME")
        cols = [d[0] for d in cur.description]
        return {row["uid"]: row for row in (dict(zip(cols, r)) for r in cur) if row.get("uid")}
    finally:
        con.close()


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
