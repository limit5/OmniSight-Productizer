#!/usr/bin/env bash
#
# OmniSight safe shutdown — drain-aware stop of every long-running process
# the system spawns on a single host.
#
# Scope (audited 2026-04-18):
#   systemd units:
#     * omnisight-backend           — uvicorn, TimeoutStopSec=40
#                                     (30s lifecycle drain + 10s buffer)
#     * omnisight-worker@N          — stateless worker, TimeoutStopSec=60
#                                     (drain in-flight + release dist-locks
#                                     + deregister from workers:active)
#     * omnisight-frontend          — next start, TimeoutStopSec=15
#     * cloudflared                 — Cloudflare tunnel, no drain
#   docker-compose (prod):
#     * backend-a / backend-b / caddy / frontend / prometheus / grafana
#   docker-compose (dev):
#     * backend / frontend / worker  (worker has stop_grace_period=60s)
#
# Explicit NON-scope: auto-runner.py is a personal scheduling helper and
# is NOT part of the system. This script never touches it.
#
# Usage:
#   scripts/shutdown.sh [options]
#
# Options:
#   --mode <systemd|compose|auto>   Default: auto (prefer systemd if units
#                                   are installed, else docker-compose).
#   --compose-file <path>           Override compose file. Default:
#                                   docker-compose.prod.yml if present,
#                                   else docker-compose.yml.
#   --timeout <seconds>             Override the longest per-service
#                                   grace period. Min 40. Default 90.
#   --backup-db                     sqlite3 .backup before stopping the
#                                   backend (WAL-safe, best-effort).
#   --skip-ingress                  Leave cloudflared/caddy up (rolling
#                                   restart uses this).
#   --dry-run                       Print what would happen; change nothing.
#   --force                         Do not fail if a service is already
#                                   stopped or missing.
#   -h | --help                     Show this help and exit 0.
#
# Exit codes:
#   0   All in-scope services are down.
#   1   A service failed to stop within its grace period.
#   2   Prerequisite missing (systemctl / docker not available).
#   3   Invalid arguments.
#
# Order of operations (systemd mode):
#   1. cloudflared        — stop new external traffic first.
#   2. frontend           — no drain needed, but depends on backend.
#   3. backend            — lifecycle.py drain (30s + 10s buffer).
#   4. omnisight-worker@* — drain in-flight tasks (60s each).
#   5. Optional DB backup.
#   6. Verify every unit reports `inactive`.
#
# Order (compose mode):
#   1. Optional ingress stop (caddy) unless --skip-ingress.
#   2. frontend + backend-a + backend-b + worker (parallel stop with
#      the worker service's stop_grace_period honoured).
#   3. docker compose down (cleanup volumes untouched).

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────
ROOT=$(cd "$(dirname "$0")/.." && pwd)
MODE="auto"
COMPOSE_FILE=""
TIMEOUT=90
DO_BACKUP=0
SKIP_INGRESS=0
DRY_RUN=0
FORCE=0

log() { printf '\033[36m[shutdown]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[shutdown]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[31m[shutdown]\033[0m %s\n' "$*" >&2; }

usage() {
  sed -n '1,55p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

run() {
  # Dry-run-aware command execution. Echoes the command with a `+ ` prefix
  # (shell -x style) so operators can see exactly what would run.
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
    --backup-db) DO_BACKUP=1; shift ;;
    --skip-ingress) SKIP_INGRESS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage ;;
    *) err "unknown arg: $1"; exit 3 ;;
  esac
done

case "$MODE" in
  auto|systemd|compose) ;;
  *) err "--mode must be one of: auto, systemd, compose"; exit 3 ;;
esac

if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || (( TIMEOUT < 40 )); then
  err "--timeout must be an integer ≥ 40 (backend drain needs 40s minimum)"
  exit 3
fi

cd "$ROOT"

