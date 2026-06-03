#!/usr/bin/env bash
# [OP-1994] Build and run the UAC2 qemu/ALSA loopback smoke.

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: run_uac_loopback.sh [--dry-run] [--keep-workdir]

Builds tools/embedded/uac_loopback_test.c, runs it under qemu-aarch64-user,
verifies /dev/snd-style nodes, and optionally exercises synthetic ALSA
loopback with arecord -> aplay.

Required for qemu execution:
  AARCH64_CC       default aarch64-linux-gnu-gcc
  QEMU_AARCH64     default qemu-aarch64

Optional:
  UAC_LOOPBACK_ALLOW_HOST_FALLBACK=1   compile/run with host gcc
  UAC_LOOPBACK_ENABLE_KERNEL=1         modprobe UAC2 modules and check /dev/snd
  UAC_LOOPBACK_UAC_MODULE              default usb_f_uac2
  UAC_LOOPBACK_ENABLE_ALSA=1           run arecord -> aplay loopback
  UAC_LOOPBACK_CAPTURE_PCM             default hw:Loopback,1,0
  UAC_LOOPBACK_PLAYBACK_PCM            default hw:Loopback,0,0
EOF
}

die() {
	echo "run_uac_loopback.sh: $*" >&2
	exit 1
}
trap 'die "command failed at line $LINENO"' ERR

log() {
	echo "run_uac_loopback.sh: $*" >&2
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
source_file="$script_dir/uac_loopback_test.c"
workdir="${UAC_LOOPBACK_BUILD_DIR:-$(mktemp -d)}"
dev_root="${UAC_LOOPBACK_DEV_ROOT:-$workdir/dev-snd}"
raw_file="$workdir/uac-loopback.raw"

aarch64_cc="${AARCH64_CC:-aarch64-linux-gnu-gcc}"
qemu_aarch64="${QEMU_AARCH64:-qemu-aarch64}"
host_cc="${CC:-gcc}"
capture_pcm="${UAC_LOOPBACK_CAPTURE_PCM:-hw:Loopback,1,0}"
playback_pcm="${UAC_LOOPBACK_PLAYBACK_PCM:-hw:Loopback,0,0}"
uac_module="${UAC_LOOPBACK_UAC_MODULE:-usb_f_uac2}"
cflags=(-std=c11 -Wall -Wextra -Werror -O2)

cleanup() {
	if [[ -z "${UAC_LOOPBACK_BUILD_DIR:-}" && "$keep_workdir" -eq 0 ]]; then
		rm -rf "$workdir"
	elif [[ "$keep_workdir" -eq 1 ]]; then
		log "kept workdir: $workdir"
	fi
}
trap cleanup EXIT

[[ -f "$source_file" ]] || die "missing helper source: $source_file"
need_cmd mktemp
run mkdir -p "$workdir" "$dev_root"

if [[ "${UAC_LOOPBACK_ENABLE_KERNEL:-0}" == "1" ]]; then
	need_cmd modprobe
	run modprobe libcomposite
	run modprobe "$uac_module"
	run modprobe snd-aloop
	dev_root="/dev/snd"
else
	log "using synthetic /dev/snd fixture; set UAC_LOOPBACK_ENABLE_KERNEL=1 for module load"
	run touch "$dev_root/controlC0" "$dev_root/pcmC0D0c" "$dev_root/pcmC0D0p"
fi

runner=()
output="$workdir/uac_loopback_test"
if ((dry_run)); then
	log "would build helper with $aarch64_cc"
	run "$aarch64_cc" "${cflags[@]}" "$source_file" -o "$output"
	runner=("$qemu_aarch64" -L "<toolchain-sysroot>")
elif command -v "$aarch64_cc" >/dev/null 2>&1 && \
   command -v "$qemu_aarch64" >/dev/null 2>&1; then
	log "building helper with $aarch64_cc"
	run "$aarch64_cc" "${cflags[@]}" "$source_file" -o "$output"
	runner=("$qemu_aarch64")
	if ((!dry_run)); then
		sysroot="$("$aarch64_cc" --print-sysroot 2>/dev/null || true)"
		[[ -n "$sysroot" && -d "$sysroot" ]] && runner+=(-L "$sysroot")
	fi
elif [[ "${UAC_LOOPBACK_ALLOW_HOST_FALLBACK:-0}" == "1" ]]; then
	log "qemu-aarch64 unavailable; running host sanity fallback"
	run "$host_cc" "${cflags[@]}" "$source_file" -o "$output"
else
	cat >&2 <<EOF
qemu-aarch64-user smoke requires both:
  $aarch64_cc
  $qemu_aarch64

Set AARCH64_CC/QEMU_AARCH64 to the platform toolchain, or set
UAC_LOOPBACK_ALLOW_HOST_FALLBACK=1 for host-only local sanity.
EOF
	exit 127
fi

log "verifying UAC2 ALSA node surface"
run "${runner[@]}" "$output" --dev-root "$dev_root" --generate "$raw_file"

if [[ "${UAC_LOOPBACK_ENABLE_ALSA:-0}" == "1" ]]; then
	need_cmd arecord
	need_cmd aplay
	looped_raw="$workdir/uac-loopback-capture.raw"
	log "running synthetic ALSA loopback arecord -> aplay"
	run aplay -D "$playback_pcm" -f S16_LE -r 48000 -c 2 "$raw_file" &
	aplay_pid=$!
	run arecord -D "$capture_pcm" -f S16_LE -r 48000 -c 2 \
		-d 1 "$looped_raw"
	wait "$aplay_pid"
	run "${runner[@]}" "$output" --dev-root "$dev_root" --verify "$looped_raw"
else
	log "skipping arecord/aplay; set UAC_LOOPBACK_ENABLE_ALSA=1 to exercise ALSA loopback"
	run "${runner[@]}" "$output" --dev-root "$dev_root" --verify "$raw_file"
fi

log "UAC2 qemu/ALSA loopback smoke passed"
