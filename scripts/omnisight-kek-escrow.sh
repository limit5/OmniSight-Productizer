#!/usr/bin/env bash
# Escrow the KEK that decrypts prod's credential columns (OP-2760).
#
# WHY ASYMMETRIC
# --------------
# The KEK (/app/data/.secret_key, auto-generated because OMNISIGHT_SECRET_KEY is
# unset) is the sole key for git_accounts.encrypted_token, llm_credentials and
# DEK wrapping. No backup lane captures it, so every artefact from every lane
# restores to permanently unreadable credentials.
#
# Escrow has a built-in tension: DR wants the key to survive the host, while
# confidentiality wants whoever can read the backups NOT to be able to read the
# key. A symmetric passphrase resolves that only by asking an operator to keep a
# copy elsewhere -- and backup-dr.env already shows how that ends: its header
# asks exactly that, and nothing verifies it ever happened.
#
# So this host holds ONLY the public half. It can write an escrow artefact and
# is structurally incapable of reading one back. "Please keep the private key
# somewhere else" stops being a request and becomes a property of the system.
#
# The private key lives off-host. If it is lost, escrow artefacts are
# unrecoverable -- that is the deliberate trade, and it is why the round trip is
# proven before the key is moved, not after.
set -Eeuo pipefail

ESCROW_HOME="${OMNISIGHT_KEK_ESCROW_GNUPGHOME:-$HOME/.config/omnisight/kek-escrow-gnupg}"
RECIPIENT="${OMNISIGHT_KEK_ESCROW_RECIPIENT:-kek-escrow@sora-dev.app}"
BACKUP_DIR="${OMNISIGHT_KEK_ESCROW_DIR:-/home/user/omnisight-prod/data/backups}"
BACKEND="${OMNISIGHT_BACKEND_CONTAINER:-omnisight-productizer-backend-a-1}"
KEK_PATH="${OMNISIGHT_KEK_PATH:-/app/data/.secret_key}"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ -d "$ESCROW_HOME" ]] || die "escrow keyring $ESCROW_HOME missing"
command -v gpg >/dev/null || die "gpg missing"

# Refuse to run if a PRIVATE key is present. Its presence would mean this host
# can read its own escrow, which defeats the entire design -- and it is exactly
# the state an accidental import would leave behind.
if GNUPGHOME="$ESCROW_HOME" gpg --list-secret-keys 2>/dev/null | grep -q '^sec'; then
  die "a SECRET key is present in $ESCROW_HOME. This host must hold the public half only; \
escrow it cannot read is the point. Remove it before running."
fi

GNUPGHOME="$ESCROW_HOME" gpg --list-keys "$RECIPIENT" >/dev/null 2>&1 \
  || die "recipient $RECIPIENT not in $ESCROW_HOME"

TS="$(date -u '+%Y%m%dT%H%M%SZ')"
OUT="$BACKUP_DIR/kek-escrow-${TS}.gpg"
mkdir -p "$BACKUP_DIR"

TMP="$(mktemp)"; chmod 600 "$TMP"
# Register the shredding trap BEFORE the plaintext key can exist, so an abort
# cannot orphan a decrypted KEK on disk.
trap 'shred -u "$TMP" 2>/dev/null || rm -f "$TMP"' EXIT INT TERM

docker exec "$BACKEND" cat "$KEK_PATH" > "$TMP" 2>/dev/null \
  || die "cannot read $KEK_PATH from $BACKEND"
[[ -s "$TMP" ]] || die "$KEK_PATH read back empty -- refusing to escrow nothing"

GNUPGHOME="$ESCROW_HOME" gpg --batch --yes --trust-model always \
  --recipient "$RECIPIENT" --output "$OUT" --encrypt "$TMP" \
  || die "gpg encrypt failed"
chmod 600 "$OUT"

# Self-check: the artefact must NOT be readable here. If it is, the private key
# leaked onto this host and the escrow is worthless.
if GNUPGHOME="$ESCROW_HOME" gpg --batch --decrypt "$OUT" >/dev/null 2>&1; then
  rm -f "$OUT"
  die "this host could DECRYPT its own escrow -- the private key is present. Artefact removed."
fi

log "escrowed KEK -> $OUT ($(stat -c%s "$OUT") bytes, recipient $RECIPIENT)"
log "self-check OK: this host cannot read it back"