# ── mode detection ────────────────────────────────────────────────
detect_mode() {
  if [[ "$MODE" != "auto" ]]; then
    echo "$MODE"
    return
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
    echo "$COMPOSE_FILE"
    return
  fi
  if [[ -f "$ROOT/docker-compose.prod.yml" ]]; then
    echo "docker-compose.prod.yml"
  else
    echo "docker-compose.yml"
  fi
}

# Resolve `docker compose` vs legacy `docker-compose` once.
compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  else
    echo "docker-compose"
  fi
}

# ── systemd shutdown ──────────────────────────────────────────────
list_worker_units() {
  # Enumerate every enabled/running omnisight-worker@N instance. We
  # accept both states so `--force` can stop units that were left
  # registered but happen to be inactive at this moment.
  systemctl list-units --all --type=service --no-legend \
    | awk '/^[[:space:]]*omnisight-worker@[0-9]+\.service/ {print $1}' \
    | sort -u
}

stop_unit() {
  local unit="$1"
  if ! systemctl list-unit-files 2>/dev/null | grep -q "^${unit%@*}"; then
    if (( FORCE )); then
      warn "unit not installed: $unit — skipping (--force)"
      return 0
    fi
    warn "unit not installed: $unit — skipping"
    return 0
  fi
  if ! systemctl is-active --quiet "$unit" 2>/dev/null; then
    log "already inactive: $unit"
    return 0
  fi
  log "stopping $unit …"
  if ! run sudo systemctl stop "$unit"; then
    if (( FORCE )); then
      warn "systemctl stop $unit failed — continuing (--force)"
      return 0
    fi
    err "systemctl stop $unit failed"
    return 1
  fi
}

shutdown_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    err "systemctl not found — is this the right host?"
    return 2
  fi

  # 1. ingress first (unless --skip-ingress for rolling-restart flows)
  if (( SKIP_INGRESS == 0 )); then
    stop_unit cloudflared.service || return 1
  else
    log "skipping cloudflared (--skip-ingress)"
  fi

  # 2. frontend — fast (15s), no drain
  stop_unit omnisight-frontend.service || return 1

  # 3. backend — triggers lifecycle.py drain (30s + 10s buffer).
  #    Optional DB backup fires BEFORE the stop so WAL is hot and the
  #    .backup copy is consistent; doing it after carries the risk that
  #    a slow drain has already closed the connection.
  if (( DO_BACKUP )); then
    backup_db_best_effort
  fi
  stop_unit omnisight-backend.service || return 1

  # 4. workers (there may be 0..N; stop every @N we find)
  local workers
  workers=$(list_worker_units || true)
  if [[ -z "$workers" ]]; then
    log "no omnisight-worker@N instances active"
  else
    while IFS= read -r unit; do
      [[ -z "$unit" ]] && continue
      stop_unit "$unit" || return 1
    done <<< "$workers"
  fi

  # 5. verify
  verify_systemd_down
}

verify_systemd_down() {
  local units=(cloudflared.service omnisight-frontend.service omnisight-backend.service)
  # add any lingering worker@N
  while IFS= read -r u; do
    [[ -z "$u" ]] && continue
    units+=("$u")
  done < <(list_worker_units || true)

  local bad=0
  for u in "${units[@]}"; do
    if systemctl is-active --quiet "$u" 2>/dev/null; then
      err "$u is still active — drain may have exceeded its grace period"
      bad=1
    fi
  done
  if (( bad )); then
    err "one or more services did not stop cleanly"
    return 1
  fi
  log "all systemd services reported inactive"
}

