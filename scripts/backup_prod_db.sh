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
# OP-1640: prod runs PostgreSQL-HA, so the default is a real pg_dump of the
# live PG. --sqlite forces the legacy SQLite path (dev / single-file installs).
SQLITE_MODE=false

REQUIRE_EXCLUSIVE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --require-exclusive) REQUIRE_EXCLUSIVE=true; shift;;
    --label) LABEL="$2"; shift 2;;
    --prune) PRUNE="$2"; shift 2;;
    --sqlite) SQLITE_MODE=true; shift;;
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

  # OP-2731 F4. This defaulted to 365 days of COMPLIANCE, which is a live trap
  # now that Object Lock is enabled on the bucket (GOVERNANCE/30d, 2026-07-27):
  # COMPLIANCE cannot be shortened or bypassed by ANYONE, including the account
  # root, so a single run would have pinned objects as undeletable for a year
  # against a 30-day lifecycle -- and against the 30-day retention this project
  # committed to in OP-2747 decision 3, where a retained category with no
  # working expiry is a defect.
  #
  # GOVERNANCE is the reversible control and still blocks the realistic threat,
  # because the backup IAM user is explicitly denied BypassGovernanceRetention.
  local retain_days="${OMNISIGHT_BACKUP_S3_RETAIN_DAYS:-30}"
  local lock_mode="${OMNISIGHT_BACKUP_S3_LOCK_MODE:-GOVERNANCE}"
  if [[ "$lock_mode" == "COMPLIANCE" ]]; then
    warn "COMPLIANCE object-lock requested for ${retain_days}d — this is \
IRREVERSIBLE and unbypassable by anyone including root. Ensure it does not exceed \
the bucket lifecycle, or objects become permanently undeletable."
  fi
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
    --object-lock-mode "$lock_mode" \
    --object-lock-retain-until-date "$retain_until" \
    "${sse_args[@]}" >/dev/null || die "immutable off-site backup upload failed"
  ok "off-site immutable backup: s3://${bucket}/${key} (storage=${storage_class}, retain-until=${retain_until})"
}

[[ -n "${OMNISIGHT_BACKUP_PASSPHRASE:-}" ]] || \
  die "OMNISIGHT_BACKUP_PASSPHRASE is required for encrypted backups"
command -v gpg >/dev/null || die "gpg missing; cannot encrypt backup"

# FX.7.10 — DLP scanner existence preflight.
#
# The DLP scan at line ~190 is the *only* gate stopping a plaintext
# secret from being baked into the encrypted backup artefact. If the
# scanner script is missing on the prod host (deploy bundle stripped,
# script renamed, /scripts not mounted, etc.), the original `if !
# python3 scripts/backup_dlp_scan.py "$PLAIN"` form would have failed
# with the *same* "backup DLP scan failed" die message that a real
# finding produces — silently demoting a deploy-artefact gap into a
# normal-looking scan-block, then shredding the plaintext while the
# operator believes "DLP works, just blocked something today".
#
# Worse: if the operator chases the message and rebuilds the deploy
# bundle without the scanner, the next run hits the same path; the
# DLP gate never actually runs, and any future plaintext secret
# slides through into the .gpg artefact under the false impression
# that DLP cleared it.
#
# Fail closed BEFORE plaintext extract:
#   - The scanner file must exist and be non-empty (-s)
#   - It must be readable (-r) by the user running the backup
#   - python3 must be on PATH (also needed by the WAL backup steps)
# A missing scanner is a deploy-artefact bug, not a scan-blocked
# event; the die message says so explicitly so operators triage to
# the right place (rebuild image / rsync scripts/) instead of going
# hunting for a phantom secret.
DLP_SCANNER="$REPO/scripts/backup_dlp_scan.py"
[[ -s "$DLP_SCANNER" ]] || \
  die "DLP scanner missing or empty: $DLP_SCANNER — deploy artefact incomplete; aborting BEFORE plaintext extract"
[[ -r "$DLP_SCANNER" ]] || \
  die "DLP scanner not readable: $DLP_SCANNER (perms?); aborting BEFORE plaintext extract"
command -v python3 >/dev/null || \
  die "python3 missing; DLP scanner cannot run; aborting BEFORE plaintext extract"

