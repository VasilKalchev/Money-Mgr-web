# Changelog

Notable changes to Money Mgr web. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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

### Changed

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
