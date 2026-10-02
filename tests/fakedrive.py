"""An in-memory Google Drive for tests: gdrive.py's `requests` calls are
answered from a dict of files instead of the network."""
import hashlib

import gdrive


class Resp:
    def __init__(self, status=200, body=None, content=b"", text="", headers=None):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": "application/json"} if body is not None else {}
        self.headers.update(headers or {})
        self.content = content
        self.text = text or str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, n):
        yield self.content


class FakeDrive:
    """One folder ("MoneyManager", id folder1) of files. add() puts a file
    in it; uploads (resumable PATCH + PUT) replace a file's content."""

    def __init__(self, monkeypatch, store):
        self.files = {}  # id -> {"name", "data", "modifiedTime", "mimeType"}
        self.uploads = []  # ids written to
        self.requests = []
        self.clock = 0
        monkeypatch.setitem(gdrive._tokens, store.root, {"value": "at", "expires": 9e99})
        monkeypatch.setattr(gdrive.requests, "get", self.get)
        monkeypatch.setattr(gdrive.requests, "patch", self.patch)
        monkeypatch.setattr(gdrive.requests, "put", self.put)

    def _tick(self):
        self.clock += 1
        return f"2026-01-01T00:{self.clock // 60:02d}:{self.clock % 60:02d}.000Z"

    def add(self, id_, name, data):
        if isinstance(data, str):
            data = open(data, "rb").read()
        self.files[id_] = {"name": name, "data": data, "modifiedTime": self._tick(),
                           "mimeType": "application/octet-stream"}
        return self.meta(id_)

    def data(self, id_):
        return self.files[id_]["data"]

    def meta(self, id_):
        f = self.files[id_]
        return {"id": id_, "name": f["name"], "md5Checksum": hashlib.md5(f["data"]).hexdigest(),
                "modifiedTime": f["modifiedTime"], "mimeType": f["mimeType"], "size": str(len(f["data"]))}

    def get(self, url, params, stream, timeout, headers):
        self.requests.append(("GET", url, params))
        assert headers["Authorization"] == "Bearer at"
        if url.endswith("/files") and "mimeType" in params["q"]:
            return Resp(200, {"files": [{"id": "folder1", "name": "MoneyManager"}]})
        if url.endswith("/files"):
            metas = sorted((self.meta(i) for i in self.files), key=lambda m: m["modifiedTime"], reverse=True)
            return Resp(200, {"files": metas})
        fid = url.rsplit("/", 1)[1]
        if fid not in self.files:
            return Resp(404, {"error": {"message": "File not found"}})
        if params.get("alt") == "media":
            return Resp(200, content=self.files[fid]["data"])
        return Resp(200, self.meta(fid))

    def patch(self, url, params, data, timeout, headers):
        self.requests.append(("PATCH", url, params))
        assert headers["Authorization"] == "Bearer at" and params == {"uploadType": "resumable"}
        fid = url.rsplit("/", 1)[1]
        if fid not in self.files:
            return Resp(404, {"error": {"message": "File not found"}})
        return Resp(200, {}, headers={"Location": f"session:{fid}"})

    def put(self, url, data, timeout, headers):
        self.requests.append(("PUT", url, None))
        fid = url.split(":", 1)[1]
        self.files[fid]["data"] = data
        self.files[fid]["modifiedTime"] = self._tick()
        self.uploads.append(fid)
        return Resp(200, self.meta(fid))
