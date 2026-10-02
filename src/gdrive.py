"""Google Drive access for the Money Manager snapshots.

The Android app writes MM*.mmbak snapshots into a Drive folder (default
"MoneyManager"). This module signs in with the user's own OAuth client
(Desktop app type), lists and downloads the snapshots, and replaces a
snapshot's content with a merged database (dbsync.py decides what and
when). Every function takes the user's dbstore.Store, where the credentials
are kept.

Talks to Google's REST endpoints directly with `requests`; no Google client
libraries needed.
"""
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_URL = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"
# Whole-Drive scopes: the snapshot folder is created by another app, so the
# narrower drive.file scope can't see it. Writing the merged database back
# means updating that app's files, which needs full access; without it,
# sync only merges into the working database.
SCOPE_WRITE = "https://www.googleapis.com/auth/drive"
SCOPE_READ = "https://www.googleapis.com/auth/drive.readonly"
DEFAULT_FOLDER = "MoneyManager"
FILE_FIELDS = "id,name,md5Checksum,modifiedTime,size,mimeType"
TIMEOUT = 30
UPLOAD_TIMEOUT = 300

_tokens = {}  # store.root -> {"value": access token, "expires": epoch secs}


class GDriveError(Exception):
    """A problem worth showing to the user as-is."""


# ---------------------------------------------------------------------------
# Stored credentials
# ---------------------------------------------------------------------------

def settings(store):
    return store.load_config().get("gdrive") or {}


def is_configured(store):
    s = settings(store)
    return bool(s.get("client_id") and s.get("client_secret"))


def is_connected(store):
    return is_configured(store) and bool(settings(store).get("refresh_token"))


def folder_name(store):
    return settings(store).get("folder_name") or DEFAULT_FOLDER


def wants_write(store):
    """Whether the user chose to let sync write merged databases back."""
    return settings(store).get("write", True)


def can_write(store):
    """Whether the saved sign-in was granted write access (sign-ins from
    before writing existed were read-only)."""
    return SCOPE_WRITE in settings(store).get("scope", "").split()


def save_credentials(store, client_id, client_secret, folder, write=True):
    """Store the OAuth client. An empty secret keeps the saved one (the form
    never echoes it back). A different client invalidates the old login.
    `write` picks the scope the next sign-in asks for."""
    old = settings(store)
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id:
        raise GDriveError("Client ID is required.")
    secret = client_secret or (old.get("client_secret") if old.get("client_id") == client_id else "")
    if not secret:
        raise GDriveError("Client secret is required.")
    new = {"client_id": client_id, "client_secret": secret, "folder_name": folder.strip() or DEFAULT_FOLDER,
           "write": bool(write)}
    if old.get("client_id") == client_id and old.get("refresh_token"):
        new["refresh_token"] = old["refresh_token"]
        new["scope"] = old.get("scope", "")
    store.save_config({"gdrive": new})
    _tokens.pop(store.root, None)


