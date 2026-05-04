#!/usr/bin/env bash
# scripts/setup_release_signer.sh — provision the operator GPG release-
# signing key for FX.9.8 prod deploy gating.
#
# Idempotent: refuses to overwrite an existing release-signer key
# unless --allow-existing is passed (used by the rotation flow). Does
# the bare minimum gpg dance + emits the fingerprint on stdout so
# downstream scripts can pipe it into prod-deploy-signers.txt.
#
# What it does NOT do:
#   - Append the fingerprint to deploy/prod-deploy-signers.txt
#     (committing a fingerprint is a deliberate audit-trailed PR step,
#     not a side effect of running this script).
#   - Set git config (let the operator decide global vs repo-local).
#   - Push anything anywhere.
#
# Usage:
#   scripts/setup_release_signer.sh --name "Your Name" --email "you@example.com"
#   scripts/setup_release_signer.sh --name "..." --email "..." --allow-existing
#   scripts/setup_release_signer.sh --name "..." --email "..." --expire 1y
#
# After running, follow docs/runbook/gpg-release-signer-setup.md §1.3
# onwards to wire the fingerprint into the signers file + git config.

set -euo pipefail

NAME=""
EMAIL=""
EXPIRE="2y"
ALLOW_EXISTING=false

err() { echo "❌ setup_release_signer: $*" >&2; exit 1; }
warn() { echo "⚠️  setup_release_signer: $*" >&2; }
log() { echo "✅ setup_release_signer: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) NAME="${2:-}"; shift 2;;
        --email) EMAIL="${2:-}"; shift 2;;
        --expire) EXPIRE="${2:-}"; shift 2;;
        --allow-existing) ALLOW_EXISTING=true; shift;;
        -h|--help) sed -n '2,25p' "$0"; exit 0;;
        *) err "unknown arg: $1";;
    esac
done

[[ -n "$NAME" ]] || err "--name is required (e.g. \"nanakusa sora\")"
[[ -n "$EMAIL" ]] || err "--email is required (e.g. rt3628@hotmail.com)"
command -v gpg >/dev/null 2>&1 || err "gpg not found in PATH — install gnupg first"

# Refuse to silently shadow an existing release-signer key for this
# email — the operator should consciously rotate (per §2 of the
# runbook) rather than accidentally provision a duplicate.
if gpg --list-secret-keys --with-colons "$EMAIL" 2>/dev/null \
        | grep -q '^sec:'; then
    if [[ "$ALLOW_EXISTING" != "true" ]]; then
        err "a secret key for <$EMAIL> already exists in the keyring. Pass --allow-existing for the rotation flow, or delete the old key with 'gpg --delete-secret-keys <FPR>' first."
    fi
    warn "existing key for <$EMAIL> present; --allow-existing set, proceeding anyway"
fi

KEYGEN_BATCH="$(mktemp)"
trap 'shred -u "$KEYGEN_BATCH" 2>/dev/null || rm -f "$KEYGEN_BATCH"' EXIT

cat > "$KEYGEN_BATCH" <<EOF
%no-protection
Key-Type: EDDSA
Key-Curve: ed25519
Key-Usage: sign
Name-Real: $NAME
Name-Email: $EMAIL
Name-Comment: OmniSight prod deploy release-signer (FX.9.8)
Expire-Date: $EXPIRE
%commit
EOF

log "generating ed25519 sign-only key for <$EMAIL> (expires in $EXPIRE)..."
gpg --batch --pinentry-mode loopback --generate-key "$KEYGEN_BATCH" 2>&1 \
    | grep -v '^gpg: directory' \
    | grep -v '^gpg: revocation certificate' \
    | grep -v '^gpg: keybox' >&2 || true

# Pull the just-created key's fingerprint. Multiple keys for the same
# email may exist after a rotation — pick the newest by creation time
# (column 6 of `sec:` line).
FPR="$(
    gpg --list-secret-keys --with-colons "$EMAIL" 2>/dev/null \
        | awk -F: '
            /^sec:/   { ctime = $6 }
            /^fpr:/   { print ctime, $10 }
          ' \
        | sort -nr \
        | awk 'NR==1 {print $2}'
)"

[[ -n "$FPR" ]] || err "failed to extract fingerprint from gpg keyring after generation"
[[ "${#FPR}" -eq 40 ]] || err "extracted fingerprint has unexpected length: $FPR"

log "key generated. fingerprint: $FPR"
log "next steps:"
echo "  1. Append to deploy/prod-deploy-signers.txt:" >&2
echo "       echo '$FPR' >> deploy/prod-deploy-signers.txt" >&2
echo "  2. Re-export the public-key bundle:" >&2
echo "       gpg --armor --export \$(awk '!/^#/ && NF {print \$1}' deploy/prod-deploy-signers.txt) > deploy/release-signers.asc" >&2
echo "  3. Wire git signing:" >&2
echo "       git config --global user.signingkey $FPR" >&2
echo "       git config --global commit.gpgsign true" >&2
echo "       git config --global tag.gpgsign true" >&2
echo "  4. Sign master tip and push:" >&2
echo "       git commit --allow-empty -S -m 'chore(release): sign master tip'" >&2
echo "       git push origin master" >&2
echo "  5. Verify gate accepts without --insecure-skip-verify:" >&2
echo "       ./scripts/check_deploy_ref.sh --kind branch --ref master" >&2

# Print fingerprint on stdout (no prefix) for piping to other scripts.
printf '%s\n' "$FPR"