# OP-2729 — reviewed-body digest allowlist for content-reviewed columns
# (claude_memory_versions.body). It lives OUTSIDE this checkout on purpose: prod
# runs from a release-PINNED tree, so keeping reviewed digests in a tracked file
# would dirty it on every review — and a dirty tree hard-fails deploy-prod.sh AND
# emergency digest rollback. Bind-mounted read-only into the ephemeral scan
# container; absent means nothing is approved and the gate keeps blocking, which
# is the correct default (absent evidence of review is not review).
# ${HOME:-} not $HOME: this script runs under `set -u`, and the drift-guard test
# invokes it with a scrubbed environment. An unset HOME must degrade to "no
# allowlist" (fail-closed), never to an unbound-variable abort before the preflight.
DLP_REVIEWED_BODIES="${OMNISIGHT_DLP_REVIEWED_BODIES_HOST:-${HOME:-}/.config/omnisight/backup-dlp-reviewed-bodies.txt}"
DLP_REVIEWED_MOUNT=()
if [[ -r "$DLP_REVIEWED_BODIES" ]]; then
  # Target a writable in-container path: the backend rootfs is READ-ONLY, so a
  # bind whose mountpoint does not already exist fails at container init. /tmp is
  # a tmpfs. Deliberately NOT /etc/omnisight — that path is already bound to the
  # deploy overlay directory, and putting operator review state inside
  # deploy-managed state is the invisible coupling this whole change exists to avoid.
  DLP_REVIEWED_MOUNT=(
    --volume "$DLP_REVIEWED_BODIES:/tmp/omnisight-dlp-reviewed-bodies.txt:ro"
    --env "OMNISIGHT_DLP_REVIEWED_BODIES=/tmp/omnisight-dlp-reviewed-bodies.txt"
  )
else
  warn "no reviewed-body allowlist at $DLP_REVIEWED_BODIES — content-reviewed columns will block (fail-closed)"
fi

# ── OP-2732: advisory exclusion, deliberately NOT the guard the ticket asked for
# The ticket wanted a hard guard against "a second concurrent run while the
# 02:17 timer instance is active". That scenario cannot happen through systemd:
# the unit is Type=oneshot, and a start issued while it is activating is MERGED
# into the running job -- ExecStart never runs twice.
#
# What a hard guard WOULD hit is deploy-prod.sh, which calls this script
# directly, outside systemd, as its pre-deploy backup. A deploy overlapping the
# backup window would have that backup refused, aborting the deploy -- and the
# documented escape hatch is --skip-backup, i.e. deploy with NO backup. A change
# made for data safety would have opened a path to deploying without any.
#
# So: flock on a dedicated fd. Kernel state, no on-disk residue, nothing to
# reap -- unlike a pidfile or mkdir lock, which survive a kill and then block
# every future run silently. Measured on this host: a mkdir lock whose holder
# is SIGKILLed stays held forever; the flock is released.
#
# Measured caveat, stated because it is real: flock is released when the fd is
# closed, and CHILDREN INHERIT IT. Killing only the parent while a child still
# holds fd 9 leaves the lock held. That is safe on the systemd path -- the unit
# is KillMode=control-group with FinalKillSignal=9, so the whole cgroup dies
# together -- but a hand-run killed with a bare `kill -9 <pid>` can leave it
# stuck until the stray child exits.
#
# Which is exactly why this is NON-FATAL by default: a stuck lock then costs a
# warning, not a backup. --require-exclusive makes it fatal and is available for
# a caller that genuinely needs mutual exclusion; nothing passes it today, and
# deploy-prod.sh must never pass it (see above).
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/omnisight-prod-backup.lock"
exec 9>"$LOCK_FILE" || true
if ! flock -n 9 2>/dev/null; then
  if [[ "$REQUIRE_EXCLUSIVE" == true ]]; then
    die "another backup holds $LOCK_FILE and --require-exclusive was given"
  fi
  warn "another backup appears to be running ($LOCK_FILE); proceeding anyway"
fi

COMPOSE_FILE="$REPO/docker-compose.prod.yml"
PG_CONTAINER="${OMNISIGHT_PG_CONTAINER:-omnisight-pg-primary}"

