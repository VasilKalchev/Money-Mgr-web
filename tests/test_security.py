"""Auth, CSRF, proxy trust and the unsupported-schema guard."""
import ipaddress

import pytest

import app as appmod
import dbstore
import users


@pytest.fixture
def anon(data_dir):
    users.create("alice", "correct horse")
    return appmod.app.test_client()


def test_anonymous_page_redirects_to_login(anon):
    r = anon.get("/editor/transactions")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_anonymous_api_gets_401_json(anon):
    r = anon.get("/api/transactions/uids")
    assert r.status_code == 401 and r.get_json()["ok"] is False


def test_no_users_redirects_to_first_account(data_dir):
    r = appmod.app.test_client().get("/editor/transactions")
    assert r.status_code == 302 and "/setup/account" in r.headers["Location"]


def test_healthz_is_public(anon):
    assert anon.get("/healthz").status_code == 200


def test_security_headers(client):
    r = client.get("/editor/transactions")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"


def test_session_version_mismatch_logs_out(client):
    users.set_password("alice", "brand new pass")
    r = client.get("/api/transactions/uids")
    assert r.status_code == 401


def test_removed_user_session_is_dead(client):
    users.create("bob", "longenough")
    users.set_admin("bob", True)
    users.remove("alice")
    assert client.get("/api/transactions/uids").status_code == 401


def test_login_flow_and_throttling(anon):
    def attempt(pw):
        with anon.session_transaction() as s:
            s["csrf"] = "t"
        return anon.post("/login", data={"username": "alice", "password": pw, "csrf_token": "t"})

    assert attempt("wrong").status_code in (200, 401)
    r = attempt("correct horse")
    assert r.status_code == 302
    with anon.session_transaction() as s:
        assert s["user"] == "alice"


def test_lockout_blocks_even_correct_password(anon):
    with anon.session_transaction() as s:
        s["csrf"] = "t"
    for _ in range(users.MAX_FAILURES["user"]):
        anon.post("/login", data={"username": "alice", "password": "nope", "csrf_token": "t"})
    r = anon.post("/login", data={"username": "alice", "password": "correct horse", "csrf_token": "t"})
    assert r.status_code != 302
    with anon.session_transaction() as s:
        assert "user" not in s


@pytest.mark.parametrize("target, ok", [
    ("/editor/transactions", True),
    ("//evil.com", False),
    ("https://evil.com", False),
    ("/\\evil.com", False),
    ("", False),
    (None, False),
])
def test_safe_next(target, ok):
    with appmod.app.test_request_context():
        assert (appmod._safe_next(target) == target) is ok


# -- CSRF ----------------------------------------------------------------------

def test_post_without_token_rejected(client):
    r = client.post("/db/backup")
    assert r.status_code == 302  # flashed and redirected back
    assert dbstore.store_for("alice").backup_count() == 0


def test_post_with_wrong_token_rejected(client):
    r = client.post("/db/backup", headers={"X-CSRF-Token": "wrong"})
    assert dbstore.store_for("alice").backup_count() == 0


def test_post_with_header_or_field_token_accepted(client):
    client.post("/db/backup", headers=client.csrf)
    client.post("/db/backup", data={"csrf_token": "t"})
    assert dbstore.store_for("alice").backup_count() == 2


def test_api_csrf_failure_is_json_400(client):
    r = client.patch("/api/transactions/t1", json={})
    assert r.status_code == 400 and r.get_json()["ok"] is False


# -- client_ip and the trusted proxy -------------------------------------------

def ip_for(monkeypatch, remote, xff=None, trusted=("10.0.0.0/8",)):
    monkeypatch.setattr(appmod, "TRUSTED_PROXIES", [ipaddress.ip_network(t) for t in trusted])
    headers = {"X-Forwarded-For": xff} if xff else {}
    with appmod.app.test_request_context(environ_base={"REMOTE_ADDR": remote}, headers=headers):
        return appmod.client_ip()


def test_client_ip_ignores_xff_from_untrusted_peer(monkeypatch):
    assert ip_for(monkeypatch, "203.0.113.5", xff="1.2.3.4") == "203.0.113.5"


def test_client_ip_uses_xff_from_trusted_proxy(monkeypatch):
    assert ip_for(monkeypatch, "10.0.0.2", xff="198.51.100.7") == "198.51.100.7"


def test_client_ip_takes_nearest_untrusted_hop_not_forged_left(monkeypatch):
    # client forged "6.6.6.6"; our proxy appended the real peer, an inner proxy appended itself
    assert ip_for(monkeypatch, "10.0.0.2", xff="6.6.6.6, 198.51.100.7, 10.0.0.9") == "198.51.100.7"


def test_client_ip_falls_back_to_peer(monkeypatch):
    assert ip_for(monkeypatch, "10.0.0.2") == "10.0.0.2"
    assert ip_for(monkeypatch, "10.0.0.2", xff="10.0.0.3") == "10.0.0.2"


def test_no_trusted_proxies_means_none_trusted(monkeypatch):
    assert ip_for(monkeypatch, "10.0.0.2", xff="1.2.3.4", trusted=()) == "10.0.0.2"


def test_proxy_header_auth(data_dir, monkeypatch):
    users.create("alice", "correct horse")
    monkeypatch.setattr(appmod, "AUTH_HEADER", "X-Remote-User")
    monkeypatch.setattr(appmod, "TRUSTED_PROXIES", [ipaddress.ip_network("127.0.0.0/8")])
    c = appmod.app.test_client()
    # test client peer is 127.0.0.1: trusted, so the header is honored and creates the account
    r = c.get("/api/transactions/uids", headers={"X-Remote-User": "Newbie"})
    assert r.status_code != 401
    assert users.get("newbie")["password_hash"] == ""
    # an untrusted peer cannot use the header
    r = c.get("/api/transactions/uids", headers={"X-Remote-User": "alice"},
              environ_overrides={"REMOTE_ADDR": "203.0.113.5"})
    assert r.status_code == 401
    # an invalid name is refused outright
    r = c.get("/", headers={"X-Remote-User": "bad name!"})
    assert r.status_code == 403


# -- unsupported schema ----------------------------------------------------------

def test_unsupported_version_is_409(client, monkeypatch):
    monkeypatch.setattr(dbstore, "SUPPORTED_USER_VERSIONS", {99})
    r = client.get("/api/transactions/uids")
    assert r.status_code == 409
    r = client.get("/editor/transactions")
    assert r.status_code == 409


def test_no_database_redirects_to_setup(data_dir):
    users.create("bob", "longenough")
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s.update(user="bob", ver=1, csrf="t")
    assert "/setup" in c.get("/editor/transactions").headers["Location"]
    assert c.get("/api/transactions/uids").status_code == 409


# -- version -------------------------------------------------------------------

def test_every_page_header_shows_the_version(client):
    import app as appmod
    for url in ("/editor/transactions", "/settings"):
        assert f'class="muted app-version" title="Version">{appmod.VERSION}<' in client.get(url).get_data(as_text=True)


def test_version_falls_back_to_pyproject_for_a_local_run(monkeypatch):
    import app as appmod
    monkeypatch.delenv("MMW_VERSION", raising=False)
    assert appmod._app_version().endswith("-dev") and appmod._app_version()[0].isdigit()
    monkeypatch.setenv("MMW_VERSION", "dev")
    assert appmod._app_version().endswith("-dev")
    monkeypatch.setenv("MMW_VERSION", "v2.3.4")
    assert appmod._app_version() == "v2.3.4"