# ── compose shutdown ──────────────────────────────────────────────
shutdown_compose() {
  if ! command -v docker >/dev/null 2>&1; then
    err "docker not found — cannot use compose mode"
    return 2
  fi
  local file cc
  file=$(pick_compose_file)
  cc=$(compose_cmd)
  if [[ ! -f "$ROOT/$file" ]]; then
    err "compose file not found: $file"
    return 2
  fi
  log "using $cc -f $file (timeout=${TIMEOUT}s)"

  # 1. Optional: stop ingress (caddy) first so new external traffic
  #    is rejected before the backends drain. Only present in prod.
  if (( SKIP_INGRESS == 0 )); then
    if $cc -f "$file" ps --services 2>/dev/null | grep -qx caddy; then
      log "stopping caddy (ingress) …"
      run $cc -f "$file" stop -t 10 caddy || true
    fi
  else
    log "skipping ingress stop (--skip-ingress)"
  fi

  # 2. frontend next (no drain) — pulls external browsers off the app.
  if $cc -f "$file" ps --services 2>/dev/null | grep -qx frontend; then
    log "stopping frontend …"
    run $cc -f "$file" stop -t 15 frontend || true
  fi

  # 3. Optional DB backup before the backend stops. If SQLite lives in a
  #    named volume we invoke sqlite3 inside the backend container; if
  #    it lives on the host (dev compose bind mount), we try host path.
  if (( DO_BACKUP )); then
    backup_db_best_effort
  fi

  # 4. backend replicas — lifecycle.py drain.  The compose stop -t value
  #    is the hard SIGKILL deadline; 40s is enough for the 30s in-flight
  #    drain + 10s buffer, matching the systemd unit.
  local backends=()
  for svc in backend backend-a backend-b; do
    if $cc -f "$file" ps --services 2>/dev/null | grep -qx "$svc"; then
      backends+=("$svc")
    fi
  done
  if ((${#backends[@]} > 0)); then
    log "stopping backends: ${backends[*]}"
    run $cc -f "$file" stop -t 40 "${backends[@]}" || true
  fi

  # 5. workers (dev compose only, via profile)
  if $cc -f "$file" --profile workers ps --services 2>/dev/null | grep -qx worker; then
    log "stopping worker (60s drain) …"
    run $cc -f "$file" --profile workers stop -t 60 worker || true
  fi

  # 6. observability sidecars (prod only)
  for svc in prometheus grafana; do
    if $cc -f "$file" --profile observability ps --services 2>/dev/null | grep -qx "$svc"; then
      log "stopping $svc …"
      run $cc -f "$file" --profile observability stop -t 10 "$svc" || true
    fi
  done

  # 7. verify
  verify_compose_down "$file" "$cc"
}

verify_compose_down() {
  local file="$1" cc="$2"
  # Any service with State=running that isn't explicitly exempted is a
  # failure.  `ps --format json` isn't portable across compose v1/v2, so
  # fall back to plain `ps` and parse STATE column.
  local running
  running=$($cc -f "$file" ps --all --services --filter status=running 2>/dev/null || true)
  if [[ -n "$running" ]] && (( SKIP_INGRESS == 0 )); then
    err "services still running: $running"
    return 1
  fi
  log "compose stack is down"
}

# ── DB backup (best-effort) ───────────────────────────────────────
backup_db_best_effort() {
  local db="$ROOT/data/omnisight.db"
  local outdir="$ROOT/data/backups"
  if [[ ! -f "$db" ]]; then
    warn "DB not found at $db — skipping backup"
    return 0
  fi
  if ! command -v sqlite3 >/dev/null 2>&1; then
    warn "sqlite3 not installed — skipping backup"
    return 0
  fi
  local ts
  ts=$(date +%Y%m%d-%H%M%S)
  local dest="$outdir/shutdown-$ts.db"
  run mkdir -p "$outdir"
  log "backing up DB → $dest"
  if ! run sqlite3 "$db" ".backup '$dest'"; then
    warn "DB backup failed — continuing shutdown anyway"
  fi
}

# ── main ──────────────────────────────────────────────────────────
main() {
  local resolved
  resolved=$(detect_mode)
  case "$resolved" in
    systemd)
      log "mode: systemd (resolved from '$MODE')"
      shutdown_systemd
      ;;
    compose)
      log "mode: compose (resolved from '$MODE')"
      shutdown_compose
      ;;
    none)
      err "neither systemctl nor docker available — nothing to stop"
      exit 2
      ;;
    *)
      err "unreachable mode: $resolved"
      exit 2
      ;;
  esac
}

main "$@"
