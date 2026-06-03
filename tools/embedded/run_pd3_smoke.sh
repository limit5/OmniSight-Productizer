#!/usr/bin/env bash
# [OP-2003] Build and run the PD3.0/UCSI qemu smoke.

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: run_pd3_smoke.sh [--dry-run] [--keep-workdir]

Builds the real charging-daemon plus tools/embedded/pd3_integration_test.c,
runs both under qemu-aarch64-user when available, and verifies a synthetic
/sys/class/typec PD3.0 contract exposed by a stub UCSI controller fixture.

Required for qemu execution:
  AARCH64_CC       default aarch64-linux-gnu-gcc
  QEMU_AARCH64     default qemu-aarch64

Optional:
  PD3_SMOKE_ALLOW_HOST_FALLBACK=1      compile/run with host gcc
  PD3_SMOKE_SKIP_IF_QEMU_MISSING=1     skip instead of failing when qemu is absent
EOF
}

die() {
	echo "run_pd3_smoke.sh: $*" >&2
	exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
	echo "run_pd3_smoke.sh: $*" >&2
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
test_src="$script_dir/pd3_integration_test.c"
daemon_src="$repo_root/src/embedded/typec/charging-daemon.c"
daemon_inc="$repo_root/src/embedded/typec"
workdir="${PD3_SMOKE_BUILD_DIR:-$(mktemp -d)}"
sysfs_root="${PD3_SMOKE_SYSFS_ROOT:-$workdir/typec}"
socket_path="${PD3_SMOKE_SOCKET_PATH:-$workdir/charging-daemon.sock}"

aarch64_cc="${AARCH64_CC:-aarch64-linux-gnu-gcc}"
qemu_aarch64="${QEMU_AARCH64:-qemu-aarch64}"
host_cc="${CC:-gcc}"
cflags=(-std=c11 -D_GNU_SOURCE -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Werror -O2)
daemon_pid=""

cleanup() {
	if [[ -n "$daemon_pid" ]] && kill -0 "$daemon_pid" >/dev/null 2>&1; then
		kill "$daemon_pid" >/dev/null 2>&1 || true
		wait "$daemon_pid" >/dev/null 2>&1 || true
	fi
	if [[ -z "${PD3_SMOKE_BUILD_DIR:-}" && "$keep_workdir" -eq 0 ]]; then
		rm -rf "$workdir"
	elif [[ "$keep_workdir" -eq 1 ]]; then
		log "kept workdir: $workdir"
	fi
}
trap cleanup EXIT

[[ -f "$test_src" ]] || die "missing helper source: $test_src"
[[ -f "$daemon_src" ]] || die "missing charging-daemon source: $daemon_src"
run mkdir -p "$workdir" "$sysfs_root/port0" "$sysfs_root/port0-partner"

create_stub_typec_fixture() {
	local port="$sysfs_root/port0"
	local partner="$sysfs_root/port0-partner"

	if ((dry_run)); then
		log "would create synthetic TypeC/UCSI fixture under $sysfs_root"
		return 0
	fi

	run printf 'sink\n' >"$port/power_role"
	run printf 'device\n' >"$port/data_role"
	run printf 'dual\n' >"$port/port_type"
	run printf 'pd-contract-negotiated\n' >"$port/ucsi_event"
	run printf '0\n' >"$port/request_voltage"
	run printf '0\n' >"$port/request_current"
	run printf '3.0\n' >"$partner/usb_power_delivery_revision"
	run printf 'yes\n' >"$partner/supports_usb_power_delivery"
	run tee "$partner/source_capabilities" >/dev/null <<'EOF'
5000 3000
9000 3000
15000 3000
20000 5000
28000 5000
EOF
}

select_toolchain() {
	runner=()
	compiler=""
	if ((dry_run)); then
		compiler="$aarch64_cc"
		runner=("$qemu_aarch64" -L "<toolchain-sysroot>")
	elif command -v "$aarch64_cc" >/dev/null 2>&1 && \
	   command -v "$qemu_aarch64" >/dev/null 2>&1; then
		compiler="$aarch64_cc"
		runner=("$qemu_aarch64")
		sysroot="$("$aarch64_cc" --print-sysroot 2>/dev/null || true)"
		[[ -n "$sysroot" && -d "$sysroot" ]] && runner+=(-L "$sysroot")
	elif [[ "${PD3_SMOKE_ALLOW_HOST_FALLBACK:-0}" == "1" ]]; then
		log "qemu-aarch64 unavailable; running host sanity fallback"
		compiler="$host_cc"
	elif [[ "${PD3_SMOKE_SKIP_IF_QEMU_MISSING:-0}" == "1" ]]; then
		log "qemu-aarch64 unavailable; skipping PD3 smoke"
		exit 0
	else
		cat >&2 <<EOF
qemu-aarch64-user smoke requires both:
  $aarch64_cc
  $qemu_aarch64

Set AARCH64_CC/QEMU_AARCH64 to the platform toolchain, set
PD3_SMOKE_ALLOW_HOST_FALLBACK=1 for host-only local sanity, or set
PD3_SMOKE_SKIP_IF_QEMU_MISSING=1 to skip on hosts without qemu.
EOF
		exit 127
	fi
}

wait_for_socket() {
	local i

	for i in $(seq 1 50); do
		[[ -S "$socket_path" ]] && return 0
		sleep 0.1
	done
	die "charging-daemon socket did not appear: $socket_path"
}

create_stub_typec_fixture
select_toolchain

daemon_bin="$workdir/charging-daemon"
test_bin="$workdir/pd3_integration_test"

log "building charging-daemon with $compiler"
run "$compiler" "${cflags[@]}" -I"$daemon_inc" "$daemon_src" -o "$daemon_bin"
log "building PD3 integration verifier with $compiler"
run "$compiler" "${cflags[@]}" "$test_src" -o "$test_bin"

log "starting charging-daemon against synthetic TypeC sysfs"
if ((dry_run)); then
	run "${runner[@]}" "$daemon_bin" "$socket_path" "$sysfs_root"
else
	"${runner[@]}" "$daemon_bin" "$socket_path" "$sysfs_root" &
	daemon_pid="$!"
	wait_for_socket
fi

log "verifying PD3 contract through charging-daemon socket"
run "${runner[@]}" "$test_bin" \
	--sysfs-root "$sysfs_root" \
	--socket "$socket_path" \
	--port port0 \
	--expect-contract contract=20000mV/5000mA \
	--expect-event pd-contract-negotiated

if ((!dry_run)); then
	grep -qx '20000' "$sysfs_root/port0/request_voltage" ||
		die "charging-daemon did not request 20000mV"
	grep -qx '5000' "$sysfs_root/port0/request_current" ||
		die "charging-daemon did not request 5000mA"
fi

log "PD3.0/UCSI qemu smoke passed"
