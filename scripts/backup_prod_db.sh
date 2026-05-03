#!/usr/bin/env bash
# backup_prod_db.sh — WAL-safe snapshot of the live prod SQLite DB with
# owner-only perms, mandatory DLP scan, and mandatory AES-256 encryption.
#
# Run on the prod host (WSL Ubuntu-24.04). Reads the live DB through
# backend-a's mounted volume (SQLite `.backup` online pragma — no
# downtime, no lock contention with readers).
#
# Env:
#   OMNISIGHT_BACKUP_PASSPHRASE   — backup is encrypted with gpg AES-256.
#                                   passphrase is NEVER stored on disk
#                                   by this script; keep it in the team
#                                   password manager alongside the .gpg
#                                   file to preserve restore capability.
#                                   Unset → fail closed.
#   OMNISIGHT_BACKUP_S3_URI        — optional s3://bucket/prefix for
#                                   off-site immutable encrypted backup.
#                                   When set, upload uses aws s3api
#                                   put-object with Object Lock retention
#                                   and server-side encryption.
#   OMNISIGHT_BACKUP_S3_KMS_KEY_ID — optional KMS key id/arn. When set,
#                                   S3 SSE uses aws:kms; otherwise AES256.
#   OMNISIGHT_BACKUP_S3_RETAIN_DAYS — Object Lock retention days
#                                   (default 365).
#   OMNISIGHT_BACKUP_S3_STORAGE_CLASS — cold storage class
#                                   (default GLACIER_IR).
#
# Flags:
#   --label <STR>   appends to filename (default "manual")
#   --prune <N>     keep only the newest N backups (default 30)
#   -h / --help
#
# Exit 0 on success, non-zero on any failure.

set -Eeuo pipefail

LABEL="manual"
PRUNE=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="$2"; shift 2;;
    --prune) PRUNE="$2"; shift 2;;
    -h|--help) sed -n '2,24p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
TS="$(date +%Y%m%d-%H%M%S)"
BKP_DIR="$REPO/data/backups"
mkdir -p "$BKP_DIR"

