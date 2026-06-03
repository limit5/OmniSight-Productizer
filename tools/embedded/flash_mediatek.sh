#!/usr/bin/env bash
# OP-1964 P0.D.1d - MediaTek Genio 1200-EVK BootROM flash wrapper.
#
# Usage:
#   tools/embedded/flash_mediatek.sh --board genio1200-evk --device <lsusb-match> --scatter <scatter.txt> --image-dir <deploy-dir> --download-agent <da.bin> [flash]
#   tools/embedded/flash_mediatek.sh --board genio1200-evk --device <lsusb-match> --scatter <scatter.txt> --image-dir <deploy-dir> --download-agent <da.bin> --tool-mode mtk-brom flash
#
# Operator sequence:
#   1. Confirm the board is a MediaTek Genio 1200-EVK.
#   2. Disconnect board power and USB-C download cable.
#   3. Hold the Genio 1200-EVK BootROM/download key sequence from the
#      NDA-cleared board manual.
#   4. Connect the USB-C download cable, apply power, then release the keys only
#      after the host enumerates the MediaTek BootROM USB device.
#   5. Run with --dry-run first and verify the scatter, image directory,
#      Download Agent, and explicit USB match.
#   6. Re-run with --yes only after paths and BootROM device identity are
#      verified.
#
# This script never auto-selects among USB devices. The --device value is
# matched against `lsusb` output before any live flash command is allowed.

set -euo pipefail

BOARD_ID="genio1200-evk"
FLASH_BIN="${GENIO_FLASH_BIN:-genio-flash}"
LSUSB_BIN="${LSUSB_BIN:-lsusb}"
BOARD=""
DEVICE_MATCH=""
SCATTER=""
IMAGE_DIR=""
DOWNLOAD_AGENT=""
TOOL_MODE="genio-flash"
DRY_RUN=false
ASSUME_YES=false
ACTION="flash"

usage() {
  sed -n '2,20p' "$0"
}

die() {
  echo "FAIL: $*" >&2
  exit 2
}

quote_cmd() {
  printf '[dry-run] %q' "$1"
  shift
  printf ' %q' "$@"
  printf '\n'
}

run_cmd() {
  if [ "$DRY_RUN" = true ]; then
    quote_cmd "$@"
    return 0
  fi
  "$@"
}

need_file() {
  local label="$1"
  local path="$2"
  [ -n "$path" ] || die "$label is required"
  [ "$DRY_RUN" = true ] && return 0
  [ -f "$path" ] || die "$label does not exist: $path"
}

need_dir() {
  local label="$1"
  local path="$2"
  [ -n "$path" ] || die "$label is required"
  [ "$DRY_RUN" = true ] && return 0
  [ -d "$path" ] || die "$label does not exist: $path"
}

assert_confirmed() {
  [ "$BOARD" = "$BOARD_ID" ] || die "--board must be $BOARD_ID; refusing to flash any other board"
  [ -n "$DEVICE_MATCH" ] || die "--device is required; pass a unique Genio 1200-EVK BootROM lsusb substring"
  [ "$ASSUME_YES" = true ] || [ "$DRY_RUN" = true ] || die "pass --yes after confirming the Genio 1200-EVK BootROM sequence and firmware paths"
}

assert_brom_device() {
  if [ "$DRY_RUN" = true ]; then
    echo "[dry-run] would verify exactly one MediaTek BootROM USB device matching: $DEVICE_MATCH"
    return 0
  fi
  command -v "$LSUSB_BIN" >/dev/null 2>&1 || die "lsusb not found; set LSUSB_BIN"

  local lines matches count
  lines="$("$LSUSB_BIN" 2>&1 || true)"
  matches="$(printf '%s\n' "$lines" | grep -i -e 'mediatek' -e '0e8d:' | grep -F -- "$DEVICE_MATCH" || true)"
  count="$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l | tr -d ' ')"
  [ "$count" = "1" ] || die "expected exactly one MediaTek BootROM USB device matching '$DEVICE_MATCH'; saw $count
lsusb output:
$lines"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --board) BOARD="${2:-}"; shift 2 ;;
    --board=*) BOARD="${1#--board=}"; shift ;;
    --device) DEVICE_MATCH="${2:-}"; shift 2 ;;
    --device=*) DEVICE_MATCH="${1#--device=}"; shift ;;
    --scatter) SCATTER="${2:-}"; shift 2 ;;
    --scatter=*) SCATTER="${1#--scatter=}"; shift ;;
    --image-dir) IMAGE_DIR="${2:-}"; shift 2 ;;
    --image-dir=*) IMAGE_DIR="${1#--image-dir=}"; shift ;;
    --download-agent) DOWNLOAD_AGENT="${2:-}"; shift 2 ;;
    --download-agent=*) DOWNLOAD_AGENT="${1#--download-agent=}"; shift ;;
    --flash-bin) FLASH_BIN="${2:-}"; shift 2 ;;
    --flash-bin=*) FLASH_BIN="${1#--flash-bin=}"; shift ;;
    --lsusb-bin) LSUSB_BIN="${2:-}"; shift 2 ;;
    --lsusb-bin=*) LSUSB_BIN="${1#--lsusb-bin=}"; shift ;;
    --tool-mode) TOOL_MODE="${2:-}"; shift 2 ;;
    --tool-mode=*) TOOL_MODE="${1#--tool-mode=}"; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --yes) ASSUME_YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    flash) ACTION="$1"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ "$ACTION" = "flash" ] || die "unsupported action: $ACTION"
[ "$TOOL_MODE" = "genio-flash" ] || [ "$TOOL_MODE" = "mtk-brom" ] || die "--tool-mode must be genio-flash or mtk-brom"
need_file "--scatter" "$SCATTER"
need_dir "--image-dir" "$IMAGE_DIR"
need_file "--download-agent" "$DOWNLOAD_AGENT"
assert_confirmed
assert_brom_device
command -v "$FLASH_BIN" >/dev/null 2>&1 || [ "$DRY_RUN" = true ] || die "flash tool not found; set --flash-bin or GENIO_FLASH_BIN"

case "$TOOL_MODE" in
  genio-flash)
    run_cmd "$FLASH_BIN" --device "$DEVICE_MATCH" --scatter "$SCATTER" --image-dir "$IMAGE_DIR" --download-agent "$DOWNLOAD_AGENT"
    ;;
  mtk-brom)
    run_cmd "$FLASH_BIN" flash --device "$DEVICE_MATCH" --scatter "$SCATTER" --image-dir "$IMAGE_DIR" --download-agent "$DOWNLOAD_AGENT"
    ;;
esac
