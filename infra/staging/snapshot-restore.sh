#!/usr/bin/env bash
#
# [OP-972] AUDIT-19b — daily prod -> staging anonymized Postgres snapshot
# restore (Phase 1, mechanism "6c").
#
# One run == one cron tick (deploy/systemd/staging-pg-snapshot.{service,timer},
# daily 02:00 — the low-traffic window). Pipeline:
#
#   1. pg_dump prod (plain SQL, --no-owner --no-privileges) using the
#      prod PG container's own pg_dump:
#          docker exec omnisight-pg-primary pg_dump -Fp ...   -> $RUN_DIR/prod.sql
#   2. infra/staging/anonymize.sh $RUN_DIR/prod.sql $RUN_DIR/anon.sql
#      — masks every PII column declared in infra/staging/anonymize-fields.yaml;
#      REFUSES (AnonymizeMissedField, exit 3) if the dump has a PII-shaped
#      column the spec doesn't cover. We delete prod.sql immediately after.
#      NOTE: anon.sql still contains the source PII in its COPY blocks — the
#      masking UPDATEs run *after* load. anon.sql is therefore as sensitive
#      as prod.sql and lives only inside $RUN_DIR (mode 0700), deleted the
#      moment the load finishes (success or failure) via the EXIT trap.
#   3. Pre-restore sanity (AC #5 / SchemaDrift, exit 2): the staging DB's
#      public table set must equal prod's. Drift => refuse + alert; the
#      operator aligns staging (run alembic / wait for the matching deploy)
#      before the next cycle. Skipped on the first run (staging DB absent).
#   4. Rollback-safe restore (AC #6 — drop-and-rename, one generation kept):
#          terminate sessions on  $STAGING_DB / ${STAGING_DB}_prev
#          DROP DATABASE  IF EXISTS  ${STAGING_DB}_prev          (previous gen)
#          ALTER DATABASE $STAGING_DB RENAME TO ${STAGING_DB}_prev  (if it exists)
#          CREATE DATABASE $STAGING_DB
#          psql -d $STAGING_DB -v ON_ERROR_STOP=1 -f anon.sql       (the load)
#      On a load failure => RestoreInterrupted (exit 4):
#          DROP DATABASE IF EXISTS $STAGING_DB
#          ALTER DATABASE ${STAGING_DB}_prev RENAME TO $STAGING_DB  (revert)
#      i.e. staging reverts to the previous snapshot. On success ${STAGING_DB}_prev
#      is KEPT — it is the manual rollback target until the next successful
#      run's step-4 drops it. (Brief unavailability of $STAGING_DB during the
#      rename/create: the staging backends are `restart: always` and recover;
#      02:00 is chosen for this.)
#   5. Audit: one row in `release_audit` + the run log line below.
#
# `release_audit` row — schema caveat (read this before "fixing" the outcome
# value): alembic 0207's `release_audit` has no `kind` column and `outcome`
# is CHECK-constrained to the develop->main promote vocabulary
# ('promoted','noop','milestone_not_accepted','ff_not_possible','push_rejected').
# Adding 'snapshot_restore' (and ideally a `kind` column) is a one-line
# alembic migration — but that is the `backend`/`db` area, out of scope for
# this `devops`/`tests` ticket. Until that follow-up lands, snapshot rows are
# written with outcome='noop' (the promote *pipeline* did nothing — this row
# is an out-of-band observation) and `detail` carries the real payload:
#   {"kind":"snapshot_restore","status":"ok|schema_drift_refused|
#     anonymize_missed_field|restore_interrupted|prereq_failed",
#    "prod_dump_bytes":N,"anon_updates":N,"duration_s":N,
#    "source":"infra/staging/snapshot-restore.sh"}
# Operators select snapshot rows with `detail->>'kind' = 'snapshot_restore'`.
# The audit write is best-effort — it never blocks or fails the restore
# (same philosophy as backend.audit / backend/agents/staging_gate.py: "don't
# kill the train because the receipt printer ran out of paper"). This mirrors
# the existing OP-877/OP-964 pattern where the develop->main promote audit
# also lands in a generic sink pending the dedicated-table re-target.
#
# Pluggability (6c -> 6g -> 6h): only step 2 changes. anonymize.sh's
# interface — `anonymize.sh <in.sql> <out>` — is the seam. Swapping in
# Greenmask (6g) is a one-file replacement of anonymize.sh that reads the
# same anonymize-fields.yaml (`greenmask:` block); this orchestrator and the
# systemd units are untouched. See anonymize.sh's footer and AUDIT-19b's
# phased-strategy table.
#
# Environment (all overridable; defaults match deploy/staging/docker-compose.yml
# + deploy/postgres-ha/docker-compose.yml + infra/staging/verify-env-contract.sh):
#   PROD_PG_CONTAINER       prod PG container name           (omnisight-pg-primary)
#   PROD_PG_USER            superuser inside that container  (omnisight)
#   PROD_PG_DB              prod database to snapshot        (omnisight)
#   STAGING_PG_HOST         staging PG host                  (127.0.0.1)
#   STAGING_PG_PORT         staging PG host port             (55432  — AUDIT-19a)
#   STAGING_PG_SUPERUSER    staging PG superuser             (omnisight)
#   STAGING_PG_DB           staging database to (re)create   (omnisight_staging)
#   STAGING_PG_PASSWORD     staging superuser password       (falls back to $PGPASSWORD)
#   OMNISIGHT_DATABASE_URL  release-audit DSN for the `release_audit` row
#                           (a SQLAlchemy URL is fine — a `+driver` suffix is
#                           stripped for psql); absent => audit row skipped
#                           (logged), the restore still runs.
#   SNAPSHOT_WORKDIR        scratch parent dir               (/var/tmp/omnisight-staging-snapshot)
#   SNAPSHOT_KEEP_DUMP      "1" => keep $RUN_DIR after the run (DEBUG ONLY —
#                           leaves real PII on disk; a loud warning is logged)
#   DOCKER_BIN / PSQL_BIN   binary overrides                 (docker / psql)
#
# Exit codes (== the systemd unit's result):
#   0  restored ok
#   2  SchemaDrift           — staging schema != prod; refused (alert logged)
#   3  AnonymizeMissedField  — uncovered PII-shaped column; refused (alert logged)
#   4  RestoreInterrupted    — load failed; staging reverted to the previous snapshot
#   5  PrereqFailed          — docker / pg_dump / psql missing, or prod/staging unreachable

