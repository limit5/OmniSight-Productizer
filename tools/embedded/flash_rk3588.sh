#!/usr/bin/env bash
# OP-1943 P0.D.1a - ATK-DLRK3588 rkdeveloptool wrapper.
#
# Defaults to the Rockchip RK3588 maskrom flash sequence:
#   1. Verify one explicit maskrom device is present via `rkdeveloptool ld`.
#   2. Download the RK3588 loader (`db`).
#   3. Write the image to the selected LBA (`wl`, default 0x0).
#   4. Reboot the board (`rd`).
#
# Usage:
#   tools/embedded/flash_rk3588.sh --device <ld-match> --loader <rk3588-loader.bin> --image <firmware.img> [flash]
#   tools/embedded/flash_rk3588.sh --device <ld-match> --loader <rk3588-loader.bin> db
#   tools/embedded/flash_rk3588.sh --device <ld-match> --loader <rk3588-loader.bin> ul
#   tools/embedded/flash_rk3588.sh --device <ld-match> --image <firmware.img> [--lba 0x0] wl
#   tools/embedded/flash_rk3588.sh --device <ld-match> rd
#
# The --device value is matched against the exact `rkdeveloptool ld` output
# line for the connected ATK-DLRK3588. This script does not auto-select among
# USB devices; it aborts unless exactly one visible maskrom line matches.

set -euo pipefail

RKDEVELOPTOOL_BIN="${RKDEVELOPTOOL_BIN:-rkdeveloptool}"
DEVICE_MATCH=""
LOADER=""
IMAGE=""
LBA="0x0"
DRY_RUN=false
ACTION="flash"

usage() {
  sed -n '2,20p' "$0"
}

die() {
  echo "FAIL: $*" >&2
  exit 2
}

run_rk() {
  if [ "$DRY_RUN" = true ]; then
    printf '[dry-run] %q' "$RKDEVELOPTOOL_BIN"
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$RKDEVELOPTOOL_BIN" "$@"
}

need_file() {
  local label="$1"
  local path="$2"
  [ -n "$path" ] || die "$label is required"
  [ -f "$path" ] || die "$label does not exist: $path"
}

assert_maskrom_device() {
  [ -n "$DEVICE_MATCH" ] || die "--device is required; pass an exact rkdeveloptool ld line or unique substring"

  if [ "$DRY_RUN" = true ]; then
    echo "[dry-run] would verify exactly one Maskrom rkdeveloptool ld line matching: $DEVICE_MATCH"
    return 0
  fi
  command -v "$RKDEVELOPTOOL_BIN" >/dev/null 2>&1 || die "rkdeveloptool not found; set RKDEVELOPTOOL_BIN"

  local lines matches count
  lines="$("$RKDEVELOPTOOL_BIN" ld 2>&1 || true)"
  matches="$(printf '%s\n' "$lines" | grep -i 'maskrom' | grep -F -- "$DEVICE_MATCH" || true)"
  count="$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l | tr -d ' ')"
  [ "$count" = "1" ] || die "expected exactly one Maskrom device matching '$DEVICE_MATCH'; saw $count
rkdeveloptool ld output:
$lines"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --device) DEVICE_MATCH="${2:-}"; shift 2 ;;
    --device=*) DEVICE_MATCH="${1#--device=}"; shift ;;
    --loader) LOADER="${2:-}"; shift 2 ;;
    --loader=*) LOADER="${1#--loader=}"; shift ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --image=*) IMAGE="${1#--image=}"; shift ;;
    --lba) LBA="${2:-}"; shift 2 ;;
    --lba=*) LBA="${1#--lba=}"; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    db|ul|wl|rd|flash) ACTION="$1"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

case "$ACTION" in
  db|ul) need_file "--loader" "$LOADER" ;;
  wl) need_file "--image" "$IMAGE" ;;
  flash) need_file "--loader" "$LOADER"; need_file "--image" "$IMAGE" ;;
esac

assert_maskrom_device

case "$ACTION" in
  db) run_rk db "$LOADER" ;;
  ul) run_rk ul "$LOADER" ;;
  wl) run_rk wl "$LBA" "$IMAGE" ;;
  rd) run_rk rd ;;
  flash)
    run_rk db "$LOADER"
    run_rk wl "$LBA" "$IMAGE"
    run_rk rd
    ;;
esac
