"""User accounts, kept in app.json under "users":

    {"<name>": {"password_hash": "...", "admin": true, "created": "...",
                "session_version": 1,
                "api_tokens": {"<id>": {"name": "...", "hash": "...", "write": false,
                                        "created": "...", "last_used": "..."}}}}

A user's data lives in dbstore.store_for(name). password_hash is "" for
users created by the trusted proxy header who never set a password; they
can't log in with the form. Bumping session_version logs out every
session of that user (password change, removal). API tokens are separate
from the password and stay valid until revoked or the user is removed.
"""
import hashlib
import hmac
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

import dbstore

# Lowercase so names are case-insensitive and safe as folder names; "@"
# so proxy headers that carry an email address work as-is.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.@-]{0,63}$")
MIN_PASSWORD = 8


class UserError(Exception):
    """A problem worth showing to the user as-is."""


def normalize(name):
    return (name or "").strip().lower()


def all_users():
    return dbstore.load_app_config().get("users") or {}


def get(name):
    return all_users().get(name)


def has_users():
    return bool(all_users())


def setup_code():
    """While there are no users: the one-time code needed to create the
    first (admin) account, so whoever reaches a fresh install first can't
    claim it. Only shown in the app's log. Kept in app.json so restarts
    (and the dev server's reloader) agree on it; dropped once used."""
    code = None

    def ensure(cfg):
        nonlocal code
        code = cfg.setdefault("setup_code", secrets.token_urlsafe(12))

    dbstore.update_app_config(ensure)
    return code


def check_setup_code(code):
    expected = dbstore.load_app_config().get("setup_code") or ""
    return bool(expected) and hmac.compare_digest((code or "").strip(), expected)


def _check_password(password):
    if len(password) < MIN_PASSWORD:
        raise UserError(f"Passwords need at least {MIN_PASSWORD} characters.")


def create(name, password, admin=False):
    """Add a user. The first user is always an admin and takes over the data
    of a pre-multi-user install. password may be None (proxy-header users)."""
    name = normalize(name)
    if not USERNAME_RE.match(name):
        raise UserError("Usernames are 1-64 characters: a-z, 0-9 and . _ @ -, starting with a letter or digit.")
    if password is not None:
        _check_password(password)
    first = False

    def add(cfg):
        nonlocal first
        users = cfg.setdefault("users", {})
        if name in users:
            raise UserError(f"User {name} already exists.")
        first = not users
        if first:
            cfg.pop("setup_code", None)
        users[name] = {
            "password_hash": generate_password_hash(password) if password else "",
            "admin": bool(admin or first),
            "created": datetime.now().isoformat(timespec="seconds"),
            "session_version": 1,
        }

    dbstore.update_app_config(add)
    os.makedirs(dbstore.user_dir(name), exist_ok=True)
    if first:
        dbstore.adopt_legacy_data(name)
    return name


def _admins(users):
    return [n for n, u in users.items() if u.get("admin")]


def set_password(name, password):
    _check_password(password)

    def change(cfg):
        u = cfg["users"][name]
        u["password_hash"] = generate_password_hash(password)
        u["session_version"] = u.get("session_version", 1) + 1

    dbstore.update_app_config(change)


def set_admin(name, admin):
    def change(cfg):
        users = cfg["users"]
        if not admin and _admins(users) == [name]:
            raise UserError("There has to be at least one admin.")
        users[name]["admin"] = bool(admin)

    dbstore.update_app_config(change)


def remove(name):
    """Delete the account. Its data is moved aside to users/.removed/, not
    deleted, so a mistake can be undone by hand."""
    def drop(cfg):
        users = cfg.get("users") or {}
        if name not in users:
            raise UserError(f"No user {name}.")
        if _admins(users) == [name]:
            raise UserError("Can't remove the only admin.")
        del users[name]

    dbstore.update_app_config(drop)
    dbstore.forget_store(name)
    src = dbstore.user_dir(name)
    if os.path.exists(src):
        dest_dir = os.path.join(dbstore.USERS_DIR, ".removed")
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(src, os.path.join(dest_dir, f"{name}-{datetime.now():%Y%m%d_%H%M%S}"))


def verify(name, password):
    """The user record if name/password match, else None."""
    u = get(normalize(name))
    if u and u.get("password_hash") and check_password_hash(u["password_hash"], password):
        return u
    # Same cost either way, so timing doesn't reveal which names exist.
    if not u:
        check_password_hash(_DUMMY_HASH, password)
    return None


