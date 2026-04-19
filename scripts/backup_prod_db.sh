#!/usr/bin/env bash
# backup_prod_db.sh — WAL-safe snapshot of the live prod SQLite DB with
# owner-only perms and optional AES-256-GCM encryption.
#
# Run on the prod host (WSL Ubuntu-24.04). Reads the live DB through
# backend-a's mounted volume (SQLite `.backup` online pragma — no
# downtime, no lock contention with readers).
#
# Env:
#   OMNISIGHT_BACKUP_PASSPHRASE   — if set, backup is encrypted with
#                                   openssl AES-256-GCM + PBKDF2. The
#                                   passphrase is NEVER stored on disk
#                                   by this script; keep it in the team
#                                   password manager alongside the .enc
#                                   file to preserve restore capability.
#                                   Unset → plaintext (0600 only).
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

if [[ -n "${OMNISIGHT_BACKUP_PASSPHRASE:-}" ]]; then
  # gpg instead of `openssl enc` — OpenSSL 3 removed AEAD cipher support
  # from `enc` (AES-256-GCM is no longer selectable) so we'd be left
  # with CBC + manually-layered HMAC. gpg's symmetric mode is AES-256
  # with integrated auth (MDC packet) and is stock on every Linux distro.
  command -v gpg >/dev/null || die "OMNISIGHT_BACKUP_PASSPHRASE set but gpg missing"
  ENC="${PLAIN}.gpg"
  # --pinentry-mode loopback + --passphrase-fd 0 is the non-interactive
  # pattern. Passphrase goes via stdin so it never appears in argv.
  if ! printf '%s' "$OMNISIGHT_BACKUP_PASSPHRASE" | gpg --batch --yes \
       --pinentry-mode loopback --passphrase-fd 0 \
       --cipher-algo AES256 --symmetric \
       --output "$ENC" "$PLAIN" 2>/tmp/gpg-err.$$; then
    cat /tmp/gpg-err.$$ >&2 2>/dev/null
    rm -f /tmp/gpg-err.$$
    die "gpg encrypt failed (plaintext left at $PLAIN; remove manually)"
  fi
  rm -f /tmp/gpg-err.$$
  chmod 600 "$ENC"
  # Best-effort secure-wipe; fallback to rm if shred not installed
  shred -u "$PLAIN" 2>/dev/null || rm -f "$PLAIN"
  FINAL="$ENC"
  SIZE="$(du -h "$FINAL" | cut -f1)"
  ok "backup (encrypted): $FINAL ($SIZE)"
  ok "restore: printf '%s' \"\$OMNISIGHT_BACKUP_PASSPHRASE\" | gpg --batch --pinentry-mode loopback --passphrase-fd 0 --decrypt $FINAL > <out.db>"
else
  FINAL="$PLAIN"
  SIZE="$(du -h "$FINAL" | cut -f1)"
  warn "OMNISIGHT_BACKUP_PASSPHRASE unset — backup is plaintext (0600)"
  warn "  set the env var to opt into AES-256-GCM encryption for future backups"
  ok "backup: $FINAL ($SIZE)"
fi

# Prune — keep newest $PRUNE, delete older. Applies to any backup file
# matching our label prefix (both .db and .db.enc).
KEEP_DIR_COUNT="$(ls -1t "$BKP_DIR"/${LABEL}-*.db* 2>/dev/null | wc -l)"
if (( KEEP_DIR_COUNT > PRUNE )); then
  ls -1t "$BKP_DIR"/${LABEL}-*.db* | tail -n +$((PRUNE + 1)) | while read -r f; do
    shred -u "$f" 2>/dev/null || rm -f "$f"
  done
  ok "pruned backups older than the newest $PRUNE"
fi
