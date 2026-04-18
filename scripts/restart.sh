#!/usr/bin/env bash
#
# OmniSight safe restart — orchestrates a full stop/start cycle with
# readiness polling, or delegates to the HA rolling-restart flow when
# the dual-replica topology is in use.
#
# This script deliberately does *not* reimplement the rolling or
# blue-green ceremony — those live in scripts/deploy.sh and are covered
# by the G2/G3 test suite. `--mode rolling` / `--mode blue-green` just
# forwards to deploy.sh with the right flags.
#
# Usage:
#   scripts/restart.sh [options]
#
# Options:
#   --mode <systemd|compose|rolling|blue-green|auto>
#                                   Default: auto.
#                                     systemd    — stop + start via systemctl
#                                     compose    — docker compose down + up -d
#                                     rolling    — delegate to
#                                                  scripts/deploy.sh --strategy rolling
#                                     blue-green — delegate to deploy.sh --strategy blue-green
#   --compose-file <path>           Override compose file (default:
#                                   docker-compose.prod.yml if present).
#   --timeout <seconds>             Passed to shutdown.sh and to the
#                                   readiness poll. Min 40. Default 90.
#   --skip-backup                   Do NOT back up the DB before restart
#                                   (default: DO back up).
#   --env <staging|prod>            Forwarded to deploy.sh when
#                                   --mode rolling / blue-green. Default prod.
#   --dry-run                       Print the plan without executing.
#   -h | --help                     Show this help.
#
# Exit codes:
#   0   Services up and /readyz returns 200 within the timeout.
#   1   Shutdown or start step failed.
#   2   Prerequisite missing.
#   3   Invalid arguments.
#   4   Readiness poll timed out (services started but did not become ready).
#
# Start order (mirror image of shutdown):
#   1. backend   — wait for http://127.0.0.1:8000/readyz == 200
#   2. workers   — systemd only; start every @N instance that was installed
#   3. frontend  — wait for http://127.0.0.1:3000/ (any 2xx)
#   4. cloudflared — last, so the tunnel only opens once the app is ready

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
MODE="auto"
COMPOSE_FILE=""
TIMEOUT=90
DO_BACKUP=1
ENV_NAME="prod"
DRY_RUN=0

log() { printf '\033[36m[restart]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[restart]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[31m[restart]\033[0m %s\n' "$*" >&2; }

usage() {
  sed -n '1,45p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '\033[90m+ %s\033[0m\n' "$*"
    return 0
  fi
  "$@"
}

# ── arg parse ─────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:?}"; shift 2 ;;
    --mode=*) MODE="${1#--mode=}"; shift ;;
    --compose-file) COMPOSE_FILE="${2:?}"; shift 2 ;;
    --compose-file=*) COMPOSE_FILE="${1#--compose-file=}"; shift ;;
    --timeout) TIMEOUT="${2:?}"; shift 2 ;;
    --timeout=*) TIMEOUT="${1#--timeout=}"; shift ;;
    --skip-backup) DO_BACKUP=0; shift ;;
    --env) ENV_NAME="${2:?}"; shift 2 ;;
    --env=*) ENV_NAME="${1#--env=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) err "unknown arg: $1"; exit 3 ;;
  esac
done

case "$MODE" in
  auto|systemd|compose|rolling|blue-green) ;;
  *) err "--mode must be one of: auto, systemd, compose, rolling, blue-green"; exit 3 ;;
esac

if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || (( TIMEOUT < 40 )); then
  err "--timeout must be integer ≥ 40"; exit 3
fi

cd "$ROOT"

# ── helpers ───────────────────────────────────────────────────────
detect_mode() {
  if [[ "$MODE" != "auto" ]]; then
    echo "$MODE"; return
  fi
  if command -v systemctl >/dev/null 2>&1 \
     && systemctl list-unit-files 2>/dev/null | grep -q '^omnisight-backend\.service'; then
    echo "systemd"
  elif command -v docker >/dev/null 2>&1; then
    echo "compose"
  else
    echo "none"
  fi
}

pick_compose_file() {
  if [[ -n "$COMPOSE_FILE" ]]; then
    echo "$COMPOSE_FILE"; return
  fi
  if [[ -f "$ROOT/docker-compose.prod.yml" ]]; then
    echo "docker-compose.prod.yml"
  else
    echo "docker-compose.yml"
  fi
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  else
    echo "docker-compose"
  fi
}

# Poll an HTTP endpoint until it returns a 2xx or timeout elapses.
# Uses curl -sSf so 5xx/no-route/connection-refused all count as "not
# ready yet" and we keep retrying.
poll_http() {
  local url="$1" label="$2" deadline_s="$3"
  local start now
  start=$(date +%s)
  log "polling $label at $url (up to ${deadline_s}s) …"
  if (( DRY_RUN )); then
    printf '\033[90m+ poll %s\033[0m\n' "$url"; return 0
  fi
  while true; do
    if curl -sSf -o /dev/null --max-time 3 "$url" 2>/dev/null; then
      log "$label ready"
      return 0
    fi
    now=$(date +%s)
    if (( now - start >= deadline_s )); then
      err "$label did not become ready within ${deadline_s}s"
      return 4
    fi
    sleep 2
  done
}

list_enabled_worker_units() {
  # Units that are enabled at boot — those are the ones we should bring
  # back up after restart. Running-but-not-enabled units are treated as
  # operator experiments and are NOT restarted.
  systemctl list-unit-files --no-legend --type=service 2>/dev/null \
    | awk '$1 ~ /^omnisight-worker@[0-9]+\.service$/ && $2=="enabled" {print $1}' \
    | sort -u
}

