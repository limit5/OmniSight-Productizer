#!/usr/bin/env bash
# [OP-1938] Build and run the UVC XU dispatcher lookup smoke.

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${SCRIPT_DIR}/uvc_xu_dispatch_test.c"
BUILD_DIR="${UVC_DISPATCH_SMOKE_BUILD_DIR:-$(mktemp -d)}"
SOCKET_DIR="${UVC_DISPATCH_SMOKE_SOCKET_DIR:-$(mktemp -d)}"
SOCKET_PATH="${SOCKET_DIR}/uvc-xu-dispatcher.sock"

AARCH64_CC="${AARCH64_CC:-aarch64-linux-gnu-gcc}"
QEMU_AARCH64="${QEMU_AARCH64:-qemu-aarch64}"
HOST_CC="${CC:-gcc}"

CFLAGS=(
	-std=c11
	-Wall
	-Wextra
	-Werror
	-O2
)

cleanup() {
	rm -f "$SOCKET_PATH"
	if [[ -z "${UVC_DISPATCH_SMOKE_BUILD_DIR:-}" ]]; then
		rm -rf "$BUILD_DIR"
	fi
	if [[ -z "${UVC_DISPATCH_SMOKE_SOCKET_DIR:-}" ]]; then
		rm -rf "$SOCKET_DIR"
	fi
}
trap cleanup EXIT

mkdir -p "$BUILD_DIR" "$SOCKET_DIR"

run_smoke() {
	local cc="$1"
	local runner="$2"
	local output="$3"

	"$cc" "${CFLAGS[@]}" "$SOURCE" -o "$output"
	if [[ -n "$runner" ]]; then
		"$runner" "$output" --socket "$SOCKET_PATH" --self-test
	else
		"$output" --socket "$SOCKET_PATH" --self-test
	fi
}

if command -v "$AARCH64_CC" >/dev/null 2>&1 && \
   command -v "$QEMU_AARCH64" >/dev/null 2>&1; then
	run_smoke "$AARCH64_CC" "$QEMU_AARCH64" \
		"${BUILD_DIR}/uvc_xu_dispatch_test.aarch64"
	exit 0
fi

if [[ "${UVC_DISPATCH_SMOKE_ALLOW_HOST_FALLBACK:-0}" == "1" ]]; then
	echo "qemu-aarch64-user toolchain unavailable; running host sanity fallback" >&2
	run_smoke "$HOST_CC" "" "${BUILD_DIR}/uvc_xu_dispatch_test.host"
	exit 0
fi

cat >&2 <<EOF
qemu-aarch64-user smoke requires both:
  ${AARCH64_CC}
  ${QEMU_AARCH64}

Set AARCH64_CC/QEMU_AARCH64 to the platform toolchain, or set
UVC_DISPATCH_SMOKE_ALLOW_HOST_FALLBACK=1 for host-only local sanity.
EOF
exit 127
