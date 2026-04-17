#!/usr/bin/env bash
# G4 #3 — Standby bootstrap hook for OmniSight PostgreSQL HA pair.
#
# Runs as the container's `command:` in docker-compose.yml (NOT as a
# docker-entrypoint-initdb.d hook — the hot standby never gets to run
# `initdb`, it clones the primary's data directory instead).
#
# Execution model:
#   1. On first boot PGDATA is empty → run `pg_basebackup` to clone the
#      primary and seed PGDATA with a consistent snapshot + base WAL.
#   2. Create `standby.signal` so PostgreSQL knows to start in standby
#      mode and follow the stream at `primary_conninfo`.
#   3. Exec into `postgres` (pid 1) which reads postgresql.conf +
#      standby.signal and begins streaming replication.
#   4. On subsequent boots PGDATA already populated → skip step 1, still
#      ensure standby.signal exists, then exec postgres.
#
# Secret handling: REPLICATION_PASSWORD is written into `primary_conninfo`
# in PGDATA/postgresql.auto.conf. File permissions are 0600 courtesy of
# postgres itself; we never echo the password.
set -euo pipefail

# --- Env guards ----------------------------------------------------------
: "${PGDATA:=/var/lib/postgresql/data}"
: "${PRIMARY_HOST:=pg-primary}"
: "${PRIMARY_PORT:=5432}"
: "${REPLICATION_USER:=replicator}"
: "${REPLICATION_PASSWORD:?REPLICATION_PASSWORD must be set for standby bootstrap}"
: "${REPLICATION_SLOT_NAME:=omnisight_standby_slot}"
: "${REPLICATION_APPLICATION_NAME:=omnisight_standby}"
: "${STANDBY_BASEBACKUP_TIMEOUT:=300}"

CONF_SRC="/etc/postgresql/postgresql.conf"
HBA_SRC="/etc/postgresql/pg_hba.conf"

# --- Wait for primary to accept connections -----------------------------
# The standby container may start before the primary has finished init.
# Poll `pg_isready` for up to STANDBY_BASEBACKUP_TIMEOUT seconds.
echo "[init-standby] waiting for primary ${PRIMARY_HOST}:${PRIMARY_PORT} to accept connections"
deadline=$(( $(date +%s) + STANDBY_BASEBACKUP_TIMEOUT ))
while ! pg_isready -h "${PRIMARY_HOST}" -p "${PRIMARY_PORT}" -q; do
    if [ "$(date +%s)" -gt "${deadline}" ]; then
        echo "[init-standby] ERROR: primary did not accept connections within ${STANDBY_BASEBACKUP_TIMEOUT}s" >&2
        exit 1
    fi
    sleep 2
done
echo "[init-standby] primary is accepting connections"

# --- Clone primary if PGDATA is empty (first-boot path) -----------------
# `pg_basebackup` copies a consistent snapshot + initial WAL so the
# standby can immediately begin streaming. Without this step there is
# nothing to replay and postgres would refuse to start.
if [ ! -s "${PGDATA}/PG_VERSION" ]; then
    echo "[init-standby] PGDATA empty — running pg_basebackup from ${PRIMARY_HOST}"
    # Empty PGDATA in case a partial/aborted clone left garbage behind.
    rm -rf "${PGDATA:?}"/* "${PGDATA:?}"/.??* 2>/dev/null || true
    mkdir -p "${PGDATA}"
    chmod 0700 "${PGDATA}"

    # `-R` creates standby.signal + primary_conninfo in postgresql.auto.conf.
    # `-X stream` fetches WAL via the same replication connection (atomic
    # snapshot). `-S` uses the named replication slot so the primary
    # retains WAL through the clone window.
    PGPASSWORD="${REPLICATION_PASSWORD}" pg_basebackup \
        --host="${PRIMARY_HOST}" \
        --port="${PRIMARY_PORT}" \
        --username="${REPLICATION_USER}" \
        --pgdata="${PGDATA}" \
        --wal-method=stream \
        --write-recovery-conf \
        --slot="${REPLICATION_SLOT_NAME}" \
        --progress \
        --verbose
    echo "[init-standby] pg_basebackup complete"
else
    echo "[init-standby] PGDATA already populated — skipping pg_basebackup"
fi

# --- Always (re)write standby.signal ------------------------------------
# `pg_basebackup -R` already drops this file on first boot but container
# restarts after a manual PGDATA edit might lose it. Idempotent.
touch "${PGDATA}/standby.signal"

# --- Install primary_conninfo (idempotent) ------------------------------
# Even with `-R`, we rewrite `postgresql.auto.conf` on every boot so
# secret rotation / primary host change just needs a container restart.
# `primary_slot_name` pins the physical slot created by init-primary.sh.
# `application_name` is the key the primary matches against
# `synchronous_standby_names` to graduate this standby to sync mode.
cat > "${PGDATA}/postgresql.auto.conf" <<-AUTO
# Managed by deploy/postgres-ha/init-standby.sh — do not edit by hand.
primary_conninfo = 'host=${PRIMARY_HOST} port=${PRIMARY_PORT} user=${REPLICATION_USER} password=${REPLICATION_PASSWORD} application_name=${REPLICATION_APPLICATION_NAME}'
primary_slot_name = '${REPLICATION_SLOT_NAME}'
AUTO
chmod 0600 "${PGDATA}/postgresql.auto.conf"

# --- Hand off to postgres (pid 1) ---------------------------------------
# Using `exec` so SIGTERM from docker stop lands directly on postgres
# rather than this shell script.
echo "[init-standby] handing off to postgres (standby mode, following ${PRIMARY_HOST})"
exec postgres \
    -c config_file="${CONF_SRC}" \
    -c hba_file="${HBA_SRC}"
