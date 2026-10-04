import hmac
import io
import ipaddress
import itertools
import json
import os
import secrets
import sqlite3
import sys
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.datastructures import MultiDict

import dbstore
import dbsync
import edits
import gdrive
import reads
import users
from dbstore import UnsupportedDatabase, check_db_supported

app = Flask(__name__)
app.secret_key = dbstore.secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=512 * 1024 * 1024,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set MMW_COOKIE_SECURE=1 when the app is only reached over HTTPS.
    SESSION_COOKIE_SECURE=os.environ.get("MMW_COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# Reverse proxies in front of the app (MMW_TRUSTED_PROXIES: addresses/CIDRs).
# Requests from them are trusted for X-Forwarded-For (the real client IP,
# for login throttling) and, if MMW_AUTH_HEADER is set, for a username from
# a proxy that has already authenticated the user (Authelia, Authentik,
# oauth2-proxy, ...). Nothing is trusted from any other address, since
# anyone who can reach the port could send these headers themselves.
TRUSTED_PROXIES = [ipaddress.ip_network(x.strip(), strict=False)
                   for x in os.environ.get("MMW_TRUSTED_PROXIES", "").split(",") if x.strip()]
AUTH_HEADER = os.environ.get("MMW_AUTH_HEADER", "").strip()


def _app_version():
    """The release tag the image was built from (MMW_VERSION). A local run, or
    an image built without one, shows pyproject's version marked as a dev build."""
    v = os.environ.get("MMW_VERSION", "").strip()
    if v and v != "dev":
        return v
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "rb") as f:
            return tomllib.load(f)["project"]["version"] + "-dev"
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "dev"


VERSION = _app_version()
app.jinja_env.globals["app_version"] = VERSION
if AUTH_HEADER and not TRUSTED_PROXIES:
    print(f"MMW_AUTH_HEADER={AUTH_HEADER} is ignored: set MMW_TRUSTED_PROXIES to the proxy's address.", file=sys.stderr)


def current_store():
    """The logged-in user's dbstore.Store."""
    return dbstore.store_for(g.user)


def current_locale():
    return current_store().load_config().get("locale", "") if g.get("user") else ""


def current_db_path():
    """The user's working database, or "" until one has been set up."""
    store = current_store()
    return store.db_path if store.db_exists() else ""


def current_db_mtime():
    path = current_db_path()
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def get_db(readonly=True):
    """A connection to the user's working database, with MMW's change log
    attached as "mmw" (see dbstore.Store.stamp_changes)."""
    db_path = current_db_path()
    store = current_store()
    store.ensure_changes()
    if not readonly:
        # Safety net for any write path that bypasses _require_db().
        check_db_supported(db_path)
    if readonly:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.execute("ATTACH DATABASE ? AS mmw", (f"file:{store.changes_path}?mode=ro",))
    else:
        store.backup_once()
        con = sqlite3.connect(db_path)
        con.execute("ATTACH DATABASE ? AS mmw", (store.changes_path,))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = OFF")
    return con


@contextmanager
def read_db():
    """A read-only connection for a request's reads (see reads.py)."""
    con = get_db()
    try:
        yield con
    finally:
        con.close()


@contextmanager
def write_db():
    """A writable connection for one edit (see edits.py), committed when the
    block finishes and discarded if it raises, so a refused edit writes
    nothing. Every transaction row it adds or changes gets MMW's change
    time in the same commit."""
    con = get_db(readonly=False)
    try:
        now = int(time.time() * 1000)
        con.create_function("mmw_now_ms", 0, lambda: now)
        for event in ("INSERT", "UPDATE"):
            con.execute(f"CREATE TEMP TRIGGER mmw_stamp_{event.lower()} AFTER {event} ON main.INOUTCOME BEGIN "
                        f"INSERT OR REPLACE INTO {dbstore.CHANGES_TABLE} (uid, changed) "
                        f"VALUES (NEW.uid, mmw_now_ms()); END")
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Routes - pages
# ---------------------------------------------------------------------------

@app.context_processor
def inject_db_name():
    if not g.get("user"):
        return {"db_name": "", "db_source": "", "has_db": False, "db_mtime": None, "user_locale": "",
                "user": None, "is_admin": False, "via_proxy": False, "sync_pending": False}
    store = current_store()
    return {
        "db_name": store.db_info().get("name") or "(no database)",
        "db_source": store.load_config().get("db_source", ""),
        "has_db": store.db_exists(),
        "db_mtime": current_db_mtime(),
        "user_locale": current_locale(),
        "user": g.user,
        "is_admin": g.is_admin,
        "via_proxy": g.via_proxy,
        "sync_pending": os.path.isfile(store.pending_path),
    }


def fmt_amount(value):
    try:
        return "{:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return value


def fmt_int(value):
    try:
        return "{:,}".format(int(value))
    except (TypeError, ValueError):
        return value


def fmt_mtime(value):
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


app.jinja_env.filters["amt"] = fmt_amount
app.jinja_env.filters["num"] = fmt_int
app.jinja_env.filters["mtime"] = fmt_mtime


# ---------------------------------------------------------------------------
# Authentication and CSRF
# ---------------------------------------------------------------------------

# Reachable without being logged in.
_PUBLIC_ENDPOINTS = {"static", "login", "first_account", "healthz", "manifest", "service_worker"}


def _is_trusted_proxy(addr):
    try:
        ip = ipaddress.ip_address((addr or "").strip())
    except ValueError:
        return False
    return any(ip in net for net in TRUSTED_PROXIES)


def client_ip():
    """The real client address: the direct peer, or, when that's a trusted
    proxy, the nearest untrusted hop in X-Forwarded-For (proxies append to
    the right, so anything further left could be forged by the client)."""
    addr = request.remote_addr or ""
    if not _is_trusted_proxy(addr):
        return addr
    for hop in reversed(request.headers.get("X-Forwarded-For", "").split(",")):
        hop = hop.strip()
        if hop and not _is_trusted_proxy(hop):
            return hop
    return addr


def _proxy_user():
    """The username a trusted reverse proxy vouches for, or None. Unknown
    names get an account (no password) on first sight."""
    if not AUTH_HEADER or not _is_trusted_proxy(request.remote_addr):
        return None
    name = users.normalize(request.headers.get(AUTH_HEADER))
    if not name:
        return None
    if not users.USERNAME_RE.match(name):
        abort(403, f"The username from {AUTH_HEADER} isn't valid here.")
    if not users.get(name):
        try:
            users.create(name, None)
        except users.UserError:
            pass  # created by a concurrent request
    return name


def _wants_json():
    return request.path.startswith("/api/")


def _bearer_token():
    """The token from an "Authorization: Bearer" header on an /api/v1/ call,
    else None. Other schemes (a proxy's Basic auth) are left alone."""
    if not request.path.startswith("/api/v1/"):
        return None
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


@app.before_request
def _authenticate():
    """Work out who's asking (an API token on /api/v1/, else trusted proxy
    header, else the session) and send anyone else to the login page, or to
    first-run account creation while there are no users at all."""
    g.user, g.via_proxy, g.is_admin, g.token = None, False, False, None
    token = _bearer_token()
    if token is not None:
        found = users.user_for_token(token)
        if not found:
            return jsonify(ok=False, error="Invalid API token."), 401
        g.user, _, g.token = found
        g.is_admin = bool(users.get(g.user).get("admin"))
        if request.method not in ("GET", "HEAD", "OPTIONS") and not g.token.get("write"):
            return jsonify(ok=False, error="This API token is read-only."), 403
        return None
    name = _proxy_user()
    if name:
        g.user, g.via_proxy = name, True
    elif session.get("user"):
        u = users.get(session["user"])
        # A password change or removal bumps/drops the version: log out.
        if u and u.get("session_version", 1) == session.get("ver"):
            g.user = session["user"]
        else:
            session.pop("user", None)
    if g.user:
        g.is_admin = bool(users.get(g.user).get("admin"))
        return None
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if _wants_json():
        return jsonify(ok=False, error="Not logged in."), 401
    if not users.has_users():
        return redirect(url_for("first_account"))
    return redirect(url_for("login", next=request.full_path.rstrip("?") if request.method == "GET" else None))


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def _check_csrf():
    """Every state-changing request must echo the session's token, as a
    csrf_token form field or an X-CSRF-Token header (base.html adds the
    header to every fetch). API token calls are exempt: they don't use the
    session cookie, so a cross-site page can't make them."""
    if request.method in ("GET", "HEAD", "OPTIONS") or g.get("token"):
        return None
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = session.get("csrf")
    if expected and hmac.compare_digest(sent, expected):
        return None
    if _wants_json():
        return jsonify(ok=False, error="Session expired or invalid form token. Reload the page."), 400
    flash("Your session expired or the form was stale. Please try again.", "error")
    return redirect(request.referrer or url_for("index"))


