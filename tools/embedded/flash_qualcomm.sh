#!/usr/bin/env bash
# OP-1963 P0.D.1c - Radxa Dragon Q6A fastboot/EDL wrapper.
#
# Usage:
#   tools/embedded/flash_qualcomm.sh --device <fastboot-serial> --image <firmware.img> --partition <name> [fastboot]
#   tools/embedded/flash_qualcomm.sh --device <edl-serial> --firehose <prog_firehose_ddr.elf> --rawprogram <rawprogram.xml> [--patch <patch.xml>] edl
#   tools/embedded/flash_qualcomm.sh --device <fastboot-serial> reboot
#
# Operator sequence:
#   1. Confirm the board is a Radxa Dragon Q6A.
#   2. Put the board in fastboot mode for partition writes, or EDL mode for
#      firehose recovery.
#   3. Run with --dry-run first and verify the printed command uses the intended
#      explicit serial/device identifier.
#   4. Re-run with --yes only after firmware paths and device mode are verified.
#
# This script never auto-selects among USB devices. The --device value is passed
# to fastboot/qdl/edl.py and live fastboot runs abort unless exactly one
# `fastboot devices` row matches it.

set -euo pipefail

FASTBOOT_BIN="${FASTBOOT_BIN:-fastboot}"
EDL_BIN="${EDL_BIN:-qdl}"
DEVICE_MATCH=""
IMAGE=""
PARTITION=""
FIREHOSE=""
RAWPROGRAM=""
PATCH_XML=""
DRY_RUN=false
ASSUME_YES=false
ACTION="fastboot"

usage() {
  sed -n '2,19p' "$0"
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

assert_confirmed() {
  [ -n "$DEVICE_MATCH" ] || die "--device is required; pass the explicit Radxa Dragon Q6A serial/device id"
  [ "$ASSUME_YES" = true ] || [ "$DRY_RUN" = true ] || die "pass --yes after confirming the Radxa Dragon Q6A device and firmware paths"
}

assert_fastboot_device() {
  if [ "$DRY_RUN" = true ]; then
    echo "[dry-run] would verify exactly one fastboot device matching: $DEVICE_MATCH"
    return 0
  fi
  command -v "$FASTBOOT_BIN" >/dev/null 2>&1 || die "fastboot not found; set FASTBOOT_BIN"

  local lines matches count
  lines="$("$FASTBOOT_BIN" devices 2>&1 || true)"
  matches="$(printf '%s\n' "$lines" | awk '$2 == "fastboot" {print $1}' | grep -F -- "$DEVICE_MATCH" || true)"
  count="$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l | tr -d ' ')"
  [ "$count" = "1" ] || die "expected exactly one fastboot device matching '$DEVICE_MATCH'; saw $count
fastboot devices output:
$lines"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --device) DEVICE_MATCH="${2:-}"; shift 2 ;;
    --device=*) DEVICE_MATCH="${1#--device=}"; shift ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --image=*) IMAGE="${1#--image=}"; shift ;;
    --partition) PARTITION="${2:-}"; shift 2 ;;
    --partition=*) PARTITION="${1#--partition=}"; shift ;;
    --firehose) FIREHOSE="${2:-}"; shift 2 ;;
    --firehose=*) FIREHOSE="${1#--firehose=}"; shift ;;
    --rawprogram) RAWPROGRAM="${2:-}"; shift 2 ;;
    --rawprogram=*) RAWPROGRAM="${1#--rawprogram=}"; shift ;;
    --patch) PATCH_XML="${2:-}"; shift 2 ;;
    --patch=*) PATCH_XML="${1#--patch=}"; shift ;;
    --fastboot-bin) FASTBOOT_BIN="${2:-}"; shift 2 ;;
    --fastboot-bin=*) FASTBOOT_BIN="${1#--fastboot-bin=}"; shift ;;
    --edl-bin) EDL_BIN="${2:-}"; shift 2 ;;
    --edl-bin=*) EDL_BIN="${1#--edl-bin=}"; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --yes) ASSUME_YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    fastboot|edl|reboot) ACTION="$1"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

assert_confirmed

case "$ACTION" in
  fastboot)
    [ -n "$PARTITION" ] || die "--partition is required for fastboot flash"
    need_file "--image" "$IMAGE"
    assert_fastboot_device
    run_cmd "$FASTBOOT_BIN" -s "$DEVICE_MATCH" flash "$PARTITION" "$IMAGE"
    run_cmd "$FASTBOOT_BIN" -s "$DEVICE_MATCH" reboot
    ;;
  reboot)
    assert_fastboot_device
    run_cmd "$FASTBOOT_BIN" -s "$DEVICE_MATCH" reboot
    ;;
  edl)
    need_file "--firehose" "$FIREHOSE"
    need_file "--rawprogram" "$RAWPROGRAM"
    [ -z "$PATCH_XML" ] || need_file "--patch" "$PATCH_XML"
    command -v "$EDL_BIN" >/dev/null 2>&1 || [ "$DRY_RUN" = true ] || die "EDL tool not found; set --edl-bin to qdl or edl.py"
    if [ -n "$PATCH_XML" ]; then
      run_cmd "$EDL_BIN" --serial "$DEVICE_MATCH" --firehose "$FIREHOSE" "$RAWPROGRAM" "$PATCH_XML"
    else
      run_cmd "$EDL_BIN" --serial "$DEVICE_MATCH" --firehose "$FIREHOSE" "$RAWPROGRAM"
    fi
    ;;
esac
