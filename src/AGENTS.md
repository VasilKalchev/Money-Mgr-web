# Backend (`src/`)

## Modules

- `app.py`: all routes, SQL, and helpers, including login/CSRF hooks and
  the user admin routes. No blueprints/models.
- `dbstore.py`: where data lives: `DATA_DIR` (`MMW_DATA_DIR` env, `/config`
  in the image), app-wide `app.json`, and `Store`, one user's
  `config.json`, managed database, schema-version validation,
  `install_db()` and backups.
- `users.py`: accounts in `app.json` (werkzeug password hashes), username
  rules, login throttling.
- `gdrive.py`, `merge.py`, `dbsync.py`: Drive/OAuth, the three-way merge and
  sync orchestration. See `docs/SYNC.md` before changing them.
- `templates/` and `static/` have their own `AGENTS.md`.

## Data layout

`<DATA_DIR>/app.json` (session key, `users`) and `<DATA_DIR>/users/<name>/`
with `config.json`, `db/current.mmbak`, `backups/` and `sync/`. Routes get
the logged-in user's `Store` from `current_store()`
(`dbstore.store_for(g.user)`); never build data paths by hand. Removed
users' folders are moved to `users/.removed/`.

Pre-multi-user installs kept `config.json`, `db/` and `backups/` at the
top of `DATA_DIR`; `dbstore.adopt_legacy_data()` moves them to the first
user created.

## Auth, CSRF and headers

- Auth (`_authenticate()` before_request): a trusted proxy header
  (`MMW_AUTH_HEADER`, honored only from `MMW_TRUSTED_PROXIES` addresses/CIDRs;
  unknown names get a password-less account) wins, else the Flask session
  (`user` + `ver`, which must match the user's `session_version`; bumping
  it on password change/removal logs other sessions out). With no users at
  all, pages go to `/setup/account` to create the admin, which needs the
  setup code from `users.setup_code()` (kept in `app.json`, printed to the
  log at startup). Unauthenticated `/api/` calls get 401 JSON. Admin-only
  routes call `_require_admin()`. Use `client_ip()`, not
  `request.remote_addr`, for the client's address (it honors
  `X-Forwarded-For` from trusted proxies).
- `_security_headers()` (after_request) sends the CSP, `X-Frame-Options`,
  `no-store` caching and HSTS (with `MMW_COOKIE_SECURE`). The CSP only
  allows same-origin resources: keep JS/CSS inline or served by the app,
  and add any new external form/redirect target to `form-action`.
- CSRF (`_check_csrf()`): every non-GET request needs the session token,
  as a `csrf_token` form field (`{{ csrf_token() }}`) or the
  `X-CSRF-Token` header, which `base.html` adds to every same-origin
  `fetch`.

## Supported databases

Only `.mmbak` files whose `PRAGMA user_version` is in
`SUPPORTED_USER_VERSIONS` (`dbstore.py`, currently `{19}`) are installed or
opened. `_require_db()` shows `unsupported_db.html` (409; JSON 409 for
`/api/` routes) otherwise, and `get_db(readonly=False)` refuses too. Add a
version only after checking its schema against `docs/MM_DB_SCHEMA.md`.

## Database access pattern

- `get_db(readonly=True)` opens the SQLite file in read-only URI mode by
  default; pass `readonly=False` for writes (this also triggers the
  one-time backup).
- `query(sql, params)`: read helper, returns `fetchall()`.
- `execute(sql, params)`: write helper, commits and closes.
- Row factory is `sqlite3.Row`, so results are accessed by column name.
- Filter bars build SQL dynamically with parallel `filters` (SQL fragments)
  and `params` (bound values) lists, joined with `AND`. See `/transactions`
  in `app.py` for the pattern to follow when adding new filters.

## Key schema gotchas (see docs/MM_DB_SCHEMA.md for full detail)

- `INOUTCOME.AMOUNT_ACCOUNT` is always a positive magnitude in the
  transaction's own **account's currency** (`ASSETS.currencyUid`, joined as
  `acu`/`account_currency_iso` in `/transactions`). Sign is implied by
  `DO_TYPE`, not stored.
- `INOUTCOME.IN_ZMONEY` is the amount as originally **entered** by the user,
  in `INOUTCOME.currencyUid` (joined as `cu`/`currency_iso`). This can
  differ from the account's currency when the app auto-converts on entry.
  Don't conflate `currency_iso` (entered currency) with
  `account_currency_iso` (account currency) when labeling amount columns.
- `ASSETS.ZDATA` is an app-internal status flag (0=normal, 1=soft-deleted,
  2=unknown, 3=hidden in UI), not a text note.
- `ZCATEGORY` rows have `STATUS` 0 (root/top-level) or 2 (child); parent
  link is `pUid`, matched within the same `TYPE` (0=income, 1=expense).
