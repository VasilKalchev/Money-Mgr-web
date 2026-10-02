# Tests and verifying changes

Run the tests with `uv run pytest` (from the repo root; `pyproject.toml`
puts `src/` on the path and `uv` installs the `dev` dependency group). `conftest.py`
points `MMW_DATA_DIR` at a scratch dir, so real data is never touched, and
`mmbak.py` builds a small synthetic `.mmbak` (user_version 19) used by
the `client` fixture (a logged-in admin with that database installed).
`mmbak.py` also has `execute()`/`query()`/`add_tx()` for editing the
synthetic databases.

Add a test next to the code you change:

- `test_users.py`: accounts, throttling
- `test_dbstore.py`: install, backups
- `test_filters.py`: /transactions filters
- `test_edits.py`: PATCH/POST write routes, upload
- `test_gdrive.py`: OAuth and Drive I/O, `requests` faked
- `test_merge.py`: the merge engine
- `test_sync.py`: sync flows and routes, against the in-memory Drive in
  `fakedrive.py`
- `test_security.py`: auth, CSRF, proxy trust
- `test_api.py`: `/api/v1/` and API tokens (the `api` fixture is a
  cookie-less client with a read-write token)
- `test_openapi.py`: `docs/openapi.yaml` lists exactly the `/api/v1/`
  routes and real responses fit its schemas
- `test_pwa.py`: manifest, service worker

## Ad-hoc checks against real data

Use Flask's test client rather than starting a live server. Every route
needs a logged-in session (set `user` and its current `ver`); non-GET
requests also need the `X-CSRF-Token` header (or `csrf_token` field)
matching `session["csrf"]`:

```
cd src && python3 -c "
from app import app; import users
c = app.test_client(); name = next(iter(users.all_users()))
with c.session_transaction() as s: s.update(user=name, ver=users.get(name)['session_version'], csrf='t')
r = c.get('/transactions?page_size=25'); print(r.status_code)"
```

Run it from `src/`. To experiment without touching real data, point
`MMW_DATA_DIR` at a scratch copy.

For filter logic, also regex the rendered HTML for the "matching
transactions" count to confirm a filter actually narrows results against
the real data, not just that the page returns 200.
