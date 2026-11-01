#!/bin/sh
# Apply migrations (serialized with flock so concurrent starts can't race), then exec the CLI.
set -eu

if [ "${1:-run}" = "run" ] && [ "${ELYSIUM_SKIP_MIGRATIONS:-0}" != "1" ]; then
    lock_dir="${ELYSIUM_DATA_DIR:-/data}"
    mkdir -p "$lock_dir"
    flock "$lock_dir/.migrate.lock" elysium-bot db upgrade
fi

exec elysium-bot "$@"