def disconnect(store):
    store.save_config({"gdrive": {}})
    _tokens.pop(store.root, None)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def auth_url(store, redirect_uri, state):
    return AUTH_URL + "?" + urlencode({
        "client_id": settings(store)["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE_WRITE if wants_write(store) else SCOPE_READ,
        # offline + consent so Google always hands back a refresh token
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


def parse_redirect(pasted):
    """(code, state) from the URL the browser ended up on after consent
    (pasted from the address bar) or from a bare authorization code."""
    pasted = pasted.strip()
    if "://" not in pasted and "code=" not in pasted:
        return pasted, None
    query = parse_qs(urlsplit(pasted).query if "://" in pasted else pasted)
    if query.get("error"):
        raise GDriveError(f"Google sign-in was refused: {query['error'][0]}.")
    if not query.get("code"):
        raise GDriveError("No authorization code found in that URL.")
    return query["code"][0], (query.get("state") or [None])[0]


def _token_request(store, data):
    s = settings(store)
    try:
        r = requests.post(TOKEN_URL, data={
            **data, "client_id": s.get("client_id", ""), "client_secret": s.get("client_secret", ""),
        }, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise GDriveError(f"Couldn't reach Google: {e}") from e
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        if body.get("error") == "invalid_grant":
            raise GDriveError(
                "Google rejected the saved sign-in (revoked, or expired - tokens from an OAuth "
                "app left in \"Testing\" expire after 7 days). Reconnect Google Drive."
            )
        raise GDriveError(f"Google sign-in failed: {body.get('error_description') or body.get('error') or r.status_code}.")
    return body


def exchange_code(store, code, redirect_uri):
    body = _token_request(store, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    if not body.get("refresh_token"):
        raise GDriveError("Google didn't return a refresh token. Remove this app's access at "
                          "myaccount.google.com/permissions and connect again.")
    store.save_config({"gdrive": {**settings(store), "refresh_token": body["refresh_token"],
                                  "scope": body.get("scope", "")}})
    _tokens[store.root] = {"value": body["access_token"], "expires": time.time() + body.get("expires_in", 3600) - 60}


def _access_token(store):
    token = _tokens.get(store.root)
    if token and time.time() < token["expires"]:
        return token["value"]
    if not is_connected(store):
        raise GDriveError("Google Drive isn't connected.")
    body = _token_request(store, {"grant_type": "refresh_token", "refresh_token": settings(store)["refresh_token"]})
    _tokens[store.root] = {"value": body["access_token"], "expires": time.time() + body.get("expires_in", 3600) - 60}
    return body["access_token"]


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------

def _check(r, ok=(200,)):
    if r.status_code not in ok:
        try:
            msg = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = r.text[:200]
        raise GDriveError(f"Google Drive error ({r.status_code}): {msg}")
    return r


def _get(store, path, stream=False, **params):
    try:
        r = requests.get(f"{DRIVE_URL}/{path}", params=params, stream=stream, timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {_access_token(store)}"})
    except requests.RequestException as e:
        raise GDriveError(f"Couldn't reach Google Drive: {e}") from e
    return _check(r)


def _quote(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_folder(store, name):
    files = _get(
        store, "files", q=f"mimeType='application/vnd.google-apps.folder' and name='{_quote(name)}' and trashed=false",
        fields="files(id,name)", orderBy="modifiedTime desc", pageSize=10,
    ).json()["files"]
    if not files:
        raise GDriveError(f"No folder named \"{name}\" found in this Google Drive.")
    return files[0]["id"]


def list_snapshots(store, folder_id):
    """MM*.mmbak files in the folder, newest first."""
    out, token = [], None
    while True:
        body = _get(
            store, "files", q=f"'{folder_id}' in parents and trashed=false and name contains 'MM'",
            fields=f"nextPageToken,files({FILE_FIELDS})",
            orderBy="modifiedTime desc", pageSize=1000, **({"pageToken": token} if token else {}),
        ).json()
        out += [f for f in body["files"] if f["name"].startswith("MM") and f["name"].endswith(".mmbak")]
        token = body.get("nextPageToken")
        if not token:
            return out


def file_meta(store, file_id):
    return _get(store, f"files/{file_id}", fields=FILE_FIELDS).json()


def download(store, file, dest):
    r = _get(store, f"files/{file['id']}", stream=True, alt="media")
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)


def upload(store, file, path):
    """Replace the content of an existing Drive file with the file at
    `path`, keeping its id and name (the app only offers its own backups
    for restoring). Drive keeps the old content as a previous version.
    Returns the file's new metadata."""
    mime = file.get("mimeType") or "application/octet-stream"
    with open(path, "rb") as f:
        data = f.read()
    try:
        # A resumable session, since simple uploads stop at 5 MB.
        r = _check(requests.patch(
            f"{UPLOAD_URL}/files/{file['id']}", params={"uploadType": "resumable"}, data="{}", timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {_access_token(store)}", "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(len(data))},
        ))
        session = r.headers.get("Location")
        if not session:
            raise GDriveError("Google Drive didn't start the upload.")
        _check(requests.put(session, data=data, timeout=UPLOAD_TIMEOUT, headers={"Content-Type": mime}), ok=(200, 201))
    except requests.RequestException as e:
        raise GDriveError(f"Couldn't reach Google Drive: {e}") from e
    return file_meta(store, file["id"])


def check_connection(store):
    """Raise GDriveError unless the folder is reachable; returns the number
    of snapshots in it."""
    return len(list_snapshots(store, find_folder(store, folder_name(store))))
