#!/usr/bin/env bash
# OP-1953 C4-B.C - sign and checksum the ATK-DLRV1126 armhf BSP archive.

set -euo pipefail

BOARD_ID="atk-dlrv1126"
ARCH_ID="armhf"

usage() {
  cat <<'USAGE'
usage: sign_bsp_armhf.sh --board atk-dlrv1126 --archive <case4-rv1126.tar.gz> [options]

Generate the customer delivery checksum, cosign blob signature, and manifest
for the Case 4 ATK-DLRV1126 RV1126 armhf BSP archive.

Options:
  --board <id>       Required safety acknowledgement; must be atk-dlrv1126.
  --archive <path>   BSP archive to checksum and sign.
  --key <path>       Cosign private key. Defaults to BSP_COSIGN_KEY, then COSIGN_KEY.
  --cosign <path>    cosign executable. Defaults to COSIGN_BIN, then cosign.
  --out-dir <path>   Output directory. Defaults to the archive directory.
  --dry-run          Print commands without writing checksum/signature files.
  -h, --help         Show this help.
USAGE
}

die() {
  echo "sign_bsp_armhf.sh: $*" >&2
  exit 2
}

quote_cmd() {
  printf '%q ' "$@"
  printf '\n'
}

BOARD=""
ARCHIVE=""
KEY_PATH="${BSP_COSIGN_KEY:-${COSIGN_KEY:-}}"
COSIGN_BIN="${COSIGN_BIN:-cosign}"
OUT_DIR=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --board)
      [[ $# -ge 2 ]] || die "--board requires a value"
      BOARD="$2"
      shift 2
      ;;
    --archive)
      [[ $# -ge 2 ]] || die "--archive requires a path"
      ARCHIVE="$2"
      shift 2
      ;;
    --key)
      [[ $# -ge 2 ]] || die "--key requires a path"
      KEY_PATH="$2"
      shift 2
      ;;
    --cosign)
      [[ $# -ge 2 ]] || die "--cosign requires a path"
      COSIGN_BIN="$2"
      shift 2
      ;;
    --out-dir)
      [[ $# -ge 2 ]] || die "--out-dir requires a path"
      OUT_DIR="$2"
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

[[ "${BOARD}" == "${BOARD_ID}" ]] || die "--board must be ${BOARD_ID}; refusing to sign any other EVK archive"
[[ -n "${ARCHIVE}" ]] || die "--archive is required"
[[ "${ARCHIVE}" == *.tar.gz || "${ARCHIVE}" == *.tgz ]] || die "--archive must be a .tar.gz or .tgz file"

if [[ "${DRY_RUN}" != "1" ]]; then
  [[ -f "${ARCHIVE}" ]] || die "archive not found: ${ARCHIVE}"
  [[ -n "${KEY_PATH}" ]] || die "set BSP_COSIGN_KEY or COSIGN_KEY, or pass --key"
  [[ -f "${KEY_PATH}" ]] || die "cosign key not found: ${KEY_PATH}"
  command -v "${COSIGN_BIN}" >/dev/null 2>&1 || die "cosign not found: ${COSIGN_BIN}"
fi

if [[ -z "${OUT_DIR}" ]]; then
  OUT_DIR="$(dirname "${ARCHIVE}")"
fi

ARCHIVE_NAME="$(basename "${ARCHIVE}")"
SHA256_PATH="${OUT_DIR}/${ARCHIVE_NAME}.sha256"
SIG_PATH="${OUT_DIR}/${ARCHIVE_NAME}.sig"
MANIFEST_PATH="${OUT_DIR}/${ARCHIVE_NAME}.delivery-manifest"

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "# dry-run for ${BOARD_ID}/${ARCH_ID}: no checksum or signature files will be written"
  quote_cmd sha256sum "${ARCHIVE}"
  quote_cmd "${COSIGN_BIN}" sign-blob --key "${KEY_PATH:-<BSP_COSIGN_KEY>}" --tlog-upload=false --output-signature "${SIG_PATH}" --yes "${ARCHIVE}"
  printf 'manifest: %s\n' "${MANIFEST_PATH}"
  exit 0
fi

mkdir -p "${OUT_DIR}"
ARCHIVE_SHA256="$(sha256sum "${ARCHIVE}" | awk '{print $1}')"
printf '%s  %s\n' "${ARCHIVE_SHA256}" "${ARCHIVE_NAME}" > "${SHA256_PATH}"
"${COSIGN_BIN}" sign-blob \
  --key "${KEY_PATH}" \
  --tlog-upload=false \
  --output-signature "${SIG_PATH}" \
  --yes \
  "${ARCHIVE}"

{
  printf 'board=%s\n' "${BOARD_ID}"
  printf 'arch=%s\n' "${ARCH_ID}"
  printf 'archive=%s\n' "${ARCHIVE_NAME}"
  printf 'sha256=%s\n' "${ARCHIVE_SHA256}"
  printf 'sha256_file=%s\n' "$(basename "${SHA256_PATH}")"
  printf 'signature_file=%s\n' "$(basename "${SIG_PATH}")"
} > "${MANIFEST_PATH}"

printf 'wrote %s\n' "${SHA256_PATH}"
printf 'wrote %s\n' "${SIG_PATH}"
printf 'wrote %s\n' "${MANIFEST_PATH}"
