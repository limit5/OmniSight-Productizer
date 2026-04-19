#!/usr/bin/env bash
# enable_autostart.sh — make the compose prod stack survive a WSL / host reboot.
#
# Runs the WSL-side half of the autostart setup (layers 2–4 of
# docs/ops/autostart_wsl.md). Layer 1 (Windows Task Scheduler
# "At startup" → boot the WSL distro) must be done from Windows
# and is NOT scriptable from inside WSL — see the doc.
#
# Idempotent: re-running is safe.

set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
USER_NAME="$(id -un)"

if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_OK= C_WARN= C_ERR= C_OFF=
fi
ok()   { printf '  %s[OK]%s   %s\n' "$C_OK"   "$C_OFF" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '  %s[FAIL]%s %s\n' "$C_ERR"  "$C_OFF" "$*" >&2; exit 1; }

[[ -f /etc/wsl.conf ]] && grep -qE '^\s*systemd\s*=\s*true' /etc/wsl.conf \
  || die "/etc/wsl.conf is missing 'systemd=true' — add it and run 'wsl.exe --shutdown' once"
[[ -d /run/systemd/system ]] || die "systemd not running as PID 1"
command -v docker >/dev/null || die "docker not installed"
ok "prereqs: systemd booted + docker installed"

sudo -v || die "sudo required"

# ── Layer 2: dockerd auto-start on WSL boot ────────────────────────
if ! systemctl is-enabled docker >/dev/null 2>&1; then
  sudo systemctl enable docker
  ok "docker.service enabled for WSL boot"
else
  ok "docker.service already enabled"
fi

# ── Layer 4: OmniSight compose stack unit ──────────────────────────
UNIT=omnisight-compose-prod.service
TMP="$(mktemp)"
sed -e "s|USER_HOME|$USER_NAME|g" -e "s|USERNAME|$USER_NAME|g" \
    "deploy/systemd/$UNIT" > "$TMP"

DST="/etc/systemd/system/$UNIT"
if sudo [ -f "$DST" ] && sudo cmp -s "$TMP" "$DST"; then
  ok "$UNIT unchanged"
else
  sudo install -m 644 "$TMP" "$DST"
  sudo systemctl daemon-reload
  ok "$UNIT installed at $DST"
fi
rm -f "$TMP"

sudo systemctl enable --now "$UNIT"
ok "$UNIT enabled + started (idempotent — compose up -d)"

# ── Layer 3 sanity ─────────────────────────────────────────────────
POLICIES="$(docker inspect $(docker ps -aq --filter 'label=com.docker.compose.project=omnisight-productizer') \
  --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null | sort -u)"
if [[ "$POLICIES" != "always" ]]; then
  warn "some containers lack restart=always: $POLICIES"
else
  ok "all containers: restart=always"
fi

# ── Status ─────────────────────────────────────────────────────────
printf '\nInstalled layers:\n'
printf '  %-30s %s\n' "docker.service" "$(systemctl is-enabled docker)"
printf '  %-30s %s\n' "$UNIT" "$(systemctl is-enabled "$UNIT")"
printf '\n%sDone with WSL-side layers 2–4.%s\n' "$C_OK" "$C_OFF"
printf 'Layer 1 (Windows → WSL distro auto-boot) must be done in Windows.\n'
printf 'See: docs/ops/autostart_wsl.md §1 for the exact Task Scheduler / PowerShell commands.\n'
