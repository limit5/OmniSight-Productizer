#!/usr/bin/env bash
# scripts/check_deploy_ref.sh — gate which release identities may be
# deployed to production.
#
# Release-train contract (RT-07a, ADR-0040): a production deploy
# identity is ONLY one of:
#   • a FINAL semver git tag  vX.Y.Z   (no -rc / -hotfix / pre-release
#     suffix — every final tag is a promoted, prod-usable build)
#   • an image DIGEST         sha256:<64 lowercase hex>
# Branch deploys (main / develop / release/* / hotfix/* / any) are
# REJECTED outright — there is no long-lived release branch under the
# single-trunk release train.
#
# Layers (evaluated in order):
#   Layer 0 — Final-ref shape gate (RT-07a): reject branch kinds, reject
#             non-final tags, reject malformed digests. A digest is
#             content-addressed and its trust is established by cosign
#             verification at pull time (RT-06), so a well-formed digest
#             needs no git allowlist / GPG check and accepts here.
#   Layer 1 — Allowlist match (tags only): the tag must satisfy a rule
#             in deploy/prod-deploy-allowlist.txt — the audit trail for
#             "who said this ref may ship". (Allowlist file content is
#             tightened separately in RT-07b.)
#   Layer 2 — GPG signature (tags only): the annotated tag's signature
#             must be made by a fingerprint in
#             deploy/prod-deploy-signers.txt.
#
# Exit 0 on accept, non-zero on reject (stderr explains why).
#
# There is NO --insecure-skip-verify bypass (removed in RT-07a — it
# bypassed both layers with no durable audit). The audited way to
# authorise a new ref is a reviewed PR to the allowlist plus a signed
# final tag, or deploying a cosign-verified image digest.
#
# Usage:
#   scripts/check_deploy_ref.sh --kind tag    --ref v1.2.3
#   scripts/check_deploy_ref.sh --kind digest --ref sha256:<64hex>
#
# Flags:
#   --kind {tag,digest,branch}  (required) 'branch' is always rejected;
#                               it is still accepted as an argument so
#                               an accidental branch deploy gets a clear
#                               release-train rejection rather than an
#                               opaque arg error.
#   --ref  <name>             (required)
#   --allowlist-only          run Layer 0 (+ Layer 1 for tags) only;
#                             used by the deploy --dry-run path and by
#                             drift-guard tests. The ref does not need
#                             to exist locally.
#   --allowlist <path>        override allowlist file path
#   --signers   <path>        override signers file path

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALLOWLIST="$REPO/deploy/prod-deploy-allowlist.txt"
SIGNERS="$REPO/deploy/prod-deploy-signers.txt"
KIND=""
REF=""
ALLOWLIST_ONLY=false

# Final release tag: vMAJOR.MINOR.PATCH only — no pre-release / build
# suffix. A non-final tag (vX.Y.Z-rc.N, vX.Y.Z-hotfix.N, …) is NOT a
# production deploy identity under the release train.
FINAL_TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'
# OCI image digest: sha256 + exactly 64 lowercase hex chars.
DIGEST_RE='^sha256:[0-9a-f]{64}$'

err() { echo "❌ check_deploy_ref: $*" >&2; exit 1; }
warn() { echo "⚠️  check_deploy_ref: $*" >&2; }
ok() { echo "✅ check_deploy_ref: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --kind) KIND="${2:-}"; shift 2;;
        --ref) REF="${2:-}"; shift 2;;
        --allowlist-only) ALLOWLIST_ONLY=true; shift;;
        --allowlist) ALLOWLIST="${2:-}"; shift 2;;
        --signers) SIGNERS="${2:-}"; shift 2;;
        -h|--help) sed -n '2,49p' "$0"; exit 0;;
        *) err "unknown arg: $1";;
    esac
done

[[ -n "$KIND" ]] || err "--kind is required (tag | digest | branch)"
[[ -n "$REF" ]] || err "--ref is required"
[[ "$KIND" == "branch" || "$KIND" == "tag" || "$KIND" == "digest" ]] \
    || err "--kind must be 'tag', 'digest', or 'branch', got '$KIND'"

# ── Layer 0: release-train final-ref shape gate (RT-07a) ─────────────
# Decide acceptance purely on the shape of the requested identity. This
# is the primary contract: branch → reject; non-final tag → reject;
# malformed digest → reject; well-formed digest → accept (cosign owns
# content-trust); final tag → continue to allowlist + GPG.
case "$KIND" in
    branch)
        err "branch deploys are not permitted under the single-trunk release train (RT-07a). A production deploy identity is a FINAL tag (vX.Y.Z) or an image digest (sha256:<64hex>). Requested 'branch:$REF' rejected."
        ;;
    digest)
        if [[ ! "$REF" =~ $DIGEST_RE ]]; then
            err "digest '$REF' is malformed (expected sha256:<64 lowercase hex>)."
        fi
        ok "Layer 0: '$REF' is a well-formed image digest; content-trust is established by cosign verification at pull time (RT-06)"
        exit 0
        ;;
    tag)
        if [[ ! "$REF" =~ $FINAL_TAG_RE ]]; then
            err "tag '$REF' is not a FINAL release tag (expected vX.Y.Z, with no -rc/-hotfix/pre-release suffix). Only promoted final tags may reach prod (RT-07a)."
        fi
        ok "Layer 0: '$REF' is a final release tag"
        ;;
esac

# Below here KIND is always 'tag' (branch errored, digest exited).

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
    err "ref 'tag:$REF' is NOT permitted by $ALLOWLIST. Add an explicit tag-regex rule via PR (audit trail)."
fi
ok "Layer 1: ref 'tag:$REF' matched allowlist"

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
    err "$SIGNERS contains zero trusted fingerprints. Add a real release-signer fingerprint via PR, or deploy a cosign-verified image digest instead."
fi

# Final tags are annotated + signed; verify the tag object's signature.
target="$REF"
verify_cmd=(git verify-tag --raw "$target")

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
    err "tag '$target' is not GPG-signed (or the signature could not be verified — git verify-tag exit=$verify_rc, no [GNUPG:] VALIDSIG line). Sign the final tag with a key listed in $SIGNERS."
fi

for fpr in "${ALLOWED_FPRS[@]}"; do
    if [[ "$fpr" == "$signer_fpr" ]]; then
        ok "Layer 2: tag '$target' signed by trusted fingerprint $signer_fpr"
        exit 0
    fi
done

err "tag '$target' is signed by $signer_fpr — that fingerprint is NOT in $SIGNERS. Add it via PR (audit trail)."
