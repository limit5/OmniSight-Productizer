#!/usr/bin/env bash
#
# OP-2733 — DR drill for the CURRENT prod backup lane (PostgreSQL + gpg AES-256).
#
# WHY THIS EXISTS
# ---------------
# The repo already ships scripts/dr_drill.sh and scripts/backup_selftest.py, and
# neither covers this lane: both are SQLite-era (12 and 9 sqlite references
# respectively, ZERO references to postgres, pg_restore or gpg). Scheduling them
# would produce a permanently green drill that proves nothing about the artefacts
# actually being produced — the same looks-configured-but-isn't failure this
# sweep exists to remove. Verified 2026-07-25: the 24 encrypted artefacts had
# NEVER been restore-tested.
#
# WHAT IT PROVES, on a real artefact, end to end:
#   1. the .gpg decrypts with the stored passphrase
#   2. the plaintext is a valid PostgreSQL custom-format dump
#   3. it RESTORES into a throwaway database (not just `pg_restore --list`)
#   4. the restored DB is sane: table count, alembic_version, non-empty audit_log
#
# SAFETY
# ------
# * The plaintext is shredded by an EXIT/INT/TERM trap registered BEFORE it is
#   created, so a SIGKILL-free abort can never orphan a decrypted prod dump.
#   (backup_prod_db.sh has the inverse bug — tracked as OP-2732.)
# * The throwaway DB is dropped by the same trap.
# * The passphrase is passed on stdin and never appears in argv or output.
# * The live database is never touched: restore goes to a fresh DB name.
#
# Failure exits non-zero so the unit's OnFailure= routes it to the JIRA alert
# channel (OP-2728) instead of into a log nobody reads.

set -Eeuo pipefail

BACKUP_DIR="${OMNISIGHT_BACKUP_DIR:-/home/user/omnisight-prod/data/backups}"
PG_CONTAINER="${OMNISIGHT_PG_CONTAINER:-omnisight-pg-primary}"
PG_USER="${OMNISIGHT_PG_USER:-omnisight}"
ENV_FILE="${OMNISIGHT_BACKUP_DR_ENV:-${HOME:-}/.config/omnisight/backup-dr.env}"
MIN_TABLES="${DR_MIN_TABLES:-100}"

WHICH="newest"
[[ "${1:-}" == "--oldest" ]] && WHICH="oldest"
[[ "${1:-}" == "--both" ]] && WHICH="both"

log() { printf '  %s\n' "$*"; }
die() { printf '  [FAIL] %s\n' "$*" >&2; exit 1; }