# Everything the pages use is inline or same-origin. form-action allows
# Google because /setup/gdrive answers its POST with a redirect there.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "form-action 'self' https://accounts.google.com",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
])


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if app.config["SESSION_COOKIE_SECURE"]:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    # Financial data: keep it out of shared caches and the back/forward cache
    # after logout.
    if request.endpoint != "static":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _login(name):
    session.clear()
    session.permanent = True
    session["user"] = name
    session["ver"] = users.get(name).get("session_version", 1)


def _safe_next(target):
    """Only follow same-site relative redirects after login."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


@app.route("/healthz")
def healthz():
    """For the container's HEALTHCHECK: up, and the data folder is readable."""
    dbstore.load_app_config()
    return "ok\n", 200, {"Content-Type": "text/plain"}


# The PWA's manifest and service worker. Both are public: browsers fetch the
# manifest without cookies, and the worker has to be served from / to
# control the whole app (it only caches the offline page, see sw.js).
@app.route("/manifest.webmanifest")
def manifest():
    return app.send_static_file("manifest.webmanifest"), 200, {"Content-Type": "application/manifest+json"}


@app.route("/sw.js")
def service_worker():
    return app.send_static_file("sw.js"), 200, {"Content-Type": "text/javascript"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if not users.has_users():
        return redirect(url_for("first_account"))
    if g.user:
        return redirect(_safe_next(request.args.get("next")))
    if request.method == "POST":
        name = users.normalize(request.form.get("username"))
        ip = client_ip()
        wait = users.locked_out(ip, name)
        if wait:
            flash(f"Too many failed logins. Try again in {wait // 60 + 1} minutes.", "error")
        elif users.verify(name, request.form.get("password", "")):
            users.clear_failures(ip, name)
            _login(name)
            return redirect(_safe_next(request.form.get("next")))
        else:
            users.record_failure(ip, name)
            flash("Wrong username or password.", "error")
    return render_template("login.html", mode="login", next=request.values.get("next", ""))


def _log_setup_code():
    print(f"First-run setup code (needed to create the admin account): {users.setup_code()}",
          file=sys.stderr, flush=True)


@app.route("/setup/account", methods=["GET", "POST"])
def first_account():
    """First run: create the admin account (it takes over the data of an
    install from before accounts existed)."""
    if users.has_users():
        return redirect(url_for("login"))
    if request.method == "POST":
        if not users.check_setup_code(request.form.get("setup_code")):
            _log_setup_code()
            flash("Wrong setup code. It's printed in the app's log.", "error")
        elif request.form.get("password") != request.form.get("password2"):
            flash("The passwords don't match.", "error")
        else:
            try:
                name = users.create(request.form.get("username"), request.form.get("password", ""), admin=True)
            except users.UserError as e:
                flash(str(e), "error")
            else:
                _login(name)
                return redirect(url_for("index"))
    return render_template("login.html", mode="first", next="")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account/password", methods=["POST"])
def change_password():
    u = users.get(g.user)
    if u.get("password_hash") and not users.verify(g.user, request.form.get("current", "")):
        flash("The current password is wrong.", "error")
    elif request.form.get("password") != request.form.get("password2"):
        flash("The new passwords don't match.", "error")
    else:
        try:
            users.set_password(g.user, request.form.get("password", ""))
        except users.UserError as e:
            flash(str(e), "error")
        else:
            _login(g.user)  # keep this session, drop the others
            flash("Password changed. Other sessions were logged out.", "ok")
    return redirect(url_for("settings"))


def _require_admin():
    if not g.is_admin:
        abort(403)


@app.route("/users", methods=["GET", "POST"])
def users_page():
    _require_admin()
    if request.method == "POST":
        try:
            name = users.create(request.form.get("username"), request.form.get("password", ""),
                                admin=request.form.get("admin") == "1")
        except users.UserError as e:
            flash(str(e), "error")
        else:
            flash(f"Created {name}. They set up their own database after logging in.", "ok")
        return redirect(url_for("users_page"))
    rows = []
    for name, u in sorted(users.all_users().items()):
        store = dbstore.store_for(name)
        rows.append({
            "name": name, "admin": u.get("admin"), "created": u.get("created", ""),
            "has_password": bool(u.get("password_hash")),
            "db_name": store.db_info().get("name", "") if store.db_exists() else "",
        })
    return render_template("users.html", rows=rows, proxy_header=AUTH_HEADER if TRUSTED_PROXIES else "")


@app.route("/users/<name>/<action>", methods=["POST"])
def user_action(name, action):
    _require_admin()
    if not users.get(name):
        abort(404)
    try:
        if action == "password":
            users.set_password(name, request.form.get("password", ""))
            if name == g.user and not g.via_proxy:
                _login(name)
            flash(f"Password for {name} set. Their other sessions were logged out.", "ok")
        elif action == "admin":
            if name == g.user:
                raise users.UserError("You can't change your own admin rights.")
            users.set_admin(name, request.form.get("admin") == "1")
            flash(f"{name} is {'now' if request.form.get('admin') == '1' else 'no longer'} an admin.", "ok")
        elif action == "remove":
            if name == g.user:
                raise users.UserError("You can't remove yourself.")
            users.remove(name)
            flash(f"Removed {name}. Their data was moved to users/.removed/ in the data folder.", "ok")
        else:
            abort(404)
    except users.UserError as e:
        flash(str(e), "error")
    return redirect(url_for("users_page"))


# ---------------------------------------------------------------------------
# Database guard
# ---------------------------------------------------------------------------

_NO_DB_EXEMPT_ENDPOINTS = {
    "static", "api_db_status", "settings", "backup_settings", "setup", "upload_db", "sync_db",
    "sync_upload", "sync_cancel", "gdrive_setup", "gdrive_callback", "gdrive_paste", "gdrive_disconnect",
    "login", "first_account", "logout", "change_password", "users_page", "user_action", "healthz",
    "manifest", "service_worker", "create_api_token", "revoke_api_token", "v1_status",
}


@app.before_request
def _require_db():
    """Until a database has been set up, send every page to /setup (and
    answer API calls with a 409) instead of 500ing on sqlite3.connect('')."""
    if request.endpoint in _NO_DB_EXEMPT_ENDPOINTS or request.endpoint is None:
        return None
    path = current_db_path()
    if not path:
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="No database has been set up yet."), 409
        return redirect(url_for("setup"))
    try:
        check_db_supported(path)
    except UnsupportedDatabase as e:
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error=str(e)), 409
        return render_template("unsupported_db.html", reason=str(e)), 409


@app.route("/api/db-status")
def api_db_status():
    return jsonify(mtime=current_db_mtime())


@app.route("/")
def index():
    """The app: one page whose screens get their data from /api/app/."""
    return render_template("app.html")


# ---------------------------------------------------------------------------
# Database setup: manual upload or Google Drive sync
# ---------------------------------------------------------------------------

@app.route("/setup")
def setup():
    """First-run page: provide the .mmbak by hand or set up Drive sync."""
    if current_store().db_exists():
        return redirect(url_for("settings"))
    return render_template("setup.html")


@app.route("/db/upload", methods=["POST"])
def upload_db():
    """Install an uploaded .mmbak as the working database (first run, or a
    replacement from Settings - the old one is backed up first)."""
    store = current_store()
    first_run = not store.db_exists()
    f = request.files.get("db_file")
    if not f or not f.filename:
        flash("Choose a .mmbak file to upload.", "error")
        return redirect(url_for("setup" if first_run else "settings"))
    staged = store.staging_path()
    f.save(staged)
    try:
        store.install_db(staged, "manual", f.filename)
    except UnsupportedDatabase as e:
        flash(f"{os.path.basename(f.filename)} was not installed: {e}", "error")
        return redirect(url_for("setup" if first_run else "settings"))
    flash(f"Installed {os.path.basename(f.filename)}.", "ok")
    return redirect(url_for("index" if first_run else "settings"))


# Where Google sends the browser after consent when this app isn't being
# reached via localhost (a Desktop-app OAuth client only accepts loopback
# redirects). That page won't load, so the user pastes its URL back to us.
PASTE_REDIRECT_URI = "http://localhost:5732/setup/gdrive/callback"


def _gdrive_redirect_uri():
    if request.host.split(":")[0] in ("localhost", "127.0.0.1"):
        return url_for("gdrive_callback", _external=True)
    return PASTE_REDIRECT_URI