set -euo pipefail

SELF="snapshot-restore.sh"
START_TS="$(date +%s)"

PROD_PG_CONTAINER="${PROD_PG_CONTAINER:-omnisight-pg-primary}"
PROD_PG_USER="${PROD_PG_USER:-omnisight}"
PROD_PG_DB="${PROD_PG_DB:-omnisight}"
STAGING_PG_HOST="${STAGING_PG_HOST:-127.0.0.1}"
STAGING_PG_PORT="${STAGING_PG_PORT:-55432}"
STAGING_PG_SUPERUSER="${STAGING_PG_SUPERUSER:-omnisight}"
STAGING_PG_DB="${STAGING_PG_DB:-omnisight_staging}"
STAGING_PG_PASSWORD="${STAGING_PG_PASSWORD:-${PGPASSWORD:-}}"
SNAPSHOT_WORKDIR="${SNAPSHOT_WORKDIR:-/var/tmp/omnisight-staging-snapshot}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
PSQL_BIN="${PSQL_BIN:-psql}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANONYMIZE_SH="${ANONYMIZE_SH:-$SCRIPT_DIR/anonymize.sh}"

PREV_DB="${STAGING_PG_DB}_prev"

# --- logging ----------------------------------------------------------------
log()   { printf '[%s] %s\n'           "$SELF" "$*" >&2; }
warn()  { printf '[%s] WARNING: %s\n'  "$SELF" "$*" >&2; }
alert() { printf '[%s] ALERT: %s\n'    "$SELF" "$*" >&2; }   # non-zero exit => journal/timer surfaces it

# --- psql wrappers ----------------------------------------------------------
prod_psql() {  # $1 = SQL ; runs inside the prod PG container
	"$DOCKER_BIN" exec -i "$PROD_PG_CONTAINER" psql -U "$PROD_PG_USER" -d "$PROD_PG_DB" -tAqc "$1"
}
staging_psql() {  # $1 = target db ; $2 = SQL
	PGPASSWORD="$STAGING_PG_PASSWORD" "$PSQL_BIN" \
		-h "$STAGING_PG_HOST" -p "$STAGING_PG_PORT" -U "$STAGING_PG_SUPERUSER" \
		-d "$1" -v ON_ERROR_STOP=1 -tAqc "$2"
}
staging_db_exists() {
	local n
	n="$(staging_psql postgres "SELECT 1 FROM pg_database WHERE datname = '$1'" || true)"
	[[ "$n" == "1" ]]
}

