"""gdrive.py with `requests` faked out; nothing here touches the network.
Syncing itself is tested in test_sync.py."""
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

import dbstore
import gdrive
from fakedrive import FakeDrive
from gdrive import GDriveError


class Resp:
    def __init__(self, status=200, body=None, content=b"", text=""):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": "application/json"} if body is not None else {}
        self.content = content
        self.text = text or str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, n):
        yield self.content


@pytest.fixture(autouse=True)
def clean_tokens(monkeypatch):
    monkeypatch.setattr(gdrive, "_tokens", {})


@pytest.fixture
def store(tmp_path):
    return dbstore.Store(str(tmp_path / "u"))


@pytest.fixture
def connected(store):
    gdrive.save_credentials(store, "cid", "secret", "")
    store.save_config({"gdrive": {**gdrive.settings(store), "refresh_token": "rt"}})
    return store


# -- credentials ------------------------------------------------------------------

def test_save_credentials_defaults_and_validation(store):
    with pytest.raises(GDriveError, match="Client ID"):
        gdrive.save_credentials(store, " ", "s", "")
    with pytest.raises(GDriveError, match="secret"):
        gdrive.save_credentials(store, "cid", "", "")
    gdrive.save_credentials(store, " cid ", " secret ", "")
    assert gdrive.settings(store)["client_id"] == "cid"
    assert gdrive.folder_name(store) == gdrive.DEFAULT_FOLDER
    assert gdrive.is_configured(store) and not gdrive.is_connected(store)


def test_blank_secret_keeps_saved_one_for_same_client(connected):
    gdrive.save_credentials(connected, "cid", "", "Other")
    s = gdrive.settings(connected)
    assert s["client_secret"] == "secret" and s["refresh_token"] == "rt" and s["folder_name"] == "Other"


def test_changing_client_drops_login_and_secret(connected):
    with pytest.raises(GDriveError):  # new client id, no new secret
        gdrive.save_credentials(connected, "other", "", "")
    gdrive.save_credentials(connected, "other", "s2", "")
    assert "refresh_token" not in gdrive.settings(connected)


def test_disconnect(connected):
    gdrive._tokens[connected.root] = {"value": "x", "expires": 9e99}
    gdrive.disconnect(connected)
    assert not gdrive.is_configured(connected) and connected.root not in gdrive._tokens


# -- oauth ------------------------------------------------------------------------

def test_auth_url(connected):
    url = gdrive.auth_url(connected, "http://localhost:5732/cb", "st")
    assert url.startswith(gdrive.AUTH_URL + "?")
    for part in ("client_id=cid", "state=st", "access_type=offline", "prompt=consent", "response_type=code"):
        assert part in url
    assert parse_qs(urlsplit(url).query)["scope"] == [gdrive.SCOPE_WRITE]
    assert "localhost%3A5732%2Fcb" in url


def test_auth_url_read_only_when_writing_is_off(store):
    gdrive.save_credentials(store, "cid", "secret", "", write=False)
    assert not gdrive.wants_write(store)
    url = gdrive.auth_url(store, "http://localhost/cb", "st")
    assert parse_qs(urlsplit(url).query)["scope"] == [gdrive.SCOPE_READ]


@pytest.mark.parametrize("pasted, expected", [
    ("http://localhost:5732/setup/gdrive/callback?state=S&code=C%2F1&scope=x", ("C/1", "S")),
    ("  http://localhost/cb?code=abc  ", ("abc", None)),
    ("code=abc&state=S", ("abc", "S")),
    ("4/0Abare-code", ("4/0Abare-code", None)),
])
def test_parse_redirect(pasted, expected):
    assert gdrive.parse_redirect(pasted) == expected


def test_parse_redirect_errors():
    with pytest.raises(GDriveError, match="access_denied"):
        gdrive.parse_redirect("http://localhost/cb?error=access_denied&state=S")
    with pytest.raises(GDriveError, match="No authorization code"):
        gdrive.parse_redirect("http://localhost/cb?state=S")


def test_exchange_code_saves_refresh_token(store, monkeypatch):
    gdrive.save_credentials(store, "cid", "secret", "")
    sent = {}

    def post(url, data, timeout):
        sent.update(data)
        return Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

    monkeypatch.setattr(gdrive.requests, "post", post)
    gdrive.exchange_code(store, "the-code", "http://localhost/cb")
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "the-code"
    assert gdrive.is_connected(store)
    assert gdrive._tokens[store.root]["value"] == "at"


