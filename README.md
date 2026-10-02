# Money Mgr web (MMW)

A self-hosted web version of the Money Manager Android app. It works on the
app's own database export (the `.mmbak` backup file): you upload a backup or
pull it from Google Drive, view and edit your transactions, accounts and
categories in the browser, and restore the result in the app. It has user
accounts, and each user has their own database, Google Drive connection,
backups and settings.

> Money Mgr web is an independent project. It is not affiliated with or
> endorsed by Realbyte Inc., the maker of Money Manager. "Money Manager" is a
> trademark of its owner and is used here only to say which app this project
> works with.

## Running with Docker

The image follows the conventions of [linuxserver.io](https://www.linuxserver.io/)
images: state in `/config`, `PUID`/`PGID`/`UMASK`/`TZ` settings, `FILE__`
secrets and a startup banner in the log.

### docker compose (recommended)

```yaml
services:
  mmw:
    build: .
    image: mmw:latest
    container_name: mmw
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - ./data:/config:z
    ports:
      - "127.0.0.1:5732:5732"
    restart: unless-stopped
```

This is the repo's `docker-compose.yml`, so `docker compose up -d --build`
is enough.

### docker cli

```
docker build -t mmw .
docker run -d \
  --name=mmw \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=Etc/UTC \
  -p 127.0.0.1:5732:5732 \
  -v /path/to/mmw/data:/config:z \
  --restart unless-stopped \
  mmw
```

Open http://localhost:5732. The examples only publish the port on this
machine (`127.0.0.1`); see [Deploying publicly](#deploying-publicly) before
exposing it.

### Parameters

| Parameter | Function |
| --- | --- |
| `-p 5732` | The web UI (plain HTTP). |
| `-e PUID=1000` | User id the app runs as and that owns `/config`, see below. |
| `-e PGID=1000` | Group id, see below. |
| `-e TZ=Etc/UTC` | [Time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for transaction times and backup names, e.g. `Europe/Sofia`. |
| `-e UMASK=022` | Umask for the files the app writes. |
| `-e MMW_BACKUP_KEEP=30` | Default number of backups to keep per user; Settings -> Advanced options overrides it. |
| `-e MMW_COOKIE_SECURE=1` | The app is only reached over HTTPS. Marks the login cookie HTTPS-only and sends HSTS. |
| `-e MMW_TRUSTED_PROXIES=` | Comma-separated addresses or CIDRs of your reverse proxy. From these, `X-Forwarded-For` is trusted for the client's real address (used by login throttling), and so is `MMW_AUTH_HEADER`. |
| `-e MMW_AUTH_HEADER=` | See [Logging in through an SSO proxy](#logging-in-through-an-sso-proxy). |
| `-v /config` | Everything stateful: accounts, settings, the databases and their backups. |

Outside the image, `MMW_DATA_DIR` sets where state is kept (`./data` by
default; the image sets it to `/config`).

### User / group identifiers

The container starts as root only to make `PUID:PGID` the owner of
`/config`, then runs the app as that user. Set them to the owner of the host
folder you mount (`id -u`, `id -g`) so files stay editable from the host.
Starting the container with `--user` skips this and ignores `PUID`/`PGID`.

### Docker secrets

Any variable can be read from a file by prefixing its name with `FILE__`,
e.g. `-e FILE__MMW_TRUSTED_PROXIES=/run/secrets/proxies`.

### Health check

The image reports its health through `GET /healthz`, which needs no login.

### Upgrading from 0.1.0

The image used to keep its state in a `/data` volume (`mm-data` in the old
compose file). It still uses `/data` while that's mounted and `/config` is
empty, but logs a warning. To move a named volume to the new `./data` bind
mount:

```
docker run --rm -v money-mgr-web_mm-data:/from -v "$PWD/data":/to:z alpine cp -a /from/. /to/
```

The `MM_*` environment variables are now called `MMW_*`.

On first run the app asks you to create the admin account. It needs a setup
code that's printed in the log (`docker logs mmw`), so
nobody else who reaches a fresh install can claim it. If the data directory
already holds a database from a version without accounts, it becomes the
admin's. The admin adds more users under **Users**.

After logging in, each user is asked how to get their database:

- **Upload manually**: pick a `.mmbak` exported from the Money Manager app.
- **Google Drive**: the setup page walks you through creating your own Google
  API project and OAuth client (Desktop app), then pulls the newest
  `MM*.mmbak` from the Drive folder the app backs up to (default
  `MoneyManager`).

## Syncing with the app

Edits made here and new transactions entered in the app are merged, so
neither side has to be thrown away:

- **Google Drive**: Settings -> "Sync now" merges the newest `MM*.mmbak` in
  the folder into the database here, then writes the result back to that
  same file on Drive (same name, since the app only lists its own backups).
  Restore that backup in Money Manager to get the changes made here onto your
  phone. Drive keeps the file's previous version, and the app's original is
  also kept in the local backup taken before the merge.
- **Manual**: Settings -> "Sync with a newer export" merges an uploaded
  `.mmbak`, and "Download" gives you the result to restore in the app.

Rows are matched by their ids, so renamed accounts and categories or edited
transactions merge cleanly with what was added in the app. Before anything is
applied, a review page lists what changed in the app and here: new, changed
and deleted transactions (with the old values of changed fields), accounts,
categories and other rows. It also asks about anything that can't be decided
automatically:

- a transaction added on both sides that looks like the same one entered
  twice: keep the app's, keep this one, or keep both;
- the same field changed differently on both sides, or a row changed on one
  side and deleted on the other: pick which version to keep;
- anything the merge would break, such as a new transaction in a category
  the other side deleted: fix it first, then check again.

Settings -> Advanced options can still replace the database outright.

## Installing as an app

MMW is a progressive web app: "Install app" in Chrome/Edge or "Add to Home
Screen" in Safari gives it its own icon and window. Browsers only offer this
over HTTPS (or on localhost), so deploy it behind a TLS proxy as below. The
installed app still needs the server: nothing is stored on the device, and
without a connection it shows an offline page.

## Deploying publicly

The app speaks plain HTTP and has its own login, but it holds financial data
and, if Drive sync is set up, a token for the user's whole Google Drive,
read-write unless writing was turned off at setup (Google has no folder-only
scope that can see the app's backups). The safest setup is not to
expose it at all and reach it over a VPN such as Tailscale or WireGuard. If it
has to be public:

1. Create the admin account before the app is reachable from outside.
2. Put it behind a reverse proxy that terminates TLS, and don't publish its
   port: the proxy should reach it over a Docker network.
3. Set `MMW_COOKIE_SECURE=1` and `MMW_TRUSTED_PROXIES` to the proxy's address.
4. Use strong passwords. There's no two-factor login built in; for that, put
   an SSO proxy in front (below).

The app sends its own security headers (CSP, `X-Frame-Options: DENY`,
`no-store` caching, HSTS with `MMW_COOKIE_SECURE`).

### With Nginx Proxy Manager

Put the app on NPM's Docker network instead of publishing a port, e.g. in a
`docker-compose.override.yml`:

```yaml
services:
  mmw:
    ports: !reset []
    networks: [npm]
    environment:
      - MMW_COOKIE_SECURE=1
      - MMW_TRUSTED_PROXIES=172.18.0.0/16   # NPM's network subnet, see below

networks:
  npm:
    external: true
    name: npm_default   # see `docker network ls`
```

Find the network name with `docker network ls` and its subnet with
`docker network inspect <name> --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'`.

In NPM, add a proxy host: forward to `mmw`, port `5732`, scheme
`http`. On the SSL tab request a Let's Encrypt certificate and turn on
*Force SSL* and *HTTP/2*. If uploading a large `.mmbak` fails with a 413,
raise the limit in the host's *Advanced* tab: `client_max_body_size 512m;`.

NPM's *Access List* can add HTTP basic auth or an IP allow-list as an extra
layer, but it doesn't provide SSO or two-factor login.

### Logging in through an SSO proxy

If a proxy in front already authenticates users (Authelia, Authentik,
oauth2-proxy, ...), the app can trust the username it passes on:

```
MMW_TRUSTED_PROXIES=172.18.0.0/16   # the proxy's address(es)
MMW_AUTH_HEADER=Remote-User         # the header carrying the username
```

The header is only honored from `MMW_TRUSTED_PROXIES`, because anyone who can
reach the app's port directly could otherwise send it, and the proxy must
overwrite any `Remote-User` the client sends (the SSO proxies above do).
Don't set `MMW_AUTH_HEADER` behind a proxy that doesn't authenticate, such as
plain NPM. Usernames are lowercased, and unknown ones get an account
automatically (without a password, so they can only log in through the proxy
until they set one in Settings). The built-in login keeps working alongside.

Failed logins are throttled: 5 per username and 20 per client address per 15
minutes.

## Running locally

```
uv run python src/app.py   # needs uv: https://docs.astral.sh/uv/
```

Runs on port 5732 with debug mode on and stores its state in `./data`, the
same folder the compose file mounts (don't run both at once).

## Layout

- `src/app.py`: all routes, SQL, and helpers.
- `src/dbstore.py`: where data lives: app-wide config and each user's database (paths, validation, install, backups) and settings.
- `src/users.py`: accounts, passwords and login throttling.
- `src/gdrive.py`: Google OAuth and Drive access.
- `src/merge.py`: the three-way merge of two databases.
- `src/dbsync.py`: syncing with Drive or an uploaded export, and the review.
- `src/templates/`: Jinja2 templates, one per page.
- `src/static/`: icons, the PWA manifest, service worker and offline page.
- `docker/entrypoint.sh`: container init (`FILE__` secrets, `PUID`/`PGID`, banner).
- `docs/MM_DB_SCHEMA.md`: reverse-engineered notes on the `.mmbak` schema.

## Backups

Each user's working database is copied to
`<data>/users/<name>/backups/<timestamp>/` (with a
`diff.txt` against the previous backup) before the first write after startup
and before any upload or sync replaces it (a sync merge also keeps the app's
file there as `incoming.mmbak`). Settings -> Advanced options sets
how many to keep, turns off the first-write backup or the diff, and has a
"Back up now" button.
