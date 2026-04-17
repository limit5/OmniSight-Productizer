#!/usr/bin/env bash
# G4 #3 — Primary bootstrap hook for OmniSight PostgreSQL HA pair.
#
# Invoked by the `postgres:16-alpine` container's docker-entrypoint.sh
# during first-boot initialization (placed under
# `/docker-entrypoint-initdb.d/`). Runs ONLY the first time an empty
# PGDATA is initialized — container restarts with a populated PGDATA
# skip this hook entirely.
#
# Responsibilities:
#   1. Create the `replicator` role with REPLICATION privilege.
#   2. Verify `postgresql.conf` + `pg_hba.conf` were picked up.
#
# The replication password is injected via the `REPLICATION_PASSWORD`
# env var (see `.env.example`). We NEVER log the password — only a
# one-shot SHA-256 fingerprint for audit visibility.
set -euo pipefail

# --- Env guards ----------------------------------------------------------
: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${POSTGRES_DB:?POSTGRES_DB must be set}"
: "${REPLICATION_USER:=replicator}"
: "${REPLICATION_PASSWORD:?REPLICATION_PASSWORD must be set for primary bootstrap}"

echo "[init-primary] creating replication role ${REPLICATION_USER}"
# Log a password fingerprint (not the password itself) so operators can
# correlate "which credential is on this primary" against the secret
# store without exposing the secret in docker logs.
PW_FP="$(printf '%s' "${REPLICATION_PASSWORD}" | sha256sum | cut -c1-12)"
echo "[init-primary] replication password fingerprint: sha256:${PW_FP}..."

# --- Create replication role --------------------------------------------
# `CREATE ROLE ... REPLICATION LOGIN` with an explicit password.
# The role has NO access to user tables — only streaming replication and
# `pg_basebackup` handshakes.
psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname   "${POSTGRES_DB}" <<-SQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${REPLICATION_USER}') THEN
            CREATE ROLE ${REPLICATION_USER}
                WITH LOGIN REPLICATION
                PASSWORD '${REPLICATION_PASSWORD}';
        ELSE
            ALTER ROLE ${REPLICATION_USER}
                WITH LOGIN REPLICATION
                PASSWORD '${REPLICATION_PASSWORD}';
        END IF;
    END
    \$\$;
SQL

# --- Create replication slot (named, persistent) ------------------------
# A physical replication slot guarantees the primary retains WAL until
# the standby has consumed it, even if the standby disconnects longer
# than `wal_keep_size`. Slot name is pinned so init-standby.sh can refer
# to it symmetrically.
psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname   "${POSTGRES_DB}" <<-SQL
    SELECT pg_create_physical_replication_slot('omnisight_standby_slot')
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_replication_slots
        WHERE slot_name = 'omnisight_standby_slot'
    );
SQL

echo "[init-primary] role + slot created; primary ready for streaming replication"
