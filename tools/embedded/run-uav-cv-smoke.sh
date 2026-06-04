#!/usr/bin/env bash
# [OP-2056] Build and run the Cases 6+7 UAV CV qemu smoke.

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: run-uav-cv-smoke.sh [--dry-run] [--keep-workdir]

Builds tools/embedded/phase6-uav-cv-test.cpp with the Case 6 UAV CV
optical-flow, object-detection, and video-stabilization sources, then runs
the synthetic frame-stream integration test under qemu-aarch64-user.

Required for qemu execution:
  AARCH64_CXX     default aarch64-linux-gnu-g++
  QEMU_AARCH64    default qemu-aarch64
  OPENCV4_PKG_CONFIG default pkg-config

Optional:
  UAV_CV_SMOKE_ALLOW_HOST_FALLBACK=1  compile/run with host c++
  UAV_CV_SMOKE_SKIP_IF_QEMU_MISSING=1 skip instead of failing
EOF
}

die() {
	echo "run-uav-cv-smoke.sh: $*" >&2
	exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
	echo "run-uav-cv-smoke.sh: $*" >&2
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
test_src="$script_dir/phase6-uav-cv-test.cpp"
cv_dir="$repo_root/src/embedded/uav/cv"
flow_src="$cv_dir/optical-flow.cpp"
detect_src="$cv_dir/object-detection.cpp"
stabilize_src="$cv_dir/video-stabilization.cpp"
workdir="${UAV_CV_SMOKE_BUILD_DIR:-$(mktemp -d)}"

aarch64_cxx="${AARCH64_CXX:-aarch64-linux-gnu-g++}"
qemu_aarch64="${QEMU_AARCH64:-qemu-aarch64}"
host_cxx="${CXX:-g++}"
pkg_config="${OPENCV4_PKG_CONFIG:-pkg-config}"
cxxflags=(-std=c++17 -Wall -Wextra -Werror -O2 -I"$cv_dir")

cleanup() {
	if [[ -z "${UAV_CV_SMOKE_BUILD_DIR:-}" && "$keep_workdir" -eq 0 ]]; then
		rm -rf "$workdir"
	elif [[ "$keep_workdir" -eq 1 ]]; then
		log "kept workdir: $workdir"
	fi
}
trap cleanup EXIT

[[ -f "$test_src" ]] || die "missing helper source: $test_src"
[[ -f "$flow_src" ]] || die "missing optical-flow source: $flow_src"
[[ -f "$detect_src" ]] || die "missing object-detection source: $detect_src"
[[ -f "$stabilize_src" ]] || die "missing video-stabilization source: $stabilize_src"
run mkdir -p "$workdir"

opencv_cflags=()
opencv_libs=()
load_opencv_flags() {
	if ((dry_run)); then
		log "would query opencv4 with $pkg_config"
		opencv_cflags=("<opencv4-cflags>")
		opencv_libs=("<opencv4-libs>")
		return 0
	fi
	command -v "$pkg_config" >/dev/null 2>&1 ||
		die "required command not found: $pkg_config"
	"$pkg_config" --exists opencv4 ||
		die "opencv4 pkg-config metadata not found"
	read -r -a opencv_cflags <<<"$("$pkg_config" --cflags opencv4)"
	read -r -a opencv_libs <<<"$("$pkg_config" --libs opencv4)"
}

runner=()
compiler=""
select_toolchain() {
	if ((dry_run)); then
		compiler="$aarch64_cxx"
		runner=("$qemu_aarch64" -L "<toolchain-sysroot>")
	elif command -v "$aarch64_cxx" >/dev/null 2>&1 && \
	   command -v "$qemu_aarch64" >/dev/null 2>&1; then
		compiler="$aarch64_cxx"
		runner=("$qemu_aarch64")
		sysroot="$("$aarch64_cxx" --print-sysroot 2>/dev/null || true)"
		[[ -n "$sysroot" && -d "$sysroot" ]] && runner+=(-L "$sysroot")
	elif [[ "${UAV_CV_SMOKE_ALLOW_HOST_FALLBACK:-0}" == "1" ]]; then
		log "qemu-aarch64 unavailable; running host sanity fallback"
		compiler="$host_cxx"
	elif [[ "${UAV_CV_SMOKE_SKIP_IF_QEMU_MISSING:-0}" == "1" ]]; then
		log "qemu-aarch64 unavailable; skipping UAV CV smoke"
		exit 0
	else
		cat >&2 <<EOF
qemu-aarch64-user smoke requires both:
  $aarch64_cxx
  $qemu_aarch64

Set AARCH64_CXX/QEMU_AARCH64 to the platform toolchain, set
UAV_CV_SMOKE_ALLOW_HOST_FALLBACK=1 for host-only local sanity, or set
UAV_CV_SMOKE_SKIP_IF_QEMU_MISSING=1 to skip on hosts without qemu.
EOF
		exit 127
	fi
}

select_toolchain
load_opencv_flags

output="$workdir/phase6-uav-cv-test"
log "building UAV CV integration helper with $compiler"
run "$compiler" "${cxxflags[@]}" "${opencv_cflags[@]}" "$test_src" \
	"$flow_src" "$detect_src" "$stabilize_src" "${opencv_libs[@]}" \
	-o "$output"

log "running synthetic frame -> optical-flow + object-detect + video-stab smoke"
run "${runner[@]}" "$output"
log "Cases 6+7 UAV CV qemu smoke passed"
