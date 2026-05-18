#!/usr/bin/env bash
# Daily Postgres → S3 backup (dockerized wrapper for hosts without pg_dump/awscli).
#
# Origin: 2026-05-18 evening Task C. The shipped scripts/backup_postgres_daily.sh
# requires pg_dump + aws CLI on the host. On this WSL prod host neither is
# installed (operator has no sudo). This wrapper achieves the same end state
# by using docker:
#   - pg_dump  ← `docker exec omnisight-pg-primary pg_dump ...`
#   - aws CLI  ← `docker run --rm amazon/aws-cli ...`
#
# Required env (from EnvironmentFile or shell):
#   OMNISIGHT_BACKUP_S3_URI            s3://bucket/prefix
#   AWS_ACCESS_KEY_ID                  AWS IAM access key
#   AWS_SECRET_ACCESS_KEY              AWS IAM secret
#   AWS_DEFAULT_REGION                 e.g. us-east-1
# Optional env:
#   OMNISIGHT_BACKUP_DIR               local staging (default: $HOME/pg-backups/daily-s3)
#   OMNISIGHT_BACKUP_LABEL             filename label (default: daily)
#   OMNISIGHT_BACKUP_RETENTION_DAYS    local file retention (default: 14)
#   PG_CONTAINER                       container with pg_dump (default: omnisight-pg-primary)
#   PG_USER                            postgres user (default: omnisight)
#   PG_DB                              postgres database (default: omnisight)
#   DRY_RUN                            "1" to log without uploading

set -Eeuo pipefail

LABEL="${OMNISIGHT_BACKUP_LABEL:-daily}"
BACKUP_DIR="${OMNISIGHT_BACKUP_DIR:-$HOME/pg-backups/daily-s3}"
RETENTION_DAYS="${OMNISIGHT_BACKUP_RETENTION_DAYS:-14}"
PG_CONTAINER="${PG_CONTAINER:-omnisight-pg-primary}"
PG_USER="${PG_USER:-omnisight}"
PG_DB="${PG_DB:-omnisight}"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ -n "${OMNISIGHT_BACKUP_S3_URI:-}" ]] || die "OMNISIGHT_BACKUP_S3_URI is required"
[[ -n "${AWS_ACCESS_KEY_ID:-}" ]] || die "AWS_ACCESS_KEY_ID is required"
[[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]] || die "AWS_SECRET_ACCESS_KEY is required"
[[ -n "${AWS_DEFAULT_REGION:-}" ]] || die "AWS_DEFAULT_REGION is required"

# Parse s3://bucket/prefix
if [[ ! "$OMNISIGHT_BACKUP_S3_URI" =~ ^s3://([^/]+)(/(.+))?$ ]]; then
  die "OMNISIGHT_BACKUP_S3_URI must be s3://bucket[/prefix]"
fi
S3_BUCKET="${BASH_REMATCH[1]}"
S3_PREFIX="${BASH_REMATCH[3]:-}"

mkdir -p "$BACKUP_DIR"
TS="$(date -u '+%Y%m%dT%H%M%SZ')"
DUMP_FILE="$BACKUP_DIR/${LABEL}-${TS}.dump.gz"
SHA_FILE="${DUMP_FILE}.sha256"

# --- 1. pg_dump via docker exec into pg-primary container ---
log "pg_dump → $DUMP_FILE  (container=$PG_CONTAINER, db=$PG_DB)"
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  log "DRY RUN: skipping pg_dump + upload"
  exit 0
fi

docker exec "$PG_CONTAINER" pg_dump \
  -U "$PG_USER" -d "$PG_DB" \
  --format=custom --no-owner --no-privileges \
  | gzip -9 > "$DUMP_FILE"

chmod 600 "$DUMP_FILE"
sha256sum "$DUMP_FILE" > "$SHA_FILE"
chmod 600 "$SHA_FILE"

SIZE_MB=$(du -m "$DUMP_FILE" | cut -f1)
log "dump size: ${SIZE_MB} MB"

# --- 2. Upload via amazon/aws-cli image ---
S3_KEY="${S3_PREFIX:+$S3_PREFIX/}${LABEL}/$(basename "$DUMP_FILE")"
log "upload → s3://$S3_BUCKET/$S3_KEY"

# Mount $BACKUP_DIR + pass AWS creds via env. SSE-S3 server-side encryption.
docker run --rm \
  -v "$BACKUP_DIR:/data:ro" \
  -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  -e AWS_DEFAULT_REGION="$AWS_DEFAULT_REGION" \
  amazon/aws-cli s3 cp \
    "/data/$(basename "$DUMP_FILE")" \
    "s3://$S3_BUCKET/$S3_KEY" \
    --sse AES256 \
  || die "S3 upload failed"

# Upload .sha256 sidecar
docker run --rm \
  -v "$BACKUP_DIR:/data:ro" \
  -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  -e AWS_DEFAULT_REGION="$AWS_DEFAULT_REGION" \
  amazon/aws-cli s3 cp \
    "/data/$(basename "$SHA_FILE")" \
    "s3://$S3_BUCKET/${S3_KEY}.sha256" \
    --sse AES256 \
  || log "WARN: SHA sidecar upload failed (dump itself uploaded OK)"

log "upload OK: s3://$S3_BUCKET/$S3_KEY"

# --- 3. Local retention pruning (drop files older than RETENTION_DAYS) ---
log "pruning local files older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name "${LABEL}-*.dump.gz" -o -name "${LABEL}-*.dump.gz.sha256" \) \
  -mtime "+$RETENTION_DAYS" -print -delete | sed 's|^|  pruned: |'

log "done"
