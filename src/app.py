import hmac
import io
import ipaddress
import itertools
import json
import os
import secrets
import sqlite3
import sys
import tomllib
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for

import dbstore
import dbsync
import edits
import gdrive
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
    db_path = current_db_path()
    if not readonly:
        # Safety net for any write path that bypasses _require_db().
        check_db_supported(db_path)
    if readonly:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        current_store().backup_once()
        con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = OFF")
    return con


def query(sql, params=(), readonly=True):
    con = get_db(readonly=readonly)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@contextmanager
def write_db():
    """A writable connection for one edit (see edits.py), committed when the
    block finishes and discarded if it raises, so a refused edit writes
    nothing."""
    con = get_db(readonly=False)
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Shared lookups
# ---------------------------------------------------------------------------

def get_accounts():
    return query("""
        SELECT a.uid, a.NIC_NAME, a.currencyUid, c.ISO, g.ACC_GROUP_NAME
        FROM ASSETS a
        LEFT JOIN CURRENCY c ON c.uid = a.currencyUid
        LEFT JOIN ASSETGROUP g ON g.uid = a.groupUid
        ORDER BY g.ORDERSEQ, a.ORDERSEQ, a.NIC_NAME
    """)


def get_accounts_grouped():
    """[(group_name, [account_row, ...]), ...] in ASSETGROUP/ASSETS.ORDERSEQ order."""
    rows = get_accounts()
    return [(k, list(g)) for k, g in itertools.groupby(rows, key=lambda r: r["ACC_GROUP_NAME"])]


def get_accounts_json():
    return [dict(a) for a in get_accounts()]


def get_asset_groups():
    return query("SELECT uid, ACC_GROUP_NAME FROM ASSETGROUP ORDER BY ORDERSEQ")


def get_transaction_years():
    rows = query("SELECT DISTINCT substr(WDATE, 1, 4) AS y FROM INOUTCOME WHERE WDATE IS NOT NULL AND WDATE <> '' ORDER BY y")
    return [r["y"] for r in rows]


def get_currencies():
    return query("""
        SELECT DISTINCT cu.uid, cu.ISO
        FROM INOUTCOME i
        JOIN CURRENCY cu ON cu.uid = i.currencyUid
        ORDER BY cu.ISO
    """)


def get_categories(type_filter=None):
    sql = """
        SELECT c.uid, c.NAME, c.TYPE, c.STATUS, c.pUid, c.ORDERSEQ, c.C_IS_DEL, p.NAME AS parent_name
        FROM ZCATEGORY c
        LEFT JOIN ZCATEGORY p ON p.uid = c.pUid AND p.TYPE = c.TYPE
        WHERE IFNULL(NULLIF(c.C_IS_DEL, ''), 0) + 0 <> 1
    """
    params = ()
    if type_filter is not None:
        sql += " AND c.TYPE = ?"
        params = (type_filter,)
    sql += " ORDER BY c.TYPE DESC, c.STATUS, c.ORDERSEQ"
    return query(sql, params)


def build_category_tree(type_filter):
    """[{uid, name, children:[{uid,name}]}] for one tree (0=income, 1=expense)."""
    rows = get_categories(type_filter=type_filter)
    roots = [r for r in rows if r["STATUS"] == 0]
    children = [r for r in rows if r["STATUS"] == 2]
    tree = []
    for r in roots:
        kids = [{"uid": c["uid"], "name": c["NAME"]} for c in children if c["pUid"] == r["uid"]]
        tree.append({"uid": r["uid"], "name": r["NAME"], "children": kids})
    return tree


def category_lookup(type_filter):
    """{uid: {name, status, parent_uid}} for one tree."""
    rows = get_categories(type_filter=type_filter)
    return {r["uid"]: {"name": r["NAME"], "status": r["STATUS"], "parent_uid": r["pUid"]} for r in rows}


