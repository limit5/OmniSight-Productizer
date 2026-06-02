#!/usr/bin/env bash
set -euo pipefail

BOARD_ID="atk-dlrv1126"
DEFAULT_OFFSET="0x40"

usage() {
  cat <<'USAGE'
usage: flash_rv1126.sh --board atk-dlrv1126 --loader <rv1126-loader.bin> --image <update.img> [options]

Wrap rkdeveloptool for the ATK-DLRV1126 Rockchip RV1126 Maskrom flash path.

Operator sequence:
  1. Disconnect board power and USB OTG.
  2. Hold the ATK-DLRV1126 MASKROM/RECOVERY button.
  3. Connect USB OTG to the flashing host, then apply power.
  4. Release the button only after `rkdeveloptool ld` reports Maskrom.
  5. Run this script with --yes once the loader/image paths are verified.

Options:
  --board <id>       Required safety acknowledgement; must be atk-dlrv1126.
  --loader <path>    RV1126 USB loader passed to `rkdeveloptool db`.
  --image <path>     Firmware image passed to `rkdeveloptool wl`.
  --offset <sector>  Write sector for update.img (default: 0x40).
  --tool <path>      rkdeveloptool executable (default: rkdeveloptool).
  --dry-run          Print commands without touching the device.
  --yes              Required for non-dry-run flashing.
  -h, --help         Show this help.
USAGE
}

die() {
  echo "flash_rv1126.sh: $*" >&2
  exit 2
}

quote_cmd() {
  printf '%q ' "$@"
  printf '\n'
}

run_cmd() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    quote_cmd "$@"
  else
    "$@"
  fi
}

BOARD=""
LOADER=""
IMAGE=""
OFFSET="${DEFAULT_OFFSET}"
TOOL="rkdeveloptool"
DRY_RUN=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --board)
      [[ $# -ge 2 ]] || die "--board requires a value"
      BOARD="$2"
      shift 2
      ;;
    --loader)
      [[ $# -ge 2 ]] || die "--loader requires a path"
      LOADER="$2"
      shift 2
      ;;
    --image)
      [[ $# -ge 2 ]] || die "--image requires a path"
      IMAGE="$2"
      shift 2
      ;;
    --offset)
      [[ $# -ge 2 ]] || die "--offset requires a sector"
      OFFSET="$2"
      shift 2
      ;;
    --tool)
      [[ $# -ge 2 ]] || die "--tool requires a path"
      TOOL="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --yes)
      ASSUME_YES=1
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

[[ "${BOARD}" == "${BOARD_ID}" ]] || die "--board must be ${BOARD_ID}; refusing to flash any other board"
[[ -n "${LOADER}" ]] || die "--loader is required"
[[ -n "${IMAGE}" ]] || die "--image is required"
[[ "${OFFSET}" =~ ^0x[0-9A-Fa-f]+$|^[0-9]+$ ]] || die "--offset must be decimal or hex"
[[ "${ASSUME_YES}" == "1" || "${DRY_RUN}" == "1" ]] || die "pass --yes after confirming the ATK-DLRV1126 Maskrom sequence"

if [[ "${DRY_RUN}" != "1" ]]; then
  command -v "${TOOL}" >/dev/null 2>&1 || die "rkdeveloptool not found: ${TOOL}"
  [[ -f "${LOADER}" ]] || die "loader not found: ${LOADER}"
  [[ -f "${IMAGE}" ]] || die "image not found: ${IMAGE}"

  if ! "${TOOL}" ld | grep -qi 'Maskrom'; then
    die "ATK-DLRV1126 is not in Maskrom; hold MASKROM while connecting USB OTG and power"
  fi
else
  echo "# dry-run for ${BOARD_ID}: no USB writes will be performed"
fi

run_cmd "${TOOL}" db "${LOADER}"
run_cmd sleep 1
run_cmd "${TOOL}" wl "${OFFSET}" "${IMAGE}"
run_cmd "${TOOL}" rd
