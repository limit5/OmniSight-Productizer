#!/usr/bin/env bash
# Local pg_dump of prod — daily. Encrypted at rest since OP-2731 P2.
#
# WHAT THIS LANE IS FOR
# ---------------------
# It is the DR floor: the lane no content gate can stop. Lane A (encrypted,
# DLP-gated) and lane C (encrypted, DLP-gated, off-site) can both withhold on a
# content verdict. This one cannot. During 2026-07-22..26 lane A was blocked
# four consecutive nights and this lane is part of why any backup existed at
# all. It is deliberately NOT gated, and that is the design, not an oversight.
#
# WHY IT IS ENCRYPTED TO A KEY THIS HOST CANNOT READ
# --------------------------------------------------
# The defect being fixed is a plaintext production dump sitting on disk — a PII
# and defence-in-depth problem, not credential disclosure: the credential
# columns are already ciphertext and tenant_deks has no rows.
#
# The audit was explicit that this must NOT be solved by encrypting under lane
# A's passphrase, because that collapses two key domains into one and a single
# lost file then takes out every lane. So this lane encrypts to the OP-2760
# escrow public key, whose private half lives off-host and was proven to
# round-trip before it was moved. The result is real separation:
#
#   lose ~/.config/omnisight/backup-dr.env  -> A and C unrecoverable, B is not
#   lose the off-host escrow private key    -> B unrecoverable, A and C are not
#
# Neither key alone restores everything, and no single on-host file loses
# everything. The cost, stated plainly: one off-host key now protects both the
# KEK escrow and this lane, so losing it is correlated damage.
set -euo pipefail

ts=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="${HOME}/backup"
ESCROW_HOME="${OMNISIGHT_KEK_ESCROW_GNUPGHOME:-$HOME/.config/omnisight/kek-escrow-gnupg}"
RECIPIENT="${OMNISIGHT_KEK_ESCROW_RECIPIENT:-kek-escrow@sora-dev.app}"
RETENTION_DAYS="${OMNISIGHT_LOCAL_RETENTION_DAYS:-14}"
mkdir -p "$BACKUP_DIR"

[[ -d "$ESCROW_HOME" ]] || { echo "FAIL: escrow keyring $ESCROW_HOME missing" >&2; exit 1; }
GNUPGHOME="$ESCROW_HOME" gpg --list-keys "$RECIPIENT" >/dev/null 2>&1 \
  || { echo "FAIL: recipient $RECIPIENT not in $ESCROW_HOME" >&2; exit 1; }

plain="${BACKUP_DIR}/daily-${ts}.sql"
out="${plain}.gpg"
# Registered before the plaintext can exist, so an abort cannot orphan an
# unencrypted production dump on disk.
trap 'shred -u "$plain" 2>/dev/null || rm -f "$plain" 2>/dev/null || true' EXIT INT TERM

docker exec omnisight-pg-primary pg_dump -U omnisight -d omnisight > "$plain"

# Validate BEFORE encrypting: a truncated dump encrypts just as happily as a
# good one, and this size floor is the only correctness check this lane has.
size=$(stat -c %s "$plain")
if [ "$size" -lt 1000000 ]; then
  echo "FAIL: dump too small ($size bytes); leaving for inspection" >&2
  exit 1
fi

GNUPGHOME="$ESCROW_HOME" gpg --batch --yes --trust-model always \
  --recipient "$RECIPIENT" --output "$out" --encrypt "$plain" \
  || { echo "FAIL: encryption failed" >&2; exit 1; }
chmod 600 "$out"
shred -u "$plain" 2>/dev/null || rm -f "$plain"

# Retention. The pattern MUST change with the extension, in this same edit: the
# old rule matched 'daily-*.sql', which the encrypted name does not, so renaming
# without touching this would have stopped retention silently and filled the
# disk. The audit flagged exactly this, and it is why P2 was never a one-line
# rename.
find "$BACKUP_DIR" -maxdepth 1 -name 'daily-*.sql.gpg' -mtime "+$RETENTION_DAYS" -print -delete \
  | sed 's|^|  pruned: |'
# Sweep pre-P2 plaintext under the contract it was written with. Without this it
# is matched by nothing and stays forever — the same class of gap that left two
# 70-day-old plaintext dumps in lane C's directory.
find "$BACKUP_DIR" -maxdepth 1 -name 'daily-*.sql' ! -name '*.gpg' -mtime "+$RETENTION_DAYS" -print -delete \
  | sed 's|^|  pruned legacy plaintext: |'

echo "OK: ${out} ($(stat -c %s "$out") bytes, encrypted to $RECIPIENT)"