@app.route("/setup/gdrive", methods=["GET", "POST"])
def gdrive_setup():
    """Guide + form for the user's own Google OAuth client; POST saves it
    and starts the Google sign-in."""
    if request.method == "POST":
        try:
            gdrive.save_credentials(
                current_store(),
                request.form.get("client_id", ""),
                request.form.get("client_secret", ""),
                request.form.get("folder_name", ""),
                write=request.form.get("write") == "1",
            )
        except gdrive.GDriveError as e:
            flash(str(e), "error")
            return redirect(url_for("gdrive_setup"))
        session["gdrive_state"] = secrets.token_urlsafe(24)
        session["gdrive_redirect_uri"] = _gdrive_redirect_uri()
        return redirect(gdrive.auth_url(current_store(), session["gdrive_redirect_uri"], session["gdrive_state"]))
    store = current_store()
    creds = gdrive.settings(store)
    return render_template(
        "gdrive_setup.html",
        saved_client_id=creds.get("client_id", ""),
        has_secret=bool(creds.get("client_secret")),
        folder_name=gdrive.folder_name(store),
        write=gdrive.wants_write(store),
        connected=gdrive.is_connected(store),
        first_run=not store.db_exists(),
        redirect_uri=session.get("gdrive_redirect_uri"),
    )


def _run_sync(fn, *args, **kwargs):
    """Run a dbsync step and flash its outcome; returns the Result, or None
    if it failed."""
    try:
        result = fn(current_store(), *args, **kwargs)
    except (gdrive.GDriveError, UnsupportedDatabase, dbsync.SyncError) as e:
        flash(f"Sync failed: {e}", "error")
        return None
    if result.message:
        flash(result.message, result.level)
    return result


def _after_sync(result):
    if result and result.status in ("review", "stale"):
        return redirect(url_for("sync_review"))
    return redirect(url_for("settings"))


def _sync_and_flash(replace=False):
    return _run_sync(dbsync.sync_gdrive, replace=replace)


def _finish_gdrive_auth(code, state):
    expected = session.pop("gdrive_state", None)
    redirect_uri = session.pop("gdrive_redirect_uri", None)
    if not expected or not redirect_uri or (state is not None and state != expected):
        flash("That Google sign-in didn't match one started here. Please start again.", "error")
        return redirect(url_for("gdrive_setup"))
    try:
        gdrive.exchange_code(current_store(), code, redirect_uri)
        gdrive.check_connection(current_store())
    except gdrive.GDriveError as e:
        flash(str(e), "error")
        return redirect(url_for("gdrive_setup"))
    first_run = not current_store().db_exists()
    flash("Connected to Google Drive.", "ok")
    if first_run:
        _sync_and_flash()
    return redirect(url_for("index" if first_run and current_store().db_exists() else "settings"))


@app.route("/setup/gdrive/callback")
def gdrive_callback():
    if request.args.get("error"):
        flash(f"Google sign-in was refused: {request.args['error']}.", "error")
        return redirect(url_for("gdrive_setup"))
    return _finish_gdrive_auth(request.args.get("code", ""), request.args.get("state"))


@app.route("/setup/gdrive/paste", methods=["POST"])
def gdrive_paste():
    try:
        code, state = gdrive.parse_redirect(request.form.get("redirect_url", ""))
    except gdrive.GDriveError as e:
        flash(str(e), "error")
        return redirect(url_for("gdrive_setup"))
    return _finish_gdrive_auth(code, state)


@app.route("/db/sync", methods=["POST"])
def sync_db():
    """Sync with Google Drive (replace=1: install the newest snapshot as-is,
    discarding local changes)."""
    if not gdrive.is_connected(current_store()):
        flash("Google Drive isn't connected.", "error")
        return redirect(url_for("settings"))
    return _after_sync(_sync_and_flash(replace=request.form.get("replace") == "1"))


@app.route("/db/sync/upload", methods=["POST"])
def sync_upload():
    """Merge an uploaded export into the working database."""
    store = current_store()
    f = request.files.get("db_file")
    if not f or not f.filename:
        flash("Choose a .mmbak file to merge.", "error")
        return redirect(url_for("settings"))
    staged = store.staging_path()
    f.save(staged)
    return _after_sync(_run_sync(dbsync.sync_file, staged, f.filename))


@app.route("/sync")
def sync_review():
    """Review a pending merge: what each side changed, and any conflicts,
    likely duplicates or problems."""
    view = dbsync.review(current_store())
    if view is None:
        return redirect(url_for("settings"))
    return render_template("sync.html", v=view)


@app.route("/sync/apply", methods=["POST"])
def sync_apply():
    decisions = dbsync.decisions_from_form(request.form)
    result = _run_sync(dbsync.apply, decisions, request.form.get("fingerprint", ""))
    if result and result.status == "review":
        # Re-show the review with the choices made so far.
        view = dbsync.review(current_store(), decisions)
        if view is not None:
            return render_template("sync.html", v=view), 409
    return _after_sync(result)


@app.route("/sync/cancel", methods=["POST"])
def sync_cancel():
    dbsync.cancel(current_store())
    flash("Sync cancelled. Nothing was changed.", "ok")
    return redirect(url_for("settings"))


@app.route("/sync/replace", methods=["POST"])
def sync_replace():
    return _after_sync(_run_sync(dbsync.replace_with_incoming))


@app.route("/db/download")
def download_db():
    """The working database as a .mmbak to restore in the app."""
    store = current_store()
    path = dbsync.download_copy(store)
    try:
        with open(path, "rb") as f:
            data = io.BytesIO(f.read())
    finally:
        os.remove(path)
    return send_file(data, as_attachment=True, download_name=dbsync.download_name(store),
                     mimetype="application/octet-stream")


@app.route("/gdrive/disconnect", methods=["POST"])
def gdrive_disconnect():
    gdrive.disconnect(current_store())
    flash("Disconnected from Google Drive. The current database was kept.", "ok")
    return redirect(url_for("settings"))


# A short curated list; "" means "follow the browser's own locale".
LOCALE_OPTIONS = [
    ("", "Auto (use browser locale)"),
    ("en-GB", "English (UK) — 31/12/2026, 24h, 1,234.56"),
    ("en-US", "English (US) — 12/31/2026, 12h, 1,234.56"),
    ("de-DE", "German — 31.12.2026, 24h, 1.234,56"),
    ("bg-BG", "Bulgarian — 31.12.2026, 24h, 1234,56"),
    ("sv-SE", "Swedish — 2026-12-31, 24h, 1 234,56"),
    ("fr-FR", "French — 31/12/2026, 24h, 1 234,56"),
    ("ja-JP", "Japanese — 2026/12/31, 24h, 1,234.56"),
]


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        locale = request.form.get("locale", "").strip()
        if locale in dict(LOCALE_OPTIONS):
            current_store().save_config({"locale": locale})
        return redirect(url_for("settings"))
    return _render_settings()


def _render_settings(new_token=None):
    store = current_store()
    exists = store.db_exists()
    info = store.db_info()
    db = {
        "exists": exists,
        "source": store.load_config().get("db_source", ""),
        "name": info.get("name", ""),
        "installed_at": info.get("installed_at", ""),
        "synced_at": info.get("synced_at", ""),
        "size": os.path.getsize(store.db_path) if exists else 0,
        "mtime": current_db_mtime(),
        "unsynced": exists and store.unsynced(),
        "backups": store.backup_count(),
        "latest_backup": store.latest_backup(),
        "backup_dir": store.backup_dir,
    }
    return render_template(
        "settings.html",
        locale_options=LOCALE_OPTIONS,
        current_locale=current_locale(),
        db=db,
        backup_cfg=store.backup_settings(),
        has_password=bool(users.get(g.user).get("password_hash")),
        gdrive_connected=gdrive.is_connected(store),
        gdrive_can_write=gdrive.can_write(store),
        gdrive_wants_write=gdrive.wants_write(store),
        gdrive_folder=gdrive.folder_name(store),
        api_tokens=sorted(users.tokens(g.user).items(), key=lambda kv: kv[1].get("created", "")),
        new_token=new_token,
    )


@app.route("/settings/api-tokens", methods=["POST"])
def create_api_token():
    """Make a token and show it once, in this response: it isn't stored
    anywhere it could be shown again (not even the session cookie)."""
    try:
        token = users.create_token(g.user, request.form.get("name"), write=request.form.get("write") == "1")
    except users.UserError as e:
        flash(str(e), "error")
        return redirect(url_for("settings"))
    return _render_settings(new_token=token)


@app.route("/settings/api-tokens/<tid>/revoke", methods=["POST"])
def revoke_api_token(tid):
    try:
        users.revoke_token(g.user, tid)
    except users.UserError as e:
        flash(str(e), "error")
    else:
        flash("Token revoked.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/backups", methods=["POST"])
def backup_settings():
    try:
        keep = int(request.form.get("keep", ""))
    except ValueError:
        keep = -1
    if not 0 <= keep <= 1000:
        flash("Backups to keep must be a whole number from 0 to 1000.", "error")
        return redirect(url_for("settings"))
    current_store().save_backup_settings(
        keep=keep,
        on_first_write=request.form.get("on_first_write") == "1",
        diff=request.form.get("diff") == "1",
    )
    flash("Backup settings saved.", "ok")
    return redirect(url_for("settings"))


