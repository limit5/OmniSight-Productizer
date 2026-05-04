#!/usr/bin/env bash
# scripts/check_deploy_ref.sh — gate which git refs may be deployed
# to production (FX.7.9). Two-layer check:
#
#   Layer 1 — Allowlist match: ref must satisfy at least one rule in
#             deploy/prod-deploy-allowlist.txt
#   Layer 2 — GPG signature:   the tag's signature (annotated tags) or
#             the branch-tip commit's signature must be made by a key
#             whose fingerprint is in deploy/prod-deploy-signers.txt
#
# Exit 0 on accept, non-zero on reject (stderr explains why).
#
# Usage:
#   scripts/check_deploy_ref.sh --kind tag    --ref v1.2.3
#   scripts/check_deploy_ref.sh --kind branch --ref main
#
# Flags:
#   --kind {branch,tag}       (required)
#   --ref  <name>             (required) for branch this is the branch
#                             name without `origin/` — verifier resolves
#                             `origin/<name>` for the GPG check
#   --allowlist-only          run Layer 1 only (used by --dry-run path
#                             and by drift-guard tests; the ref does not
#                             need to exist locally)
#   --insecure-skip-verify    skip BOTH layers; LOUD warning printed.
#                             Equivalent env: OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY=1
#   --allowlist <path>        override allowlist file path
#   --signers   <path>        override signers file path

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALLOWLIST="$REPO/deploy/prod-deploy-allowlist.txt"
SIGNERS="$REPO/deploy/prod-deploy-signers.txt"
KIND=""
REF=""
ALLOWLIST_ONLY=false
SKIP_VERIFY="${OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY:-}"

err() { echo "❌ check_deploy_ref: $*" >&2; exit 1; }
warn() { echo "⚠️  check_deploy_ref: $*" >&2; }
ok() { echo "✅ check_deploy_ref: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --kind) KIND="${2:-}"; shift 2;;
        --ref) REF="${2:-}"; shift 2;;
        --allowlist-only) ALLOWLIST_ONLY=true; shift;;
        --insecure-skip-verify) SKIP_VERIFY=1; shift;;
        --allowlist) ALLOWLIST="${2:-}"; shift 2;;
        --signers) SIGNERS="${2:-}"; shift 2;;
        -h|--help) sed -n '2,30p' "$0"; exit 0;;
        *) err "unknown arg: $1";;
    esac
done

[[ -n "$KIND" ]] || err "--kind is required (branch | tag)"
[[ -n "$REF" ]] || err "--ref is required"
[[ "$KIND" == "branch" || "$KIND" == "tag" ]] \
    || err "--kind must be 'branch' or 'tag', got '$KIND'"

if [[ "${SKIP_VERIFY:-}" == "1" ]]; then
    warn "──────────────────────────────────────────────────────────────"
    warn "OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY=1 (or --insecure-skip-verify)"
    warn "Skipping BOTH ref allowlist + GPG signature verification."
    warn "This is the FX.7.9 emergency escape hatch. Every use is"
    warn "logged to shell history and SHOULD be raised in the post-"
    warn "deploy review. Do not leave this set as a default."
    warn "──────────────────────────────────────────────────────────────"
    exit 0
fi

# ── Layer 1: allowlist ──────────────────────────────────────────────
[[ -f "$ALLOWLIST" ]] || err "allowlist file missing: $ALLOWLIST"

_match_allowlist() {
    local kind="$1" ref="$2" file="$3"
    local rule_kind rule_value line
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line//$'\r'/}"
        line="${line%%#*}"
        # trim leading + trailing whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        rule_kind="${line%% *}"
        rule_value="${line#* }"
        # trim again in case of multiple spaces
        rule_value="${rule_value#"${rule_value%%[![:space:]]*}"}"
        case "$rule_kind" in
            branch)
                [[ "$kind" == "branch" && "$ref" == "$rule_value" ]] && return 0
                ;;
            branch-regex)
                if [[ "$kind" == "branch" ]] && [[ "$ref" =~ $rule_value ]]; then
                    return 0
                fi
                ;;
            tag-regex)
                if [[ "$kind" == "tag" ]] && [[ "$ref" =~ $rule_value ]]; then
                    return 0
                fi
                ;;
            *)
                err "allowlist syntax error in $file: unknown rule kind '$rule_kind' (expected branch | branch-regex | tag-regex)"
                ;;
        esac
    done < "$file"
    return 1
}

