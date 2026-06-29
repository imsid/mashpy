#!/bin/sh
# Pilot image entrypoint. Two modes, selected by MASH_DATABASE_URL:
#
#   set   -> external-database mode (docker compose, real deployments):
#            skip embedded Postgres entirely and run the host.
#   unset -> single-container mode: init/start the embedded Postgres on the
#            data volume, point the host at it, then run the host.
set -e

if [ -z "${MASH_DATABASE_URL:-}" ]; then
    PG_BIN=$(ls -d /usr/lib/postgresql/*/bin | head -n 1)
    PGDATA="${PILOT_DATA_DIR:-/var/lib/pilot}/pg"
    SOCKET_DIR=/var/run/postgresql

    mkdir -p "$PGDATA" "$SOCKET_DIR"
    chown -R postgres:postgres "$PGDATA" "$SOCKET_DIR"
    chmod 700 "$PGDATA"

    as_postgres() {
        su -s /bin/sh postgres -c "$1"
    }

    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        echo "pilot: initializing embedded Postgres at $PGDATA"
        as_postgres "$PG_BIN/initdb --no-locale -E UTF8 -D '$PGDATA'" > /dev/null
    fi

    echo "pilot: starting embedded Postgres"
    as_postgres "$PG_BIN/pg_ctl -D '$PGDATA' -w -t 60 -l '$PGDATA/postgres.log' \
        -o \"-c listen_addresses='127.0.0.1' -c unix_socket_directories='$SOCKET_DIR'\" start"

    # Idempotent provisioning: CREATE fails when the object exists, which is fine.
    # Anything genuinely broken surfaces as a clear DB error when the host starts.
    as_postgres "$PG_BIN/psql -h $SOCKET_DIR -c \"CREATE ROLE mash LOGIN PASSWORD 'mash'\"" 2> /dev/null || true
    as_postgres "$PG_BIN/psql -h $SOCKET_DIR -c 'CREATE DATABASE mash_pilot OWNER mash'" 2> /dev/null || true

    export MASH_DATABASE_URL="postgresql://mash:mash@127.0.0.1:5432/mash_pilot"
fi

exec "$@"
