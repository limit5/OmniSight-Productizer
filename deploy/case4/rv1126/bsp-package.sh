#!/usr/bin/env bash
set -euo pipefail

BOARD_ID="atk-dlrv1126"
SOC_ID="rv1126"
TARGET_TRIPLE="arm-linux-gnueabihf"
ARCHIVE_NAME="omnisight-${BOARD_ID}-bsp-armhf.tar.gz"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_HEADERS_DIR="${SCRIPT_DIR}/sdk-headers"
LICENSE_MANIFEST="${SCRIPT_DIR}/LICENSES.spdx.json"

usage() {
  cat <<'USAGE'
usage: bsp-package.sh --kernel <zImage> --dtb <atk-dlrv1126.dtb> --stitching-bin <uvc-stitching> --sign-key <private.pem> [options]

Build the customer BSP/SDK archive for ATK-DLRV1126 (RV1126 armhf).

Required:
  --kernel <path>          armhf RV1126 kernel image; packaged as boot/zImage.
  --dtb <path>             ATK-DLRV1126 device-tree blob; packaged as boot/atk-dlrv1126.dtb.
  --stitching-bin <path>   armhf uvc-stitching binary; packaged as usr/bin/uvc-stitching.
  --sign-key <path>        OpenSSL-compatible private key for SHA256SUMS.sig.

Options:
  --dispatcher-bin <path>  Optional uvc-xu-dispatcher binary; packaged as usr/bin/uvc-xu-dispatcher.
  --output-dir <path>      Output directory (default: dist/case4/rv1126).
  --package-version <ver>  Package version label (default: OP-1952).
  --dry-run                Validate inputs and print the staging plan without writing the archive.
  -h, --help               Show this help.
USAGE
}

die() {
  echo "bsp-package.sh: $*" >&2
  exit 2
}

quote_cmd() {
  printf '%q ' "$@"
  printf '\n'
}

require_file() {
  local path="$1"
  local label="$2"

  [[ -f "${path}" ]] || die "${label} not found: ${path}"
}

require_executable() {
  local tool="$1"

  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
}

copy_file() {
  local src="$1"
  local dst="$2"
  local mode="$3"

  install -D -m "${mode}" "${src}" "${dst}"
}

write_release_metadata() {
  local path="$1"

  cat >"${path}" <<EOF
board_id=${BOARD_ID}
soc_id=${SOC_ID}
target_triple=${TARGET_TRIPLE}
package_version=${PACKAGE_VERSION}
kernel=boot/zImage
dtb=boot/atk-dlrv1126.dtb
stitching_binary=usr/bin/uvc-stitching
license_manifest=LICENSES.spdx.json
EOF
}

write_customer_runbook() {
  local path="$1"

  cat >"${path}" <<'EOF'
# ATK-DLRV1126 Customer Bring-Up Runbook

## Package contents

- `boot/zImage` — RV1126 armhf kernel image.
- `boot/atk-dlrv1126.dtb` — ATK-DLRV1126 device-tree blob.
- `usr/bin/uvc-stitching` — customer stitching pipeline binary.
- `usr/bin/uvc-xu-dispatcher` — optional UVC XU dispatcher binary when supplied.
- `sdk-headers/*.h` — customer SDK headers.
- `LICENSES.spdx.json` — package license manifest.
- `RELEASE.txt` — release metadata for this package.

## Host verification

1. Verify `SHA256SUMS.sig` with the OmniSight release public key.
2. Run `sha256sum -c SHA256SUMS` from the release directory.
3. Extract the archive and run `sha256sum -c PACKAGE-CONTENTS.sha256` from the extracted package root.
4. Confirm `RELEASE.txt` reports `board_id=atk-dlrv1126` and `target_triple=arm-linux-gnueabihf`.

## EVK install

1. Flash the customer image using the RV1126 flash procedure for ATK-DLRV1126.
2. Copy `usr/bin/uvc-stitching` to the target rootfs if it was not baked into the image.
3. Install SDK headers into the customer build sysroot include directory.
4. Boot the EVK and confirm `/run/uvc-xu-dispatcher.sock` exists when the dispatcher is supplied.
5. Start the stitching application with the camera topology agreed for the customer delivery.

## Acceptance

The package is accepted when checksum verification passes, the EVK boots with
the packaged kernel and DTB, and the stitching binary starts on the ATK-DLRV1126
target without a loader or architecture mismatch.
EOF
}