@pytest.mark.parametrize("granted, writable", [(gdrive.SCOPE_WRITE, True), (gdrive.SCOPE_READ, False), (None, False)])
def test_granted_scope_decides_writing(store, monkeypatch, granted, writable):
    gdrive.save_credentials(store, "cid", "secret", "")
    body = {"access_token": "at", "refresh_token": "rt", **({"scope": granted} if granted else {})}
    monkeypatch.setattr(gdrive.requests, "post", lambda *a, **k: Resp(200, body))
    gdrive.exchange_code(store, "c", "http://localhost/cb")
    assert gdrive.can_write(store) is writable


def test_resaving_same_client_keeps_granted_scope(connected):
    connected.save_config({"gdrive": {**gdrive.settings(connected), "scope": gdrive.SCOPE_WRITE}})
    gdrive.save_credentials(connected, "cid", "", "Other")
    assert gdrive.can_write(connected)
    gdrive.save_credentials(connected, "other", "s2", "")
    assert not gdrive.can_write(connected)


def test_exchange_code_without_refresh_token(store, monkeypatch):
    gdrive.save_credentials(store, "cid", "secret", "")
    monkeypatch.setattr(gdrive.requests, "post", lambda *a, **k: Resp(200, {"access_token": "at"}))
    with pytest.raises(GDriveError, match="refresh token"):
        gdrive.exchange_code(store, "c", "http://localhost/cb")
    assert not gdrive.is_connected(store)


def test_invalid_grant_message(connected, monkeypatch):
    monkeypatch.setattr(gdrive.requests, "post", lambda *a, **k: Resp(400, {"error": "invalid_grant"}))
    with pytest.raises(GDriveError, match="Reconnect"):
        gdrive._access_token(connected)


def test_token_error_and_network_error(connected, monkeypatch):
    monkeypatch.setattr(gdrive.requests, "post",
                        lambda *a, **k: Resp(401, {"error": "invalid_client", "error_description": "bad client"}))
    with pytest.raises(GDriveError, match="bad client"):
        gdrive._access_token(connected)

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(gdrive.requests, "post", boom)
    with pytest.raises(GDriveError, match="Couldn't reach Google"):
        gdrive._access_token(connected)


def test_access_token_is_cached_until_expiry(connected, monkeypatch):
    calls = []

    def post(url, data, timeout):
        calls.append(data["grant_type"])
        return Resp(200, {"access_token": f"at{len(calls)}", "expires_in": 3600})

    monkeypatch.setattr(gdrive.requests, "post", post)
    assert gdrive._access_token(connected) == "at1"
    assert gdrive._access_token(connected) == "at1"
    assert calls == ["refresh_token"]
    gdrive._tokens[connected.root]["expires"] = 0
    assert gdrive._access_token(connected) == "at2"


def test_access_token_requires_connection(store):
    with pytest.raises(GDriveError, match="isn't connected"):
        gdrive._access_token(store)


# -- drive ------------------------------------------------------------------------

@pytest.fixture
def drive(connected, monkeypatch):
    """Fake Drive: the test sets .pages (list of file-list bodies) and .files (id -> bytes)."""
    class Drive:
        pass

    d = Drive()
    d.pages, d.files, d.requests = [], {}, []
    d.folders = [{"id": "folder1", "name": "MoneyManager"}]
    monkeypatch.setitem(gdrive._tokens, connected.root, {"value": "at", "expires": 9e99})

    def get(url, params, stream, timeout, headers):
        d.requests.append((url, params, headers))
        assert headers["Authorization"] == "Bearer at"
        if url.endswith("/files") and "mimeType" in params["q"]:
            return Resp(200, {"files": d.folders})
        if url.endswith("/files"):
            return Resp(200, d.pages[len(d.requests_for_list()) - 1])
        fid = url.rsplit("/", 1)[1]
        return Resp(200, content=d.files[fid])

    d.requests_for_list = lambda: [r for r in d.requests if r[0].endswith("/files") and "mimeType" not in r[1]["q"]]
    monkeypatch.setattr(gdrive.requests, "get", get)
    return d


