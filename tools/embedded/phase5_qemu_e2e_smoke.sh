#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: phase5_qemu_e2e_smoke.sh [--dry-run] [--keep-workdir]

Runs the Case 5 Phase 1B qemu-only close gate:
  Rockchip toolchain -> daemon + UAC2 gadget config + WebRTC build
  -> Yocto smoke -> qemu-aarch64 execution -> configfs/WebRTC assertions.

Required for real execution:
  OMNISIGHT_TOOLCHAIN_ROOT  writable toolchain install root

Optional overrides:
  OMNISIGHT_PHASE5_TOOLCHAIN_ID     default rockchip-rk35xx-aarch64-gcc-11
  OMNISIGHT_PHASE5_TARGET_TRIPLE    default aarch64-linux-gnu
  OMNISIGHT_PHASE5_YOCTO_SMOKE      default tools/embedded/yocto_smoke.sh
  OMNISIGHT_PHASE5_UAC2_SRC         default src/embedded/case5/uac2_gadget_config.c
  OMNISIGHT_PHASE5_UAC2_BIN         prebuilt UAC2 config helper
  OMNISIGHT_PHASE5_WEBRTC_SRC       default src/embedded/case5/webrtc_peer_connection.c
  OMNISIGHT_PHASE5_WEBRTC_BIN       prebuilt WebRTC smoke helper
  OMNISIGHT_PHASE5_WEBRTC_ARGS      default --self-test
  OMNISIGHT_PHASE5_YOCTO_MACHINE    default rk3588
  OMNISIGHT_PHASE5_CONFIGFS_ROOT    default synthetic tmpfs configfs fixture
EOF
}

die() {
  echo "phase5_qemu_e2e_smoke.sh: $*" >&2
  exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
  echo "phase5_qemu_e2e_smoke.sh: $*" >&2
}

