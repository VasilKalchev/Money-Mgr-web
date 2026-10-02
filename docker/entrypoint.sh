#!/bin/sh
# Container init, after linuxserver.io's images: FILE__ secrets, the legacy
# /data mount, PUID/PGID ownership, UMASK, a startup banner, then the app
# as the unprivileged PUID:PGID.
set -e

log() { echo "[mmw-init] $*"; }

# FILE__NAME=/run/secrets/x sets NAME to that file's contents (docker secrets).
for var in $(env | sed -n 's/^FILE__\([A-Za-z0-9_]*\)=.*/\1/p'); do
    eval "file=\$FILE__$var"
    if [ -r "$file" ]; then
        export "$var=$(cat "$file")"
        log "$var set from $file"
    else
        log "FILE__$var: can't read $file" >&2
    fi
done

# Images up to 0.1.0 kept their state in /data. Keep using it while that's
# what is mounted and /config is still empty.
if [ "$MMW_DATA_DIR" = /config ] && [ -n "$(ls -A /data 2>/dev/null)" ] && [ -z "$(ls -A /config 2>/dev/null)" ]; then
    log "Using /data. It is deprecated: mount the volume at /config instead."
    export MMW_DATA_DIR=/data
fi

umask "${UMASK:-022}"

if [ "$(id -u)" = 0 ]; then
    PUID=${PUID:-1000}
    PGID=${PGID:-1000}
    mkdir -p "$MMW_DATA_DIR"
    # Only walk the whole tree when the top folder's owner is off (first run,
    # or PUID/PGID changed), so restarts stay fast.
    if [ "$(stat -c %u:%g "$MMW_DATA_DIR")" != "$PUID:$PGID" ]; then
        log "Setting the owner of $MMW_DATA_DIR to $PUID:$PGID"
        chown -R "$PUID:$PGID" "$MMW_DATA_DIR"
    fi
    # PUID needn't have a passwd entry, so su-exec would set HOME to /.
    set -- su-exec "$PUID:$PGID" env HOME=/tmp "$@"
else
    # Started with --user: PUID/PGID don't apply.
    PUID=$(id -u)
    PGID=$(id -g)
fi

cat <<EOF
───────────────────────────────────────
  MMW (Money Mgr web) ${MMW_VERSION:-dev}
───────────────────────────────────────
User UID:    $PUID
User GID:    $PGID
Umask:       $(umask)
Timezone:    ${TZ:-UTC}
Data folder: $MMW_DATA_DIR
───────────────────────────────────────
EOF

exec "$@"
