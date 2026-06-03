#!/usr/bin/env bash
# [OP-2006] Build and run the SIP PBX interop smoke.

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: run_sip_interop.sh [--dry-run] [--keep-workdir]

Builds tools/embedded/sip_pbx_interop_test.c, starts a reference SIP PBX
backend through docker compose, then runs REGISTER refresh, INVITE, RTP media,
and BYE checks under qemu-aarch64-user when available.

Required for qemu execution:
  AARCH64_CC       default aarch64-linux-gnu-gcc
  QEMU_AARCH64     default qemu-aarch64

Optional:
  SIP_INTEROP_BACKEND=asterisk|freeswitch|self-test   default asterisk
  SIP_INTEROP_ALLOW_HOST_FALLBACK=1                   compile/run with host gcc
  SIP_INTEROP_SKIP_COMPOSE=1                          target an already running PBX
  SIP_INTEROP_REQUIRE_RTP_RX=1                        require inbound RTP from PBX
  SIP_INTEROP_SERVER_HOST                             default 127.0.0.1
  SIP_INTEROP_SERVER_PORT                             default 5060
  SIP_INTEROP_USER                                    default 1001
  SIP_INTEROP_DOMAIN                                  default server host
  SIP_INTEROP_CALLEE                                  default 600 (Asterisk), 9196 (FreeSWITCH)
EOF
}

die() {
	echo "run_sip_interop.sh: $*" >&2
	exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
	echo "run_sip_interop.sh: $*" >&2
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
source_file="$script_dir/sip_pbx_interop_test.c"
compose_file="$script_dir/docker-compose.asterisk.yml"
workdir="${SIP_INTEROP_BUILD_DIR:-$(mktemp -d)}"

aarch64_cc="${AARCH64_CC:-aarch64-linux-gnu-gcc}"
qemu_aarch64="${QEMU_AARCH64:-qemu-aarch64}"
host_cc="${CC:-gcc}"
backend="${SIP_INTEROP_BACKEND:-asterisk}"
server_host="${SIP_INTEROP_SERVER_HOST:-127.0.0.1}"
server_port="${SIP_INTEROP_SERVER_PORT:-5060}"
user="${SIP_INTEROP_USER:-1001}"
domain="${SIP_INTEROP_DOMAIN:-$server_host}"
timeout_ms="${SIP_INTEROP_TIMEOUT_MS:-5000}"
rtp_packets="${SIP_INTEROP_RTP_PACKETS:-8}"
cflags=(-std=c11 -Wall -Wextra -Werror -O2)
compose_project="${SIP_INTEROP_COMPOSE_PROJECT:-omnisight-sip-interop}"
compose_started=0

case "$backend" in
	asterisk) callee="${SIP_INTEROP_CALLEE:-600}" ;;
	freeswitch) callee="${SIP_INTEROP_CALLEE:-9196}" ;;
	self-test) callee="${SIP_INTEROP_CALLEE:-600}" ;;
	*) die "unknown SIP_INTEROP_BACKEND: $backend" ;;
esac

cleanup() {
	if [[ "$compose_started" -eq 1 ]]; then
		run docker compose -p "$compose_project" -f "$compose_file" down -v
	fi
	if [[ -z "${SIP_INTEROP_BUILD_DIR:-}" && "$keep_workdir" -eq 0 ]]; then
		rm -rf "$workdir"
	elif [[ "$keep_workdir" -eq 1 ]]; then
		log "kept workdir: $workdir"
	fi
}
trap cleanup EXIT

[[ -f "$source_file" ]] || die "missing helper source: $source_file"
[[ -f "$compose_file" ]] || die "missing compose fixture: $compose_file"
need_cmd mktemp
run mkdir -p "$workdir"

runner=()
output="$workdir/sip_pbx_interop_test"
if ((dry_run)); then
	log "would build helper with $aarch64_cc"
	run "$aarch64_cc" "${cflags[@]}" "$source_file" -o "$output"
	runner=("$qemu_aarch64" -L "<toolchain-sysroot>")
elif command -v "$aarch64_cc" >/dev/null 2>&1 && \
   command -v "$qemu_aarch64" >/dev/null 2>&1; then
	log "building helper with $aarch64_cc"
	run "$aarch64_cc" "${cflags[@]}" "$source_file" -o "$output"
	runner=("$qemu_aarch64")
	sysroot="$("$aarch64_cc" --print-sysroot 2>/dev/null || true)"
	[[ -n "$sysroot" && -d "$sysroot" ]] && runner+=(-L "$sysroot")
elif [[ "${SIP_INTEROP_ALLOW_HOST_FALLBACK:-0}" == "1" ]]; then
	log "qemu-aarch64 unavailable; running host sanity fallback"
	run "$host_cc" "${cflags[@]}" "$source_file" -o "$output"
else
	cat >&2 <<EOF
qemu-aarch64-user smoke requires both:
  $aarch64_cc
  $qemu_aarch64

Set AARCH64_CC/QEMU_AARCH64 to the platform toolchain, or set
SIP_INTEROP_ALLOW_HOST_FALLBACK=1 for host-only local sanity.
EOF
	exit 127
fi

if [[ "$backend" == "self-test" ]]; then
	log "running built-in SIP PBX self-test"
	run "${runner[@]}" "$output" --self-test --timeout-ms "$timeout_ms" \
		--rtp-packets "$rtp_packets"
	exit 0
fi

if [[ "${SIP_INTEROP_SKIP_COMPOSE:-0}" != "1" ]]; then
	need_cmd docker
	log "starting $backend backend with docker compose"
	if [[ "$backend" == "freeswitch" ]]; then
		run docker compose -p "$compose_project" -f "$compose_file" \
			--profile freeswitch up -d freeswitch
	else
		run docker compose -p "$compose_project" -f "$compose_file" up -d asterisk
	fi
	compose_started=1
	run sleep "${SIP_INTEROP_BACKEND_READY_SECONDS:-8}"
else
	log "skipping docker compose; targeting existing $backend at $server_host:$server_port"
fi

args=(
	--server-host "$server_host"
	--server-port "$server_port"
	--user "$user"
	--domain "$domain"
	--callee "$callee"
	--timeout-ms "$timeout_ms"
	--rtp-packets "$rtp_packets"
)
if [[ "${SIP_INTEROP_REQUIRE_RTP_RX:-1}" == "1" ]]; then
	args+=(--require-rtp-rx)
fi

log "running SIP $backend interop cycle"
run "${runner[@]}" "$output" "${args[@]}"
log "SIP PBX interop smoke passed for $backend"
