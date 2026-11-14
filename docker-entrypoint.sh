#!/bin/sh
set -eu

if [ "${SEED_DATABASE:-true}" = "true" ] && [ ! -e "$APP_DB_PATH" ]; then
    python -m app.db.seed_data \
        --reference-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

exec "$@"