if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_OK= C_WARN= C_ERR= C_OFF=
fi
ok()   { printf '  %s[OK]%s   %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '  %s[FAIL]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

utc_days_from_now() {
  local days="$1"
  date -u -d "+${days} days" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
    || python3 - "$days" <<'PY'
import datetime
import sys

days = int(sys.argv[1])
now = datetime.datetime.now(datetime.timezone.utc)
print((now + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
}

upload_offsite_immutable() {
  local src="$1"
  [[ -n "${OMNISIGHT_BACKUP_S3_URI:-}" ]] || {
    warn "off-site immutable backup skipped (OMNISIGHT_BACKUP_S3_URI unset)"
    return 0
  }
  command -v aws >/dev/null || die "aws CLI missing; cannot upload immutable off-site backup"
  [[ "$OMNISIGHT_BACKUP_S3_URI" == s3://* ]] || die "OMNISIGHT_BACKUP_S3_URI must start with s3://"

  local retain_days="${OMNISIGHT_BACKUP_S3_RETAIN_DAYS:-365}"
  [[ "$retain_days" =~ ^[1-9][0-9]*$ ]] || die "OMNISIGHT_BACKUP_S3_RETAIN_DAYS must be a positive integer"
  local storage_class="${OMNISIGHT_BACKUP_S3_STORAGE_CLASS:-GLACIER_IR}"
  local retain_until
  retain_until="$(utc_days_from_now "$retain_days")"

  local without_scheme="${OMNISIGHT_BACKUP_S3_URI#s3://}"
  local bucket="${without_scheme%%/*}"
  local prefix=""
  if [[ "$without_scheme" == */* ]]; then
    prefix="${without_scheme#*/}"
  fi
  [[ -n "$bucket" ]] || die "OMNISIGHT_BACKUP_S3_URI is missing bucket"
  prefix="${prefix%/}"
  local key
  if [[ -n "$prefix" ]]; then
    key="${prefix}/$(basename "$src")"
  else
    key="$(basename "$src")"
  fi

  local sse_args=(--server-side-encryption AES256)
  if [[ -n "${OMNISIGHT_BACKUP_S3_KMS_KEY_ID:-}" ]]; then
    sse_args=(--server-side-encryption aws:kms --ssekms-key-id "$OMNISIGHT_BACKUP_S3_KMS_KEY_ID")
  fi

  aws s3api put-object \
    --bucket "$bucket" \
    --key "$key" \
    --body "$src" \
    --storage-class "$storage_class" \
    --object-lock-mode COMPLIANCE \
    --object-lock-retain-until-date "$retain_until" \
    "${sse_args[@]}" >/dev/null || die "immutable off-site backup upload failed"
  ok "off-site immutable backup: s3://${bucket}/${key} (storage=${storage_class}, retain-until=${retain_until})"
}

[[ -n "${OMNISIGHT_BACKUP_PASSPHRASE:-}" ]] || \
  die "OMNISIGHT_BACKUP_PASSPHRASE is required for encrypted backups"
command -v gpg >/dev/null || die "gpg missing; cannot encrypt backup"

# Prefer the docker-compose-managed volume (canonical live DB).
# Fallback to host path if someone's running without compose.
COMPOSE_FILE="$REPO/docker-compose.prod.yml"
LIVE_DB=""
if docker compose -f "$COMPOSE_FILE" ps --services --filter status=running 2>/dev/null | grep -qx backend-a; then
  LIVE_DB="docker"
elif [[ -f "$REPO/data/omnisight.db" ]]; then
  LIVE_DB="host"
else
  die "no live DB found (backend-a not running and no host data/omnisight.db)"
fi

PLAIN="$BKP_DIR/${LABEL}-${TS}.db"

if [[ "$LIVE_DB" == "docker" ]]; then
  ok "using live DB via backend-a (WAL-safe online backup)"
  # Capture backup + quick_check inside the container (where sqlite3
  # lib + the DB coexist), then stream to host via tar to preserve
  # perms + avoid a mid-copy read by the app. `sqlite3.Connection.backup`
  # is the canonical WAL-safe online API — works while the app is
  # actively writing.
  docker compose -f "$COMPOSE_FILE" exec -T backend-a python3 - <<'PY' > "$PLAIN" || die "backup via container failed"
import sqlite3, sys, os, tempfile
src = sqlite3.connect("/app/data/omnisight.db")
tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
tmp.close()
dst = sqlite3.connect(tmp.name)
with dst:
    src.backup(dst)
check = dst.execute("PRAGMA quick_check;").fetchone()[0]
dst.close(); src.close()
if check != "ok":
    sys.stderr.write(f"quick_check failed: {check!r}\n"); sys.exit(1)
with open(tmp.name, "rb") as fh:
    sys.stdout.buffer.write(fh.read())
os.unlink(tmp.name)
PY
else
  ok "using host file directly (no compose running)"
  python3 - "$REPO/data/omnisight.db" "$PLAIN" <<'PY' || die "backup failed"
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
check = dst.execute("PRAGMA quick_check;").fetchone()[0]
dst.close(); src.close()
if check != "ok":
    sys.stderr.write(f"quick_check: {check!r}\n"); sys.exit(1)
PY
fi

# Perms 0600 — the WSL filesystem is visible from Windows via \\wsl$;
# 0644 would leak every admin hash / session token / audit record to
# any Windows user on the host.
chmod 600 "$PLAIN"

if ! python3 scripts/backup_dlp_scan.py "$PLAIN"; then
  shred -u "$PLAIN" 2>/dev/null || rm -f "$PLAIN"
  die "backup DLP scan failed; plaintext backup shredded"
fi
ok "backup DLP scan passed"

# gpg instead of `openssl enc` — OpenSSL 3 removed AEAD cipher support
# from `enc` (AES-256-GCM is no longer selectable) so we'd be left
# with CBC + manually-layered HMAC. gpg's symmetric mode is AES-256
# with integrated auth (MDC packet) and is stock on every Linux distro.
ENC="${PLAIN}.gpg"
# --pinentry-mode loopback + --passphrase-fd 0 is the non-interactive
# pattern. Passphrase goes via stdin so it never appears in argv.
if ! printf '%s' "$OMNISIGHT_BACKUP_PASSPHRASE" | gpg --batch --yes \
     --pinentry-mode loopback --passphrase-fd 0 \
     --cipher-algo AES256 --symmetric \
     --output "$ENC" "$PLAIN" 2>/tmp/gpg-err.$$; then
  cat /tmp/gpg-err.$$ >&2 2>/dev/null
  rm -f /tmp/gpg-err.$$
  shred -u "$PLAIN" 2>/dev/null || rm -f "$PLAIN"
  die "gpg encrypt failed"
fi
rm -f /tmp/gpg-err.$$
chmod 600 "$ENC"
# Best-effort secure-wipe; fallback to rm if shred not installed.
shred -u "$PLAIN" 2>/dev/null || rm -f "$PLAIN"
FINAL="$ENC"
SIZE="$(du -h "$FINAL" | cut -f1)"
ok "backup (encrypted): $FINAL ($SIZE)"
ok "restore: printf '%s' \"\$OMNISIGHT_BACKUP_PASSPHRASE\" | gpg --batch --pinentry-mode loopback --passphrase-fd 0 --decrypt $FINAL > <out.db>"
upload_offsite_immutable "$FINAL"

# Prune — keep newest $PRUNE, delete older. Applies to any backup file
# matching our label prefix (both short-lived .db and final .db.gpg).
KEEP_DIR_COUNT="$(ls -1t "$BKP_DIR"/${LABEL}-*.db* 2>/dev/null | wc -l)"
if (( KEEP_DIR_COUNT > PRUNE )); then
  ls -1t "$BKP_DIR"/${LABEL}-*.db* | tail -n +$((PRUNE + 1)) | while read -r f; do
    shred -u "$f" 2>/dev/null || rm -f "$f"
  done
  ok "pruned backups older than the newest $PRUNE"
fi
