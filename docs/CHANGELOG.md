# Changelog

Notable changes to Money Mgr web. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- API: `POST /transactions` adds up to 1000 transactions in one request,
  sent as `{"transactions": [...]}`, in order and all or nothing, so the
  lines of a split payment are never half written. A refusal lists every
  transaction that failed and why.
- API: adding a transfer returns its other row as `mirror`.
- API: `GET /categories?show_deleted=show` lists deleted categories too,
  so a transaction that keeps a deleted category can still be shown with
  its name and root. Every category now has `deleted`.
- API: `GET /transactions` filters by `transfer_id` (repeatable), to find
  the other row of a transfer.

## [1.3.0] - 2026-10-04

### Added

- A new app at `/`, laid out like Money Manager on the phone and
  installable as a PWA: transactions by day, calendar and month with
  income, expense and total, and the month's budgets; adding, editing,
  copying and deleting transactions with the app's account grid, category
  picker, amount pad (in any currency) and note suggestions, and transfers
  with a fee; bookmarks; search with filters; stats per category with a pie
  chart and each category's trend, by week, month, year or any period;
  accounts with their balances, a balance chart and each account's
  statement, and modifying an account's balance; adding, renaming,
  reordering, hiding and deleting accounts and categories. The previous
  pages stay as the editor, under More.

### Changed

- The previous pages (transactions, accounts, categories) moved under
  `/editor/`; their old addresses redirect there.

## [1.2.0] - 2026-10-04

### Added

- API: `PATCH /transactions` changes up to 1000 transactions in one
  request, in order and all or nothing. A refusal lists every change that
  failed and why.
- API: `expect` on a transaction PATCH, the values the transaction must
  have first, so a script can check and change a row in one call. A
  mismatch answers 412 and writes nothing.

### Changed

- API: a transaction's `updated_ms`, and `updated_since` and
  `sort=updated` with it, are now when MMW saw the row change, including
  rows that came in from the app at a sync. Before, a synced row kept the
  time it changed on the phone, so a client reading incrementally could
  miss it. The app's own time is `app_updated_ms`.
- Edits made in the pages follow the same rules as the API. Editing either
  row of a transfer (note, description, date, time, amount) updates the
  other row too, and a transfer's receiving row no longer offers an account
  to move it to. A category must belong to the row's income or expense
  tree.
- Account and category edits are checked: names can't be empty, an
  account's status and order must be valid, and a category can't take the
  name of another one beside it in the tree.

## [1.1.0] - 2026-10-04

### Added

- The version shows in the page header and under Settings > About.
- Delete transactions from the Transactions page, one at a time or in bulk
  with the row checkboxes. Deletes are staged like other edits and written
  on Save. Like in the app, they are soft-deleted and a transfer goes with
  its other leg and fee.
- API: `entered_amount` and `entered_currency` when adding or editing a
  transaction, written as given (for foreign amounts, or restating pre-euro
  amounts without losing the exact entered value).
- API: `to_amount` when adding a transfer, the amount that arrived.
- API: editing either row of a transfer updates the other row's date, time,
  note and description (and amount in the same currency, or `to_amount`),
  and returns it as `mirror`.
- API: `type` can change between income and expense, and between the two
  balance adjustments, keeping the transaction's uid.
- API: `updated_since` filter on the transaction list, and the server's
  time zone in `/status`.

### Changed

- API: editing a transfer's row no longer leaves its other row's date, time,
  note and description behind (see above).

### Fixed

- Sync could leave a transaction's date (`WDATE`) and its timestamp
  (`ZDATE`) days apart, when one side changed the date and the other only
  the timestamp. The two now merge together: such a change is one conflict
  over both, and picking a side takes both from it.
- Edits made in the pages now set the transaction's last-change time, so
  they show up in `updated_since`.

## [1.0.0] - 2026-10-02

### Added

- Prebuilt Docker images for amd64 and arm64 at
  `ghcr.io/vasilkalchev/money-mgr-web`.
- A JSON API at `/api/v1/` for scripts and other apps: list and filter
  transactions, read accounts, categories and currencies, and add, edit and
  delete transactions. Each user makes read-only or read-write tokens under
  **Settings > API tokens**. Documented as an OpenAPI spec in
  `docs/openapi.yaml`.
