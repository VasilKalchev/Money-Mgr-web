# Docker packaging

This is becoming a self-hosted docker project, packaged the linuxserver.io
way: `/config` volume, `PUID`/`PGID`/`UMASK`/`TZ`, `FILE__` secrets and a
startup banner, all in `docker/entrypoint.sh` (starts as root, chowns
`/config`, drops to `PUID:PGID` with `su-exec`). A legacy `/data` mount
is still used while `/config` is empty. `GET /healthz` (no login) backs the
image's `HEALTHCHECK`. The image runs gunicorn with a
single worker (the first-write backup guard, Drive token cache and login
throttling are per-process), so don't add workers.