@app.route("/db/backup", methods=["POST"])
def backup_now():
    folder = current_store().backup()
    flash(f"Backed up to {os.path.basename(folder)}.", "ok")
    return redirect(url_for("settings"))


@app.route("/api/transactions/uids")
def api_transaction_uids():
    """All INOUTCOME.uid values matching the current /editor/transactions filters,
    across every page -- used by "select all matching rows" in the bulk
    editor, as opposed to just the rows rendered on the current page."""
    with read_db() as con:
        uids = reads.matching_uids(con, reads.parse_transaction_filters(con, request.args))
    return jsonify(uids=uids)


@app.route("/api/transactions/group-stats")
def api_transaction_group_stats():
    """Grouping stats (same-account rows within N seconds of each other)
    over every matching transaction, not just whatever page is loaded."""
    try:
        seconds = max(1, int(request.args.get("seconds", 60)))
    except ValueError:
        seconds = 60
    with read_db() as con:
        stats = reads.group_stats(con, reads.parse_transaction_filters(con, request.args), seconds)
    return jsonify(**stats)


@app.route("/editor")
def editor():
    return redirect(url_for("transactions"))


# The editor's pages were at the top level until the app took /.
@app.route("/transactions")
@app.route("/accounts")
@app.route("/categories")
def old_editor_page():
    query = request.query_string.decode()
    return redirect(f"/editor{request.path}" + (f"?{query}" if query else ""), 301)


def group_accounts(rows):
    """[(group name, [row, ...]), ...] for account rows, which arrive in the
    app's group order (ASSETGROUP.ORDERSEQ). A plain consecutive grouping
    keeps that order, unlike Jinja's `groupby` filter, which would silently
    re-sort groups alphabetically."""
    return [(k, list(g)) for k, g in itertools.groupby(rows, key=lambda r: r["ACC_GROUP_NAME"])]


