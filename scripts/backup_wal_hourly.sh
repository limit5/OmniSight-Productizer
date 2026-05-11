#!/usr/bin/env bash
# [OP-887] Hourly WAL archive marker for sub-hour RPO.

set -Eeuo pipefail

LABEL="hourly"
MAX_RETRIES=3

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${OMNISIGHT_BACKUP_DIR:-$REPO/data/backups/postgres}"
mkdir -p "$BACKUP_DIR/wal" "$BACKUP_DIR/reports"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
alert() {
  local code="$1" detail="$2"
  [[ -n "${OMNISIGHT_BACKUP_ALERT_WEBHOOK:-}" ]] || return 0
  command -v curl >/dev/null || return 0
  curl -fsS -X POST -H 'Content-Type: application/json' \
    --data "{\"code\":\"${code}\",\"component\":\"postgres-wal-hourly\",\"detail\":\"${detail}\"}" \
    "$OMNISIGHT_BACKUP_ALERT_WEBHOOK" >/dev/null || true
}
die() { log "BackupUploadFailed: $*" >&2; alert "BackupUploadFailed" "$*"; exit 1; }

check_daily_backup_freshness() {
  local report="$BACKUP_DIR/reports/daily-latest.env"
  [[ -f "$report" ]] || {
    alert "BackupMissed" "daily backup report missing: $report"
    log "BackupMissed: daily backup report missing: $report"
    return 0
  }
  local now
  local mtime
  now="$(date +%s)"
  mtime="$(stat -c '%Y' "$report")"
  if (( now - mtime > 93600 )); then
    alert "BackupMissed" "daily backup report older than 26h: $report"
    log "BackupMissed: daily backup report older than 26h: $report"
  fi
}

s3_parts() {
  local rest="${1#s3://}"
  [[ "$1" == s3://* && -n "$rest" ]] || return 1
  S3_BUCKET="${rest%%/*}"
  S3_PREFIX=""
  [[ "$rest" == */* ]] && S3_PREFIX="${rest#*/}"
  S3_PREFIX="${S3_PREFIX%/}"
}

aws_args() {
  [[ -n "${OMNISIGHT_BACKUP_S3_ENDPOINT:-}" ]] && printf '%s\n' --endpoint-url "$OMNISIGHT_BACKUP_S3_ENDPOINT"
}

upload_with_retry() {
  local src="$1" key="$2" attempt=1
  local args=()
  mapfile -t args < <(aws_args)
  while (( attempt <= MAX_RETRIES )); do
    if aws "${args[@]}" s3api put-object --bucket "$S3_BUCKET" --key "$key" \
      --body "$src" --server-side-encryption AES256 >/dev/null; then
      log "uploaded s3://$S3_BUCKET/$key"
      return 0
    fi
    log "BackupUploadFailed: upload attempt $attempt/$MAX_RETRIES failed for $key"
    sleep "$attempt"
    attempt=$((attempt + 1))
  done
  return 1
}

[[ -n "${OMNISIGHT_DATABASE_URL:-}" ]] || die "OMNISIGHT_DATABASE_URL is required"
[[ -n "${OMNISIGHT_BACKUP_S3_URI:-}" ]] || die "OMNISIGHT_BACKUP_S3_URI is required"
s3_parts "$OMNISIGHT_BACKUP_S3_URI" || die "OMNISIGHT_BACKUP_S3_URI must be s3://bucket/prefix"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  log "dry-run: would switch WAL and upload marker to $OMNISIGHT_BACKUP_S3_URI"
  exit 0
fi

command -v psql >/dev/null || die "psql is required"
command -v tar >/dev/null || die "tar is required"
command -v gzip >/dev/null || die "gzip is required"
command -v sha256sum >/dev/null || die "sha256sum is required"
command -v aws >/dev/null || die "aws CLI is required for S3-compatible upload"

check_daily_backup_freshness

TS="$(date -u '+%Y%m%dT%H%M%SZ')"
WORK="$BACKUP_DIR/wal/${LABEL}-${TS}"
mkdir -p "$WORK"

psql "$OMNISIGHT_DATABASE_URL" -v ON_ERROR_STOP=1 -Atc "SELECT pg_switch_wal();" > "$WORK/switched-lsn.txt"
psql "$OMNISIGHT_DATABASE_URL" -v ON_ERROR_STOP=1 -Atc "SELECT pg_current_wal_lsn();" > "$WORK/current-lsn.txt"
psql "$OMNISIGHT_DATABASE_URL" -v ON_ERROR_STOP=1 -Atc "SHOW archive_mode; SHOW archive_command; SHOW archive_timeout;" > "$WORK/archive-settings.txt"

if [[ -n "${OMNISIGHT_WAL_ARCHIVE_DIR:-}" ]]; then
  [[ -d "$OMNISIGHT_WAL_ARCHIVE_DIR" ]] || die "OMNISIGHT_WAL_ARCHIVE_DIR does not exist: $OMNISIGHT_WAL_ARCHIVE_DIR"
  mkdir -p "$WORK/wal-files"
  while IFS= read -r wal_file; do
    cp -p "$wal_file" "$WORK/wal-files/"
  done < <(find "$OMNISIGHT_WAL_ARCHIVE_DIR" -type f -mmin -70 | sort)
  find "$WORK/wal-files" -type f -printf '%f\n' | sort > "$WORK/wal-files.txt"
else
  printf 'OMNISIGHT_WAL_ARCHIVE_DIR unset; archived WAL file copy skipped\n' > "$WORK/wal-files.txt"
fi

(cd "$WORK" && sha256sum *.txt > SHA256SUMS)
TAR="$WORK.tar.gz"
tar -C "$(dirname "$WORK")" -czf "$TAR" "$(basename "$WORK")"
chmod 600 "$TAR"

KEY="${S3_PREFIX:+$S3_PREFIX/}wal/$(basename "$TAR")"
upload_with_retry "$TAR" "$KEY" || die "hourly WAL marker upload failed after $MAX_RETRIES retries"

REPORT="$BACKUP_DIR/reports/wal-latest.env"
{
  printf 'status=success\n'
  printf 'timestamp=%s\n' "$TS"
  printf 'local_path=%s\n' "$TAR"
  printf 's3_uri=s3://%s/%s\n' "$S3_BUCKET" "$KEY"
} > "$REPORT"
chmod 600 "$REPORT"
log "hourly WAL archive marker complete: $REPORT"
