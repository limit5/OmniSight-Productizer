#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: phase6-qemu-e2e-smoke.sh [--dry-run] [--keep-workdir]

Runs the Case 6 Phase 1B qemu-only close gate:
  Rockchip toolchain -> C6 UAV stack build -> Yocto smoke
  -> qemu-aarch64 sensor fusion/gimbal/RF/UAV CV integration smokes.

Required for real execution:
  OMNISIGHT_TOOLCHAIN_ROOT  writable toolchain install root

Optional overrides:
  OMNISIGHT_PHASE6_TOOLCHAIN_ID     default rockchip-rk35xx-aarch64-gcc-11
  OMNISIGHT_PHASE6_TARGET_TRIPLE    default aarch64-linux-gnu
  OMNISIGHT_PHASE6_YOCTO_SMOKE      default tools/embedded/yocto_smoke.sh
  OMNISIGHT_PHASE6_YOCTO_MACHINE    default rk3588
  OMNISIGHT_PHASE6_SENSOR_SMOKE     default tools/embedded/run-sensor-fusion-smoke.sh
  OMNISIGHT_PHASE6_GIMBAL_SMOKE     default tools/embedded/run-gimbal-smoke.sh
  OMNISIGHT_PHASE6_RF_SMOKE         default tools/embedded/run-rf-smoke.sh
  OMNISIGHT_PHASE6_UAV_CV_SMOKE     default tools/embedded/run-uav-cv-smoke.sh
EOF
}

