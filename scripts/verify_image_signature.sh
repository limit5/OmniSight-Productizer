#!/usr/bin/env bash
# OP-763 D2 — verify the cosign signature on an OmniSight image.
#
# Usage:
#   scripts/verify_image_signature.sh <image-ref> [--key <path>]
#
# Examples:
#   scripts/verify_image_signature.sh ghcr.io/sora/omnisight-backend:sha-abcd1234ef00
#   scripts/verify_image_signature.sh ghcr.io/sora/omnisight-backend:v0.4.0
#
# Exit codes:
#   0  — signature verified ("OK" printed to stdout)
#   1  — signature missing or invalid ("FAIL" printed to stderr)
#   2  — bad invocation / missing dependency
#
# Verification mode is auto-selected:
#   - If `deploy/cosign/cosign.pub` is a real PEM key (i.e. begins
#     with `-----BEGIN PUBLIC KEY-----`), key-based verification is
#     used.
#   - Otherwise (the file is the placeholder shipped at first
#     install, or `--key` not given), KEYLESS verification is used,
#     pinned to the GitHub-Actions OIDC issuer and a workflow-ref
#     identity regex matching `.github/workflows/build-images.yml@*`
#     in this repository's owner namespace.
#
# Override identity claims via env when verifying images built by a
# fork or a different workflow file:
#   COSIGN_CERT_IDENTITY_REGEX
#   COSIGN_CERT_OIDC_ISSUER

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: verify_image_signature.sh <image-ref> [--key <path-to-cosign.pub>]

  image-ref   Required. Full registry path including tag or digest.
              e.g. ghcr.io/sora/omnisight-backend:sha-abcd1234ef00
  --key       Optional. Use key-based verification with the given
              public key. Auto-detected if deploy/cosign/cosign.pub
              is a real PEM block.

Exit 0 + "OK" on verified signature, exit 1 + "FAIL" otherwise.
EOF
}

if [ $# -lt 1 ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 2
fi

IMAGE_REF="$1"; shift
KEY_PATH=""

while [ $# -gt 0 ]; do
  case "$1" in
    --key)
      KEY_PATH="${2:-}"
      shift 2
      ;;
    --key=*)
      KEY_PATH="${1#--key=}"
      shift
      ;;
    *)
      echo "FAIL: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if ! command -v cosign >/dev/null 2>&1; then
  echo "FAIL: cosign not installed (https://docs.sigstore.dev/cosign/installation/)" >&2
  exit 2
fi

# Discover the project's stock public-key location, but only honor
# it if it's a *real* PEM block — the placeholder file shipped with
# the repo is intentionally NOT a key, and we must not try to use
# it as one.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_KEY="${SCRIPT_DIR}/../deploy/cosign/cosign.pub"

if [ -z "$KEY_PATH" ] && [ -f "$DEFAULT_KEY" ] \
   && head -n 1 "$DEFAULT_KEY" | grep -q '^-----BEGIN PUBLIC KEY-----$'; then
  KEY_PATH="$DEFAULT_KEY"
fi

if [ -n "$KEY_PATH" ]; then
  if [ ! -f "$KEY_PATH" ]; then
    echo "FAIL: --key path not found: $KEY_PATH" >&2
    exit 2
  fi
  if ! head -n 1 "$KEY_PATH" | grep -q '^-----BEGIN PUBLIC KEY-----$'; then
    echo "FAIL: --key path is not a PEM public key: $KEY_PATH" >&2
    exit 2
  fi
  echo "verifying ${IMAGE_REF} via key ${KEY_PATH}" >&2
  if cosign verify --key "$KEY_PATH" "$IMAGE_REF" >/dev/null 2>&1; then
    echo "OK"
    exit 0
  fi
  echo "FAIL: cosign key-based verification failed for ${IMAGE_REF}" >&2
  exit 1
fi

# Keyless path. Identity claims default to the OmniSight published
# build-images workflow on github.com — override via env for forks.
default_identity_re='^https://github\.com/.+/.+/\.github/workflows/build-images\.yml@.*$'
identity_re="${COSIGN_CERT_IDENTITY_REGEX:-$default_identity_re}"
oidc_issuer="${COSIGN_CERT_OIDC_ISSUER:-https://token.actions.githubusercontent.com}"

echo "verifying ${IMAGE_REF} via keyless (identity_regex=${identity_re}, issuer=${oidc_issuer})" >&2
if cosign verify \
      --certificate-identity-regexp "$identity_re" \
      --certificate-oidc-issuer "$oidc_issuer" \
      "$IMAGE_REF" >/dev/null 2>&1; then
  echo "OK"
  exit 0
fi

echo "FAIL: cosign keyless verification failed for ${IMAGE_REF}" >&2
exit 1