# ── OP-1640: prefer a REAL pg_dump of the live PostgreSQL ──
# Prod runs PostgreSQL-HA (pg-primary). The legacy SQLite path backed up a
# STALE /app/data/omnisight.db (a pre-PG leftover in the data volume) — NOT
# the live DB — so it was useless rollback insurance. pg_dump (custom format)
# is MVCC-consistent. The mandatory DLP scan runs against a THROWAWAY DB
# restored from the dump (proving the very artifact we encrypt is clean), via
# backend-a (psycopg2 + db_ha network); the temp DB is always dropped.
# ── OP-2732: one cleanup handler, armed before anything can exist ────────────
# The old shape armed a trap only AFTER the plaintext was written, and dropped
# only the temp DB -- so a failure between creation and encryption orphaned a
# full PLAINTEXT production dump. Worse, `> "$PLAIN"` creates the file before
# pg_dump even runs, so `|| die "pg_dump failed"` left a partial plaintext with
# no trap armed at all.
#
# Both variables are initialised above the branch, so the PG path and the legacy
# SQLite path (which had NO trap whatsoever) are covered by construction and
# there is no creation-to-arming window left to reason about.
#
# EXIT only, deliberately. bash already runs an EXIT trap on SIGTERM, and the
# unit's timeout path is SIGTERM + 90s grace before SIGKILL, so EXIT covers it.
# Adding INT/TERM to a handler that does not itself exit makes the script RESUME
# after its own cleanup and run the handler twice -- and a signal arriving after
# the shred would exit 0 on a terminated run, suppressing the OnFailure alert
# this lane exists to have. SIGKILL is unfixable in userspace either way.
#
# Never `trap - EXIT`: each variable is nulled as its resource is destroyed, so
# the handler stays armed through the gpg stage and degrades to a no-op.
PLAIN=""
TMP_DB=""
_cleanup() {
  local rc=$?
  if [[ -n "$TMP_DB" ]]; then
    # Name-SHAPE assertion, never a pattern sweep: a LIKE 'omnisight%' orphan
    # sweep would match the live production database.
    if [[ "$TMP_DB" =~ ^omnisight_backup_dlp_[0-9] ]]; then
      # Killing `docker exec` does not signal the process inside the container,
      # so a pg_restore may still hold the temp DB and dropdb would fail with
      # "being accessed by other users" -- silently, behind `|| true`.
      docker exec "$PG_CONTAINER" psql -U "${PG_USER:-omnisight}" -d postgres -tAc \
        "select pg_terminate_backend(pid) from pg_stat_activity where datname='$TMP_DB'" >/dev/null 2>&1 || true
      docker exec "$PG_CONTAINER" dropdb -U "${PG_USER:-omnisight}" --if-exists "$TMP_DB" >/dev/null 2>&1 || true
    fi
  fi
  if [[ -n "$PLAIN" && -e "$PLAIN" ]]; then
    shred -u "$PLAIN" 2>/dev/null || rm -f "$PLAIN"
  fi
  rm -f "/tmp/gpg-err.$$" 2>/dev/null || true
  return $rc
}
# Narrow counterpart to _cleanup for the success path, where the temp DB is
# finished with but the plaintext is still needed by the gpg stage.
_drop_tmp_db() {
  [[ -n "$TMP_DB" ]] || return 0
  [[ "$TMP_DB" =~ ^omnisight_backup_dlp_[0-9] ]] || return 0
  docker exec "$PG_CONTAINER" psql -U "${PG_USER:-omnisight}" -d postgres -tAc \
    "select pg_terminate_backend(pid) from pg_stat_activity where datname='$TMP_DB'" >/dev/null 2>&1 || true
  docker exec "$PG_CONTAINER" dropdb -U "${PG_USER:-omnisight}" --if-exists "$TMP_DB" >/dev/null 2>&1 || true
  TMP_DB=""
}
trap _cleanup EXIT