# --- release_audit row (best-effort; see the header caveat) -----------------
write_audit_row() {  # $1 = status ; $2 = prod_dump_bytes ; $3 = anon_updates
	local status="$1" dump_bytes="${2:-0}" anon_updates="${3:-0}"
	local duration=$(( $(date +%s) - START_TS ))
	local dsn="${OMNISIGHT_DATABASE_URL:-}"
	local detail
	detail="$(python3 - "$status" "$dump_bytes" "$anon_updates" "$duration" <<'PY'
import json, sys
status, dump_bytes, anon_updates, duration = sys.argv[1:5]
def _int(x):
    try: return int(x)
    except Exception: return 0
print(json.dumps({
    "kind": "snapshot_restore",
    "status": status,
    "prod_dump_bytes": _int(dump_bytes),
    "anon_updates": _int(anon_updates),
    "duration_s": _int(duration),
    "source": "infra/staging/snapshot-restore.sh",
}, separators=(",", ":")))
PY
)"
	log "audit: status=$status detail=$detail"
	if [[ -z "$dsn" ]]; then
		log "audit: OMNISIGHT_DATABASE_URL unset — release_audit row skipped (run log only)"
		return 0
	fi
	# SQLAlchemy URL -> libpq URL (strip a `+driver` suffix on the scheme).
	dsn="${dsn/+asyncpg/}"; dsn="${dsn/+psycopg2/}"; dsn="${dsn/+psycopg/}"; dsn="${dsn/+pg8000/}"
	local detail_sql="${detail//\'/\'\'}"   # SQL-escape single quotes (JSON has none here, but be safe)
	if "$PSQL_BIN" "$dsn" -v ON_ERROR_STOP=1 -qc \
		"INSERT INTO release_audit (outcome, fix_version, detail) VALUES ('noop', NULL, '$detail_sql')" \
		>/dev/null 2>&1
	then
		log "audit: release_audit row written (outcome=noop, detail.kind=snapshot_restore)"
	else
		warn "audit: release_audit INSERT failed — non-fatal (DSN unreachable or table absent). Run log retains the record."
	fi
}

# --- final-status emit + cleanup --------------------------------------------
EXIT_STATUS_NAME="prereq_failed"   # overwritten as we make progress
PROD_DUMP_BYTES=0
ANON_UPDATES=0
RUN_DIR=""
finish() {
	local rc="${1:-$?}"
	set +e   # the trap must always run to completion (cleanup + best-effort audit)
	if [[ -n "$RUN_DIR" && -d "$RUN_DIR" ]]; then
		if [[ "${SNAPSHOT_KEEP_DUMP:-0}" == "1" ]]; then
			warn "SNAPSHOT_KEEP_DUMP=1 — leaving $RUN_DIR in place; it contains REAL PII. Delete it manually."
		else
			rm -rf "$RUN_DIR" 2>/dev/null || warn "could not remove scratch dir $RUN_DIR — it may contain PII"
		fi
	fi
	write_audit_row "$EXIT_STATUS_NAME" "$PROD_DUMP_BYTES" "$ANON_UPDATES"
	exit "$rc"
}
trap 'finish $?' EXIT

# ── 0. Prerequisites ────────────────────────────────────────────────────────
command -v "$DOCKER_BIN" >/dev/null 2>&1 || { alert "docker not found ($DOCKER_BIN) — cannot snapshot prod"; exit 5; }
command -v "$PSQL_BIN"   >/dev/null 2>&1 || { alert "psql not found ($PSQL_BIN) — install postgresql-client"; exit 5; }
command -v python3       >/dev/null 2>&1 || { alert "python3 not found"; exit 5; }
[[ -x "$ANONYMIZE_SH" || -f "$ANONYMIZE_SH" ]] || { alert "anonymizer not found: $ANONYMIZE_SH"; exit 5; }
"$DOCKER_BIN" exec "$PROD_PG_CONTAINER" pg_isready -U "$PROD_PG_USER" -d "$PROD_PG_DB" >/dev/null 2>&1 \
	|| { alert "prod PG container '$PROD_PG_CONTAINER' not ready (pg_isready failed)"; exit 5; }
PGPASSWORD="$STAGING_PG_PASSWORD" "$PSQL_BIN" -h "$STAGING_PG_HOST" -p "$STAGING_PG_PORT" \
	-U "$STAGING_PG_SUPERUSER" -d postgres -tAqc 'SELECT 1' >/dev/null 2>&1 \
	|| { alert "staging PG unreachable at ${STAGING_PG_HOST}:${STAGING_PG_PORT} as ${STAGING_PG_SUPERUSER}"; exit 5; }
