# Money Mgr web (MMW)

A self-hosted Flask viewer/editor for a Money Manager (Android app) SQLite
export (`.mmbak` file). Multi-user: each account has its own database, Drive
connection, backups and settings.

## Running

```
python3 src/app.py      # local, port 5732, debug on, state in ./data
docker compose up -d --build   # self-hosted, ./data mounted at /config
```

The project is abbreviated MMW (env vars `MMW_*`, `localStorage` keys
`mmw_*`, image/container `mmw`). "MM" alone means the Money Manager app
itself (`MM*.mmbak` files, `docs/MM_DB_SCHEMA.md`).

## Where things are

Each area has its own `AGENTS.md` with the details; read the one for the
area you are working in.

- `src/` - the Python backend: `app.py` (all routes, SQL, auth/CSRF),
  `dbstore.py`, `users.py`, `gdrive.py`, `merge.py`, `dbsync.py`.
  See `src/AGENTS.md`.
  - `src/templates/` - Jinja2 pages and the frontend conventions.
    See `src/templates/AGENTS.md`.
  - `src/static/` - PWA files. See `src/static/AGENTS.md`.
- `tests/` - pytest suite and how to verify changes. See `tests/AGENTS.md`.
- `docker/`, `Dockerfile`, `docker-compose.yml` - image packaging.
  See `docker/AGENTS.md`.
- `docs/SYNC.md` - how sync, Drive/OAuth and backups work. Read it before
  touching `dbsync.py`, `merge.py`, `gdrive.py` or the backup code in
  `dbstore.py`.
- `docs/MM_DB_SCHEMA.md` - reverse-engineered schema notes; read this before
  writing new queries against unfamiliar tables/columns.
- `docs/CHANGELOG.md` - add user-visible changes under `[Unreleased]`.
- `data/` - local-run state; the compose file mounts it at `/config`.
