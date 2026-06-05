#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: phase7-qemu-e2e-smoke.sh [--dry-run] [--keep-workdir]

Runs the Case 7 Phase 1B qemu-only close gate:
  Rockchip RK3588 toolchain -> full POS stack build -> Yocto smoke
  -> qemu-aarch64 execution of EMV/PED/cryptogram, HSM, P2PE,
  scanner/printer, NFC, and MSR smoke targets.

Required for real execution:
  OMNISIGHT_TOOLCHAIN_ROOT  writable toolchain install root

Optional overrides:
  OMNISIGHT_PHASE7_TOOLCHAIN_ID       default rockchip-rk35xx-aarch64-gcc-11
  OMNISIGHT_PHASE7_TARGET_TRIPLE      default aarch64-linux-gnu
  OMNISIGHT_PHASE7_CMAKE_TOOLCHAIN_FILE optional vendor CMake toolchain file
  OMNISIGHT_PHASE7_YOCTO_SMOKE        default tools/embedded/yocto_smoke.sh
  OMNISIGHT_PHASE7_YOCTO_MACHINE      default rk3588
  OMNISIGHT_PHASE7_ROOTFS_BIN         preverified rootfs binary fallback
EOF
}

die() {
  echo "phase7-qemu-e2e-smoke.sh: $*" >&2
  exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
  echo "phase7-qemu-e2e-smoke.sh: $*" >&2
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
toolchain_id="${OMNISIGHT_PHASE7_TOOLCHAIN_ID:-rockchip-rk35xx-aarch64-gcc-11}"
target_triple="${OMNISIGHT_PHASE7_TARGET_TRIPLE:-aarch64-linux-gnu}"
verify_toolchain="$repo_root/tools/embedded/verify_toolchain.sh"
yocto_layer="$repo_root/yocto/meta-omnisight-camera"
yocto_smoke="${OMNISIGHT_PHASE7_YOCTO_SMOKE:-$repo_root/tools/embedded/yocto_smoke.sh}"
yocto_machine="${OMNISIGHT_PHASE7_YOCTO_MACHINE:-rk3588}"
qemu_bin="${QEMU_AARCH64:-qemu-aarch64}"
workdir=""

pos_dirs=(
  "$repo_root/src/embedded/pos/emv"
  "$repo_root/src/embedded/pos/emv/vendor"
  "$repo_root/src/embedded/pos/hsm"
  "$repo_root/src/embedded/pos/p2pe"
  "$repo_root/src/embedded/pos/scanner"
  "$repo_root/src/embedded/pos/printer"
  "$repo_root/src/embedded/pos/integration"
  "$repo_root/src/embedded/pos/nfc"
  "$repo_root/src/embedded/pos/msr"
)

cmake_smoke_targets=(
  omnisight-pos-emv-ped-pin-entry-smoke
  omnisight-pos-emv-cryptogram-generator-smoke
  omnisight-pos-emv-verifone-adapter-smoke
  omnisight-pos-p2pe-tdes-dukpt-smoke
  omnisight-pos-p2pe-aes-dukpt-smoke
  omnisight-pos-p2pe-ksn-management-smoke
  omnisight-pos-zebra-snapi-adapter-smoke
  omnisight-pos-honeywell-sdk-adapter-smoke
  omnisight-pos-printer-escpos-smoke
  omnisight-pos-scanner-printer-unified-smoke
  omnisight-pos-msr-magstripe-driver-smoke
)

runtime_smoke_bins=(
  "${cmake_smoke_targets[@]}"
  omnisight-pos-nfc-pn532-driver-smoke
  omnisight-pos-nfc-pn5180-driver-smoke
  phase7-hsm-test
)

cleanup() {
  if [[ -n "$workdir" && "$keep_workdir" -eq 0 ]]; then
    rm -rf "$workdir"
  elif [[ -n "$workdir" ]]; then
    log "kept workdir: $workdir"
  fi
}
trap cleanup EXIT

need_executable "$verify_toolchain" "missing executable"
need_file "$yocto_layer/conf/layer.conf" "missing Yocto layer"
need_file "$repo_root/tools/embedded/phase7-hsm-test.cpp" \
  "missing C7.B.5 HSM integration helper"
need_file "$repo_root/tools/embedded/hsm-sdk-mock.h" \
  "missing C7.B.5 HSM SDK mock registry"
for dir in "${pos_dirs[@]}"; do
  need_file "$dir/CMakeLists.txt" "missing Case 7 CMake fragment"
done

if [[ -n "${OMNISIGHT_PHASE7_CMAKE_TOOLCHAIN_FILE:-}" ]]; then
  need_file "$OMNISIGHT_PHASE7_CMAKE_TOOLCHAIN_FILE" \
    "missing vendor CMake toolchain file"
fi

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
compiler="$toolchain_root/$toolchain_id/bin/$target_triple-gcc"
cxx_compiler="$toolchain_root/$toolchain_id/bin/$target_triple-g++"

log "verifying Rockchip RK3588 toolchain $toolchain_id"
run env OMNISIGHT_TOOLCHAIN_ROOT="$toolchain_root" "$verify_toolchain" "$toolchain_id"
[[ -x "$compiler" || "$dry_run" -eq 1 ]] || die "compiler missing after toolchain verification: $compiler"
[[ -x "$cxx_compiler" || "$dry_run" -eq 1 ]] || die "C++ compiler missing after toolchain verification: $cxx_compiler"

log "building Case 7 POS smoke targets with $target_triple compiler"
cat >"$workdir/CMakeLists.txt" <<EOF
cmake_minimum_required(VERSION 3.16)
project(phase7_qemu_e2e_smoke CXX)
include("$repo_root/src/embedded/pos/emv/CMakeLists.txt")
include("$repo_root/src/embedded/pos/emv/vendor/CMakeLists.txt")
include("$repo_root/src/embedded/pos/p2pe/CMakeLists.txt")
include("$repo_root/src/embedded/pos/scanner/CMakeLists.txt")
include("$repo_root/src/embedded/pos/printer/CMakeLists.txt")
include("$repo_root/src/embedded/pos/integration/CMakeLists.txt")
include("$repo_root/src/embedded/pos/nfc/CMakeLists.txt")
include("$repo_root/src/embedded/pos/msr/CMakeLists.txt")
EOF

cmake_args=(
  -S "$workdir"
  -B "$workdir/build"
  -DCMAKE_CXX_COMPILER="$cxx_compiler"
  -DCMAKE_RUNTIME_OUTPUT_DIRECTORY="$workdir/bin"
)
if [[ -n "${OMNISIGHT_PHASE7_CMAKE_TOOLCHAIN_FILE:-}" ]]; then
  cmake_args+=(-DCMAKE_TOOLCHAIN_FILE="$OMNISIGHT_PHASE7_CMAKE_TOOLCHAIN_FILE")
fi
if ((dry_run)); then
  cmake_args+=(-DCMAKE_SYSROOT="<toolchain-sysroot>")
else
  sysroot="$("$cxx_compiler" --print-sysroot 2>/dev/null || true)"
  [[ -n "$sysroot" && -d "$sysroot" ]] && cmake_args+=(-DCMAKE_SYSROOT="$sysroot")
fi
run cmake "${cmake_args[@]}"
for target in "${cmake_smoke_targets[@]}"; do
  run cmake --build "$workdir/build" --target "$target"
done

log "building NFC smoke helpers with source-local smoke macros"
run "$cxx_compiler" -std=c++17 -Wall -Wextra -Werror \
  -DOMNISIGHT_PN532_DRIVER_SMOKE_MAIN=1 \
  -I"$repo_root/src/embedded/pos/nfc" \
  "$repo_root/src/embedded/pos/nfc/pn532-driver.cpp" \
  -o "$workdir/bin/omnisight-pos-nfc-pn532-driver-smoke"
run "$cxx_compiler" -std=c++17 -Wall -Wextra -Werror \
  -DOMNISIGHT_PN5180_DRIVER_SMOKE_MAIN=1 \
  -I"$repo_root/src/embedded/pos/nfc" \
  "$repo_root/src/embedded/pos/nfc/pn5180-driver.cpp" \
  -o "$workdir/bin/omnisight-pos-nfc-pn5180-driver-smoke"

log "building C7.B.5 unified HSM client smoke helper"
run "$cxx_compiler" -std=c++17 -Wall -Wextra -Werror -O2 \
  -I"$repo_root/tools/embedded" \
  -I"$repo_root/src/embedded/pos/hsm" \
  "$repo_root/tools/embedded/phase7-hsm-test.cpp" \
  "$repo_root/src/embedded/pos/hsm/hsm-client-registry.cpp" \
  "$repo_root/src/embedded/pos/hsm/thales-nshield-client.cpp" \
  "$repo_root/src/embedded/pos/hsm/utimaco-client.cpp" \
  "$repo_root/src/embedded/pos/hsm/safenet-luna-client.cpp" \
  -o "$workdir/bin/phase7-hsm-test"

log "running Yocto RK3588 smoke"
if [[ -x "$yocto_smoke" ]]; then
  run env YOCTO_SMOKE_MACHINE="$yocto_machine" "$yocto_smoke"
elif [[ -n "${OMNISIGHT_PHASE7_ROOTFS_BIN:-}" ]]; then
  [[ -f "$OMNISIGHT_PHASE7_ROOTFS_BIN" || "$dry_run" -eq 1 ]] ||
    die "OMNISIGHT_PHASE7_ROOTFS_BIN does not exist: $OMNISIGHT_PHASE7_ROOTFS_BIN"
  log "using preverified rootfs binary: $OMNISIGHT_PHASE7_ROOTFS_BIN"
else
  if ((dry_run)); then
    log "would require Yocto smoke or OMNISIGHT_PHASE7_ROOTFS_BIN"
  else
    die "missing Yocto smoke; set OMNISIGHT_PHASE7_YOCTO_SMOKE or OMNISIGHT_PHASE7_ROOTFS_BIN"
  fi
fi

if ((dry_run)); then
  qemu_args=("$qemu_bin" -L "<toolchain-sysroot>")
else
  qemu_args=("$qemu_bin")
  [[ -n "${sysroot:-}" && -d "$sysroot" ]] && qemu_args+=(-L "$sysroot")
fi

log "running Case 7 POS smoke targets under $qemu_bin"
for target in "${runtime_smoke_bins[@]}"; do
  run "${qemu_args[@]}" "$workdir/bin/$target"
done

log "Case 7 Phase 1B qemu e2e smoke passed"
