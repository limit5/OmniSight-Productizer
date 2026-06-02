#!/usr/bin/env bash
# OP-1945 — UART-side post-flash smoke for USB-UVC dispatch bring-up.
set -euo pipefail

BAUD=115200
TIMEOUT=60
DEVICE=""
LOGIN_USER="${OMNISIGHT_SERIAL_SMOKE_LOGIN_USER:-root}"
DISPATCH_URL="${OMNISIGHT_SERIAL_SMOKE_DISPATCH_URL:-http://localhost:8000/dispatch?vendor=ft-c600}"
DISPATCH_EXPECT="${OMNISIGHT_SERIAL_SMOKE_DISPATCH_EXPECT:-ok}"

usage() {
  cat <<'EOF'
Usage: tools/embedded/serial_smoke.sh --device /dev/ttyUSB0 [options]

Required:
  --device PATH             Serial console device. Must be explicit.

Options:
  --baud RATE               UART baud rate (default: 115200).
  --timeout SECONDS         Per-step timeout (default: 60).
  --login-user USER         User sent when a login prompt appears (default: root).
  --dispatch-url URL        Target-side dispatch URL
                            (default: http://localhost:8000/dispatch?vendor=ft-c600).
  --dispatch-expect TEXT    Text expected in dispatch response (default: ok).
  -h, --help                Show this help.
EOF
}

log() { printf '[serial-smoke] %s\n' "$*"; }
err() { printf '[serial-smoke] ERROR: %s\n' "$*" >&2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --device)
      DEVICE="${2:-}"
      shift 2
      ;;
    --baud)
      BAUD="${2:-}"
      shift 2
      ;;
    --timeout)
      TIMEOUT="${2:-}"
      shift 2
      ;;
    --login-user)
      LOGIN_USER="${2:-}"
      shift 2
      ;;
    --dispatch-url)
      DISPATCH_URL="${2:-}"
      shift 2
      ;;
    --dispatch-expect)
      DISPATCH_EXPECT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "unknown argument: $1"
      usage >&2
      exit 2
      ;;
  esac
done

[ -n "$DEVICE" ] || { err "--device is required; pass /dev/ttyUSB0 explicitly"; exit 2; }
[ -e "$DEVICE" ] || { err "serial device not found: $DEVICE"; exit 2; }

command -v stty >/dev/null 2>&1 || { err "stty not found"; exit 2; }

read_until() {
  local pattern="$1"
  local timeout="$2"
  local deadline=$(( $(date +%s) + timeout ))
  local chunk=""
  local buffer=""

  while [ "$(date +%s)" -lt "$deadline" ]; do
    if IFS= read -r -t 0.5 -n 256 -u 3 chunk; then
      buffer="${buffer}${chunk}"
      printf '%s' "$chunk" >&2
      if [[ "$buffer" =~ $pattern ]]; then
        printf '%s' "$buffer"
        return 0
      fi
    fi
  done

  printf '%s' "$buffer"
  return 1
}

send_line() {
  printf '%s\r\n' "$1" >&3
}

target_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

target_run() {
  local name="$1"
  local cmd="$2"
  local marker="__OMNISIGHT_SERIAL_SMOKE_${name}_$$_${RANDOM}__"
  local output=""
  local rc=""

  log "target: $name"
  send_line "printf '\\n${marker}:BEGIN\\n'; ${cmd}; rc=\$?; printf '\\n${marker}:RC:%s\\n' \"\$rc\""
  output="$(read_until "${marker}:RC:[0-9]+" "$TIMEOUT")" || {
    err "timeout waiting for $name"
    return 1
  }
  rc="$(printf '%s' "$output" | sed -n "s/.*${marker}:RC:\\([0-9][0-9]*\\).*/\\1/p" | tail -1)"
  [ "${rc:-1}" = "0" ] || { err "$name failed on target with rc=${rc:-unknown}"; return 1; }
  printf '%s' "$output"
}

exec 3<>"$DEVICE"
stty -F "$DEVICE" "$BAUD" cs8 -cstopb -parenb -ixon -ixoff -echo raw min 0 time 1

log "waiting for serial login or shell prompt on $DEVICE at ${BAUD}"
prompt="$(read_until '(login:|[#>$] )' "$TIMEOUT")" || {
  err "target did not reach a login or shell prompt"
  exit 1
}

if [[ "$prompt" =~ login: ]]; then
  log "login prompt observed; sending user '$LOGIN_USER'"
  send_line "$LOGIN_USER"
  read_until '([#>$] )' "$TIMEOUT" >/dev/null || {
    err "target did not reach a shell prompt after login"
    exit 1
  }
fi

lsusb_output="$(target_run lsusb "lsusb")"
[ -n "$(printf '%s' "$lsusb_output" | sed '/OMNISIGHT_SERIAL_SMOKE/d' | tr -d '[:space:]')" ] || {
  err "lsusb returned no devices"
  exit 1
}

video_output="$(target_run video_enum "ls /dev/video*")"
printf '%s' "$video_output" | grep -q '/dev/video' || {
  err "no /dev/video* nodes reported"
  exit 1
}

dispatch_output="$(target_run dispatch_verify "curl -fsS $(target_quote "$DISPATCH_URL")")"
printf '%s' "$dispatch_output" | grep -qi -- "$DISPATCH_EXPECT" || {
  err "dispatch response did not contain expected text: $DISPATCH_EXPECT"
  exit 1
}

log "PASS: UART prompt, UVC enumeration, and vendor dispatch verified"
