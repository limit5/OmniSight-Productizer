#!/usr/bin/env bash
# Routine escrow check (OP-2760). Runs WITHOUT the private key, by design.
#
# This host cannot decrypt its own escrow, so it cannot prove recoverability on
# its own -- and saying so is the point. What it CAN prove routinely is that an
# escrow artefact exists, is recent, and is addressed to the expected recipient.
# That catches the realistic silent failure: escrow quietly stopping.
#
# Full proof of recoverability requires the off-host private key and is an
# operator rehearsal (see docs/operations/kek-escrow.md). Do not conflate the
# two: a routine PASS here means "escrow is running", never "the backups are
# recoverable".
set -Eeuo pipefail
DIR="${OMNISIGHT_KEK_ESCROW_DIR:-/home/user/omnisight-prod/data/backups}"
MAX_AGE_DAYS="${OMNISIGHT_KEK_ESCROW_MAX_AGE_DAYS:-2}"
EXPECT_KEYID="${OMNISIGHT_KEK_ESCROW_KEYID:-848A5F172FD988B6}"
die() { echo "[FAIL] $*" >&2; exit 1; }

mapfile -t ALL < <(ls -1t "$DIR"/kek-escrow-*.gpg 2>/dev/null || true)
[[ ${#ALL[@]} -gt 0 ]] || die "no KEK escrow artefact in $DIR. Prod backups do not \
carry the key that decrypts their own credential columns (OP-2760)."
NEWEST="${ALL[0]}"

AGE_S=$(( $(date +%s) - $(stat -c %Y "$NEWEST") ))
AGE_D=$(awk "BEGIN{printf \"%.1f\", $AGE_S/86400}")
awk "BEGIN{exit !($AGE_S > $MAX_AGE_DAYS*86400)}" \
  && die "newest escrow $(basename "$NEWEST") is ${AGE_D}d old (limit ${MAX_AGE_DAYS}d) -- escrow has stopped"

# Recipient check needs no private key: the keyid is in the PKESK packet.
# gpg exits NON-ZERO here by design -- it cannot decrypt, which is the whole
# point of this host holding only the public half. Under `set -o pipefail` that
# non-zero poisons `gpg | grep` and the check fails even when the recipient is
# correct. Capture first, then match. (Found by the normal case: all three
# induced-failure tests "passed" precisely because they were meant to fail.)
PACKETS="$(gpg --list-packets "$NEWEST" 2>/dev/null || true)"
grep -q "keyid $EXPECT_KEYID" <<< "$PACKETS" \
  || die "$(basename "$NEWEST") is NOT encrypted to $EXPECT_KEYID -- it may be \
addressed to a key nobody holds, which is indistinguishable from no escrow at all"

echo "OK: $(basename "$NEWEST") present, ${AGE_D}d old, addressed to $EXPECT_KEYID"
echo "NOTE: routine check only. Recoverability is proven by an operator rehearsal"
echo "      with the off-host private key, not by this host."