run() {
  if ((dry_run)); then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

need_cmd() {
  if ((dry_run)); then
    log "would require command: $1"
    return 0
  fi
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

need_file() {
  if [[ -f "$1" ]]; then
    return 0
  fi
  ((dry_run)) && log "would require file: $1" && return 0
  die "$2: $1"
}

need_executable() {
  if [[ -x "$1" ]]; then
    return 0
  fi
  ((dry_run)) && log "would require executable: $1" && return 0
  die "$2: $1"
}

dry_run=0
keep_workdir=0
while (($# > 0)); do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --keep-workdir) keep_workdir=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
toolchain_id="${OMNISIGHT_PHASE5_TOOLCHAIN_ID:-rockchip-rk35xx-aarch64-gcc-11}"
target_triple="${OMNISIGHT_PHASE5_TARGET_TRIPLE:-aarch64-linux-gnu}"
verify_toolchain="$repo_root/tools/embedded/verify_toolchain.sh"
daemon_dir="$repo_root/src/embedded/uvc-xu-dispatcher"
yocto_layer="$repo_root/yocto/meta-omnisight-camera"
yocto_smoke="${OMNISIGHT_PHASE5_YOCTO_SMOKE:-$repo_root/tools/embedded/yocto_smoke.sh}"
yocto_machine="${OMNISIGHT_PHASE5_YOCTO_MACHINE:-rk3588}"
uac2_src="${OMNISIGHT_PHASE5_UAC2_SRC:-$repo_root/src/embedded/case5/uac2_gadget_config.c}"
webrtc_src="${OMNISIGHT_PHASE5_WEBRTC_SRC:-$repo_root/src/embedded/case5/webrtc_peer_connection.c}"
qemu_bin="${QEMU_AARCH64:-qemu-aarch64}"
workdir=""
daemon_pid=""

cleanup() {
  if [[ -n "$daemon_pid" ]] && kill -0 "$daemon_pid" >/dev/null 2>&1; then
    kill "$daemon_pid" >/dev/null 2>&1 || true
    wait "$daemon_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$workdir" && "$keep_workdir" -eq 0 ]]; then
    rm -rf "$workdir"
  elif [[ -n "$workdir" ]]; then
    log "kept workdir: $workdir"
  fi
}
trap cleanup EXIT

need_executable "$verify_toolchain" "missing executable"
need_file "$daemon_dir/skeleton.cmake" "missing daemon CMake fragment"
need_file "$daemon_dir/main.c" "missing daemon source"
need_file "$daemon_dir/vendor_registry.c" "missing vendor registry"
need_file "$daemon_dir/rockchip_rk35xx_handler.c" "missing Rockchip handler"
need_file "$yocto_layer/conf/layer.conf" "missing Yocto layer"

if [[ -z "${OMNISIGHT_TOOLCHAIN_ROOT:-}" ]]; then
  ((dry_run)) || die "OMNISIGHT_TOOLCHAIN_ROOT must be set for real execution"
  toolchain_root="\${OMNISIGHT_TOOLCHAIN_ROOT}"
else
  toolchain_root="$OMNISIGHT_TOOLCHAIN_ROOT"
fi

case "$toolchain_root" in
  ""|"/") die "unsafe OMNISIGHT_TOOLCHAIN_ROOT: $toolchain_root" ;;
esac

need_cmd cmake
need_cmd "$qemu_bin"
need_cmd mktemp

workdir="$(mktemp -d)"
socket_path="$workdir/uvc-xu-dispatcher.sock"
configfs_root="${OMNISIGHT_PHASE5_CONFIGFS_ROOT:-$workdir/configfs}"
compiler="$toolchain_root/$toolchain_id/bin/$target_triple-gcc"

log "verifying Rockchip toolchain $toolchain_id"
run env OMNISIGHT_TOOLCHAIN_ROOT="$toolchain_root" "$verify_toolchain" "$toolchain_id"
[[ -x "$compiler" || "$dry_run" -eq 1 ]] || die "compiler missing after toolchain verification: $compiler"

log "building daemon with $target_triple compiler"
cat >"$workdir/CMakeLists.txt" <<EOF
cmake_minimum_required(VERSION 3.16)
project(phase5_qemu_e2e_smoke C)
include("$daemon_dir/skeleton.cmake")
target_sources(uvc-xu-dispatcher PRIVATE
  "$daemon_dir/vendor_registry.c"
  "$daemon_dir/rockchip_rk35xx_handler.c"
)
target_link_libraries(uvc-xu-dispatcher PRIVATE pthread)
EOF
run cmake -S "$workdir" -B "$workdir/build" \
  -DCMAKE_C_COMPILER="$compiler" \
  -DCMAKE_RUNTIME_OUTPUT_DIRECTORY="$workdir/bin"
run cmake --build "$workdir/build" --target uvc-xu-dispatcher

if [[ -n "${OMNISIGHT_PHASE5_UAC2_BIN:-}" ]]; then
  uac2_bin="$OMNISIGHT_PHASE5_UAC2_BIN"
  need_executable "$uac2_bin" "missing UAC2 config helper"
else
  need_file "$uac2_src" "missing C5.A.1 UAC2 config source"
  uac2_bin="$workdir/uac2_gadget_config"
  log "building UAC2 gadget config helper"
  run "$compiler" "$uac2_src" -o "$uac2_bin"
fi

if [[ -n "${OMNISIGHT_PHASE5_WEBRTC_BIN:-}" ]]; then
  webrtc_bin="$OMNISIGHT_PHASE5_WEBRTC_BIN"
  need_executable "$webrtc_bin" "missing WebRTC smoke helper"
else
  need_file "$webrtc_src" "missing C5.D-WebRTC.1 PeerConnection source"
  webrtc_bin="$workdir/webrtc_peer_connection"
  log "building WebRTC PeerConnection smoke helper"
  run "$compiler" "$webrtc_src" -o "$webrtc_bin"
fi

log "running Yocto Rockchip smoke"
if [[ -x "$yocto_smoke" ]]; then
  run env YOCTO_SMOKE_MACHINE="$yocto_machine" "$yocto_smoke"
elif [[ -n "${OMNISIGHT_PHASE5_ROOTFS_BIN:-}" ]]; then
  [[ -f "$OMNISIGHT_PHASE5_ROOTFS_BIN" || "$dry_run" -eq 1 ]] ||
    die "OMNISIGHT_PHASE5_ROOTFS_BIN does not exist: $OMNISIGHT_PHASE5_ROOTFS_BIN"
  log "using preverified rootfs binary: $OMNISIGHT_PHASE5_ROOTFS_BIN"
else
  if ((dry_run)); then
    log "would require Yocto smoke or OMNISIGHT_PHASE5_ROOTFS_BIN"
  else
    die "missing Yocto smoke; set OMNISIGHT_PHASE5_YOCTO_SMOKE or OMNISIGHT_PHASE5_ROOTFS_BIN"
  fi
fi

log "creating synthetic configfs fixture"
run mkdir -p "$configfs_root/usb_gadget"

if ((dry_run)); then
  qemu_args=("$qemu_bin" -L "<toolchain-sysroot>")
else
  sysroot="$("$compiler" --print-sysroot)"
  qemu_args=("$qemu_bin")
  [[ -n "$sysroot" && -d "$sysroot" ]] && qemu_args+=(-L "$sysroot")
fi

log "starting daemon under $qemu_bin"
if ((dry_run)); then
  run "${qemu_args[@]}" "$workdir/bin/uvc-xu-dispatcher" "$socket_path" "$configfs_root"
else
  "${qemu_args[@]}" "$workdir/bin/uvc-xu-dispatcher" "$socket_path" "$configfs_root" &
  daemon_pid="$!"
  for _ in $(seq 1 50); do
    [[ -S "$socket_path" ]] && break
    sleep 0.1
  done
  [[ -S "$socket_path" ]] || die "daemon socket did not appear: $socket_path"
fi

log "running UAC2 config helper under $qemu_bin"
run env OMNISIGHT_CONFIGFS_ROOT="$configfs_root" "${qemu_args[@]}" "$uac2_bin"
uac2_function="$configfs_root/usb_gadget/omnisight-c5/functions/uac2.usb0"
if ((dry_run)); then
  log "would assert UAC2 configfs function exists: $uac2_function"
else
  [[ -d "$uac2_function" ]] || die "UAC2 configfs function did not appear: $uac2_function"
fi

log "running WebRTC PeerConnection smoke under $qemu_bin"
read -r -a webrtc_args <<<"${OMNISIGHT_PHASE5_WEBRTC_ARGS:---self-test}"
run "${qemu_args[@]}" "$webrtc_bin" "${webrtc_args[@]}"

log "Case 5 Phase 1B qemu e2e smoke passed"
