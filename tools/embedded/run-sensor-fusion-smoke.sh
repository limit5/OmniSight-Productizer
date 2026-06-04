#!/usr/bin/env bash
# [OP-2048] Build and run the Case 6 sensor fusion qemu smoke.

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: run-sensor-fusion-smoke.sh [--dry-run] [--keep-workdir]

Builds tools/embedded/phase6-sensor-fusion-test.cpp with the Case 6 UAV
GPS parser and EKF sources, then runs the synthetic IMU/GPS/barometer
convergence test under qemu-aarch64-user when available.

Required for qemu execution:
  AARCH64_CXX     default aarch64-linux-gnu-g++
  QEMU_AARCH64    default qemu-aarch64

Optional:
  SENSOR_FUSION_SMOKE_ALLOW_HOST_FALLBACK=1  compile/run with host c++
  SENSOR_FUSION_SMOKE_SKIP_IF_QEMU_MISSING=1 skip instead of failing
EOF
}

die() {
	echo "run-sensor-fusion-smoke.sh: $*" >&2
	exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
	echo "run-sensor-fusion-smoke.sh: $*" >&2
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

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
test_src="$script_dir/phase6-sensor-fusion-test.cpp"
sensor_dir="$repo_root/src/embedded/uav/sensors"
gps_src="$sensor_dir/gps-nmea-driver.cpp"
ekf_src="$sensor_dir/ekf-flight-integration.cpp"
workdir="${SENSOR_FUSION_SMOKE_BUILD_DIR:-$(mktemp -d)}"

aarch64_cxx="${AARCH64_CXX:-aarch64-linux-gnu-g++}"
qemu_aarch64="${QEMU_AARCH64:-qemu-aarch64}"
host_cxx="${CXX:-g++}"
cxxflags=(-std=c++17 -Wall -Wextra -Werror -O2 -I"$sensor_dir")

cleanup() {
	if [[ -z "${SENSOR_FUSION_SMOKE_BUILD_DIR:-}" && "$keep_workdir" -eq 0 ]]; then
		rm -rf "$workdir"
	elif [[ "$keep_workdir" -eq 1 ]]; then
		log "kept workdir: $workdir"
	fi
}
trap cleanup EXIT

[[ -f "$test_src" ]] || die "missing helper source: $test_src"
[[ -f "$gps_src" ]] || die "missing GPS driver source: $gps_src"
[[ -f "$ekf_src" ]] || die "missing EKF source: $ekf_src"
run mkdir -p "$workdir"

runner=()
output="$workdir/phase6-sensor-fusion-test"
if ((dry_run)); then
	log "would build helper with $aarch64_cxx"
	run "$aarch64_cxx" "${cxxflags[@]}" "$test_src" "$gps_src" "$ekf_src" -o "$output"
	runner=("$qemu_aarch64" -L "<toolchain-sysroot>")
elif command -v "$aarch64_cxx" >/dev/null 2>&1 && \
   command -v "$qemu_aarch64" >/dev/null 2>&1; then
	log "building helper with $aarch64_cxx"
	run "$aarch64_cxx" "${cxxflags[@]}" "$test_src" "$gps_src" "$ekf_src" -o "$output"
	runner=("$qemu_aarch64")
	sysroot="$("$aarch64_cxx" --print-sysroot 2>/dev/null || true)"
	[[ -n "$sysroot" && -d "$sysroot" ]] && runner+=(-L "$sysroot")
elif [[ "${SENSOR_FUSION_SMOKE_ALLOW_HOST_FALLBACK:-0}" == "1" ]]; then
	log "qemu-aarch64 unavailable; running host sanity fallback"
	run "$host_cxx" "${cxxflags[@]}" "$test_src" "$gps_src" "$ekf_src" -o "$output"
elif [[ "${SENSOR_FUSION_SMOKE_SKIP_IF_QEMU_MISSING:-0}" == "1" ]]; then
	log "qemu-aarch64 unavailable; skipping sensor fusion smoke"
	exit 0
else
	cat >&2 <<EOF
qemu-aarch64-user smoke requires both:
  $aarch64_cxx
  $qemu_aarch64

Set AARCH64_CXX/QEMU_AARCH64 to the platform toolchain, set
SENSOR_FUSION_SMOKE_ALLOW_HOST_FALLBACK=1 for host-only local sanity, or set
SENSOR_FUSION_SMOKE_SKIP_IF_QEMU_MISSING=1 to skip on hosts without qemu.
EOF
	exit 127
fi

log "running synthetic IMU/GPS/barometer -> EKF convergence smoke"
run "${runner[@]}" "$output"
log "Case 6 sensor fusion qemu smoke passed"
