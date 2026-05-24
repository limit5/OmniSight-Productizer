#!/usr/bin/env bash
# scripts/check_deploy_ref.sh — gate which release identities may be
# deployed to production.
#
# Release-train contract (RT-20, ADR-0040 §"Decisions LOCKED"): the
# single-trunk release train is IMAGE-TAG-ONLY. A release is a promoted
# GitLab CR image tag (vX.Y.Z) that resolves to a validated image
# DIGEST, recorded in the release_train/release_audit row. **No v* git
# tag is ever created** — a v* git tag would trip the existing `^v` CI
# build rule and rebuild a DIFFERENT digest, breaking "validated digest
# == shipped digest". So the ONLY production deploy identity this gate
# accepts is:
#   • an image DIGEST   sha256:<64 lowercase hex>
# A digest is content-addressed and its trust is established by cosign
# verification at pull time (RT-06), so a well-formed digest needs no
# further git-side allowlist / GPG check and accepts here.
#
# Everything else is REJECTED outright:
#   • branch deploys (main / develop / release/* / hotfix/* / any) —
#     there is no long-lived release branch under the single-trunk train.
#   • git tags — the v*-git-tag deploy identity, and its allowlist +
#     GPG-signature gating (RT-07a), are RETIRED by RT-20 (image-tag-
#     only). A git tag is never a production deploy identity; deploy the
#     promoted image digest instead.
#
# Exit 0 on accept, non-zero on reject (stderr explains why).
#
# There is NO --insecure-skip-verify bypass. The audited way to ship is
# the promote pipeline producing a cosign-verified image digest.
#
# Usage:
#   scripts/check_deploy_ref.sh --kind digest --ref sha256:<64hex>
#
# Flags:
#   --kind {digest,tag,branch}  (required) only 'digest' can accept;
#                               'tag' and 'branch' are always rejected.
#                               They are still accepted as arguments so an
#                               accidental tag/branch deploy gets a clear
#                               release-train rejection rather than an
#                               opaque arg error.
#   --ref  <name>             (required)
#   --allowlist-only          retained no-op (back-compat with the deploy
#                             --dry-run path). The digest gate consults no
#                             allowlist, so this flag has no effect.

set -euo pipefail

KIND=""
REF=""

# OCI image digest: sha256 + exactly 64 lowercase hex chars.
DIGEST_RE='^sha256:[0-9a-f]{64}$'

err() { echo "❌ check_deploy_ref: $*" >&2; exit 1; }
ok() { echo "✅ check_deploy_ref: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --kind) KIND="${2:-}"; shift 2;;
        --ref) REF="${2:-}"; shift 2;;
        --allowlist-only) shift;;   # retained no-op (digest gate has no allowlist)
        -h|--help) sed -n '2,46p' "$0"; exit 0;;
        *) err "unknown arg: $1";;
    esac
done

[[ -n "$KIND" ]] || err "--kind is required (digest | tag | branch)"
[[ -n "$REF" ]] || err "--ref is required"
[[ "$KIND" == "branch" || "$KIND" == "tag" || "$KIND" == "digest" ]] \
    || err "--kind must be 'digest', 'tag', or 'branch', got '$KIND'"

# ── Release-train deploy-identity gate (RT-20, ADR-0040) ─────────────
# IMAGE-TAG-ONLY: the sole accepted production deploy identity is a
# well-formed image digest (cosign owns its content-trust). A git tag or
# a branch is never a deploy identity and is rejected outright.
case "$KIND" in
    branch)
        err "branch deploys are not permitted under the single-trunk release train (RT-07a/RT-20). A production deploy identity is an image digest (sha256:<64hex>). Requested 'branch:$REF' rejected."
        ;;
    tag)
        err "git-tag deploys are not permitted under the single-trunk release train (RT-20, image-tag-only): no v* git tag is ever created — a release is a promoted image tag that resolves to a validated digest. Deploy by image digest (sha256:<64hex>), not 'tag:$REF'."
        ;;
    digest)
        if [[ ! "$REF" =~ $DIGEST_RE ]]; then
            err "digest '$REF' is malformed (expected sha256:<64 lowercase hex>)."
        fi
        ok "'$REF' is a well-formed image digest; content-trust is established by cosign verification at pull time (RT-06)"
        exit 0
        ;;
esac
