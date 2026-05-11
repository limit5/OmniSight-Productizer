#!/usr/bin/env bash
# [OP-887] Daily Postgres dump to S3-compatible storage.
#
# Required env:
#   OMNISIGHT_DATABASE_URL              Postgres connection URL.
#   OMNISIGHT_BACKUP_S3_URI             s3://bucket/prefix target.
# Optional env:
#   OMNISIGHT_BACKUP_S3_ENDPOINT        S3-compatible endpoint, e.g. https://sora.services.
#   OMNISIGHT_BACKUP_DIR                Local staging dir (default: data/backups/postgres).
#   OMNISIGHT_BACKUP_ALERT_WEBHOOK      Operator webhook for failures.
#   OMNISIGHT_BACKUP_STORAGE_QUOTA_BYTES Local staging quota; oldest files auto-purged.

set -Eeuo pipefail

LABEL="daily"
MAX_RETRIES=3

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${OMNISIGHT_BACKUP_DIR:-$REPO/data/backups/postgres}"
REPORT_DIR="$BACKUP_DIR/reports"
mkdir -p "$BACKUP_DIR" "$REPORT_DIR"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "BackupUploadFailed: $*" >&2; alert "BackupUploadFailed" "$*"; exit 1; }

alert() {
  local code="$1"
  local detail="$2"
  [[ -n "${OMNISIGHT_BACKUP_ALERT_WEBHOOK:-}" ]] || return 0
  command -v curl >/dev/null || return 0
  curl -fsS -X POST \
    -H 'Content-Type: application/json' \
    --data "{\"code\":\"${code}\",\"component\":\"postgres-daily-backup\",\"detail\":\"${detail}\"}" \
    "$OMNISIGHT_BACKUP_ALERT_WEBHOOK" >/dev/null || true
}

s3_parts() {
  local uri="$1"
  [[ "$uri" == s3://* ]] || return 1
  local rest="${uri#s3://}"
  S3_BUCKET="${rest%%/*}"
  S3_PREFIX=""
  [[ "$rest" == */* ]] && S3_PREFIX="${rest#*/}"
  S3_PREFIX="${S3_PREFIX%/}"
  [[ -n "$S3_BUCKET" ]]
}

aws_args() {
  if [[ -n "${OMNISIGHT_BACKUP_S3_ENDPOINT:-}" ]]; then
    printf '%s\n' --endpoint-url "$OMNISIGHT_BACKUP_S3_ENDPOINT"
  fi
}

purge_oldest_until_under_quota() {
  local quota="${OMNISIGHT_BACKUP_STORAGE_QUOTA_BYTES:-}"
  [[ -n "$quota" ]] || return 0
  [[ "$quota" =~ ^[1-9][0-9]*$ ]] || die "OMNISIGHT_BACKUP_STORAGE_QUOTA_BYTES must be positive integer bytes"

  local used
  used="$(du -sb "$BACKUP_DIR" | awk '{print $1}')"
  while (( used > quota )); do
    local oldest
    oldest="$(find "$BACKUP_DIR" -type f \( -name '*.dump.gz' -o -name '*.wal.gz' -o -name '*.sha256' \) -printf '%T@ %p\n' | sort -n | awk 'NR==1 {print substr($0, index($0,$2))}')"
    [[ -n "$oldest" ]] || die "StorageQuotaExceeded and no purge candidates remain"
    rm -f "$oldest"
    log "StorageQuotaExceeded: purged oldest local backup $oldest"
    alert "StorageQuotaExceeded" "purged oldest local backup $oldest"
    used="$(du -sb "$BACKUP_DIR" | awk '{print $1}')"
  done
}

upload_with_retry() {
  local src="$1"
  local key="$2"
  local attempt=1
  local args=()
  mapfile -t args < <(aws_args)
  while (( attempt <= MAX_RETRIES )); do
    if aws "${args[@]}" s3api put-object \
      --bucket "$S3_BUCKET" \
      --key "$key" \
      --body "$src" \
      --server-side-encryption AES256 >/dev/null; then
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
  log "dry-run: would dump OMNISIGHT_DATABASE_URL to $OMNISIGHT_BACKUP_S3_URI"
  exit 0
fi

command -v pg_dump >/dev/null || die "pg_dump is required"
command -v gzip >/dev/null || die "gzip is required"
command -v sha256sum >/dev/null || die "sha256sum is required"
command -v aws >/dev/null || die "aws CLI is required for S3-compatible upload"

TS="$(date -u '+%Y%m%dT%H%M%SZ')"
BASE="$BACKUP_DIR/${LABEL}-${TS}.dump.gz"
SHA="${BASE}.sha256"
KEY_BASE="${S3_PREFIX:+$S3_PREFIX/}daily/$(basename "$BASE")"

pg_dump "$OMNISIGHT_DATABASE_URL" --format=custom --no-owner --no-privileges \
  | gzip -9 > "$BASE"
chmod 600 "$BASE"
sha256sum "$BASE" > "$SHA"
chmod 600 "$SHA"

purge_oldest_until_under_quota
upload_with_retry "$BASE" "$KEY_BASE" || die "daily dump upload failed after $MAX_RETRIES retries"
upload_with_retry "$SHA" "${KEY_BASE}.sha256" || die "daily dump SHA upload failed after $MAX_RETRIES retries"

REPORT="$REPORT_DIR/daily-latest.env"
{
  printf 'status=success\n'
  printf 'timestamp=%s\n' "$TS"
  printf 'local_path=%s\n' "$BASE"
  printf 'sha256_path=%s\n' "$SHA"
  printf 's3_uri=s3://%s/%s\n' "$S3_BUCKET" "$KEY_BASE"
} > "$REPORT"
chmod 600 "$REPORT"
log "daily Postgres backup complete: $REPORT"
