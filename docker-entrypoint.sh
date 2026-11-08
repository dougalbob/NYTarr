#!/bin/sh
set -eu
umask 027
if [ "$(id -u)" = 0 ]; then
    PUID=${PUID:-99}
    PGID=${PGID:-100}
    case "$PUID:$PGID" in *[!0-9:]*|:*|*:) echo 'PUID and PGID must be numeric' >&2; exit 1;; esac
    [ "$PUID" -gt 0 ] || { echo 'PUID must be non-root' >&2; exit 1; }
    mkdir -p "${CONFIG_DIR:-/config}"
    # Only appdata; never recursively change ownership of a media library.
    chown -R "$PUID:$PGID" "${CONFIG_DIR:-/config}"
    exec gosu "$PUID:$PGID" "$@"
fi
exec "$@"