SCRATCH="" ; TMPDB="" ; CONTAINER_DUMP=""
cleanup() {
  [[ -n "$TMPDB" ]] && docker exec "$PG_CONTAINER" dropdb -U "$PG_USER" --if-exists "$TMPDB" >/dev/null 2>&1 || true
  [[ -n "$CONTAINER_DUMP" ]] && docker exec "$PG_CONTAINER" rm -f "$CONTAINER_DUMP" >/dev/null 2>&1 || true
  if [[ -n "$SCRATCH" && -d "$SCRATCH" ]]; then
    find "$SCRATCH" -type f -exec shred -u {} + 2>/dev/null || rm -rf "$SCRATCH"
    rm -rf "$SCRATCH" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

[[ -r "$ENV_FILE" ]] || die "backup env file unreadable: $ENV_FILE"
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
[[ -n "${OMNISIGHT_BACKUP_PASSPHRASE:-}" ]] || die "OMNISIGHT_BACKUP_PASSPHRASE not set in $ENV_FILE"
command -v gpg >/dev/null || die "gpg missing"
docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" >/dev/null 2>&1 || die "$PG_CONTAINER not ready"

# Sort by MTIME, not by name. `ls -1 | sort` is lexicographic and only coincides
# with chronological while every artefact is `manual-YYYYMMDD-HHMMSS.dump.gpg`; a
# hostname prefix or ISO dashes would silently make the drill test the wrong file
# AND compute staleness from it. -t is newest-first, so ALL[0] is newest.
mapfile -t ALL < <(ls -1t "$BACKUP_DIR"/*.dump.gpg 2>/dev/null)
[[ ${#ALL[@]} -gt 0 ]] || die "no .dump.gpg artefacts in $BACKUP_DIR"

NEWEST="${ALL[0]}"; OLDEST="${ALL[-1]}"   # ls -1t => newest first
case "$WHICH" in
  newest) TARGETS=("$NEWEST") ;;
  oldest) TARGETS=("$OLDEST") ;;
  both)   TARGETS=("$OLDEST" "$NEWEST") ;;
esac

SCRATCH="$(mktemp -d /tmp/omnisight-dr-drill.XXXXXX)"; chmod 700 "$SCRATCH"
log "artefacts available: ${#ALL[@]}  (drilling: ${#TARGETS[@]})"

FAILURES=0
for ARTEFACT in "${TARGETS[@]}"; do
  BASE="$(basename "$ARTEFACT" .dump.gpg)"
  log "--- $BASE"
  log "    sha256 $(sha256sum "$ARTEFACT" | cut -c1-32)…  $(stat -c %s "$ARTEFACT") bytes"

  PLAIN="$SCRATCH/$BASE.dump"
  if ! printf '%s' "$OMNISIGHT_BACKUP_PASSPHRASE" \
       | gpg --batch --quiet --pinentry-mode loopback --passphrase-fd 0 \
             --decrypt "$ARTEFACT" > "$PLAIN" 2>"$SCRATCH/gpg.err"; then
    log "    [FAIL] decrypt: $(head -1 "$SCRATCH/gpg.err")"
    FAILURES=$((FAILURES+1)); continue
  fi
  chmod 600 "$PLAIN"
  head -c 5 "$PLAIN" | grep -q "PGDMP" || {
    log "    [FAIL] not a PostgreSQL custom dump"; FAILURES=$((FAILURES+1)); continue
  }
  log "    decrypt OK ($(stat -c %s "$PLAIN") bytes, PGDMP header present)"

  TMPDB="dr_drill_$(date +%s)_$$"
  CONTAINER_DUMP="/tmp/${TMPDB}.dump"
  docker exec -i "$PG_CONTAINER" sh -c "cat > $CONTAINER_DUMP" < "$PLAIN"
  TOC="$(docker exec "$PG_CONTAINER" pg_restore --list "$CONTAINER_DUMP" 2>/dev/null | grep -cE '^[0-9]+;' || true)"
  [[ "$TOC" -gt 0 ]] || {
    log "    [FAIL] pg_restore --list produced no TOC entries"; FAILURES=$((FAILURES+1)); continue
  }

  docker exec "$PG_CONTAINER" createdb -U "$PG_USER" "$TMPDB" >/dev/null 2>&1 \
    || { log "    [FAIL] createdb"; FAILURES=$((FAILURES+1)); continue; }
  docker exec "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$TMPDB" --no-owner --no-privileges \
    "$CONTAINER_DUMP" >/dev/null 2>&1 || true   # warnings are normal; the assertions below decide

  TABLES="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$TMPDB" -tAc \
    "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null || echo 0)"
  HEAD="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$TMPDB" -tAc \
    "select version_num from alembic_version" 2>/dev/null || echo '')"
  AUDIT="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$TMPDB" -tAc \
    "select count(*) from audit_log" 2>/dev/null || echo 0)"

  log "    TOC=$TOC tables=$TABLES alembic=${HEAD:-none} audit_log=$AUDIT"
  OK=1
  [[ "$TABLES" -ge "$MIN_TABLES" ]] || { log "    [FAIL] only $TABLES tables (< $MIN_TABLES)"; OK=0; }
  [[ -n "$HEAD" ]] || { log "    [FAIL] no alembic_version row"; OK=0; }
  [[ "$AUDIT" -gt 0 ]] || { log "    [FAIL] audit_log empty"; OK=0; }

  # OP-2760: none of the assertions above touches an ENCRYPTED column, so this
  # drill reported green for months against artefacts whose every credential was
  # permanently unreadable -- the KEK lives on a docker volume no lane captured.
  #
  # This host cannot prove decryptability itself: it holds only the public half
  # of the escrow key, deliberately (see docs/operations/kek-escrow.md). So the
  # routine assertion is that an escrow artefact EXISTS, is recent, and is
  # addressed to the recipient we expect. That catches the realistic silent
  # failure -- escrow quietly stopping -- and is honest about its limit.
  #
  # It is NOT proof of recoverability. That needs the off-host private key and
  # is an operator rehearsal. A drill that borrowed the live key to "prove" a
  # restore would pass every night while measuring the wrong system, since the
  # disaster being modelled is one where this host is gone.
  if [[ -x "$HOME/.local/bin/omnisight-kek-escrow-check.sh" ]]; then
    if "$HOME/.local/bin/omnisight-kek-escrow-check.sh" >/dev/null 2>&1; then
      log "    KEK escrow present + addressed correctly (routine; NOT proof of recoverability)"
    else
      log "    [FAIL] KEK escrow missing/stale/misaddressed — a restore from this artefact"
      log "           would yield UNREADABLE credentials (OP-2760)"
      OK=0
    fi
  else
    log "    [FAIL] omnisight-kek-escrow-check.sh not installed — cannot assert the"
    log "           restored credentials would be decryptable (OP-2760)"
    OK=0
  fi

  [[ "$OK" -eq 1 ]] && log "    RESTORE VERIFIED" || FAILURES=$((FAILURES+1))

  docker exec "$PG_CONTAINER" dropdb -U "$PG_USER" --if-exists "$TMPDB" >/dev/null 2>&1 || true
  docker exec "$PG_CONTAINER" rm -f "$CONTAINER_DUMP" >/dev/null 2>&1 || true
  shred -u "$PLAIN" 2>/dev/null || rm -f "$PLAIN"
  TMPDB=""; CONTAINER_DUMP=""
done

# A drill that restores a four-day-old artefact and reports "passed" is worse than
# no drill: it converts a dead backup lane into a green light. Staleness is a
# FAILURE, not a warning — it must reach the operator through the unit's
# OnFailure= alert (OP-2728), which a log line never does. This was a real defect
# in the first version of this script: prod backups had been failing since
# 2026-07-23 while this exited 0 every night.
NEWEST_AGE_D=$(( ( $(date +%s) - $(stat -c %Y "$NEWEST") ) / 86400 ))
log "newest artefact is ${NEWEST_AGE_D}d old"
if [[ "$NEWEST_AGE_D" -gt "${DR_MAX_AGE_DAYS:-3}" ]]; then
  log "    [FAIL] newest artefact ${NEWEST_AGE_D}d old (> ${DR_MAX_AGE_DAYS:-3}d)"
  log "           the backup lane is not producing — this is not a drill defect"
  FAILURES=$((FAILURES+1))
fi

[[ "$FAILURES" -eq 0 ]] \
  || die "$FAILURES failure(s); drilled=${#TARGETS[@]}, newest ${NEWEST_AGE_D}d old"
log "DR drill passed"
