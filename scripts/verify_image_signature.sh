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
# Verification is key-based only. By default this script uses
# `deploy/cosign/cosign.pub`; pass `--key <path>` to verify with a
# different PEM public key during rotation drills.

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: verify_image_signature.sh <image-ref> [--key <path-to-cosign.pub>]

  image-ref   Required. Full registry path including tag or digest.
              e.g. ghcr.io/sora/omnisight-backend:sha-abcd1234ef00
  --key       Optional. Use the given PEM public key instead of the
              default deploy/cosign/cosign.pub.

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

# OP-1736 — self-locate cosign before the `command -v` check. A clean /
# minimal PATH (e.g. a promote run that was historically wrapped in
# `env -i PATH=/usr/bin:/bin:/usr/local/bin`) drops a cosign installed in a
# user bindir such as ~/bin, which silently aborted the promote mid-retag.
# Honour an explicit $COSIGN_BIN, else probe the common install dirs and
# prepend the first hit to PATH. We still fail-closed if cosign is truly
# absent — verification is NEVER skipped when the tool is missing.
COSIGN_SEARCH_DIRS=("${HOME:-}/bin" "/usr/local/bin" "${HOME:-}/go/bin")

if [ -n "${COSIGN_BIN:-}" ]; then
  if [ ! -x "$COSIGN_BIN" ]; then
    echo "FAIL: COSIGN_BIN is set to '${COSIGN_BIN}' but it is not an executable file" >&2
    exit 2
  fi
  PATH="$(dirname "$COSIGN_BIN"):$PATH"
elif ! command -v cosign >/dev/null 2>&1; then
  for _cosign_dir in "${COSIGN_SEARCH_DIRS[@]}"; do
    if [ -x "${_cosign_dir}/cosign" ]; then
      PATH="${_cosign_dir}:$PATH"
      break
    fi
  done
fi
export PATH

if ! command -v cosign >/dev/null 2>&1; then
  echo "FAIL: cosign not found in PATH or ${COSIGN_SEARCH_DIRS[*]}; set COSIGN_BIN to its path (https://docs.sigstore.dev/cosign/installation/)" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_KEY="${SCRIPT_DIR}/../deploy/cosign/cosign.pub"

if [ -z "$KEY_PATH" ]; then
  KEY_PATH="$DEFAULT_KEY"
fi

if [ ! -f "$KEY_PATH" ]; then
  echo "FAIL: cosign public key not found: $KEY_PATH" >&2
  exit 2
fi

if ! head -n 1 "$KEY_PATH" | grep -q '^-----BEGIN PUBLIC KEY-----$'; then
  echo "FAIL: cosign public key is not a PEM public key: $KEY_PATH" >&2
  exit 2
fi

echo "verifying ${IMAGE_REF} via key ${KEY_PATH}" >&2
if cosign verify --key "$KEY_PATH" --insecure-ignore-tlog=true "$IMAGE_REF" >/dev/null 2>&1; then
  echo "OK"
  exit 0
fi

echo "FAIL: cosign key-based verification failed for ${IMAGE_REF}" >&2
exit 1
