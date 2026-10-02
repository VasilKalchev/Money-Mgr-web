"""Google Drive pull-sync for the Money Manager snapshots.

The Android app writes MM*.mmbak snapshots into a Drive folder (default
"MoneyManager"). This module signs in with the user's own OAuth client
(Desktop app type, read-only Drive scope), finds the newest snapshot and
installs it as that user's working database. Nothing is ever written to
Drive. Every function takes the user's dbstore.Store, where the
credentials are kept.

Talks to Google's REST endpoints directly with `requests`; no Google client
libraries needed.
"""
import os
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

import dbstore

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_URL = "https://www.googleapis.com/drive/v3"
# Read-only, but whole-Drive: the snapshot folder is created by another app,
# so the narrower drive.file scope can't see it.
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DEFAULT_FOLDER = "MoneyManager"
TIMEOUT = 30

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


def save_credentials(store, client_id, client_secret, folder):
    """Store the OAuth client. An empty secret keeps the saved one (the form
    never echoes it back). A different client invalidates the old login."""
    old = settings(store)
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id:
        raise GDriveError("Client ID is required.")
    secret = client_secret or (old.get("client_secret") if old.get("client_id") == client_id else "")
    if not secret:
        raise GDriveError("Client secret is required.")
    new = {"client_id": client_id, "client_secret": secret, "folder_name": folder.strip() or DEFAULT_FOLDER}
    if old.get("client_id") == client_id and old.get("refresh_token"):
        new["refresh_token"] = old["refresh_token"]
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
        "scope": SCOPE,
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
    store.save_config({"gdrive": {**settings(store), "refresh_token": body["refresh_token"]}})
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

def _get(store, path, stream=False, **params):
    try:
        r = requests.get(f"{DRIVE_URL}/{path}", params=params, stream=stream, timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {_access_token(store)}"})
    except requests.RequestException as e:
        raise GDriveError(f"Couldn't reach Google Drive: {e}") from e
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = r.text[:200]
        raise GDriveError(f"Google Drive error ({r.status_code}): {msg}")
    return r


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
            fields="nextPageToken,files(id,name,md5Checksum,modifiedTime,size)",
            orderBy="modifiedTime desc", pageSize=1000, **({"pageToken": token} if token else {}),
        ).json()
        out += [f for f in body["files"] if f["name"].startswith("MM") and f["name"].endswith(".mmbak")]
        token = body.get("nextPageToken")
        if not token:
            return out


def download(store, file, dest):
    r = _get(store, f"files/{file['id']}", stream=True, alt="media")
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)


def check_connection(store):
    """Raise GDriveError unless the folder is reachable; returns the number
    of snapshots in it."""
    return len(list_snapshots(store, find_folder(store, folder_name(store))))


def sync(store, overwrite=False):
    """Pull the newest snapshot into the working database.

    Returns (status, name): "installed", "up_to_date", or "local_modified" when
    a newer snapshot exists but the working db has unsynced edits and
    `overwrite` wasn't given (nothing is changed then)."""
    snapshots = list_snapshots(store, find_folder(store, folder_name(store)))
    if not snapshots:
        raise GDriveError(f"No MM*.mmbak snapshots in the \"{folder_name(store)}\" folder.")
    latest = snapshots[0]

    if store.db_exists():
        info = store.db_info()
        if info.get("remote_id") == latest["id"] and info.get("remote_md5") == latest.get("md5Checksum"):
            return "up_to_date", latest["name"]
        if store.local_modified() and not overwrite:
            return "local_modified", latest["name"]

    staged = store.staging_path()
    try:
        download(store, latest, staged)
        if latest.get("md5Checksum") and dbstore.file_md5(staged) != latest["md5Checksum"]:
            raise GDriveError("Download was corrupted (checksum mismatch); try again.")
    except Exception:
        os.remove(staged)
        raise
    store.install_db(
        staged, "gdrive", latest["name"],
        remote_id=latest["id"], remote_md5=latest.get("md5Checksum"), remote_modified=latest.get("modifiedTime"),
    )
    return "installed", latest["name"]