def category_path(lookup, uid):
    """(root uid, "Root > Child" or "Root") for a category in a
    category_lookup() tree, or ("", "") if it isn't there."""
    cat = lookup.get(uid)
    if not cat:
        return "", ""
    if cat["status"] == 0:
        return uid, cat["name"]
    root_uid = cat["parent_uid"]
    return root_uid, f'{lookup.get(root_uid, {}).get("name", "")} > {cat["name"]}'


def escape_like(s):
    """Escape %, _ and \\ so a value can be embedded in a LIKE pattern literally."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_distinct_values(column, limit=1000):
    """Distinct non-empty values of a column, most-recently-used first, for
    autocomplete suggestions on free-text inputs (note/description)."""
    rows = query(
        f"""
        SELECT {column} AS v FROM INOUTCOME
        WHERE IFNULL({column}, '') <> ''
        GROUP BY {column}
        ORDER BY MAX(CAST(UTIME AS INTEGER)) DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [r["v"] for r in rows]


def text_search_clause(column, raw):
    """Build a LIKE/NOT LIKE clause for a free-text search box. A leading '!'
    negates the match (e.g. '!foo' = does not contain 'foo')."""
    negate = raw.startswith("!")
    term = raw[1:] if negate else raw
    op = "NOT LIKE" if negate else "LIKE"
    clause = f"IFNULL({column}, '') {op} ? ESCAPE '\\'"
    return clause, f"%{escape_like(term)}%"


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
    "static", "index", "api_db_status", "settings", "backup_settings", "setup", "upload_db", "sync_db",
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
    return redirect(url_for("transactions"))


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
    return redirect(url_for("transactions" if first_run else "settings"))


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
    return redirect(url_for("transactions" if first_run and current_store().db_exists() else "settings"))


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


