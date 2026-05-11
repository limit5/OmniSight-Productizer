#!/usr/bin/env bash
# [OP-887] Weekly isolated restore test and diff report.

set -Eeuo pipefail

RTO_SECONDS="${OMNISIGHT_BACKUP_RTO_SECONDS:-1800}"
RESTORE_DB_URL="${OMNISIGHT_RESTORE_TEST_DATABASE_URL:-}"
SOURCE_DB_URL="${OMNISIGHT_DATABASE_URL:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --report-dir) REPORT_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${OMNISIGHT_BACKUP_DIR:-$REPO/data/backups/postgres}"
REPORT_DIR="${REPORT_DIR:-$BACKUP_DIR/reports}"
mkdir -p "$REPORT_DIR"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
alert() {
  local code="$1" detail="$2"
  [[ -n "${OMNISIGHT_BACKUP_ALERT_WEBHOOK:-}" ]] || return 0
  command -v curl >/dev/null || return 0
  curl -fsS -X POST -H 'Content-Type: application/json' \
    --data "{\"code\":\"${code}\",\"component\":\"postgres-restore-test\",\"detail\":\"${detail}\"}" \
    "$OMNISIGHT_BACKUP_ALERT_WEBHOOK" >/dev/null || true
}
open_p1_ticket() {
  local detail="$1"
  [[ -n "${OMNISIGHT_JIRA_REST_URL:-}" && -n "${OMNISIGHT_JIRA_AUTH_HEADER:-}" ]] || return 0
  command -v curl >/dev/null || return 0
  curl -fsS -X POST \
    -H "Authorization: ${OMNISIGHT_JIRA_AUTH_HEADER}" \
    -H 'Content-Type: application/json' \
    --data "{\"fields\":{\"project\":{\"key\":\"OP\"},\"summary\":\"P1 RestoreTestFailed\",\"description\":\"${detail}\",\"issuetype\":{\"name\":\"Bug\"},\"priority\":{\"name\":\"Highest\"}}}" \
    "$OMNISIGHT_JIRA_REST_URL/issue" >/dev/null || true
}
fail_restore() {
  local detail="$1"
  alert "RestoreTestFailed" "$detail"
  open_p1_ticket "$detail"
  log "RestoreTestFailed: $detail" >&2
  exit 1
}

latest_daily() {
  find "$BACKUP_DIR" -type f -name 'daily-*.dump.gz' -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | awk 'NR==1 {print substr($0, index($0,$2))}'
}

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  log "dry-run: would restore latest daily dump into isolated OMNISIGHT_RESTORE_TEST_DATABASE_URL"
  exit 0
fi

[[ -n "$SOURCE_DB_URL" ]] || fail_restore "OMNISIGHT_DATABASE_URL is required"
[[ -n "$RESTORE_DB_URL" ]] || fail_restore "OMNISIGHT_RESTORE_TEST_DATABASE_URL is required"
[[ "$RESTORE_DB_URL" != "$SOURCE_DB_URL" ]] || fail_restore "restore target must be isolated from source"
command -v gzip >/dev/null || fail_restore "gzip is required"
command -v pg_restore >/dev/null || fail_restore "pg_restore is required"
command -v psql >/dev/null || fail_restore "psql is required"

BACKUP="$(latest_daily)"
[[ -n "$BACKUP" ]] || fail_restore "no daily backup found under $BACKUP_DIR"
[[ -f "${BACKUP}.sha256" ]] || fail_restore "missing SHA manifest for $BACKUP"
(cd "$(dirname "$BACKUP")" && sha256sum --check "$(basename "${BACKUP}.sha256")") \
  || fail_restore "SHA check failed for $BACKUP"

START="$(date +%s)"
TMP_DUMP="$(mktemp "${TMPDIR:-/tmp}/op887-restore.XXXXXX.dump")"
trap 'rm -f "$TMP_DUMP"' EXIT
gzip -dc "$BACKUP" > "$TMP_DUMP"

psql "$RESTORE_DB_URL" -v ON_ERROR_STOP=1 -c 'DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;'
pg_restore --dbname="$RESTORE_DB_URL" --clean --if-exists --no-owner --no-privileges "$TMP_DUMP"

SOURCE_COUNTS="$(mktemp "${TMPDIR:-/tmp}/op887-source.XXXXXX")"
RESTORE_COUNTS="$(mktemp "${TMPDIR:-/tmp}/op887-restore-counts.XXXXXX")"
trap 'rm -f "$TMP_DUMP" "$SOURCE_COUNTS" "$RESTORE_COUNTS"' EXIT

dump_table_counts() {
  local db_url="$1"
  local out="$2"
  local table_sql="
SELECT quote_ident(schemaname) || '.' || quote_ident(tablename),
       schemaname || '.' || tablename
FROM pg_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY schemaname, tablename;"
  psql "$db_url" -v ON_ERROR_STOP=1 -F $'\t' -Atc "$table_sql" \
    | while IFS=$'\t' read -r qualified label; do
        [[ -n "$qualified" ]] || continue
        count="$(psql "$db_url" -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM $qualified;")"
        printf '%s=%s\n' "$label" "$count"
      done > "$out"
}

dump_table_counts "$SOURCE_DB_URL" "$SOURCE_COUNTS"
dump_table_counts "$RESTORE_DB_URL" "$RESTORE_COUNTS"

DIFF_FILE="$REPORT_DIR/restore-test-latest.diff"
if ! diff -u "$SOURCE_COUNTS" "$RESTORE_COUNTS" > "$DIFF_FILE"; then
  fail_restore "restore diff was not clean; see $DIFF_FILE"
fi

ELAPSED="$(( $(date +%s) - START ))"
REPORT="$REPORT_DIR/restore-test-latest.md"
{
  printf '# OP-887 weekly restore test\n\n'
  printf '* status: success\n'
  printf '* backup: `%s`\n' "$BACKUP"
  printf '* restore_elapsed_seconds: %s\n' "$ELAPSED"
  printf '* rto_target_seconds: %s\n' "$RTO_SECONDS"
  printf '* rpo_target_seconds: 3600\n'
  printf '* diff: clean (`%s`)\n' "$DIFF_FILE"
} > "$REPORT"
chmod 600 "$REPORT" "$DIFF_FILE"

if (( ELAPSED > RTO_SECONDS )); then
  fail_restore "restore exceeded RTO: ${ELAPSED}s > ${RTO_SECONDS}s"
fi

log "weekly restore test complete in ${ELAPSED}s: $REPORT"
