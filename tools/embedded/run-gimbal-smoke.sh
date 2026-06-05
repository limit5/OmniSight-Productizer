#!/usr/bin/env bash
# [OP-2053] Build and run the Case 6/7 gimbal qemu smoke.

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: run-gimbal-smoke.sh [--dry-run] [--keep-workdir]

Builds tools/embedded/phase6-gimbal-test.cpp with the Case 6/7 UAV gimbal
PID and PWM/Hall sources, then runs the synthetic IMU orientation stream
convergence test under qemu-aarch64-user when available.

Required for qemu execution:
  AARCH64_CXX     default aarch64-linux-gnu-g++
  QEMU_AARCH64    default qemu-aarch64

Optional:
  GIMBAL_SMOKE_ALLOW_HOST_FALLBACK=1  compile/run with host c++
  GIMBAL_SMOKE_SKIP_IF_QEMU_MISSING=1 skip instead of failing
EOF
}

die() {
	echo "run-gimbal-smoke.sh: $*" >&2
	exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
	echo "run-gimbal-smoke.sh: $*" >&2
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
test_src="$script_dir/phase6-gimbal-test.cpp"
gimbal_dir="$repo_root/src/embedded/uav/gimbal"
pid_src="$gimbal_dir/pid-controller.cpp"
pwm_src="$gimbal_dir/pwm-output-abstraction.cpp"
workdir="${GIMBAL_SMOKE_BUILD_DIR:-$(mktemp -d)}"

aarch64_cxx="${AARCH64_CXX:-aarch64-linux-gnu-g++}"
qemu_aarch64="${QEMU_AARCH64:-qemu-aarch64}"
host_cxx="${CXX:-g++}"
cxxflags=(-std=c++17 -Wall -Wextra -Werror -O2 -I"$gimbal_dir")

cleanup() {
	if [[ -z "${GIMBAL_SMOKE_BUILD_DIR:-}" && "$keep_workdir" -eq 0 ]]; then
		rm -rf "$workdir"
	elif [[ "$keep_workdir" -eq 1 ]]; then
		log "kept workdir: $workdir"
	fi
}
trap cleanup EXIT

[[ -f "$test_src" ]] || die "missing helper source: $test_src"
[[ -f "$pid_src" ]] || die "missing PID source: $pid_src"
[[ -f "$pwm_src" ]] || die "missing PWM/Hall source: $pwm_src"
run mkdir -p "$workdir"

runner=()
output="$workdir/phase6-gimbal-test"
if ((dry_run)); then
	log "would build helper with $aarch64_cxx"
	run "$aarch64_cxx" "${cxxflags[@]}" "$test_src" "$pid_src" "$pwm_src" -o "$output"
	runner=("$qemu_aarch64" -L "<toolchain-sysroot>")
elif command -v "$aarch64_cxx" >/dev/null 2>&1 && \
   command -v "$qemu_aarch64" >/dev/null 2>&1; then
	log "building helper with $aarch64_cxx"
	run "$aarch64_cxx" "${cxxflags[@]}" "$test_src" "$pid_src" "$pwm_src" -o "$output"
	runner=("$qemu_aarch64")
	sysroot="$("$aarch64_cxx" --print-sysroot 2>/dev/null || true)"
	[[ -n "$sysroot" && -d "$sysroot" ]] && runner+=(-L "$sysroot")
elif [[ "${GIMBAL_SMOKE_ALLOW_HOST_FALLBACK:-0}" == "1" ]]; then
	log "qemu-aarch64 unavailable; running host sanity fallback"
	run "$host_cxx" "${cxxflags[@]}" "$test_src" "$pid_src" "$pwm_src" -o "$output"
elif [[ "${GIMBAL_SMOKE_SKIP_IF_QEMU_MISSING:-0}" == "1" ]]; then
	log "qemu-aarch64 unavailable; skipping gimbal smoke"
	exit 0
else
	cat >&2 <<EOF
qemu-aarch64-user smoke requires both:
  $aarch64_cxx
  $qemu_aarch64

Set AARCH64_CXX/QEMU_AARCH64 to the platform toolchain, set
GIMBAL_SMOKE_ALLOW_HOST_FALLBACK=1 for host-only local sanity, or set
GIMBAL_SMOKE_SKIP_IF_QEMU_MISSING=1 to skip on hosts without qemu.
EOF
	exit 127
fi

log "running synthetic IMU -> PID -> PWM/Hall convergence smoke"
run "${runner[@]}" "$output"
log "Case 6/7 gimbal qemu smoke passed"