def parse_transaction_filters(args):
    """Parse the /transactions query-string filters into (a) the raw values
    the template needs to re-render the filter bar and (b) a `build_where`
    closure that both the page route and the "select all matching rows" API
    reuse to build the same WHERE clause."""
    account_uids = [v for v in args.getlist("account") if v]
    category_uids = [v for v in args.getlist("category") if v]
    # Root categories selected on their own, without their children.
    category_only_uids = [v for v in args.getlist("category_only") if v]
    do_types = [v for v in args.getlist("type") if v]
    date_from = args.get("from", "")
    date_to = args.get("to", "")
    q_note = args.get("q_note", "")
    q_desc = args.get("q_desc", "")
    has_note = args.get("has_note", "")  # "", "yes", "no"
    has_desc = args.get("has_desc", "")
    amount_min = args.get("amount_min", "")
    amount_max = args.get("amount_max", "")
    entered_currency = args.get("entered_currency", "")
    show_deleted = args.get("show_deleted", "hide")
    if show_deleted not in ("hide", "show", "only"):
        show_deleted = "hide"
    show_mirror = args.get("show_mirror", "hide")
    if show_mirror not in ("hide", "show", "only"):
        show_mirror = "hide"

    # A selected root category also pulls in all of its children, in either tree.
    expanded_category_uids = set(category_uids) | set(category_only_uids)
    for uid in category_uids:
        for t in (0, 1):
            for r in build_category_tree(t):
                if r["uid"] == uid:
                    expanded_category_uids.update(c["uid"] for c in r["children"])

    # Named filter clauses, so per-facet counts below can recompute with any
    # one of them left out (e.g. "how many deleted rows match everything
    # else?").
    filter_specs = []  # [(name, sql_clause, params)]

    if account_uids:
        placeholders = ",".join("?" for _ in account_uids)
        filter_specs.append(("account", f"i.assetUid IN ({placeholders})", list(account_uids)))
    if expanded_category_uids:
        placeholders = ",".join("?" for _ in expanded_category_uids)
        filter_specs.append(("category", f"i.ctgUid IN ({placeholders})", list(expanded_category_uids)))
    if do_types:
        placeholders = ",".join("?" for _ in do_types)
        filter_specs.append(("type", f"i.DO_TYPE IN ({placeholders})", list(do_types)))
    if date_from:
        filter_specs.append(("date_from", "i.WDATE >= ?", [date_from]))
    if date_to:
        filter_specs.append(("date_to", "i.WDATE <= ?", [date_to]))
    if q_note:
        clause, param = text_search_clause("i.ZCONTENT", q_note)
        filter_specs.append(("q_note", clause, [param]))
    if q_desc:
        clause, param = text_search_clause("i.ZDATA", q_desc)
        filter_specs.append(("q_desc", clause, [param]))
    if has_note == "yes":
        filter_specs.append(("has_note", "IFNULL(i.ZCONTENT, '') <> ''", []))
    elif has_note == "no":
        filter_specs.append(("has_note", "IFNULL(i.ZCONTENT, '') = ''", []))
    if has_desc == "yes":
        filter_specs.append(("has_desc", "IFNULL(i.ZDATA, '') <> ''", []))
    elif has_desc == "no":
        filter_specs.append(("has_desc", "IFNULL(i.ZDATA, '') = ''", []))
    if amount_min:
        try:
            filter_specs.append(("amount_min", "i.AMOUNT_ACCOUNT >= ?", [float(amount_min)]))
        except ValueError:
            amount_min = ""
    if amount_max:
        try:
            filter_specs.append(("amount_max", "i.AMOUNT_ACCOUNT <= ?", [float(amount_max)]))
        except ValueError:
            amount_max = ""
    if entered_currency:
        filter_specs.append(("entered_currency", "i.currencyUid = ?", [entered_currency]))

    def build_where(exclude=None):
        clauses = []
        p = []
        if exclude != "show_deleted":
            if show_deleted == "hide":
                clauses.append("i.IS_DEL = 0")
            elif show_deleted == "only":
                clauses.append("i.IS_DEL <> 0")
        if exclude != "show_mirror":
            if show_mirror == "hide":
                clauses.append("i.DO_TYPE <> '4'")
            elif show_mirror == "only":
                clauses.append("i.DO_TYPE = '4'")
        for name, clause, cparams in filter_specs:
            if name == exclude:
                continue
            clauses.append(clause)
            p.extend(cparams)
        return (" AND ".join(clauses) if clauses else "1=1"), p

    return {
        "account_uids": account_uids, "category_uids": category_uids,
        "category_only_uids": category_only_uids, "do_types": do_types,
        "date_from": date_from, "date_to": date_to, "q_note": q_note, "q_desc": q_desc,
        "has_note": has_note, "has_desc": has_desc, "amount_min": amount_min, "amount_max": amount_max,
        "entered_currency": entered_currency, "show_deleted": show_deleted, "show_mirror": show_mirror,
        "build_where": build_where,
    }


@app.route("/api/transactions/uids")
def api_transaction_uids():
    """All INOUTCOME.uid values matching the current /transactions filters,
    across every page -- used by "select all matching rows" in the bulk
    editor, as opposed to just the rows rendered on the current page."""
    f = parse_transaction_filters(request.args)
    where, params = f["build_where"]()
    rows = query(f"SELECT i.uid FROM INOUTCOME i WHERE {where}", params)
    return jsonify(uids=[r["uid"] for r in rows])