die() {
  echo "phase6-qemu-e2e-smoke.sh: $*" >&2
  exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
  echo "phase6-qemu-e2e-smoke.sh: $*" >&2
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

need_dir() {
  if [[ -d "$1" ]]; then
    return 0
  fi
  ((dry_run)) && log "would require directory: $1" && return 0
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
toolchain_id="${OMNISIGHT_PHASE6_TOOLCHAIN_ID:-rockchip-rk35xx-aarch64-gcc-11}"
target_triple="${OMNISIGHT_PHASE6_TARGET_TRIPLE:-aarch64-linux-gnu}"
verify_toolchain="$repo_root/tools/embedded/verify_toolchain.sh"
yocto_layer="$repo_root/yocto/meta-omnisight-camera"
yocto_smoke="${OMNISIGHT_PHASE6_YOCTO_SMOKE:-$repo_root/tools/embedded/yocto_smoke.sh}"
yocto_machine="${OMNISIGHT_PHASE6_YOCTO_MACHINE:-rk3588}"
sensor_smoke="${OMNISIGHT_PHASE6_SENSOR_SMOKE:-$repo_root/tools/embedded/run-sensor-fusion-smoke.sh}"
gimbal_smoke="${OMNISIGHT_PHASE6_GIMBAL_SMOKE:-$repo_root/tools/embedded/run-gimbal-smoke.sh}"
rf_smoke="${OMNISIGHT_PHASE6_RF_SMOKE:-$repo_root/tools/embedded/run-rf-smoke.sh}"
uav_cv_smoke="${OMNISIGHT_PHASE6_UAV_CV_SMOKE:-$repo_root/tools/embedded/run-uav-cv-smoke.sh}"
qemu_bin="${QEMU_AARCH64:-qemu-aarch64}"
workdir=""

cleanup() {
  if [[ -n "$workdir" && "$keep_workdir" -eq 0 ]]; then
    rm -rf "$workdir"
  elif [[ -n "$workdir" ]]; then
    log "kept workdir: $workdir"
  fi
}
trap cleanup EXIT

need_executable "$verify_toolchain" "missing executable"
need_executable "$sensor_smoke" "missing C6.B.4 sensor fusion smoke"
need_executable "$gimbal_smoke" "missing C6.C.3 gimbal smoke"
need_executable "$rf_smoke" "missing C6.D.4 RF telemetry smoke"
need_executable "$uav_cv_smoke" "missing C6.E.4 UAV CV smoke"
need_dir "$yocto_layer" "missing Yocto layer"
need_file "$yocto_layer/conf/layer.conf" "missing Yocto layer config"
need_file "$repo_root/src/embedded/uav/flight/CMakeLists.txt" "missing C6.A flight CMake fragment"
need_file "$repo_root/src/embedded/uav/mavlink/CMakeLists.txt" "missing MAVLink CMake fragment"
need_file "$repo_root/src/embedded/uav/sensors/CMakeLists.txt" "missing sensor fusion CMake fragment"
need_file "$repo_root/src/embedded/uav/gimbal/CMakeLists.txt" "missing gimbal CMake fragment"
need_file "$repo_root/src/embedded/uav/rf/CMakeLists.txt" "missing RF CMake fragment"
need_file "$repo_root/src/embedded/uav/cv/CMakeLists.txt" "missing UAV CV CMake fragment"

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
cc="$toolchain_root/$toolchain_id/bin/$target_triple-gcc"
cxx="$toolchain_root/$toolchain_id/bin/$target_triple-g++"

log "verifying Rockchip toolchain $toolchain_id"
run env OMNISIGHT_TOOLCHAIN_ROOT="$toolchain_root" "$verify_toolchain" "$toolchain_id"
[[ -x "$cc" || "$dry_run" -eq 1 ]] || die "C compiler missing after toolchain verification: $cc"
[[ -x "$cxx" || "$dry_run" -eq 1 ]] || die "C++ compiler missing after toolchain verification: $cxx"

log "building Case 6 UAV stack with $target_triple compiler"
cat >"$workdir/CMakeLists.txt" <<EOF
cmake_minimum_required(VERSION 3.16)
project(phase6_qemu_e2e_smoke CXX)
add_subdirectory("$repo_root/src/embedded/uav/flight" flight)
add_subdirectory("$repo_root/src/embedded/uav/mavlink" mavlink)
add_subdirectory("$repo_root/src/embedded/uav/sensors" sensors)
add_subdirectory("$repo_root/src/embedded/uav/gimbal" gimbal)
add_subdirectory("$repo_root/src/embedded/uav/rf" rf)
add_subdirectory("$repo_root/src/embedded/uav/cv" cv)
add_custom_target(omnisight-uav-c6-stack ALL
  DEPENDS
    omnisight-uav-autopilot-abstraction
    omnisight-uav-flight-mode-fsm
    omnisight-uav-mavlink-transport
    omnisight-uav-gps-nmea-driver
    omnisight-uav-barometer-driver
    omnisight-uav-ekf-flight-integration
    omnisight-uav-gimbal-pid-controller
    omnisight-uav-gimbal-pwm-output-abstraction
    omnisight-uav-rf-lora-packet-framing
    omnisight-uav-rf-telemetry-qos
    omnisight-uav-cv-optical-flow
    omnisight-uav-cv-object-detection
    omnisight-uav-cv-video-stabilization
)
EOF
run cmake -S "$workdir" -B "$workdir/build" \
  -DCMAKE_CXX_COMPILER="$cxx"
run cmake --build "$workdir/build" --target omnisight-uav-c6-stack

log "running Yocto Rockchip smoke"
if [[ -x "$yocto_smoke" ]]; then
  run env YOCTO_SMOKE_MACHINE="$yocto_machine" \
    YOCTO_SMOKE_CAMERA_LAYER="$yocto_layer" \
    "$yocto_smoke"
elif [[ -n "${OMNISIGHT_PHASE6_ROOTFS_BIN:-}" ]]; then
  [[ -f "$OMNISIGHT_PHASE6_ROOTFS_BIN" || "$dry_run" -eq 1 ]] ||
    die "OMNISIGHT_PHASE6_ROOTFS_BIN does not exist: $OMNISIGHT_PHASE6_ROOTFS_BIN"
  log "using preverified rootfs binary: $OMNISIGHT_PHASE6_ROOTFS_BIN"
else
  if ((dry_run)); then
    log "would require Yocto smoke or OMNISIGHT_PHASE6_ROOTFS_BIN"
  else
    die "missing Yocto smoke; set OMNISIGHT_PHASE6_YOCTO_SMOKE or OMNISIGHT_PHASE6_ROOTFS_BIN"
  fi
fi

log "running C6.B.4 sensor fusion integration smoke under $qemu_bin"
run env AARCH64_CXX="$cxx" QEMU_AARCH64="$qemu_bin" "$sensor_smoke"

log "running C6.C.3 gimbal integration smoke under $qemu_bin"
run env AARCH64_CXX="$cxx" QEMU_AARCH64="$qemu_bin" "$gimbal_smoke"

log "running C6.D.4 RF telemetry integration smoke under $qemu_bin"
run env AARCH64_CXX="$cxx" QEMU_AARCH64="$qemu_bin" "$rf_smoke"

log "running C6.E.4 UAV CV integration smoke under $qemu_bin"
run env AARCH64_CXX="$cxx" QEMU_AARCH64="$qemu_bin" "$uav_cv_smoke"

log "Case 6 Phase 1B qemu e2e smoke passed"