- Two-way sync with the Money Manager app. Syncing merges the app's newer
  backup into the database here instead of replacing it: new transactions
  from the app come in, and edits, renames and transactions added here are
  kept. A review page shows what changed on each side before the merge is
  applied, with the old values of changed fields. Possible duplicates (the
  same transaction entered on both sides), conflicting edits and anything the
  merge would break are settled there too.
  - Google Drive: "Sync now" merges the newest backup and writes the result
    back to that same file on Drive, ready to restore in the app. With
    nothing new on Drive, it uploads the changes made here.
  - Manual: "Sync with a newer export" merges an uploaded `.mmbak`, and
    "Download" gives back the result to restore in the app.
- Settings shows when the database was last synced and whether it has
  changes the app doesn't have yet.
- User accounts with a login page. The first run asks for an admin account,
  protected by a one-time setup code printed in the log;
  admins add, remove and promote users and reset passwords under **Users**.
  Everyone can change their own password in Settings. Failed logins are
  throttled, and all forms and edits are CSRF-protected.
- Each user has their own database, Google Drive connection, backups and
  settings under `users/<name>/` in the data folder.
- `MMW_TRUSTED_PROXIES`: reverse proxy addresses whose `X-Forwarded-For` is
  trusted for the client address.
- Optional login through a reverse proxy that authenticates (Authelia,
  Authentik, oauth2-proxy, ...): `MMW_AUTH_HEADER` names the username header,
  honored only from `MMW_TRUSTED_PROXIES`.
- `MMW_COOKIE_SECURE=1` marks the session cookie HTTPS-only and sends HSTS.
- Security headers on every response: Content-Security-Policy,
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy` and `no-store`
  caching.
- README section on deploying publicly, including Nginx Proxy Manager.
- Backup settings in Settings (under "Advanced options"): how many backups to
  keep, whether to back up before the first edit, whether to write `diff.txt`,
  and a "Back up now" button. `MMW_BACKUP_KEEP` is now only the default.
- An "Advanced" toggle that hides the less common controls: on Transactions,
  the amount, sort and visibility filters, the note/description "has" filters,
  the year/period pickers and transaction grouping. Filters that are in use
  stay visible. The toggle is shared across pages and remembered per browser.

- Docker image packaged like linuxserver.io's: `PUID`/`PGID` (the container
  fixes `/config`'s owner and drops to that user), `UMASK`, `TZ` (with
  time zone data), `FILE__` prefixed variables for docker secrets, and a
  startup banner with the version and settings.
- `GET /healthz` and a `HEALTHCHECK` in the image.
- Image labels with the version and build date (`--build-arg VERSION=...`,
  `BUILD_DATE=...`).
- Installable as an app (PWA) on phones and desktops, with its own icon and
  window. Pages fit narrow screens better, and losing the connection shows
  an offline page. Nothing is stored on the device. Installing needs HTTPS
  (or localhost).

### Changed

- Google Drive now asks for full Drive access so sync can write the merged
  database back (writing can be turned off at setup, which keeps read-only
  access). Existing read-only connections keep working but don't upload;
  reconnect to allow it.
- Uploading a file or pulling from Drive without merging ("Replace the
  database") moved under Settings -> Advanced options. "Sync now" no longer
  refuses when there are local edits.
- Backups taken before a sync merge also keep the app's file as
  `incoming.mmbak`.
- The project is abbreviated MMW: environment variables are renamed from
  `MM_*` to `MMW_*` (`MMW_DATA_DIR`, `MMW_BACKUP_KEEP`, ...), and browser
  settings move from `mm_*` to `mmw_*` keys (carried over automatically).
- The image keeps its state in `/config` instead of `/data`. A `/data`
  mount is still used, with a warning, while `/config` is empty.
- `docker-compose.yml`: the service and container are called `mmw`, and it
  bind-mounts `./data` at `/config` instead of the `mm-data` volume. The
  README shows how to copy an existing volume over.
- `docker-compose.yml` publishes the port on `127.0.0.1` only.
- Data folder layout: `app.json` plus `users/<name>/{config.json,db,backups}`.
  An existing single-user `config.json`, `db/` and `backups/` move to the
  first account created. The session key moves to `app.json`, so existing
  browser sessions are dropped once.
- The database source in the header is now a badge ("Google Drive" or
  "Uploaded") instead of being appended to the file name.

## [0.1.0] - 2026-10-02

### Added

- Self-hosted Flask viewer/editor for a Money Manager `.mmbak` database:
  transactions (filters, inline and bulk edits, adding rows), accounts and
  categories.
- One managed working database in the data directory, installed by upload or
  pulled from Google Drive (read-only, the user's own OAuth client).
- Automatic backups with a `diff.txt` against the previous one.
- Docker image and compose file.