if [[ "$SQLITE_MODE" == false ]] && docker inspect "$PG_CONTAINER" >/dev/null 2>&1; then
  ok "PG mode: pg_dump live PostgreSQL via $PG_CONTAINER"
  docker compose -f "$COMPOSE_FILE" ps --services --filter status=running 2>/dev/null | grep -qx backend-a \
    || die "backend-a not running; cannot run the PG DLP scan"
  PG_USER="$(docker exec "$PG_CONTAINER" printenv POSTGRES_USER 2>/dev/null || true)"; PG_USER="${PG_USER:-omnisight}"
  PG_DB="$(docker exec "$PG_CONTAINER" printenv POSTGRES_DB 2>/dev/null || true)"; PG_DB="${PG_DB:-omnisight}"
  PLAIN="$BKP_DIR/${LABEL}-${TS}.dump"
  # MVCC-consistent custom-format dump streamed to the host file (umask 077).
  ( umask 077; docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" \
      --format=custom --no-owner --no-privileges > "$PLAIN" ) || die "pg_dump failed"
  chmod 600 "$PLAIN"
  # A post-write integrity assertion INSTEAD of a free-space preflight. The
  # database is ~45 MB against 231 GB free, so any preflight threshold sits
  # orders of magnitude below the noise floor, and the disk-floor check already
  # alarms at 85/92% every 15 minutes. A preflight would only add a third way to
  # REFUSE a backup that would have succeeded. This cannot: it catches ENOSPC
  # truncation and every other corruption mode, on the bytes actually written.
  [[ -s "$PLAIN" ]] || die "pg_dump produced an empty file"
  head -c 5 "$PLAIN" | grep -q PGDMP || die "dump lacks the PGDMP header — truncated or corrupt"
  # Restore into a throwaway DB inside pg-primary, scan it, always drop it.
  TMP_DB="omnisight_backup_dlp_${TS}_$$"
  docker exec "$PG_CONTAINER" createdb -U "$PG_USER" "$TMP_DB" || die "DLP temp DB create failed"
  docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$TMP_DB" \
      --no-owner --no-privileges < "$PLAIN" || die "DLP temp DB restore failed"
  # Scan via backend-a. OP-1731: the scanner derives the temp-DB connection
  # URL from the container's own OMNISIGHT_DATABASE_URL (host = pg-primary,
  # preserved; only the DB name swapped to $TMP_DB) — see
  # backup_dlp_scan.build_tmp_db_url. This replaces a fragile host/shell
  # `rsplit('/')` that produced a HOSTLESS DSN in a develop-tip worktree /
  # ephemeral `compose run` context, where psycopg2 silently fell back to
  # 127.0.0.1 → connection-refused → the plaintext pg_dump was shredded.
  # `docker compose run` inherits backend-a's service networks (including the
  # external db_ha / postgres-ha_pg-ha net), so pg-primary resolves regardless
  # of cwd / COMPOSE_PROJECT_NAME / worktree. The password never appears in
  # host process argv (URL stays inside the container). A connect/query error
  # still returns a non-zero exit → hard fail → shred (never a silent pass).
  if ! docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
        --volume "$DLP_SCANNER:/app/scripts/backup_dlp_scan.py:ro" \
        ${DLP_REVIEWED_MOUNT[@]+"${DLP_REVIEWED_MOUNT[@]}"} \
        --entrypoint python3 backend-a \
        /app/scripts/backup_dlp_scan.py --postgres-tmp-db "$TMP_DB"; then
    # DLP block: both resources must go, and the handler already does exactly
    # that. Null afterwards so the EXIT handler degrades to a no-op.
    _cleanup; TMP_DB=""; PLAIN=""
    die "backup DLP scan failed; plaintext pg_dump shredded"
  fi
  # Drop ONLY the temp DB here. Calling the full handler would also shred the
  # plaintext, which gpg still needs -- that mistake broke this lane on its
  # first real run and is why this path is narrow and explicit.
  _drop_tmp_db
  ok "backup DLP scan passed (PG; scanned a restored temp copy of the dump)"
else
  # ── Legacy SQLite path: only with explicit --sqlite (dev / single-file). ──
  # Fail closed otherwise so we never silently back up a stale SQLite again.
  [[ "$SQLITE_MODE" == true ]] || \
    die "PostgreSQL container '$PG_CONTAINER' not found and --sqlite not set — refusing to back up a possibly-stale SQLite file (OP-1640)"
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
    ok "using live SQLite via backend-a (WAL-safe online backup)"
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
    ok "using host SQLite file directly (no compose running)"
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
  chmod 600 "$PLAIN"
  if ! python3 "$DLP_SCANNER" "$PLAIN"; then
    shred -u "$PLAIN" 2>/dev/null || rm -f "$PLAIN"
    die "backup DLP scan failed; plaintext backup shredded"
  fi
  ok "backup DLP scan passed"
fi

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
KEEP_DIR_COUNT="$( { ls -1t "$BKP_DIR"/${LABEL}-*.db* "$BKP_DIR"/${LABEL}-*.dump* 2>/dev/null || true; } | wc -l )"
if (( KEEP_DIR_COUNT > PRUNE )); then
  ls -1t "$BKP_DIR"/${LABEL}-*.db* "$BKP_DIR"/${LABEL}-*.dump* 2>/dev/null | tail -n +$((PRUNE + 1)) | while read -r f; do
    shred -u "$f" 2>/dev/null || rm -f "$f"
  done
  ok "pruned backups older than the newest $PRUNE"
fi
