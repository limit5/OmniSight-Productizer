#!/usr/bin/env bash
# OP-1969 C4-C.C - Sign and checksum the Radxa Dragon Q6A customer BSP bundle.
set -euo pipefail

EVK=""
VERSION=""
ARCHIVE=""
IMAGE=""
OUT_DIR=""
COSIGN_KEY=""
PUBLIC_KEY=""
GIT_SHA="${GIT_COMMIT:-unknown}"
DRY_RUN=0

usage() {
  cat <<'USAGE'
usage: sign_bsp_qcs6490.sh --evk radxa-dragon-q6a --version <version> --archive <bsp.tar.gz> --image <qcs6490.img> --out-dir <dir> --cosign-key <key> --public-key <cosign.pub> [options]

Creates the Case 4 Radxa Dragon Q6A delivery manifest, SHA256SUMS,
cosign blob signatures, and verification log for the customer BSP bundle.

Options:
  --evk <id>          Required safety acknowledgement; must be radxa-dragon-q6a.
  --version <value>   BSP delivery version written into the manifest.
  --archive <path>    BSP archive to sign.
  --image <path>      Fastboot or EDL flash image to sign.
  --out-dir <dir>     Output directory for manifest, checksums, signatures.
  --cosign-key <path> Private cosign key used by `cosign sign-blob`.
  --public-key <path> Public key used by `cosign verify-blob`.
  --git-sha <sha>     Source revision written into the manifest.
  --dry-run           Validate arguments and print planned commands only.
  -h, --help          Show this help.
USAGE
}

die() {
  printf 'sign_bsp_qcs6490.sh: %s\n' "$*" >&2
  exit 2
}

log() {
  printf '[sign-bsp-qcs6490] %s\n' "$*"
}

quote_cmd() {
  printf '%q ' "$@"
  printf '\n'
}

require_file() {
  local label="$1"
  local path="$2"
  [[ -n "${path}" ]] || die "${label} is required"
  [[ -f "${path}" ]] || die "${label} not found: ${path}"
}

json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  printf '%s' "${value}"
}