KERNEL=""
DTB=""
STITCHING_BIN=""
DISPATCHER_BIN=""
OUTPUT_DIR="dist/case4/rv1126"
PACKAGE_VERSION="OP-1952"
SIGN_KEY=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kernel)
      [[ $# -ge 2 ]] || die "--kernel requires a path"
      KERNEL="$2"
      shift 2
      ;;
    --dtb)
      [[ $# -ge 2 ]] || die "--dtb requires a path"
      DTB="$2"
      shift 2
      ;;
    --stitching-bin)
      [[ $# -ge 2 ]] || die "--stitching-bin requires a path"
      STITCHING_BIN="$2"
      shift 2
      ;;
    --dispatcher-bin)
      [[ $# -ge 2 ]] || die "--dispatcher-bin requires a path"
      DISPATCHER_BIN="$2"
      shift 2
      ;;
    --output-dir)
      [[ $# -ge 2 ]] || die "--output-dir requires a path"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --package-version)
      [[ $# -ge 2 ]] || die "--package-version requires a value"
      PACKAGE_VERSION="$2"
      shift 2
      ;;
    --sign-key)
      [[ $# -ge 2 ]] || die "--sign-key requires a path"
      SIGN_KEY="$2"
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

[[ -n "${KERNEL}" ]] || die "--kernel is required"
[[ -n "${DTB}" ]] || die "--dtb is required"
[[ -n "${STITCHING_BIN}" ]] || die "--stitching-bin is required"
[[ -n "${SIGN_KEY}" ]] || die "--sign-key is required"

require_file "${KERNEL}" "kernel image"
require_file "${DTB}" "device-tree blob"
require_file "${STITCHING_BIN}" "stitching binary"
require_file "${SIGN_KEY}" "signing key"
require_file "${LICENSE_MANIFEST}" "license manifest"
[[ -d "${SDK_HEADERS_DIR}" ]] || die "SDK headers directory not found: ${SDK_HEADERS_DIR}"
if [[ -n "${DISPATCHER_BIN}" ]]; then
  require_file "${DISPATCHER_BIN}" "dispatcher binary"
fi

require_executable tar
require_executable sha256sum
require_executable openssl

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "# dry-run for ${BOARD_ID}/${SOC_ID}/${TARGET_TRIPLE}"
  quote_cmd install -D -m 0644 "${KERNEL}" "${OUTPUT_DIR}/stage/boot/zImage"
  quote_cmd install -D -m 0644 "${DTB}" "${OUTPUT_DIR}/stage/boot/atk-dlrv1126.dtb"
  quote_cmd install -D -m 0755 "${STITCHING_BIN}" "${OUTPUT_DIR}/stage/usr/bin/uvc-stitching"
  [[ -z "${DISPATCHER_BIN}" ]] || quote_cmd install -D -m 0755 "${DISPATCHER_BIN}" "${OUTPUT_DIR}/stage/usr/bin/uvc-xu-dispatcher"
  quote_cmd tar -C "${OUTPUT_DIR}/stage" -czf "${OUTPUT_DIR}/${ARCHIVE_NAME}" .
  quote_cmd openssl dgst -sha256 -sign "${SIGN_KEY}" -out "${OUTPUT_DIR}/SHA256SUMS.sig" "${OUTPUT_DIR}/SHA256SUMS"
  exit 0
fi

mkdir -p "${OUTPUT_DIR}"
OUTPUT_DIR="$(cd "${OUTPUT_DIR}" && pwd)"
STAGE_DIR="${OUTPUT_DIR}/stage"
CHECKSUM_FILE="${OUTPUT_DIR}/SHA256SUMS"
CONTENT_CHECKSUM_FILE="${STAGE_DIR}/PACKAGE-CONTENTS.sha256"
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}/boot" "${STAGE_DIR}/usr/bin" "${STAGE_DIR}/sdk-headers"

copy_file "${KERNEL}" "${STAGE_DIR}/boot/zImage" 0644
copy_file "${DTB}" "${STAGE_DIR}/boot/atk-dlrv1126.dtb" 0644
copy_file "${STITCHING_BIN}" "${STAGE_DIR}/usr/bin/uvc-stitching" 0755
if [[ -n "${DISPATCHER_BIN}" ]]; then
  copy_file "${DISPATCHER_BIN}" "${STAGE_DIR}/usr/bin/uvc-xu-dispatcher" 0755
fi
cp "${SDK_HEADERS_DIR}"/*.h "${STAGE_DIR}/sdk-headers/"
cp "${LICENSE_MANIFEST}" "${STAGE_DIR}/LICENSES.spdx.json"
write_release_metadata "${STAGE_DIR}/RELEASE.txt"
write_customer_runbook "${STAGE_DIR}/CUSTOMER-RUNBOOK.md"

(
  cd "${STAGE_DIR}"
  find . -type f ! -name PACKAGE-CONTENTS.sha256 -print | sort | sed 's#^\./##' | xargs sha256sum >"${CONTENT_CHECKSUM_FILE}"
)
tar -C "${STAGE_DIR}" -czf "${OUTPUT_DIR}/${ARCHIVE_NAME}" .
(
  cd "${OUTPUT_DIR}"
  sha256sum "${ARCHIVE_NAME}" >SHA256SUMS
)
openssl dgst -sha256 -sign "${SIGN_KEY}" -out "${OUTPUT_DIR}/SHA256SUMS.sig" "${OUTPUT_DIR}/SHA256SUMS"

echo "archive=${OUTPUT_DIR}/${ARCHIVE_NAME}"
echo "checksums=${OUTPUT_DIR}/SHA256SUMS"
echo "signature=${OUTPUT_DIR}/SHA256SUMS.sig"