@app.route("/api/transactions/group-stats")
def api_transaction_group_stats():
    """Grouping stats (same-account rows within N seconds of each other)
    over every matching transaction, not just whatever page is loaded --
    the point of grouping is the stats, so this computes them directly in
    SQL/Python rather than requiring the client to fetch and render however
    many thousand rows actually match."""
    f = parse_transaction_filters(request.args)
    where, params = f["build_where"]()
    try:
        threshold_ms = max(1, int(request.args.get("seconds", 60))) * 1000
    except ValueError:
        threshold_ms = 60000

    rows = query(
        f"""
        SELECT i.assetUid AS account, CAST(i.ZDATE AS INTEGER) AS t
        FROM INOUTCOME i WHERE {where}
        ORDER BY t ASC
        """,
        params,
    )

    group_count = 0
    group_sizes = []
    gaps_ms = []
    prev_account = None
    prev_t = None
    cur_size = 0
    for r in rows:
        account, t = r["account"], r["t"]
        same_cluster = prev_account is not None and account == prev_account and abs(prev_t - t) <= threshold_ms
        if not same_cluster:
            if group_count > 0:
                group_sizes.append(cur_size)
            group_count += 1
            cur_size = 0
        else:
            gaps_ms.append(abs(t - prev_t))
        cur_size += 1
        prev_account, prev_t = account, t
    if group_count > 0:
        group_sizes.append(cur_size)

    total = len(rows)
    multi_groups = sum(1 for g in group_sizes if g > 1)
    avg_size = total / group_count if group_count else 0
    avg_gap_sec = (sum(gaps_ms) / len(gaps_ms) / 1000) if gaps_ms else 0
    median_gap_sec = 0
    if gaps_ms:
        s = sorted(gaps_ms)
        mid = len(s) // 2
        median_ms = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
        median_gap_sec = median_ms / 1000

    return jsonify(
        total_rows=total, groups=group_count, multi_groups=multi_groups,
        avg_size=avg_size, avg_gap_sec=avg_gap_sec, median_gap_sec=median_gap_sec,
    )


TX_SORT_OPTIONS = {
    "date": "i.WDATE {dir}, CAST(i.ZDATE AS INTEGER) {dir}",
    "amount": "i.AMOUNT_ACCOUNT {dir}",
    "entered_amount": "i.IN_ZMONEY {dir}",
    "account": "a.NIC_NAME COLLATE NOCASE {dir}",
    "updated": "i.UTIME {dir}",
}