# ── optional DB backup ────────────────────────────────────────────
maybe_backup_db() {
  (( DO_BACKUP )) || { log "skipping DB backup (--skip-backup)"; return 0; }
  local db="$ROOT/data/omnisight.db"
  local outdir="$ROOT/data/backups"
  if [[ ! -f "$db" ]]; then
    warn "DB not found at $db — skipping backup"; return 0
  fi
  if ! command -v sqlite3 >/dev/null 2>&1; then
    warn "sqlite3 not installed — skipping backup"; return 0
  fi
  local ts dest
  ts=$(date +%Y%m%d-%H%M%S)
  dest="$outdir/restart-$ts.db"
  run mkdir -p "$outdir"
  log "backing up DB → $dest"
  run sqlite3 "$db" ".backup '$dest'" || warn "DB backup failed — continuing"
}

# ── systemd restart ───────────────────────────────────────────────
restart_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    err "systemctl not found"; return 2
  fi

  maybe_backup_db

  # Step 1: graceful stop via shutdown.sh (same host, same mode).
  local shutdown_args=(--mode systemd --timeout "$TIMEOUT")
  (( DRY_RUN )) && shutdown_args+=(--dry-run)
  log "delegating stop to scripts/shutdown.sh …"
  if ! "$ROOT/scripts/shutdown.sh" "${shutdown_args[@]}"; then
    err "shutdown failed — aborting restart"
    return 1
  fi

  # Step 2: start in dependency order.
  # backend first, poll /readyz, then workers, then frontend, then
  # cloudflared (tunnel must open LAST so external traffic is never
  # pointed at a still-booting backend).
  log "starting omnisight-backend …"
  run sudo systemctl start omnisight-backend.service
  poll_http "http://127.0.0.1:8000/readyz" "backend /readyz" "$TIMEOUT" || return 4

  local workers
  workers=$(list_enabled_worker_units || true)
  if [[ -z "$workers" ]]; then
    log "no enabled omnisight-worker@N instances to start"
  else
    while IFS= read -r unit; do
      [[ -z "$unit" ]] && continue
      log "starting $unit …"
      run sudo systemctl start "$unit"
    done <<< "$workers"
  fi

  log "starting omnisight-frontend …"
  run sudo systemctl start omnisight-frontend.service
  poll_http "http://127.0.0.1:3000/" "frontend" 60 || warn "frontend not ready — check logs"

  if systemctl list-unit-files 2>/dev/null | grep -q '^cloudflared\.service'; then
    log "starting cloudflared (tunnel) …"
    run sudo systemctl start cloudflared.service
  fi

  log "restart complete"
}

# ── compose restart ───────────────────────────────────────────────
restart_compose() {
  if ! command -v docker >/dev/null 2>&1; then
    err "docker not found"; return 2
  fi
  local file cc
  file=$(pick_compose_file)
  cc=$(compose_cmd)
  if [[ ! -f "$ROOT/$file" ]]; then
    err "compose file not found: $file"; return 2
  fi

  maybe_backup_db

  local shutdown_args=(--mode compose --compose-file "$file" --timeout "$TIMEOUT")
  (( DRY_RUN )) && shutdown_args+=(--dry-run)
  log "delegating stop to scripts/shutdown.sh …"
  if ! "$ROOT/scripts/shutdown.sh" "${shutdown_args[@]}"; then
    err "shutdown failed"; return 1
  fi

  log "starting stack ($cc -f $file up -d) …"
  run $cc -f "$file" up -d

  # Readiness: prod has backend-a:8000 + backend-b:8001; dev has backend:8000.
  if $cc -f "$file" ps --services 2>/dev/null | grep -qx backend-a; then
    poll_http "http://127.0.0.1:8000/readyz" "backend-a /readyz" "$TIMEOUT" || return 4
    poll_http "http://127.0.0.1:8001/readyz" "backend-b /readyz" "$TIMEOUT" || return 4
  else
    poll_http "http://127.0.0.1:8000/readyz" "backend /readyz" "$TIMEOUT" || return 4
  fi

  if $cc -f "$file" ps --services 2>/dev/null | grep -qx frontend; then
    poll_http "http://127.0.0.1:3000/" "frontend" 60 || warn "frontend not ready"
  fi
  log "restart complete"
}

# ── rolling / blue-green (delegate to deploy.sh) ──────────────────
restart_rolling() {
  if [[ ! -x "$ROOT/scripts/deploy.sh" ]]; then
    err "scripts/deploy.sh missing or not executable — rolling mode requires it"
    return 2
  fi
  log "delegating to scripts/deploy.sh --strategy rolling $ENV_NAME"
  if (( DRY_RUN )); then
    printf '\033[90m+ scripts/deploy.sh --strategy rolling %s\033[0m\n' "$ENV_NAME"
    return 0
  fi
  exec "$ROOT/scripts/deploy.sh" --strategy rolling "$ENV_NAME"
}

restart_blue_green() {
  if [[ ! -x "$ROOT/scripts/deploy.sh" ]]; then
    err "scripts/deploy.sh missing — blue-green requires it"; return 2
  fi
  log "delegating to scripts/deploy.sh --strategy blue-green $ENV_NAME"
  if (( DRY_RUN )); then
    printf '\033[90m+ scripts/deploy.sh --strategy blue-green %s\033[0m\n' "$ENV_NAME"
    return 0
  fi
  exec "$ROOT/scripts/deploy.sh" --strategy blue-green "$ENV_NAME"
}

# ── main ──────────────────────────────────────────────────────────
main() {
  local resolved
  resolved=$(detect_mode)
  case "$resolved" in
    systemd) restart_systemd ;;
    compose) restart_compose ;;
    rolling) restart_rolling ;;
    blue-green) restart_blue_green ;;
    none) err "neither systemctl nor docker available"; exit 2 ;;
    *) err "unreachable mode: $resolved"; exit 2 ;;
  esac
}

main "$@"