log "prereqs ok — prod=${PROD_PG_CONTAINER}/${PROD_PG_DB}  staging=${STAGING_PG_HOST}:${STAGING_PG_PORT}/${STAGING_PG_DB}"

# scratch dir (mode 0700; per-run subdir; wiped by the EXIT trap)
mkdir -p "$SNAPSHOT_WORKDIR"
chmod 700 "$SNAPSHOT_WORKDIR" 2>/dev/null || true
RUN_DIR="$(mktemp -d "${SNAPSHOT_WORKDIR}/run.XXXXXX")"
chmod 700 "$RUN_DIR"
RAW_DUMP="$RUN_DIR/prod.sql"
ANON_DUMP="$RUN_DIR/anon.sql"

# ── 1. pg_dump prod ─────────────────────────────────────────────────────────
log "step 1/4: pg_dump prod (${PROD_PG_DB}) via ${PROD_PG_CONTAINER} -> ${RAW_DUMP}"
if ! "$DOCKER_BIN" exec -i "$PROD_PG_CONTAINER" \
	pg_dump -U "$PROD_PG_USER" -d "$PROD_PG_DB" -Fp --no-owner --no-privileges >"$RAW_DUMP"
then
	alert "pg_dump of prod failed"
	exit 5
fi
PROD_DUMP_BYTES="$(wc -c <"$RAW_DUMP" 2>/dev/null || echo 0)"
[[ "$PROD_DUMP_BYTES" -gt 0 ]] || { alert "pg_dump produced an empty file"; exit 5; }
log "step 1/4: ok — ${PROD_DUMP_BYTES} bytes"

# ── 2. anonymize ────────────────────────────────────────────────────────────
log "step 2/4: anonymize -> ${ANON_DUMP}"
set +e
bash "$ANONYMIZE_SH" "$RAW_DUMP" "$ANON_DUMP"
anon_rc=$?
set -e
# Whatever happened, the raw dump (real PII) is no longer needed — shred it now.
rm -f "$RAW_DUMP"
if [[ "$anon_rc" -eq 3 ]]; then
	EXIT_STATUS_NAME="anonymize_missed_field"
	alert "AnonymizeMissedField — anonymize.sh refused (uncovered PII-shaped column). Extend infra/staging/anonymize-fields.yaml, then re-run. Restore SKIPPED — staging untouched."
	exit 3
elif [[ "$anon_rc" -ne 0 ]]; then
	alert "anonymize.sh failed (rc=$anon_rc) — restore SKIPPED, staging untouched"
	exit 5
fi
ANON_UPDATES="$(grep -cE '^(UPDATE |DELETE FROM )' "$ANON_DUMP" 2>/dev/null || true)"; ANON_UPDATES="${ANON_UPDATES:-0}"
log "step 2/4: ok — anonymized dump has ${ANON_UPDATES} masking statement(s)"

# ── 3. pre-restore sanity: staging schema must match prod (AC #5) ───────────
log "step 3/4: schema-drift check (staging public tables == prod public tables?)"
PROD_TABLES="$(prod_psql "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1" || true)"
[[ -n "$PROD_TABLES" ]] || { alert "could not read prod table list"; exit 5; }
if staging_db_exists "$STAGING_PG_DB"; then
	STAGING_TABLES="$(staging_psql "$STAGING_PG_DB" "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1" || true)"
	if [[ "$PROD_TABLES" != "$STAGING_TABLES" ]]; then
		EXIT_STATUS_NAME="schema_drift_refused"
		alert "SchemaDrift — staging '${STAGING_PG_DB}' public table set differs from prod '${PROD_PG_DB}'. Refusing the restore (a mismatched schema means staging's app code can't run against the restored data). Align staging first (run alembic / wait for the matching deploy), then re-run. Diff:"
		diff <(printf '%s\n' "$PROD_TABLES") <(printf '%s\n' "$STAGING_TABLES") >&2 || true
		exit 2
	fi
	log "step 3/4: ok — table sets match ($(printf '%s\n' "$PROD_TABLES" | grep -c . ) public tables)"
else
	log "step 3/4: staging DB '${STAGING_PG_DB}' does not exist yet — first run, skipping drift check (it will be created from the prod dump)"
fi

# ── 4. rollback-safe restore (drop-and-rename; AC #6) ───────────────────────
log "step 4/4: restore into '${STAGING_PG_DB}' (keeping one rollback generation as '${PREV_DB}')"
EXIT_STATUS_NAME="restore_interrupted"   # any failure from here on (until the final 'ok') is a restore failure