run_cmd() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    quote_cmd "$@"
  else
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --evk)
      [[ $# -ge 2 ]] || die "--evk requires a value"
      EVK="$2"
      shift 2
      ;;
    --version)
      [[ $# -ge 2 ]] || die "--version requires a value"
      VERSION="$2"
      shift 2
      ;;
    --archive)
      [[ $# -ge 2 ]] || die "--archive requires a path"
      ARCHIVE="$2"
      shift 2
      ;;
    --image)
      [[ $# -ge 2 ]] || die "--image requires a path"
      IMAGE="$2"
      shift 2
      ;;
    --out-dir)
      [[ $# -ge 2 ]] || die "--out-dir requires a path"
      OUT_DIR="$2"
      shift 2
      ;;
    --cosign-key)
      [[ $# -ge 2 ]] || die "--cosign-key requires a path"
      COSIGN_KEY="$2"
      shift 2
      ;;
    --public-key)
      [[ $# -ge 2 ]] || die "--public-key requires a path"
      PUBLIC_KEY="$2"
      shift 2
      ;;
    --git-sha)
      [[ $# -ge 2 ]] || die "--git-sha requires a value"
      GIT_SHA="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ "${EVK}" == "radxa-dragon-q6a" ]] || die "--evk must be radxa-dragon-q6a"
[[ -n "${VERSION}" ]] || die "--version is required"
[[ -n "${OUT_DIR}" ]] || die "--out-dir is required"
require_file "--archive" "${ARCHIVE}"
require_file "--image" "${IMAGE}"
require_file "--cosign-key" "${COSIGN_KEY}"
require_file "--public-key" "${PUBLIC_KEY}"

ARCHIVE_BASENAME="$(basename "${ARCHIVE}")"
IMAGE_BASENAME="$(basename "${IMAGE}")"
PUBLIC_KEY_BASENAME="$(basename "${PUBLIC_KEY}")"
STAGED_ARCHIVE="${OUT_DIR}/${ARCHIVE_BASENAME}"
STAGED_IMAGE="${OUT_DIR}/${IMAGE_BASENAME}"
STAGED_PUBLIC_KEY="${OUT_DIR}/${PUBLIC_KEY_BASENAME}"
MANIFEST="${OUT_DIR}/radxa-dragon-q6a-delivery-manifest.json"
SHA_FILE="${OUT_DIR}/SHA256SUMS"
ARCHIVE_SIG="${OUT_DIR}/${ARCHIVE_BASENAME}.sig"
IMAGE_SIG="${OUT_DIR}/${IMAGE_BASENAME}.sig"
VERIFY_LOG="${OUT_DIR}/cosign-verify.log"
CREATED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [[ "${DRY_RUN}" != "1" ]]; then
  command -v sha256sum >/dev/null 2>&1 || die "sha256sum not found"
  command -v cosign >/dev/null 2>&1 || die "cosign not found"
  [[ -n "${COSIGN_PASSWORD:-}" ]] || die "COSIGN_PASSWORD must be set for cosign key use"
  mkdir -p "${OUT_DIR}"
else
  log "dry-run: no files will be written"
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  quote_cmd mkdir -p "${OUT_DIR}"
  quote_cmd cp -f "${ARCHIVE}" "${STAGED_ARCHIVE}"
  quote_cmd cp -f "${IMAGE}" "${STAGED_IMAGE}"
  quote_cmd cp -f "${PUBLIC_KEY}" "${STAGED_PUBLIC_KEY}"
  quote_cmd sha256sum "${STAGED_ARCHIVE}" "${STAGED_IMAGE}"
  quote_cmd cosign sign-blob --yes --key "${COSIGN_KEY}" --output-signature "${ARCHIVE_SIG}" "${STAGED_ARCHIVE}"
  quote_cmd cosign sign-blob --yes --key "${COSIGN_KEY}" --output-signature "${IMAGE_SIG}" "${STAGED_IMAGE}"
  quote_cmd cosign verify-blob --key "${STAGED_PUBLIC_KEY}" --signature "${ARCHIVE_SIG}" "${STAGED_ARCHIVE}"
  quote_cmd cosign verify-blob --key "${STAGED_PUBLIC_KEY}" --signature "${IMAGE_SIG}" "${STAGED_IMAGE}"
  exit 0
fi

if [[ "${ARCHIVE}" != "${STAGED_ARCHIVE}" ]]; then
  cp -f "${ARCHIVE}" "${STAGED_ARCHIVE}"
fi
if [[ "${IMAGE}" != "${STAGED_IMAGE}" ]]; then
  cp -f "${IMAGE}" "${STAGED_IMAGE}"
fi
if [[ "${PUBLIC_KEY}" != "${STAGED_PUBLIC_KEY}" ]]; then
  cp -f "${PUBLIC_KEY}" "${STAGED_PUBLIC_KEY}"
fi

ARCHIVE_SHA="$(sha256sum "${STAGED_ARCHIVE}" | awk '{print $1}')"
IMAGE_SHA="$(sha256sum "${STAGED_IMAGE}" | awk '{print $1}')"

cat >"${MANIFEST}" <<EOF
{
  "ticket": "OP-1969",
  "parent": "OP-1926",
  "case": "case4",
  "evk": "radxa-dragon-q6a",
  "soc": "qcs6490",
  "arch": "aarch64",
  "version": "$(json_escape "${VERSION}")",
  "git_sha": "$(json_escape "${GIT_SHA}")",
  "created_at": "$(json_escape "${CREATED_AT}")",
  "artifacts": [
    {
      "role": "bsp_archive",
      "file": "$(json_escape "${ARCHIVE_BASENAME}")",
      "sha256": "${ARCHIVE_SHA}",
      "signature": "$(json_escape "${ARCHIVE_BASENAME}.sig")"
    },
    {
      "role": "flash_image",
      "file": "$(json_escape "${IMAGE_BASENAME}")",
      "sha256": "${IMAGE_SHA}",
      "signature": "$(json_escape "${IMAGE_BASENAME}.sig")"
    }
  ]
}
EOF

{
  printf '%s  %s\n' "${ARCHIVE_SHA}" "${ARCHIVE_BASENAME}"
  printf '%s  %s\n' "${IMAGE_SHA}" "${IMAGE_BASENAME}"
} >"${SHA_FILE}"

log "signing ${ARCHIVE_BASENAME}"
run_cmd cosign sign-blob --yes --key "${COSIGN_KEY}" --output-signature "${ARCHIVE_SIG}" "${STAGED_ARCHIVE}"

log "signing ${IMAGE_BASENAME}"
run_cmd cosign sign-blob --yes --key "${COSIGN_KEY}" --output-signature "${IMAGE_SIG}" "${STAGED_IMAGE}"

{
  printf 'archive: %s\n' "${ARCHIVE_BASENAME}"
  cosign verify-blob --key "${STAGED_PUBLIC_KEY}" --signature "${ARCHIVE_SIG}" "${STAGED_ARCHIVE}"
  printf '\nimage: %s\n' "${IMAGE_BASENAME}"
  cosign verify-blob --key "${STAGED_PUBLIC_KEY}" --signature "${IMAGE_SIG}" "${STAGED_IMAGE}"
} >"${VERIFY_LOG}" 2>&1

log "wrote ${STAGED_ARCHIVE}"
log "wrote ${STAGED_IMAGE}"
log "wrote ${STAGED_PUBLIC_KEY}"
log "wrote ${MANIFEST}"
log "wrote ${SHA_FILE}"
log "wrote ${ARCHIVE_SIG}"
log "wrote ${IMAGE_SIG}"
log "wrote ${VERIFY_LOG}"
