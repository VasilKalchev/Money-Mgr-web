"""The installable app: manifest, service worker and offline page."""
import pytest

import app as appmod
import users


@pytest.fixture
def anon(data_dir):
    users.create("alice", "correct horse")
    return appmod.app.test_client()


def test_manifest_is_public_and_its_icons_exist(anon):
    r = anon.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.mimetype == "application/manifest+json"
    m = r.get_json(force=True)
    assert m["start_url"] == "/" and m["display"] == "standalone"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    for icon in m["icons"]:
        assert anon.get(icon["src"]).status_code == 200


def test_service_worker_is_public_at_the_root(anon):
    r = anon.get("/sw.js")
    assert r.status_code == 200 and r.mimetype == "text/javascript"
    # Always revalidated, so a new version reaches browsers on the next load.
    assert r.headers["Cache-Control"] in ("no-cache", "no-store")
    assert "/static/offline.html" in r.get_data(as_text=True)


def test_offline_page_is_public(anon):
    assert anon.get("/static/offline.html").status_code == 200


def test_pages_link_the_manifest_and_register_the_worker(client):
    html = client.get("/settings").get_data(as_text=True)
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert "serviceWorker.register('/sw.js')" in html