# 4a. terminate sessions on the staging DB(s) so RENAME/DROP can proceed.
staging_psql postgres \
	"SELECT pg_terminate_backend(pid) FROM pg_stat_activity
	 WHERE datname IN ('${STAGING_PG_DB}', '${PREV_DB}') AND pid <> pg_backend_pid()" >/dev/null 2>&1 || true

# 4b. drop the previous generation, rotate the current one out of the way.
# WITH (FORCE) (PG 13+) terminates any stragglers the 4a sweep raced.
staging_psql postgres "DROP DATABASE IF EXISTS \"${PREV_DB}\" WITH (FORCE)" >/dev/null
HAD_PREV=0
if staging_db_exists "$STAGING_PG_DB"; then
	staging_psql postgres "ALTER DATABASE \"${STAGING_PG_DB}\" RENAME TO \"${PREV_DB}\"" >/dev/null
	HAD_PREV=1
	log "step 4/4: rotated existing '${STAGING_PG_DB}' -> '${PREV_DB}'"
fi

# 4c. fresh empty DB, then load the anonymized dump.
revert_staging() {
	warn "reverting staging to the previous snapshot"
	staging_psql postgres \
		"SELECT pg_terminate_backend(pid) FROM pg_stat_activity
		 WHERE datname = '${STAGING_PG_DB}' AND pid <> pg_backend_pid()" >/dev/null 2>&1 || true
	staging_psql postgres "DROP DATABASE IF EXISTS \"${STAGING_PG_DB}\" WITH (FORCE)" >/dev/null 2>&1 || true
	if [[ "$HAD_PREV" -eq 1 ]]; then
		staging_psql postgres "ALTER DATABASE \"${PREV_DB}\" RENAME TO \"${STAGING_PG_DB}\"" >/dev/null 2>&1 \
			&& log "reverted: '${PREV_DB}' -> '${STAGING_PG_DB}'" \
			|| alert "REVERT FAILED — staging '${STAGING_PG_DB}' is missing and '${PREV_DB}' could not be renamed back. Manual intervention required."
	else
		warn "no previous snapshot to revert to (first run) — staging '${STAGING_PG_DB}' left absent; next cycle will recreate it"
	fi
}

if ! staging_psql postgres "CREATE DATABASE \"${STAGING_PG_DB}\"" >/dev/null; then
	EXIT_STATUS_NAME="restore_interrupted"
	alert "RestoreInterrupted — could not CREATE DATABASE '${STAGING_PG_DB}'"
	revert_staging
	exit 4
fi

log "step 4/4: loading anonymized dump into '${STAGING_PG_DB}'"
if ! PGPASSWORD="$STAGING_PG_PASSWORD" "$PSQL_BIN" \
	-h "$STAGING_PG_HOST" -p "$STAGING_PG_PORT" -U "$STAGING_PG_SUPERUSER" \
	-d "$STAGING_PG_DB" -v ON_ERROR_STOP=1 -q -f "$ANON_DUMP"
then
	EXIT_STATUS_NAME="restore_interrupted"
	alert "RestoreInterrupted — loading the anonymized dump failed mid-way (disk full / connection drop?)."
	revert_staging
	# the anonymized dump (residual PII) is removed by the EXIT trap (finish())
	exit 4
fi

# Belt-and-braces: confirm the masking epilogue actually ran (no plaintext
# example.com / non-redacted emails should remain in users.email).
LEAK_CHECK="$(staging_psql "$STAGING_PG_DB" \
	"SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='users'" || echo 0)"
if [[ "$LEAK_CHECK" == "1" ]]; then
	REMAINING="$(staging_psql "$STAGING_PG_DB" \
		"SELECT count(*) FROM public.users WHERE email IS NOT NULL AND email NOT LIKE 'redacted-%@staging.test'" || echo '?')"
	if [[ "$REMAINING" =~ ^[0-9]+$ && "$REMAINING" -gt 0 ]]; then
		EXIT_STATUS_NAME="restore_interrupted"
		alert "post-load leak check FAILED: ${REMAINING} users.email value(s) are not anonymized — the masking epilogue did not run. Reverting."
		revert_staging
		exit 4
	fi
fi

EXIT_STATUS_NAME="ok"
log "step 4/4: ok — '${STAGING_PG_DB}' restored from an anonymized prod snapshot ($(( $(date +%s) - START_TS ))s). Rollback target kept as '${PREV_DB}'."
log "done."
exit 0