def snap(id_, name, md5=None):
    return {"id": id_, "name": name, "md5Checksum": md5, "modifiedTime": "2025-01-01T00:00:00Z"}


def test_find_folder(connected, drive):
    assert gdrive.find_folder(connected, "MoneyManager") == "folder1"
    drive.folders = []
    with pytest.raises(GDriveError, match="No folder named"):
        gdrive.find_folder(connected, "MoneyManager")


def test_find_folder_quotes_name(connected, drive):
    gdrive.find_folder(connected, "Bob's \\ folder")
    assert "name='Bob\\'s \\\\ folder'" in drive.requests[0][1]["q"]


def test_list_snapshots_filters_and_paginates(connected, drive):
    drive.pages = [
        {"files": [snap("1", "MM_b.mmbak"), snap("2", "MMother.txt")], "nextPageToken": "p2"},
        {"files": [snap("3", "Mail.mmbak"), snap("4", "MM_a.mmbak")]},
    ]
    got = gdrive.list_snapshots(connected, "folder1")
    assert [f["id"] for f in got] == ["1", "4"]
    assert drive.requests_for_list()[1][1]["pageToken"] == "p2"


def test_drive_errors(connected, monkeypatch):
    monkeypatch.setitem(gdrive._tokens, connected.root, {"value": "at", "expires": 9e99})
    monkeypatch.setattr(gdrive.requests, "get",
                        lambda *a, **k: Resp(403, {"error": {"message": "Rate limit"}}))
    with pytest.raises(GDriveError, match=r"\(403\): Rate limit"):
        gdrive.find_folder(connected, "x")
    monkeypatch.setattr(gdrive.requests, "get", lambda *a, **k: Resp(500, text="<html>oops</html>"))
    with pytest.raises(GDriveError, match="oops"):
        gdrive.find_folder(connected, "x")

    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(gdrive.requests, "get", boom)
    with pytest.raises(GDriveError, match="Couldn't reach Google Drive"):
        gdrive.find_folder(connected, "x")


def test_check_connection_counts_snapshots(connected, drive):
    drive.pages = [{"files": [snap("1", "MM_a.mmbak"), snap("2", "MM_b.mmbak")]}]
    assert gdrive.check_connection(connected) == 2


# -- writing ----------------------------------------------------------------------

def test_upload_replaces_content_and_keeps_name(connected, monkeypatch, tmp_path):
    fake = FakeDrive(monkeypatch, connected)
    meta = fake.add("f1", "MMGF(1-2-26-101010).mmbak", b"old content")
    new = tmp_path / "new.mmbak"
    new.write_bytes(b"x" * (6 * 1024 * 1024))  # over the 5 MB simple-upload limit
    after = gdrive.upload(connected, meta, str(new))
    assert fake.data("f1") == new.read_bytes()
    assert after["name"] == "MMGF(1-2-26-101010).mmbak" and after["id"] == "f1"
    assert after["md5Checksum"] == dbstore.file_md5(str(new))
    assert [r[0] for r in fake.requests] == ["PATCH", "PUT", "GET"]


def test_upload_errors(connected, monkeypatch, tmp_path):
    fake = FakeDrive(monkeypatch, connected)
    path = tmp_path / "x.mmbak"
    path.write_bytes(b"x")
    with pytest.raises(GDriveError, match="404"):
        gdrive.upload(connected, {"id": "missing", "name": "MM.mmbak"}, str(path))
    monkeypatch.setattr(gdrive.requests, "patch",
                        lambda *a, **k: Resp(403, {"error": {"message": "Request had insufficient authentication scopes."}}))
    fake.add("f1", "MM_a.mmbak", b"old")
    with pytest.raises(GDriveError, match="insufficient"):
        gdrive.upload(connected, fake.meta("f1"), str(path))
    assert fake.data("f1") == b"old"


def test_file_meta(connected, monkeypatch):
    fake = FakeDrive(monkeypatch, connected)
    fake.add("f1", "MM_a.mmbak", b"abc")
    assert gdrive.file_meta(connected, "f1")["md5Checksum"] == hashlib.md5(b"abc").hexdigest()
    with pytest.raises(GDriveError, match="404"):
        gdrive.file_meta(connected, "nope")