_DUMMY_HASH = generate_password_hash("not a real password")


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------

# "mmw_<id>_<secret>": the id finds the record, only a SHA-256 of the whole
# token is stored (the secret is random, so a slow hash adds nothing).
TOKEN_PREFIX = "mmw_"
TOKEN_NAME_MAX = 64
MAX_TOKENS = 50
# last_used is rewritten at most this often, not on every request.
TOKEN_TOUCH_SECONDS = 60


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def tokens(name):
    """{id: record} for a user, without the hashes."""
    return {tid: {k: v for k, v in t.items() if k != "hash"}
            for tid, t in ((get(name) or {}).get("api_tokens") or {}).items()}


def create_token(name, label, write=False):
    """Make a token for the user and return it. It's only ever shown now."""
    label = (label or "").strip()
    if not label or len(label) > TOKEN_NAME_MAX:
        raise UserError(f"Give the token a name of 1-{TOKEN_NAME_MAX} characters.")
    tid = secrets.token_hex(4)
    token = f"{TOKEN_PREFIX}{tid}_{secrets.token_urlsafe(32)}"

    def add(cfg):
        toks = cfg["users"][name].setdefault("api_tokens", {})
        if len(toks) >= MAX_TOKENS:
            raise UserError(f"You already have {MAX_TOKENS} tokens. Revoke some first.")
        toks[tid] = {
            "name": label, "hash": _token_hash(token), "write": bool(write),
            "created": datetime.now().isoformat(timespec="seconds"), "last_used": "",
        }

    dbstore.update_app_config(add)
    return token


def revoke_token(name, tid):
    def drop(cfg):
        if (cfg["users"][name].get("api_tokens") or {}).pop(tid, None) is None:
            raise UserError("No such token.")

    dbstore.update_app_config(drop)


def user_for_token(token):
    """(username, token id, record) for a valid token, else None. Bumps the
    token's last_used now and then."""
    token = (token or "").strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    tid = token[len(TOKEN_PREFIX):].split("_", 1)[0]
    want = _token_hash(token)
    for name, u in all_users().items():
        t = (u.get("api_tokens") or {}).get(tid)
        if t and hmac.compare_digest(t.get("hash", ""), want):
            _touch_token(name, tid, t)
            return name, tid, t
    return None


def _touch_token(name, tid, t):
    now = datetime.now()
    try:
        if (now - datetime.fromisoformat(t.get("last_used") or "")).total_seconds() < TOKEN_TOUCH_SECONDS:
            return
    except ValueError:
        pass

    def touch(cfg):
        rec = ((cfg.get("users") or {}).get(name, {}).get("api_tokens") or {}).get(tid)
        if rec:
            rec["last_used"] = now.isoformat(timespec="seconds")

    dbstore.update_app_config(touch)


# ---------------------------------------------------------------------------
# Login throttling (in-process, fine for the single gunicorn worker)
# ---------------------------------------------------------------------------

# Failures allowed per LOCKOUT_SECONDS window. The per-IP limit is looser:
# behind a reverse proxy every request has the proxy's address.
MAX_FAILURES = {"user": 5, "ip": 20}
LOCKOUT_SECONDS = 15 * 60
_failures = {}  # (kind, value) -> [timestamps]
_fail_lock = threading.Lock()


def _recent(key, now):
    return [t for t in _failures.get(key, []) if now - t < LOCKOUT_SECONDS]


def locked_out(ip, name):
    """Seconds until another attempt is allowed for this IP or this
    username, or 0."""
    now = time.time()
    with _fail_lock:
        waits = []
        for key in (("ip", ip), ("user", normalize(name))):
            recent, limit = _recent(key, now), MAX_FAILURES[key[0]]
            if len(recent) >= limit:
                waits.append(LOCKOUT_SECONDS - (now - recent[-limit]))
        return int(max(waits)) + 1 if waits else 0


def record_failure(ip, name):
    now = time.time()
    with _fail_lock:
        for key in (("ip", ip), ("user", normalize(name))):
            _failures[key] = _recent(key, now) + [now]


def clear_failures(ip, name):
    with _fail_lock:
        _failures.pop(("ip", ip), None)
        _failures.pop(("user", normalize(name)), None)
