#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: phase0_qemu_e2e_smoke.sh [--dry-run] [--keep-workdir]

Runs the Phase 0 qemu-only close gate:
  Rockchip toolchain -> daemon build -> Yocto smoke -> qemu-aarch64 daemon
  -> userspace integration test.

Required for real execution:
  OMNISIGHT_TOOLCHAIN_ROOT  writable toolchain install root

Optional overrides:
  OMNISIGHT_PHASE0_TOOLCHAIN_ID  default rockchip-rk35xx-aarch64-gcc-11
  OMNISIGHT_PHASE0_TARGET_TRIPLE default aarch64-linux-gnu
  OMNISIGHT_PHASE0_YOCTO_SMOKE   default tools/embedded/yocto_smoke.sh
  OMNISIGHT_PHASE0_TEST_SRC      default tools/embedded/uvc_xu_dispatch_test.c
  OMNISIGHT_PHASE0_SYSFS_ROOT    default synthetic tmpfs sysfs fixture
EOF
}

die() {
  echo "phase0_qemu_e2e_smoke.sh: $*" >&2
  exit 1
}
trap 'die "command failed at line $LINENO"' ERR

need_cmd() {
  if ((dry_run)); then
    log "would require command: $1"
    return 0
  fi
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

log() {
  echo "phase0_qemu_e2e_smoke.sh: $*" >&2
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
toolchain_id="${OMNISIGHT_PHASE0_TOOLCHAIN_ID:-rockchip-rk35xx-aarch64-gcc-11}"
target_triple="${OMNISIGHT_PHASE0_TARGET_TRIPLE:-aarch64-linux-gnu}"
verify_toolchain="$repo_root/tools/embedded/verify_toolchain.sh"
daemon_dir="$repo_root/src/embedded/uvc-xu-dispatcher"
yocto_layer="$repo_root/yocto/meta-omnisight-camera"
yocto_smoke="${OMNISIGHT_PHASE0_YOCTO_SMOKE:-$repo_root/tools/embedded/yocto_smoke.sh}"
test_src="${OMNISIGHT_PHASE0_TEST_SRC:-$repo_root/tools/embedded/uvc_xu_dispatch_test.c}"
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
need_file "$daemon_dir/vendor_registry.c" "missing P0.B.2 vendor registry"
need_file "$daemon_dir/ft_c600_handler.c" "missing P0.B.3a FT-C600 handler"
need_file "$daemon_dir/rockchip_rk35xx_handler.c" "missing P0.B.3b Rockchip handler"
need_file "$yocto_layer/conf/layer.conf" "missing Yocto layer"
need_file "$yocto_layer/recipes-core/uvcvideo-xu-dispatcher/uvcvideo-xu-dispatcher_%.bbappend" \
  "missing Rockchip Yocto bbappend"
need_file "$test_src" "missing P0.B.4 integration test source"

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
sysfs_root="${OMNISIGHT_PHASE0_SYSFS_ROOT:-$workdir/sysfs}"
compiler="$toolchain_root/$toolchain_id/bin/$target_triple-gcc"

log "verifying Rockchip toolchain $toolchain_id"
run env OMNISIGHT_TOOLCHAIN_ROOT="$toolchain_root" "$verify_toolchain" "$toolchain_id"
[[ -x "$compiler" || "$dry_run" -eq 1 ]] || die "compiler missing after toolchain verification: $compiler"

log "building daemon with $target_triple compiler"
cat >"$workdir/CMakeLists.txt" <<EOF
cmake_minimum_required(VERSION 3.16)
project(phase0_qemu_e2e_smoke C)
include("$daemon_dir/skeleton.cmake")
target_sources(uvc-xu-dispatcher PRIVATE
  "$daemon_dir/vendor_registry.c"
  "$daemon_dir/ft_c600_handler.c"
  "$daemon_dir/rockchip_rk35xx_handler.c"
)
target_link_libraries(uvc-xu-dispatcher PRIVATE pthread)
EOF
run cmake -S "$workdir" -B "$workdir/build" \
  -DCMAKE_C_COMPILER="$compiler" \
  -DCMAKE_RUNTIME_OUTPUT_DIRECTORY="$workdir/bin"
run cmake --build "$workdir/build" --target uvc-xu-dispatcher

log "running Yocto Rockchip smoke"
if [[ -x "$yocto_smoke" ]]; then
  run "$yocto_smoke" rockchip-rk35xx
elif [[ -n "${OMNISIGHT_PHASE0_ROOTFS_BIN:-}" ]]; then
  [[ -f "$OMNISIGHT_PHASE0_ROOTFS_BIN" || "$dry_run" -eq 1 ]] ||
    die "OMNISIGHT_PHASE0_ROOTFS_BIN does not exist: $OMNISIGHT_PHASE0_ROOTFS_BIN"
  log "using preverified rootfs binary: $OMNISIGHT_PHASE0_ROOTFS_BIN"
else
  if ((dry_run)); then
    log "would require Yocto smoke or OMNISIGHT_PHASE0_ROOTFS_BIN"
  else
    die "missing Yocto smoke; set OMNISIGHT_PHASE0_YOCTO_SMOKE or OMNISIGHT_PHASE0_ROOTFS_BIN"
  fi
fi

log "creating synthetic UVC sysfs fixture"
run mkdir -p "$sysfs_root/video0/device"
if ((dry_run == 0)); then
  printf '2207\n' >"$sysfs_root/video0/device/idVendor"
  printf '3588\n' >"$sysfs_root/video0/device/idProduct"
fi

log "starting daemon under $qemu_bin"
if ((dry_run)); then
  run "$qemu_bin" -L "<toolchain-sysroot>" "$workdir/bin/uvc-xu-dispatcher" \
    "$socket_path" "$sysfs_root"
else
  sysroot="$("$compiler" --print-sysroot)"
  qemu_args=("$qemu_bin")
  [[ -n "$sysroot" && -d "$sysroot" ]] && qemu_args+=(-L "$sysroot")
  "${qemu_args[@]}" "$workdir/bin/uvc-xu-dispatcher" "$socket_path" "$sysfs_root" &
  daemon_pid="$!"
  for _ in $(seq 1 50); do
    [[ -S "$socket_path" ]] && break
    sleep 0.1
  done
  [[ -S "$socket_path" ]] || die "daemon socket did not appear: $socket_path"
fi

log "building and running P0.B.4 integration test"
run "$compiler" "$test_src" -o "$workdir/uvc_xu_dispatch_test"
if ((dry_run)); then
  run "$qemu_bin" -L "<toolchain-sysroot>" "$workdir/uvc_xu_dispatch_test" "$socket_path"
else
  "${qemu_args[@]}" "$workdir/uvc_xu_dispatch_test" "$socket_path"
fi

log "Phase 0 qemu e2e smoke passed"