@app.route("/transactions")
def transactions():
    f = parse_transaction_filters(request.args)
    account_uids = f["account_uids"]
    category_uids = f["category_uids"]
    do_types = f["do_types"]
    date_from = f["date_from"]
    date_to = f["date_to"]
    q_note = f["q_note"]
    q_desc = f["q_desc"]
    has_note = f["has_note"]
    has_desc = f["has_desc"]
    amount_min = f["amount_min"]
    amount_max = f["amount_max"]
    entered_currency = f["entered_currency"]
    show_deleted = f["show_deleted"]
    show_mirror = f["show_mirror"]
    build_where = f["build_where"]

    where, params = build_where()

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

    total_rows = query(f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE {where}", params)[0]["n"]
    total_pages = max(1, (total_rows + page_size - 1) // page_size)
    page = min(page, total_pages)
    offset = (page - 1) * page_size

    # Facet counts: each is computed with every filter EXCEPT the one that
    # facet itself controls, so a checkbox/select can show "how many rows
    # would this option add or remove" rather than just the current total.
    deleted_where, deleted_params = build_where(exclude="show_deleted")
    deleted_count = query(
        f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE ({deleted_where}) AND i.IS_DEL <> 0", deleted_params
    )[0]["n"]
    mirror_where, mirror_params = build_where(exclude="show_mirror")
    mirror_count = query(
        f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE ({mirror_where}) AND i.DO_TYPE = '4'", mirror_params
    )[0]["n"]

    note_where, note_params = build_where(exclude="has_note")
    note_counts = {
        "yes": query(f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE ({note_where}) AND IFNULL(i.ZCONTENT,'')<>''", note_params)[0]["n"],
        "no": query(f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE ({note_where}) AND IFNULL(i.ZCONTENT,'')=''", note_params)[0]["n"],
    }
    desc_where, desc_params = build_where(exclude="has_desc")
    desc_counts = {
        "yes": query(f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE ({desc_where}) AND IFNULL(i.ZDATA,'')<>''", desc_params)[0]["n"],
        "no": query(f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE ({desc_where}) AND IFNULL(i.ZDATA,'')=''", desc_params)[0]["n"],
    }

    account_where, account_params = build_where(exclude="account")
    account_counts = {
        r["uid"]: r["n"] for r in query(
            f"SELECT i.assetUid AS uid, COUNT(*) AS n FROM INOUTCOME i WHERE {account_where} GROUP BY i.assetUid",
            account_params,
        )
    }

    category_where, category_params = build_where(exclude="category")
    category_counts_raw = {
        r["uid"]: r["n"] for r in query(
            f"SELECT i.ctgUid AS uid, COUNT(*) AS n FROM INOUTCOME i WHERE {category_where} GROUP BY i.ctgUid",
            category_params,
        )
    }
    # A root category's displayed count includes all of its children's rows;
    # the raw per-uid counts are also passed for the "root only" filter state.
    category_counts = dict(category_counts_raw)
    for t in (0, 1):
        for r in build_category_tree(t):
            child_total = sum(category_counts_raw.get(c["uid"], 0) for c in r["children"])
            category_counts[r["uid"]] = category_counts_raw.get(r["uid"], 0) + child_total

    type_where, type_params = build_where(exclude="type")
    type_counts = {
        r["t"]: r["n"] for r in query(
            f"SELECT i.DO_TYPE AS t, COUNT(*) AS n FROM INOUTCOME i WHERE {type_where} GROUP BY i.DO_TYPE",
            type_params,
        )
    }

    sort_by = request.args.get("sort", "date")
    if sort_by not in TX_SORT_OPTIONS:
        sort_by = "date"
    sort_dir = request.args.get("dir", "desc")
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    order_by = TX_SORT_OPTIONS[sort_by].format(dir=sort_dir)

    sql = f"""
        SELECT i.uid, i.WDATE, i.ZDATE AS zdate_ms, i.IS_DEL AS is_del,
               time(CAST(i.ZDATE AS INTEGER) / 1000, 'unixepoch', 'localtime') AS tx_time,
               i.AMOUNT_ACCOUNT, i.DO_TYPE, i.ZCONTENT, i.ZDATA, i.currencyUid AS tx_currency_uid,
               i.IN_ZMONEY AS entered_amount,
               datetime(CAST(i.UTIME AS INTEGER) / 1000, 'unixepoch', 'localtime') AS updated_str,
               cu.ISO AS currency_iso,
               acu.ISO AS account_currency_iso,
               a.uid AS account_uid, a.NIC_NAME AS account_name,
               ta.NIC_NAME AS to_account_name,
               i.ctgUid AS category_uid
        FROM INOUTCOME i
        LEFT JOIN ASSETS a    ON a.uid = i.assetUid
        LEFT JOIN ASSETS ta   ON ta.uid = i.toAssetUid
        LEFT JOIN CURRENCY cu ON cu.uid = i.currencyUid
        LEFT JOIN CURRENCY acu ON acu.uid = a.currencyUid
        WHERE {where}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    rows = query(sql, params + [page_size, offset])

    lookups = {0: category_lookup(0), 1: category_lookup(1)}

    enriched = []
    for r in rows:
        d = dict(r)
        tree_type = 0 if d["DO_TYPE"] == "0" else 1
        d["tree_type"] = tree_type
        d["root_uid"], d["category_path"] = category_path(lookups[tree_type], d["category_uid"])
        d["editable_category"] = d["DO_TYPE"] in ("0", "1")
        enriched.append(d)

    return render_template(
        "transactions.html",
        rows=enriched,
        accounts=get_accounts(),
        accounts_grouped=get_accounts_grouped(),
        accounts_json=get_accounts_json(),
        category_trees={0: build_category_tree(0), 1: build_category_tree(1)},
        currencies=get_currencies(),
        tx_years=get_transaction_years(),
        filters=dict(account=account_uids, category=category_uids, category_only=f["category_only_uids"],
                     type=do_types, date_from=date_from, date_to=date_to,
                     q_note=q_note, q_desc=q_desc,
                     has_note=has_note, has_desc=has_desc, amount_min=amount_min, amount_max=amount_max,
                     entered_currency=entered_currency,
                     show_deleted=show_deleted, show_mirror=show_mirror,
                     sort=sort_by, dir=sort_dir),
        note_suggestions=get_distinct_values("ZCONTENT"),
        desc_suggestions=get_distinct_values("ZDATA"),
        deleted_count=deleted_count, mirror_count=mirror_count,
        note_counts=note_counts, desc_counts=desc_counts,
        account_counts=account_counts, category_counts=category_counts,
        category_counts_own=category_counts_raw, type_counts=type_counts,
        sort_options=list(TX_SORT_OPTIONS),
        page=page,
        total_pages=total_pages,
        total_rows=total_rows,
        page_size=page_size,
        page_size_options=page_size_options,
    )


def get_account_rows():
    """Every account with its group, currency, live-row count and balance,
    in the app's group/account order."""
    return query("""
        SELECT a.uid, a.NIC_NAME, c.ISO, g.uid AS group_uid, g.ACC_GROUP_NAME,
               a.ORDERSEQ, a.TYPE AS account_type, a.ZDATA AS account_flags,
               a.CARD_ACCOUNT_NAME,
               datetime(CAST(a.A_UTIME AS INTEGER) / 1000, 'unixepoch', 'localtime') AS updated_str,
               COUNT(i.uid) AS tx_count,
               ROUND(SUM(CASE i.DO_TYPE
                   WHEN '0' THEN i.AMOUNT_ACCOUNT
                   WHEN '7' THEN i.AMOUNT_ACCOUNT
                   WHEN '4' THEN i.AMOUNT_ACCOUNT
                   ELSE -i.AMOUNT_ACCOUNT END), 2) AS balance
        FROM ASSETS a
        LEFT JOIN CURRENCY c ON c.uid = a.currencyUid
        LEFT JOIN ASSETGROUP g ON g.uid = a.groupUid
        LEFT JOIN INOUTCOME i ON i.assetUid = a.uid AND i.IS_DEL = 0
        GROUP BY a.uid
        ORDER BY g.ORDERSEQ, a.ORDERSEQ, a.NIC_NAME
    """)


@app.route("/accounts")
def accounts():
    rows = get_account_rows()
    # Rows already arrive in group order (from ASSETGROUP.ORDERSEQ), so a
    # plain consecutive grouping preserves that -- unlike Jinja's `groupby`
    # filter, which would silently re-sort groups alphabetically.
    grouped = [(k, list(g)) for k, g in itertools.groupby(rows, key=lambda r: r["ACC_GROUP_NAME"])]
    return render_template("accounts.html", grouped_rows=grouped, groups=get_asset_groups())


@app.route("/categories")
def categories():
    income = get_categories(type_filter=0)
    expense = get_categories(type_filter=1)

    totals_sql = """
        SELECT c.uid, acu.ISO AS currency_iso,
               ROUND(SUM(i.AMOUNT_ACCOUNT), 2) AS total, COUNT(i.uid) AS rows
        FROM ZCATEGORY c
        JOIN INOUTCOME i ON i.ctgUid = c.uid AND i.IS_DEL = 0
        LEFT JOIN ASSETS a ON a.uid = i.assetUid
        LEFT JOIN CURRENCY acu ON acu.uid = a.currencyUid
        GROUP BY c.uid, acu.ISO
        ORDER BY acu.ISO
    """
    totals = {}
    for r in query(totals_sql):
        totals.setdefault(r["uid"], []).append((r["currency_iso"] or "?", r["total"], r["rows"]))

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
# Routes - public API (/api/v1/, see docs/openapi.yaml)
# ---------------------------------------------------------------------------

TX_TYPE_NAMES = {
    "0": "income", "1": "expense", "3": "transfer", "4": "transfer_mirror",
    "7": "balance_increase", "8": "balance_decrease",
}
TX_TYPE_CODES = {name: code for code, name in TX_TYPE_NAMES.items()}
ACCOUNT_STATUS_NAMES = {"0": "normal", "1": "deleted", "3": "hidden"}

_V1_TX_SQL = """
    SELECT i.uid, i.WDATE, i.ZDATE, i.DO_TYPE, i.IS_DEL, i.AMOUNT_ACCOUNT, i.IN_ZMONEY,
           i.ZCONTENT, i.ZDATA, i.ctgUid, i.txUidTrans, i.UTIME,
           time(CAST(i.ZDATE AS INTEGER) / 1000, 'unixepoch', 'localtime') AS tx_time,
           cu.ISO AS currency_iso, acu.ISO AS account_currency_iso,
           a.uid AS account_uid, a.NIC_NAME AS account_name,
           ta.uid AS to_account_uid, ta.NIC_NAME AS to_account_name
    FROM INOUTCOME i
    LEFT JOIN ASSETS a     ON a.uid = i.assetUid
    LEFT JOIN ASSETS ta    ON ta.uid = i.toAssetUid
    LEFT JOIN CURRENCY cu  ON cu.uid = i.currencyUid
    LEFT JOIN CURRENCY acu ON acu.uid = a.currencyUid
"""


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
    _, path = category_path(lookups[0 if do_type == "0" else 1], r["ctgUid"])
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
        "updated_ms": _num_or_none(r["UTIME"], int),
    }


def _category_lookups():
    return {0: category_lookup(0), 1: category_lookup(1)}


def _v1_get_transaction(uid):
    rows = query(_V1_TX_SQL + " WHERE i.uid = ?", (uid,))
    if not rows:
        raise ApiError("not found", 404)
    return _v1_transaction(rows[0], _category_lookups())


def _v1_get_transactions(uids):
    """{uid: transaction} for these uids; missing ones are left out."""
    uids, lookups, out = list(uids), _category_lookups(), {}
    for n in range(0, len(uids), 500):
        part = uids[n:n + 500]
        for r in query(f"{_V1_TX_SQL} WHERE i.uid IN ({','.join('?' for _ in part)})", part):
            out[r["uid"]] = _v1_transaction(r, lookups)
    return out


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
    row = con.execute(_V1_TX_SQL + " WHERE i.uid = ?", (uid,)).fetchone()
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
    return jsonify(ok=True, accounts=[{
        "uid": r["uid"],
        "name": r["NIC_NAME"],
        "group_uid": r["group_uid"],
        "group": r["ACC_GROUP_NAME"],
        "currency": r["ISO"],
        "balance": r["balance"] or 0,
        "transaction_count": r["tx_count"],
        "status": ACCOUNT_STATUS_NAMES.get(str(r["account_flags"]), "other"),
    } for r in get_account_rows()])


@app.route("/api/v1/categories")
def v1_categories():
    return jsonify(ok=True, income=build_category_tree(0), expense=build_category_tree(1))


@app.route("/api/v1/currencies")
def v1_currencies():
    rows = query("SELECT uid, ISO, RATE, DECIMAL_POINT FROM CURRENCY ORDER BY ISO")
    return jsonify(ok=True, currencies=[{
        "uid": r["uid"], "iso": r["ISO"], "rate": _num_or_none(r["RATE"]),
        "decimals": _num_or_none(r["DECIMAL_POINT"], int),
    } for r in rows])


@app.route("/api/v1/transactions")
def v1_transactions():
    """The /transactions filters, as the same query-string parameters, with
    limit/offset paging."""
    f = parse_transaction_filters(request.args)
    where, params = f["build_where"]()
    updated_since = _int_arg("updated_since", None, 0)
    if updated_since is not None:
        where += " AND CAST(i.UTIME AS INTEGER) >= ?"
        params.append(updated_since)
    limit = _int_arg("limit", 100, 1, 1000)
    offset = _int_arg("offset", 0, 0)
    sort_by = request.args.get("sort", "date")
    sort_dir = request.args.get("dir", "desc")
    if sort_by not in TX_SORT_OPTIONS:
        raise ApiError(f"sort must be one of: {', '.join(TX_SORT_OPTIONS)}")
    if sort_dir not in ("asc", "desc"):
        raise ApiError("dir must be asc or desc")
    total = query(f"SELECT COUNT(*) AS n FROM INOUTCOME i WHERE {where}", params)[0]["n"]
    rows = query(
        f"{_V1_TX_SQL} WHERE {where} ORDER BY {TX_SORT_OPTIONS[sort_by].format(dir=sort_dir)}, i.uid LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    lookups = _category_lookups()
    return jsonify(ok=True, total=total, limit=limit, offset=offset,
                   transactions=[_v1_transaction(r, lookups) for r in rows])


@app.route("/api/v1/transactions/<uid>")
def v1_transaction(uid):
    return jsonify(ok=True, transaction=_v1_get_transaction(uid))


@app.route("/api/v1/transactions", methods=["POST"])
def v1_add_transaction():
    """Same fields as the add form; type may also be a name, and a missing
    date means now."""
    data = dict(_json_body())
    data["type"] = _type_code(data.get("type"))
    if data["type"] == "4":
        raise ApiError("add a transfer as type transfer; its mirror row is made for you")
    if not data.get("date"):
        now = datetime.now()
        data["date"], data["time"] = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
    with write_db() as con:
        uid = edits.create_transaction(con, data)
    return jsonify(ok=True, transaction=_v1_get_transaction(uid)), 201


@app.route("/api/v1/transactions/<uid>", methods=["PATCH"])
def v1_edit_transaction(uid):
    """Change any of edits.TRANSACTION_FIELDS in one go (see
    edits.update_transaction); type may also be a name, and expect holds
    field values the row must have first. A transfer's other row comes back
    as mirror."""
    with write_db() as con:
        mirror_uid = _v1_apply_change(con, uid, _json_body(), _category_lookups())
    out = {"ok": True, "transaction": _v1_get_transaction(uid)}
    if mirror_uid:
        out["mirror"] = _v1_get_transaction(mirror_uid)
    return jsonify(out)


V1_BATCH_LIMIT = 1000


@app.route("/api/v1/transactions", methods=["PATCH"])
def v1_edit_transactions():
    """Several PATCHes as one: each change is a uid plus what the single
    PATCH takes, applied in list order, and all of them or none. A refusal
    lists every change that failed, with the status the single PATCH would
    have answered."""
    changes = _json_body().get("changes")
    if not isinstance(changes, list) or not changes:
        raise ApiError("send changes, a list of changes")
    if len(changes) > V1_BATCH_LIMIT:
        raise ApiError(f"send at most {V1_BATCH_LIMIT} changes at a time")
    lookups, done, errors = _category_lookups(), [], []
    with write_db() as con:
        for i, change in enumerate(changes):
            uid = change.get("uid") if isinstance(change, dict) else None
            # A failed change is undone on its own, so the ones after it are
            # checked against the rows as the changes before it left them.
            con.execute("SAVEPOINT change")
            try:
                if not isinstance(uid, str):
                    raise ApiError("each change needs the uid of its transaction")
                done.append((uid, _v1_apply_change(con, uid, {k: v for k, v in change.items() if k != "uid"},
                                                   lookups)))
            except edits.EditError as e:
                con.execute("ROLLBACK TO change")
                errors.append({"index": i, "uid": uid, "status": e.status, "error": e.message})
            con.execute("RELEASE change")
        if errors:
            raise ApiError(f"{len(errors)} of {len(changes)} changes failed; nothing was written", errors=errors)
    rows = _v1_get_transactions({u for pair in done for u in pair if u})
    return jsonify(ok=True, results=[{"transaction": rows[u], **({"mirror": rows[m]} if m else {})}
                                     for u, m in done])


@app.route("/api/v1/transactions/<uid>", methods=["DELETE"])
def v1_delete_transaction(uid):
    with write_db() as con:
        deleted = edits.delete_transaction(con, uid)
    return jsonify(ok=True, deleted=deleted)


if not users.has_users():
    _log_setup_code()


if __name__ == "__main__":
    app.run(debug=True, port=5732)