@app.route("/editor/transactions")
def transactions():
    page_size_options = [25, 50, 100, 250, 500, 1000]
    try:
        page_size = int(request.args.get("page_size", 100))
    except ValueError:
        page_size = 100
    if page_size not in page_size_options:
        page_size = 100
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    sort_by = request.args.get("sort", "date")
    if sort_by not in reads.TX_SORT_OPTIONS:
        sort_by = "date"
    sort_dir = request.args.get("dir", "desc")
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    with read_db() as con:
        f = reads.parse_transaction_filters(con, request.args)
        total_rows = reads.count_transactions(con, f)
        total_pages = max(1, (total_rows + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = reads.list_transactions(con, f, sort_by, sort_dir, page_size, (page - 1) * page_size)
        facets = reads.transaction_facets(con, f)
        lookups = reads.category_lookups(con)
        category_trees = {0: reads.build_category_tree(con, 0), 1: reads.build_category_tree(con, 1)}
        accounts = reads.get_accounts(con)
        currencies = reads.get_used_currencies(con)
        tx_years = reads.get_transaction_years(con)
        note_suggestions = reads.get_suggestions(con, "note")
        desc_suggestions = reads.get_suggestions(con, "description")

    enriched = []
    for r in rows:
        d = dict(r)
        tree_type = 0 if d["DO_TYPE"] == "0" else 1
        d["tree_type"] = tree_type
        d["root_uid"], d["category_path"] = reads.category_path(lookups[tree_type], d["ctgUid"])
        d["editable_category"] = d["DO_TYPE"] in ("0", "1")
        enriched.append(d)

    return render_template(
        "transactions.html",
        rows=enriched,
        accounts=accounts,
        accounts_grouped=group_accounts(accounts),
        accounts_json=[dict(a) for a in accounts],
        category_trees=category_trees,
        currencies=currencies,
        tx_years=tx_years,
        filters=dict(account=f["account_uids"], category=f["category_uids"], category_only=f["category_only_uids"],
                     type=f["do_types"], date_from=f["date_from"], date_to=f["date_to"],
                     q_note=f["q_note"], q_desc=f["q_desc"],
                     has_note=f["has_note"], has_desc=f["has_desc"],
                     amount_min=f["amount_min"], amount_max=f["amount_max"],
                     entered_currency=f["entered_currency"],
                     show_deleted=f["show_deleted"], show_mirror=f["show_mirror"],
                     sort=sort_by, dir=sort_dir),
        note_suggestions=note_suggestions,
        desc_suggestions=desc_suggestions,
        deleted_count=facets["deleted"], mirror_count=facets["mirror"],
        note_counts=facets["note"], desc_counts=facets["description"],
        account_counts=facets["account"], category_counts=facets["category"],
        category_counts_own=facets["category_own"], type_counts=facets["type"],
        sort_options=list(reads.TX_SORT_OPTIONS),
        page=page,
        total_pages=total_pages,
        total_rows=total_rows,
        page_size=page_size,
        page_size_options=page_size_options,
    )


@app.route("/editor/accounts")
def accounts():
    with read_db() as con:
        rows = reads.get_account_rows(con)
        groups = reads.get_asset_groups(con)
    return render_template("accounts.html", grouped_rows=group_accounts(rows), groups=groups)


@app.route("/editor/categories")
def categories():
    with read_db() as con:
        income = reads.get_categories(con, 0)
        expense = reads.get_categories(con, 1)
        totals = reads.get_category_totals(con)

    def build_tree(rows):
        roots = [r for r in rows if r["STATUS"] == 0]
        children = [r for r in rows if r["STATUS"] == 2]
        return [(r, [c for c in children if c["pUid"] == r["uid"]]) for r in roots]

    return render_template(
        "categories.html",
        income_tree=build_tree(income),
        expense_tree=build_tree(expense),
        totals=totals,
    )


# ---------------------------------------------------------------------------
# Routes - inline-edit API
# ---------------------------------------------------------------------------

# The pages' inline edits name MM columns (base.html keys staged edits by
# them); these map each to its field in edits.py.
TRANSACTION_FIELDS = {"AMOUNT_ACCOUNT": "amount", "ZCONTENT": "note", "ZDATA": "description",
                      "ctgUid": "category", "assetUid": "account"}
ACCOUNT_FIELDS = {"NIC_NAME": "name", "groupUid": "group", "ORDERSEQ": "order", "ZDATA": "status"}
CATEGORY_FIELDS = {"NAME": "name", "ORDERSEQ": "order"}


class ApiError(edits.EditError):
    """A bad API request: answered as {"ok": false, "error": message, **extra},
    like an edit edits.py refuses."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message, status)
        self.extra = extra


@app.errorhandler(edits.EditError)
def _api_error(e):
    return jsonify(ok=False, error=e.message, **getattr(e, "extra", {})), e.status


@app.errorhandler(404)
@app.errorhandler(405)
def _api_http_error(e):
    if not _wants_json():
        return e
    resp = jsonify(ok=False, error=e.description)
    resp.status_code = e.code
    if getattr(e, "valid_methods", None):
        resp.headers["Allow"] = ", ".join(e.valid_methods)
    return resp


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("send a JSON object")
    return data


def _page_change(fields):
    """The {field: value} change a page's {"field": <column>, "value": ...}
    body asks for, with fields mapping the columns it may name."""
    data = _json_body()
    field = data.get("field")
    if not isinstance(field, str) or field not in fields:
        raise ApiError("field not allowed")
    return {fields[field]: data.get("value", "")}


@app.route("/api/transactions", methods=["POST"])
def api_add_transaction():
    with write_db() as con:
        uid = edits.create_transaction(con, _json_body())
    return jsonify(ok=True, uid=uid, mtime=current_db_mtime())


@app.route("/api/transactions/<uid>", methods=["PATCH"])
def api_edit_transaction(uid):
    change = _page_change(TRANSACTION_FIELDS)
    with write_db() as con:
        edits.update_transaction(con, uid, change)
    return jsonify(ok=True, mtime=current_db_mtime())


@app.route("/api/transactions/<uid>/datetime", methods=["PATCH"])
def api_edit_transaction_datetime(uid):
    data = _json_body()
    with write_db() as con:
        edits.update_transaction(con, uid, {"date": data.get("date", ""), "time": data.get("time", "")})
    return jsonify(ok=True, mtime=current_db_mtime())


@app.route("/api/transactions/<uid>", methods=["DELETE"])
def api_delete_transaction(uid):
    with write_db() as con:
        deleted = edits.delete_transaction(con, uid)
    return jsonify(ok=True, deleted=deleted, mtime=current_db_mtime())


@app.route("/api/accounts/<uid>", methods=["PATCH"])
def api_edit_account(uid):
    change = _page_change(ACCOUNT_FIELDS)
    with write_db() as con:
        edits.update_account(con, uid, change)
    return jsonify(ok=True, mtime=current_db_mtime())


@app.route("/api/categories/<uid>/<int:type_>", methods=["PATCH"])
def api_edit_category(uid, type_):
    change = _page_change(CATEGORY_FIELDS)
    with write_db() as con:
        edits.update_category(con, uid, type_, change)
    return jsonify(ok=True, mtime=current_db_mtime())


# ---------------------------------------------------------------------------
# Routes - the app's JSON (/api/app/)
# ---------------------------------------------------------------------------

# Private like the editor's /api/ helpers: shaped for the app's screens
# (templates/app.html, static/app/) and free to change with them.

def _app_currencies(con):
    """({uid: {iso, symbol, decimals, rate}}, the main currency's uid)."""
    out, main = {}, None
    for r in reads.get_currencies(con):
        decimals = _num_or_none(r["DECIMAL_POINT"], int)
        out[r["uid"]] = {"iso": r["ISO"], "symbol": r["SYMBOL"] or r["ISO"], "order": _num_or_none(r["ORDER_SEQ"], int),
                         "decimals": 2 if decimals is None else decimals, "rate": _num_or_none(r["RATE"]) or 1.0}
        if str(r["IS_MAIN_CURRENCY"]) == "1":
            main = r["uid"]
    return out, main or next((u for u, c in out.items() if c["rate"] == 1.0), None)


def _in_main(r, currencies, decimals):
    """A reads._TX_SELECT row's amount in the main currency, rounded to its
    decimals, as the app adds up income and expense (see reads.sums())."""
    rate = currencies.get(r["account_currency_uid"], {}).get("rate", 1.0)
    return round((_num_or_none(r["AMOUNT_ACCOUNT"]) or 0.0) * rate, decimals)


def _category_names(lookup, uid):
    """(root's name, child's name or "") of a category in a
    reads.category_lookup() tree; ("", "") if it isn't there."""
    cat = lookup.get(uid)
    if not cat:
        return "", ""
    if cat["status"] == 0:
        return cat["name"], ""
    return lookup.get(cat["parent_uid"], {}).get("name", ""), cat["name"]


def _app_row(r, lookups, currencies):
    """A reads._TX_SELECT row as the app's lists show it: the amount as
    entered, in the currency it was entered in."""
    category, subcategory = _category_names(lookups[0 if r["DO_TYPE"] == "0" else 1], r["ctgUid"])
    amount, currency = _num_or_none(r["IN_ZMONEY"]), r["currency_uid"]
    if amount is None or currency not in currencies:
        amount, currency = _num_or_none(r["AMOUNT_ACCOUNT"]) or 0.0, r["account_currency_uid"]
    return {
        "uid": r["uid"], "type": r["DO_TYPE"], "date": r["WDATE"], "time": (r["tx_time"] or "")[:5],
        "amount": amount, "currency": currency, "category": category, "subcategory": subcategory,
        "account": r["account_name"] or "", "to_account": r["to_account_name"] or "",
        "note": r["ZCONTENT"] or "", "description": r["ZDATA"] or "",
    }


@app.route("/api/app/meta")
def app_meta():
    """What the app's screens look things up in: the currencies, the
    accounts (status: edits.ACCOUNT_STATUSES; hidden: deleted or hidden in
    the app, left out of pickers), the account groups, both category trees
    and the app's week_start (0 Sunday .. 6 Saturday)."""
    with read_db() as con:
        currencies, main = _app_currencies(con)
        accounts = reads.get_accounts(con)
        groups = reads.get_asset_groups(con)
        income, expense = reads.build_category_tree(con, 0), reads.build_category_tree(con, 1)
        settings = reads.get_settings(con)
    week_start = _num_or_none(settings.get("week_start_day"), int)
    return jsonify(ok=True, currencies=currencies, main_currency=main, categories={"income": income, "expense": expense},
                   week_start=week_start if week_start in range(7) else 1,
                   groups=[{"uid": g["uid"], "name": g["ACC_GROUP_NAME"] or ""} for g in groups],
                   accounts=[{"uid": a["uid"], "name": a["NIC_NAME"], "group": a["ACC_GROUP_NAME"] or "",
                              "group_uid": a["group_uid"], "currency": a["currencyUid"],
                              "status": str(a["account_flags"] or "0"),
                              "hidden": str(a["account_flags"]) in ("1", "3")}
                             for a in accounts])


@app.route("/api/app/days")
def app_days():
    """The transactions dated from..to (YYYY-MM-DD; the other /editor/transactions
    filters apply too), newest first and grouped by day, each day with its
    income and expense in the main currency. Balance adjustments count in
    neither, as in the app's own statistics."""
    for name in ("from", "to"):
        try:
            datetime.strptime(request.args.get(name, ""), "%Y-%m-%d")
        except ValueError:
            raise ApiError(f"{name} must be a date (YYYY-MM-DD)")
    with read_db() as con:
        currencies, main = _app_currencies(con)
        lookups = reads.category_lookups(con)
        rows = reads.list_transactions(con, reads.parse_transaction_filters(con, request.args), limit=None)
    decimals = currencies.get(main, {}).get("decimals", 2)
    days = []
    for r in rows:
        if not days or days[-1]["date"] != r["WDATE"]:
            days.append({"date": r["WDATE"], "income": 0.0, "expense": 0.0, "rows": []})
        day = days[-1]
        if r["DO_TYPE"] in ("0", "1"):
            day["income" if r["DO_TYPE"] == "0" else "expense"] += _in_main(r, currencies, decimals)
        day["rows"].append(_app_row(r, lookups, currencies))
    # Not rounded to the currency's decimals, see app_sums().
    for day in days:
        day["income"], day["expense"] = round(day["income"], 6), round(day["expense"], 6)
    return jsonify(ok=True, days=days)


def _app_filters(con, **extra):
    """The /editor/transactions filters in the request, with extra ones on top."""
    args = MultiDict(request.args)
    for k, v in extra.items():
        args.setlist(k, v if isinstance(v, list) else [v])
    return reads.parse_transaction_filters(con, args)


def _check_dates(*names):
    for name in names:
        try:
            datetime.strptime(request.args.get(name, ""), "%Y-%m-%d")
        except ValueError:
            raise ApiError(f"{name} must be a date (YYYY-MM-DD)")


@app.route("/api/app/sums")
def app_sums():
    """Income and expense per day, from..to, in the main currency."""
    _check_dates("from", "to")
    with read_db() as con:
        rows = reads.sums(con, _app_filters(con, type=["0", "1"]), "date")
    days = {}
    for r in rows:
        days.setdefault(r["key"], [0.0, 0.0])[r["DO_TYPE"] == "1"] += r["in_main"] or 0.0
    # Not rounded to the currency's decimals: restated amounts carry
    # fractions of a cent, which would drift once a year of days is added up.
    return jsonify(ok=True, days=[{"date": k, "income": round(v[0], 6), "expense": round(v[1], 6)}
                                  for k, v in sorted(days.items())])


@app.route("/api/app/stats")
def app_stats():
    """Income and expense, from..to, per category in the main currency:
    each root with its total and its children's, largest first. A root's own
    rows (not in a child) show as a child called Other, as in the app."""
    _check_dates("from", "to")
    with read_db() as con:
        currencies, main = _app_currencies(con)
        lookups = reads.category_lookups(con)
        rows = reads.sums(con, _app_filters(con, type=["0", "1"]), "category")
    decimals = currencies.get(main, {}).get("decimals", 2)
    out = {}
    for tree, name in ((0, "income"), (1, "expense")):
        roots = {}
        for r in rows:
            if r["DO_TYPE"] != str(tree):
                continue
            amount = r["in_main"] or 0.0
            cat = lookups[tree].get(r["key"])
            root_uid = r["key"] if not cat or cat["status"] == 0 else cat["parent_uid"]
            root = roots.setdefault(root_uid, {"uid": root_uid, "amount": 0.0, "children": {},
                                               "name": lookups[tree].get(root_uid, {}).get("name", "")})
            root["amount"] += amount
            own = not cat or cat["status"] == 0
            child = root["children"].setdefault(r["key"], {"uid": r["key"], "amount": 0.0,
                                                           "name": "Other" if own else cat["name"]})
            child["amount"] += amount
        cats = sorted(roots.values(), key=lambda c: -c["amount"])
        for c in cats:
            c["amount"] = round(c["amount"], decimals)
            c["children"] = sorted(({**k, "amount": round(k["amount"], decimals)} for k in c["children"].values()),
                                   key=lambda k: -k["amount"])
        out[name] = {"total": round(sum(c["amount"] for c in cats), decimals), "categories": cats}
    return jsonify(ok=True, **out)


# What a row does to its account's balance, by DO_TYPE: income, adjustments
# up and a transfer's receiving leg add, the rest subtract.
_BALANCE_SIGN = {"0": 1, "7": 1, "4": 1, "1": -1, "8": -1, "3": -1}


@app.route("/api/app/accounts")
def app_accounts():
    """The accounts by group with their balances in their own currency, and
    each group's and all accounts' totals in the main currency. Deleted
    accounts are left out; hidden ones count in the totals but aren't listed."""
    with read_db() as con:
        currencies, main = _app_currencies(con)
        rows = reads.get_account_rows(con)
    decimals = currencies.get(main, {}).get("decimals", 2)
    groups, assets, liabilities = [], 0.0, 0.0
    for r in rows:
        status = str(r["account_flags"])
        if status == "1":
            continue
        balance = r["balance"] or 0.0
        in_main = balance * currencies.get(r["currency_uid"], {}).get("rate", 1.0)
        if in_main >= 0:
            assets += in_main
        else:
            liabilities -= in_main
        if not groups or groups[-1]["uid"] != r["group_uid"]:
            groups.append({"uid": r["group_uid"], "name": r["ACC_GROUP_NAME"] or "", "total": 0.0, "accounts": []})
        groups[-1]["total"] += in_main
        if status != "3":
            groups[-1]["accounts"].append({"uid": r["uid"], "name": r["NIC_NAME"], "balance": balance,
                                           "currency": r["currency_uid"]})
    for grp in groups:
        grp["total"] = round(grp["total"], decimals)
    return jsonify(ok=True, assets=round(assets, decimals), liabilities=round(liabilities, decimals), groups=groups)


def _opening_balance(con, uid, before):
    """An account's balance at the start of the day `before` (YYYY-MM-DD)."""
    day_before = (datetime.strptime(before, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    f = reads.parse_transaction_filters(con, MultiDict([("account", uid), ("to", day_before), ("show_mirror", "show")]))
    return sum(_BALANCE_SIGN.get(r["DO_TYPE"], 0) * (r["total"] or 0.0) for r in reads.sums(con, f, "account"))


@app.route("/api/app/accounts/<uid>")
def app_account(uid):
    """One account from..to, in its own currency: its balance before from
    and at the end, and its rows (a transfer's receiving leg too) by day,
    newest first, each row with the balance after it. With sums=1, only
    each day's deposits and withdrawals."""
    _check_dates("from", "to")
    sums_only = request.args.get("sums") == "1"
    with read_db() as con:
        currencies, _ = _app_currencies(con)
        account = next((a for a in reads.get_accounts(con) if a["uid"] == uid), None)
        if not account:
            raise ApiError("not found", 404)
        opening = _opening_balance(con, uid, request.args["from"])
        f = _app_filters(con, account=uid, show_mirror="show", show_deleted="hide")
        if sums_only:
            rows = reads.sums(con, f, "date")
        else:
            lookups = reads.category_lookups(con)
            rows = reads.list_transactions(con, f, sort="date", direction="asc", limit=None)
    days, balance = {}, opening
    for r in rows:
        signed = _BALANCE_SIGN.get(r["DO_TYPE"], 0) * (_num_or_none(r["total" if sums_only else "AMOUNT_ACCOUNT"]) or 0.0)
        balance += signed
        day = days.setdefault(r["key" if sums_only else "WDATE"], {"deposit": 0.0, "withdrawal": 0.0, "rows": []})
        day["deposit" if signed >= 0 else "withdrawal"] += abs(signed)
        if not sums_only:
            day["rows"].insert(0, {**_app_row(r, lookups, currencies), "amount": abs(signed),
                                   "currency": account["currencyUid"], "balance": round(balance, 6)})
    # Not rounded to the currency's decimals, see app_sums().
    out = []
    for date, day in sorted(days.items(), reverse=True):
        day.update(deposit=round(day["deposit"], 6), withdrawal=round(day["withdrawal"], 6))
        if sums_only:
            del day["rows"]
        out.append({"date": date, **day})
    return jsonify(ok=True, name=account["NIC_NAME"], currency=account["currencyUid"],
                   opening=round(opening, 6), closing=round(balance, 6), days=out)


@app.route("/api/app/transactions/<uid>")
def app_transaction(uid):
    """One transaction as the edit form shows it: its amount in its
    account's currency, and as entered (entered_currency: the currency's
    uid)."""
    with read_db() as con:
        r = reads.get_transaction(con, uid)
    if not r:
        raise ApiError("not found", 404)
    return jsonify(ok=True, transaction={
        "uid": r["uid"], "type": r["DO_TYPE"], "date": r["WDATE"], "time": (r["tx_time"] or "")[:5],
        "account": r["account_uid"], "to_account": r["to_account_uid"], "category": r["ctgUid"] or "",
        "amount": _num_or_none(r["AMOUNT_ACCOUNT"]), "currency": r["account_currency_uid"],
        "entered_amount": _num_or_none(r["IN_ZMONEY"]), "entered_currency": r["currency_uid"],
        "note": r["ZCONTENT"] or "", "description": r["ZDATA"] or "",
    })


@app.route("/api/app/networth")
def app_networth():
    """All accounts' total (deleted ones aside) in the main currency at the
    start of from, and how it changed each day to to, for the Accounts
    chart. Balances add up raw amounts, as the account list's do."""
    _check_dates("from", "to")
    start = datetime.strptime(request.args["from"], "%Y-%m-%d")
    with read_db() as con:
        currencies, _ = _app_currencies(con)
        live = [("account", a["uid"]) for a in reads.get_accounts(con) if str(a["account_flags"]) != "1"]
        if not live:
            return jsonify(ok=True, opening=0.0, days=[])
        before = reads.sums(con, reads.parse_transaction_filters(con, MultiDict(
            [("to", (start - timedelta(days=1)).strftime("%Y-%m-%d")), ("show_mirror", "show"), *live])), "account")
        during = reads.sums(con, reads.parse_transaction_filters(con, MultiDict(
            [("from", request.args["from"]), ("to", request.args["to"]), ("show_mirror", "show"), *live])), "date")

    def signed(r):
        return (_BALANCE_SIGN.get(r["DO_TYPE"], 0) * (r["total"] or 0.0)
                * currencies.get(r["currency_uid"], {}).get("rate", 1.0))

    days = {}
    for r in during:
        days[r["key"]] = days.get(r["key"], 0.0) + signed(r)
    return jsonify(ok=True, opening=round(sum(signed(r) for r in before), 6),
                   days=[{"date": k, "change": round(v, 6)} for k, v in sorted(days.items())])


@app.route("/api/app/accounts", methods=["POST"])
def app_add_account():
    """An account: name, group and currency, as edits.create_account takes them."""
    with write_db() as con:
        uid = edits.create_account(con, _json_body())
    return jsonify(ok=True, uid=uid)


@app.route("/api/app/accounts/<uid>", methods=["PATCH"])
def app_edit_account(uid):
    """Any of edits.ACCOUNT_FIELDS (status 3 hides it, 1 deletes it), or
    move: -1 or 1 to move it up or down in its group."""
    data = dict(_json_body())
    move = data.pop("move", None)
    with write_db() as con:
        if move is not None:
            edits.move_account(con, uid, move)
        if data:
            edits.update_account(con, uid, data)
    return jsonify(ok=True)


@app.route("/api/app/categories/<int:tree>", methods=["POST"])
def app_add_category(tree):
    """A category in a tree (0 income, 1 expense): name, and parent for a child."""
    with write_db() as con:
        uid = edits.create_category(con, tree, _json_body())
    return jsonify(ok=True, uid=uid)


@app.route("/api/app/categories/<int:tree>/<uid>", methods=["PATCH"])
def app_edit_category(tree, uid):
    """A new name, or move: -1 or 1 to move it up or down among its siblings."""
    data = dict(_json_body())
    move = data.pop("move", None)
    with write_db() as con:
        if move is not None:
            edits.move_category(con, uid, tree, move)
        if data:
            edits.update_category(con, uid, tree, data)
    return jsonify(ok=True)


@app.route("/api/app/categories/<int:tree>/<uid>", methods=["DELETE"])
def app_delete_category(tree, uid):
    with write_db() as con:
        edits.delete_category(con, uid, tree)
    return jsonify(ok=True)


@app.route("/api/app/bookmarks")
def app_bookmarks():
    """The app's bookmarks, in the fields the add form takes."""
    with read_db() as con:
        rows = reads.get_bookmarks(con)
    return jsonify(ok=True, bookmarks=[{
        "uid": r["uid"], "type": str(r["DO_TYPE"]), "account": r["assetUid"] or "", "to_account": r["toAssetUid"] or "",
        "category": r["ctgUid"] or "", "amount": _num_or_none(r["AMOUNT_SUB"]), "currency": r["currencyUid"] or "",
        "note": r["PAYEE"] or "", "description": r["MEMO"] or "",
    } for r in rows])


@app.route("/api/app/bookmarks", methods=["POST"])
def app_add_bookmark():
    with write_db() as con:
        uid = edits.create_bookmark(con, _json_body())
    return jsonify(ok=True, uid=uid)


@app.route("/api/app/bookmarks/<uid>", methods=["DELETE"])
def app_delete_bookmark(uid):
    with write_db() as con:
        edits.delete_bookmark(con, uid)
    return jsonify(ok=True)


@app.route("/api/app/suggestions")
def app_suggestions():
    """Notes used before, most recent first, for the form's note field."""
    with read_db() as con:
        notes = reads.get_suggestions(con, "note", limit=500)
    return jsonify(ok=True, notes=notes)


APP_SEARCH_LIMIT = 300


@app.route("/api/app/search")
def app_search():
    """Transactions with q in their note or description, under the
    /editor/transactions filters, newest first (the first APP_SEARCH_LIMIT), and
    the income, expense and transfer totals of all of them in the main
    currency."""
    with read_db() as con:
        currencies, _ = _app_currencies(con)
        lookups = reads.category_lookups(con)
        f = reads.narrow_by_text(reads.parse_transaction_filters(con, request.args), request.args.get("q", "").strip())
        count = reads.count_transactions(con, f)
        rows = reads.list_transactions(con, f, limit=APP_SEARCH_LIMIT)
        sums = reads.sums(con, f, "account")
    totals = {"0": 0.0, "1": 0.0, "3": 0.0}
    for r in sums:
        if r["DO_TYPE"] in totals:
            totals[r["DO_TYPE"]] += r["in_main"] or 0.0
    return jsonify(ok=True, count=count, rows=[_app_row(r, lookups, currencies) for r in rows],
                   income=round(totals["0"], 6), expense=round(totals["1"], 6), transfer=round(totals["3"], 6))


# Account groups of this ASSETGROUP.TYPE hold cash.
CASH_GROUP_TYPE = 11


@app.route("/api/app/total")
def app_total():
    """The Total tab for month (YYYY-MM): each expense budget in force with
    what's been spent against it (in the main currency; a root category's
    budget counts its children's rows, and a child's budget is listed under
    its root's), the month's expenses and the month before's, and the
    expenses paid from cash accounts."""
    try:
        first = datetime.strptime(request.args.get("month", "") + "-01", "%Y-%m-%d")
    except ValueError:
        raise ApiError("month must be YYYY-MM")
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    before = first - timedelta(days=1)

    def expenses(con, start, end, by):
        args = MultiDict([("from", start.strftime("%Y-%m-%d")), ("to", end.strftime("%Y-%m-%d")), ("type", "1")])
        return reads.sums(con, reads.parse_transaction_filters(con, args), by)

    with read_db() as con:
        lookup = reads.category_lookup(con, 1)
        tree = reads.build_category_tree(con, 1)
        budgets = reads.get_budgets(con, first.strftime("%Y%m"))
        by_category = expenses(con, first, last, "category")
        by_account = expenses(con, first, last, "account")
        previous = sum(r["in_main"] or 0.0 for r in expenses(con, before.replace(day=1), before, "account"))
        cash_groups = {g["uid"] for g in reads.get_asset_groups(con) if g["TYPE"] == CASH_GROUP_TYPE}
        cash_accounts = {a["uid"] for a in reads.get_account_rows(con) if a["group_uid"] in cash_groups}
    spent = {}
    for r in by_category:
        spent[r["key"]] = spent.get(r["key"], 0.0) + (r["in_main"] or 0.0)
    for uid, amount in list(spent.items()):
        cat = lookup.get(uid)
        if cat and cat["status"] != 0:
            spent[cat["parent_uid"]] = spent.get(cat["parent_uid"], 0.0) + amount
    total = sum(v for k, v in spent.items() if not (lookup.get(k) and lookup[k]["status"] != 0))
    out, roots = [], {}
    for b in budgets:
        if str(b["DO_TYPE"]) != "1" or b["amount"] is None:
            continue
        if b["is_total"] in (1, "1"):
            out.insert(0, {"name": "Total Budget", "amount": b["amount"], "spent": round(total, 6), "children": []})
            continue
        cat = lookup.get(b["category"])
        if not cat:
            continue
        item = {"uid": b["category"], "name": cat["name"], "amount": b["amount"],
                "spent": round(spent.get(b["category"], 0.0), 6), "children": []}
        if cat["status"] == 0:
            roots[b["category"]] = item
            out.append(item)
        elif cat["parent_uid"] in roots:
            roots[cat["parent_uid"]]["children"].append(item)
        else:
            out.append(item)
    # In the category tree's order, as the app lists them.
    order = {uid: i for i, uid in enumerate(u for r in tree for u in [r["uid"], *(c["uid"] for c in r["children"])])}
    out.sort(key=lambda b: -1 if "uid" not in b else order.get(b["uid"], len(order)))
    for b in out:
        b["children"].sort(key=lambda c: order.get(c["uid"], len(order)))
    cash = sum(r["in_main"] or 0.0 for r in by_account if r["key"] in cash_accounts)
    return jsonify(ok=True, budgets=out, expenses=round(total, 6), previous=round(previous, 6), cash=round(cash, 6))


@app.route("/api/app/transactions", methods=["POST"])
def app_add_transaction():
    """Fields as edits.create_transaction takes them."""
    with write_db() as con:
        uid = edits.create_transaction(con, _json_body())
    return jsonify(ok=True, uid=uid)


@app.route("/api/app/transactions/<uid>", methods=["PATCH"])
def app_edit_transaction(uid):
    """The fields that changed, as edits.update_transaction takes them."""
    with write_db() as con:
        edits.update_transaction(con, uid, _json_body())
    return jsonify(ok=True)


@app.route("/api/app/transactions/<uid>", methods=["DELETE"])
def app_delete_transaction(uid):
    with write_db() as con:
        edits.delete_transaction(con, uid)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes - public API (/api/v1/, see docs/openapi.yaml)
# ---------------------------------------------------------------------------

TX_TYPE_NAMES = {
    "0": "income", "1": "expense", "3": "transfer", "4": "transfer_mirror",
    "7": "balance_increase", "8": "balance_decrease",
}
TX_TYPE_CODES = {name: code for code, name in TX_TYPE_NAMES.items()}
ACCOUNT_STATUS_NAMES = {"0": "normal", "1": "deleted", "3": "hidden"}


def _num_or_none(v, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def _type_code(value):
    """A transaction type as a DO_TYPE code, from its name or its code."""
    return TX_TYPE_CODES.get(value, value) if isinstance(value, str) else None


def _int_arg(name, default, lo, hi=None):
    raw = request.args.get(name, "")
    if raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ApiError(f"{name} must be a whole number")
    if v < lo or (hi is not None and v > hi):
        raise ApiError(f"{name} must be from {lo}" + (f" to {hi}" if hi is not None else " up"))
    return v


def _v1_transaction(r, lookups):
    do_type = r["DO_TYPE"]
    _, path = reads.category_path(lookups[0 if do_type == "0" else 1], r["ctgUid"])
    return {
        "uid": r["uid"],
        "type": TX_TYPE_NAMES.get(do_type, do_type),
        "date": r["WDATE"],
        "time": r["tx_time"],
        "timestamp_ms": _num_or_none(r["ZDATE"], int),
        "amount": _num_or_none(r["AMOUNT_ACCOUNT"]),
        "currency": r["account_currency_iso"],
        "entered_amount": _num_or_none(r["IN_ZMONEY"]),
        "entered_currency": r["currency_iso"],
        "account_uid": r["account_uid"],
        "account": r["account_name"],
        "to_account_uid": r["to_account_uid"],
        "to_account": r["to_account_name"],
        "category_uid": r["ctgUid"] or None,
        "category": path or None,
        "note": r["ZCONTENT"] or "",
        "description": r["ZDATA"] or "",
        "transfer_id": r["txUidTrans"] or None,
        "deleted": r["IS_DEL"] not in (None, 0, "0", ""),
        "updated_ms": _num_or_none(r["changed_ms"], int),
        "app_updated_ms": _num_or_none(r["UTIME"], int),
    }


def _v1_get_transaction(con, uid):
    row = reads.get_transaction(con, uid)
    if not row:
        raise ApiError("not found", 404)
    return _v1_transaction(row, reads.category_lookups(con))


def _v1_get_transactions(con, uids):
    """{uid: transaction} for these uids; missing ones are left out."""
    lookups = reads.category_lookups(con)
    return {uid: _v1_transaction(r, lookups) for uid, r in reads.get_transactions(con, uids).items()}


def _same_value(have, want):
    if isinstance(have, bool) or isinstance(want, bool):
        return have is want
    if isinstance(have, (int, float)) and isinstance(want, (int, float)):
        return abs(have - want) < 1e-9
    return have == want


def _v1_check_expect(con, uid, expect, lookups):
    """Refuse (412) unless the transaction's fields, as GET shows them, have
    the values in expect. Read on con, so earlier changes in the same request
    count."""
    if not isinstance(expect, dict):
        raise ApiError("expect must be an object")
    row = reads.get_transaction(con, uid)
    if not row:
        raise ApiError("not found", 404)
    have = _v1_transaction(row, lookups)
    unknown = set(expect) - set(have)
    if unknown:
        raise ApiError(f"can't check: {', '.join(sorted(unknown))}")
    wrong = [f"{k} is {json.dumps(have[k], ensure_ascii=False)}, not {json.dumps(v, ensure_ascii=False)}"
             for k, v in expect.items() if not _same_value(have[k], v)]
    if wrong:
        raise ApiError("expect not met: " + "; ".join(wrong), 412)


def _v1_apply_change(con, uid, change, lookups):
    """One PATCH's change (its fields, plus expect) on con. Returns the
    transfer's other row's uid, or None."""
    data = dict(change)
    expect = data.pop("expect", None)
    if "type" in data:
        data["type"] = _type_code(data["type"])
    if expect is not None:
        _v1_check_expect(con, uid, expect, lookups)
    return edits.update_transaction(con, uid, data)


def _time_zone():
    """The server's local time zone, which dates and times are in: its IANA
    name where it can be found (TZ, else /etc/localtime's target), else the
    abbreviation, and the current offset from UTC."""
    now = datetime.now().astimezone()
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        try:
            target = os.path.realpath("/etc/localtime")
            name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
        except OSError:
            name = ""
    return {"name": name or now.tzname(), "utc_offset_minutes": int(now.utcoffset().total_seconds() // 60)}


@app.route("/api/v1/status")
def v1_status():
    store = current_store()
    return jsonify(
        ok=True, user=g.user, auth="token" if g.token else "proxy" if g.via_proxy else "session",
        can_write=bool(g.token["write"]) if g.token else True,
        database={"name": store.db_info().get("name"), "mtime": current_db_mtime()} if store.db_exists() else None,
        time_zone=_time_zone(),
    )


@app.route("/api/v1/accounts")
def v1_accounts():
    with read_db() as con:
        rows = reads.get_account_rows(con)
    return jsonify(ok=True, accounts=[{
        "uid": r["uid"],
        "name": r["NIC_NAME"],
        "group_uid": r["group_uid"],
        "group": r["ACC_GROUP_NAME"],
        "currency": r["ISO"],
        "balance": r["balance"] or 0,
        "transaction_count": r["tx_count"],
        "status": ACCOUNT_STATUS_NAMES.get(str(r["account_flags"]), "other"),
    } for r in rows])


@app.route("/api/v1/categories")
def v1_categories():
    show_deleted = request.args.get("show_deleted", "hide")
    if show_deleted not in ("hide", "show"):
        raise ApiError("show_deleted must be hide or show")
    deleted = show_deleted == "show"
    with read_db() as con:
        income, expense = reads.build_category_tree(con, 0, deleted), reads.build_category_tree(con, 1, deleted)
    return jsonify(ok=True, income=income, expense=expense)


@app.route("/api/v1/currencies")
def v1_currencies():
    with read_db() as con:
        rows = reads.get_currencies(con)
    return jsonify(ok=True, currencies=[{
        "uid": r["uid"], "iso": r["ISO"], "rate": _num_or_none(r["RATE"]),
        "decimals": _num_or_none(r["DECIMAL_POINT"], int),
    } for r in rows])


@app.route("/api/v1/transactions")
def v1_transactions():
    """The /editor/transactions filters, as the same query-string parameters, with
    limit/offset paging."""
    updated_since = _int_arg("updated_since", None, 0)
    limit = _int_arg("limit", 100, 1, 1000)
    offset = _int_arg("offset", 0, 0)
    sort_by = request.args.get("sort", "date")
    sort_dir = request.args.get("dir", "desc")
    if sort_by not in reads.TX_SORT_OPTIONS:
        raise ApiError(f"sort must be one of: {', '.join(reads.TX_SORT_OPTIONS)}")
    if sort_dir not in ("asc", "desc"):
        raise ApiError("dir must be asc or desc")
    with read_db() as con:
        f = reads.parse_transaction_filters(con, request.args)
        total = reads.count_transactions(con, f, updated_since)
        # The API's "updated" is when MMW saw a row change.
        order = "changed" if sort_by == "updated" else sort_by
        rows = reads.list_transactions(con, f, order, sort_dir, limit, offset, updated_since)
        lookups = reads.category_lookups(con)
    return jsonify(ok=True, total=total, limit=limit, offset=offset,
                   transactions=[_v1_transaction(r, lookups) for r in rows])


@app.route("/api/v1/transactions/<uid>")
def v1_transaction(uid):
    with read_db() as con:
        tx = _v1_get_transaction(con, uid)
    return jsonify(ok=True, transaction=tx)


def _v1_add(con, data, lookups=None):
    """One POST's transaction on con: the add form's fields, type may also
    be a name, and a missing date means now. Returns (its uid, the
    transfer's other row's uid or None)."""
    if not isinstance(data, dict):
        raise ApiError("each transaction must be an object")
    data = dict(data)
    data["type"] = _type_code(data.get("type"))
    if data["type"] == "4":
        raise ApiError("add a transfer as type transfer; its mirror row is made for you")
    if not data.get("date"):
        now = datetime.now()
        data["date"], data["time"] = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
    uid = edits.create_transaction(con, data)
    return uid, reads.transfer_other_uid(con, uid)


@app.route("/api/v1/transactions", methods=["POST"])
def v1_add_transaction():
    """One transaction (see _v1_add), or several as {"transactions": [...]},
    all or none like the batch PATCH. A transfer's other row comes back as
    mirror."""
    data = _json_body()
    if "transactions" in data:
        return jsonify(ok=True, results=_v1_batch(data["transactions"], "transactions", _v1_add)), 201
    with write_db() as con:
        uid, mirror_uid = _v1_add(con, data)
        out = {"ok": True, "transaction": _v1_get_transaction(con, uid)}
        if mirror_uid:
            out["mirror"] = _v1_get_transaction(con, mirror_uid)
    return jsonify(out), 201


@app.route("/api/v1/transactions/<uid>", methods=["PATCH"])
def v1_edit_transaction(uid):
    """Change any of edits.TRANSACTION_FIELDS in one go (see
    edits.update_transaction); type may also be a name, and expect holds
    field values the row must have first. A transfer's other row comes back
    as mirror."""
    with write_db() as con:
        mirror_uid = _v1_apply_change(con, uid, _json_body(), reads.category_lookups(con))
        out = {"ok": True, "transaction": _v1_get_transaction(con, uid)}
        if mirror_uid:
            out["mirror"] = _v1_get_transaction(con, mirror_uid)
    return jsonify(out)


V1_BATCH_LIMIT = 1000


def _batch_uid(item):
    return item.get("uid") if isinstance(item, dict) else None


def _v1_batch(items, name, apply, uid_of=lambda item: None):
    """Several writes as one request: apply(con, item, lookups) -> (uid,
    other transfer row's uid or None) for each item, in list order, and all
    of them or none. A refusal lists every item that failed, with its
    uid_of() and the status the single request would have answered. Returns
    the results, rows as the whole batch left them."""
    if not isinstance(items, list) or not items:
        raise ApiError(f"send {name}, a list of {name}")
    if len(items) > V1_BATCH_LIMIT:
        raise ApiError(f"send at most {V1_BATCH_LIMIT} {name} at a time")
    done, errors = [], []
    with write_db() as con:
        lookups = reads.category_lookups(con)
        for i, item in enumerate(items):
            # A failed item is undone on its own, so the ones after it are
            # checked against the rows as the items before it left them.
            con.execute("SAVEPOINT item")
            try:
                done.append(apply(con, item, lookups))
            except edits.EditError as e:
                con.execute("ROLLBACK TO item")
                errors.append({"index": i, "uid": uid_of(item), "status": e.status, "error": e.message})
            con.execute("RELEASE item")
        if errors:
            raise ApiError(f"{len(errors)} of {len(items)} {name} failed; nothing was written", errors=errors)
        rows = _v1_get_transactions(con, {u for pair in done for u in pair if u})
    return [{"transaction": rows[u], **({"mirror": rows[m]} if m else {})} for u, m in done]


def _v1_batch_change(con, change, lookups):
    uid = _batch_uid(change)
    if not isinstance(uid, str):
        raise ApiError("each change needs the uid of its transaction")
    return uid, _v1_apply_change(con, uid, {k: v for k, v in change.items() if k != "uid"}, lookups)


@app.route("/api/v1/transactions", methods=["PATCH"])
def v1_edit_transactions():
    """Several PATCHes as one: each change is a uid plus what the single
    PATCH takes (see _v1_batch)."""
    return jsonify(ok=True, results=_v1_batch(_json_body().get("changes"), "changes", _v1_batch_change,
                                               _batch_uid))


@app.route("/api/v1/transactions/<uid>", methods=["DELETE"])
def v1_delete_transaction(uid):
    with write_db() as con:
        deleted = edits.delete_transaction(con, uid)
    return jsonify(ok=True, deleted=deleted)


if not users.has_users():
    _log_setup_code()


if __name__ == "__main__":
    app.run(debug=True, port=5732)