if ! _match_allowlist "$KIND" "$REF" "$ALLOWLIST"; then
    err "ref '$KIND:$REF' is NOT permitted by $ALLOWLIST. Add an explicit rule via PR (audit trail) or use --insecure-skip-verify for an emergency one-off."
fi
ok "Layer 1: ref '$KIND:$REF' matched allowlist"

if [[ "$ALLOWLIST_ONLY" == "true" ]]; then
    exit 0
fi

# ── Layer 2: GPG signature verification ─────────────────────────────
[[ -f "$SIGNERS" ]] || err "signers file missing: $SIGNERS"

# Parse 40-char hex fingerprints, normalise to upper case, drop comments.
ALLOWED_FPRS=()
while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    raw_line="${raw_line//$'\r'/}"
    raw_line="${raw_line%%#*}"
    fpr="${raw_line//[[:space:]]/}"
    [[ -z "$fpr" ]] && continue
    upper="$(printf '%s' "$fpr" | tr '[:lower:]' '[:upper:]')"
    if [[ "$upper" =~ ^[0-9A-F]{40}$ ]]; then
        ALLOWED_FPRS+=("$upper")
    else
        err "signers syntax error in $SIGNERS: '$raw_line' is not a valid 40-char hex GPG fingerprint"
    fi
done < "$SIGNERS"

if [[ "${#ALLOWED_FPRS[@]}" -eq 0 ]]; then
    err "$SIGNERS contains zero trusted fingerprints. Add a real release-signer fingerprint via PR, or use --insecure-skip-verify to bypass for an audited one-off deploy."
fi

# Resolve the ref the GPG check will verify.
if [[ "$KIND" == "tag" ]]; then
    target="$REF"
    verify_cmd=(git verify-tag --raw "$target")
else
    target="origin/$REF"
    verify_cmd=(git verify-commit --raw "$target")
fi

# `git verify-*` writes GPG status to stderr; --raw emits machine-
# readable "[GNUPG:] ..." lines. Capture both streams.
set +e
verify_out="$("${verify_cmd[@]}" 2>&1)"
verify_rc=$?
set -e

# Find the first VALIDSIG line and extract the long fingerprint
# (field 3 of "[GNUPG:] VALIDSIG <fpr> <date> ..."). grep exits 1 when
# no match — under `set -o pipefail` that would silently kill the
# whole script via `set -e`, so disable pipefail for this assignment
# and let the empty-string branch below produce the user-facing error.
set +o pipefail
signer_fpr="$(
    printf '%s\n' "$verify_out" \
        | grep -E '^\[GNUPG:\] VALIDSIG ' \
        | head -n1 \
        | awk '{print toupper($3)}'
)"
set -o pipefail

if [[ -z "$signer_fpr" ]]; then
    printf '%s\n' "$verify_out" >&2
    err "ref '$KIND:$target' is not GPG-signed (or the signature could not be verified — git verify-* exit=$verify_rc, no [GNUPG:] VALIDSIG line). Sign with a key listed in $SIGNERS, or use --insecure-skip-verify."
fi

for fpr in "${ALLOWED_FPRS[@]}"; do
    if [[ "$fpr" == "$signer_fpr" ]]; then
        ok "Layer 2: '$KIND:$target' signed by trusted fingerprint $signer_fpr"
        exit 0
    fi
done

err "ref '$KIND:$target' is signed by $signer_fpr — that fingerprint is NOT in $SIGNERS. Add it via PR (audit trail) or use --insecure-skip-verify."
