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
declare -A GRACE_PERIODS=(
  [postgres]=30
  [pg-primary]=30
  [backend]=40
  [backend-a]=40
  [backend-b]=40
  [caddy]=15
  [frontend]=15
  [cloudflared]=15
)
DEFAULT_GRACE=10
PG_WAL_SAFETY_TIMEOUT=25
# Post-stop verification budget (family8 §3.4). The verification loop polls
# until every in-scope service has left the `running`/`active` state, up to
# this many seconds, before declaring failure. Overridable for tests.
VERIFY_TIMEOUT="${OMNISIGHT_SHUTDOWN_VERIFY_TIMEOUT:-30}"

# Per-service stop timing, populated by record_elapsed() as each service is
# stopped. Feeds the §3.4 structured verification evidence line (the §11 RTO
# histogram reads per-service wall-clock from these, not from docker/systemd).
declare -A STOP_ELAPSED=()
declare -a STOP_ORDER=()

log() { printf '\033[36m[shutdown]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[shutdown]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[31m[shutdown]\033[0m %s\n' "$*" >&2; }
jsonl_stop_result() {
  local service="$1" grace="$2" method="$3" result="$4"
  printf '{"service":"%s","grace_used":%s,"method":"%s","result":"%s"}\n' \
    "$service" "$grace" "$method" "$result"
}

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

service_grace() {
  local service="$1"
  echo "${GRACE_PERIODS[$service]:-$DEFAULT_GRACE}"
}

# Print the four §3.2 service-class grace budgets. Each concrete service in
# GRACE_PERIODS maps to exactly one class; we read the representative member
# of each class so this stays in lock-step with the map above (rather than
# re-hardcoding the numbers). --dry-run surfaces this so an operator can
# confirm the wire-up before a live stop (family8 §3.2; OP-1748 AC).
print_class_budgets() {
  local pg backend stateless
  pg=$(service_grace postgres)        # PG class    (postgres / pg-primary)
  backend=$(service_grace backend)    # backend     (backend / backend-a/-b)
  stateless=$(service_grace caddy)    # stateless   (caddy / frontend / cloudflared)
  log "service-class grace budgets (family8 §3.2): PG=${pg}s backend=${backend}s stateless=${stateless}s other=${DEFAULT_GRACE}s"
}

# Record how long a single service took to stop (wall-clock seconds, from the
# script's own timer). Order-preserving so the evidence line reads in stop
# order. Called once per handled service from the stop_* functions.
record_elapsed() {
  local service="$1" t0="$2" now
  now=$(date +%s)
  STOP_ELAPSED[$service]=$(( now - t0 ))
  STOP_ORDER+=("$service")
}

# Emit the single structured key=value verification-evidence line mandated by
# §3.4: the mode + timeout used, the resulting exit code, the services that
# reached inactive/exited (in stop order) with per-service wall-clock, and any
# service still up. One line so log aggregation can extract the RTO histogram.
emit_verification_evidence() {
  local mode="$1" exit_code="$2" still_running="$3"
  local s down_list elapsed_kv
  local parts=()
  down_list=$(IFS=,; echo "${STOP_ORDER[*]:-}")
  for s in "${STOP_ORDER[@]:-}"; do
    [[ -z "$s" ]] && continue
    parts+=("${s}:${STOP_ELAPSED[$s]:-0}s")
  done
  elapsed_kv=$(IFS=,; echo "${parts[*]:-}")
  printf '[shutdown] verify mode=%s timeout=%s exit=%s down=%s elapsed=%s still_running=%s\n' \
    "$mode" "$TIMEOUT" "$exit_code" "${down_list:-none}" "${elapsed_kv:-none}" "${still_running:-none}"
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
  local unit="$1" t0
  t0=$(date +%s)
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
    record_elapsed "$unit" "$t0"
    return 0
  fi
  log "stopping $unit …"
  if ! run sudo systemctl stop "$unit"; then
    if (( FORCE )); then
      warn "systemctl stop $unit failed — continuing (--force)"
      record_elapsed "$unit" "$t0"
      return 0
    fi
    err "systemctl stop $unit failed"
    return 1
  fi
  record_elapsed "$unit" "$t0"
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

  local bad=0 still=""
  for u in "${units[@]}"; do
    if systemctl is-active --quiet "$u" 2>/dev/null; then
      err "$u is still active — drain may have exceeded its grace period"
      still+="${still:+ }$u"
      bad=1
    fi
  done
  if (( bad )); then
    emit_verification_evidence systemd 1 "$still"
    err "one or more services did not stop cleanly"
    return 1
  fi
  emit_verification_evidence systemd 0 ""
  log "all systemd services reported inactive"
}

# ── compose shutdown ──────────────────────────────────────────────
shutdown_compose() {
  if ! command -v docker >/dev/null 2>&1; then
    err "docker not found — cannot use compose mode"
    return 2
  fi
  local file compose_path cc
  file=$(pick_compose_file)
  if [[ "$file" == /* ]]; then
    compose_path="$file"
  else
    compose_path="$ROOT/$file"
  fi
  cc=$(compose_cmd)
  if [[ ! -f "$compose_path" ]]; then
    err "compose file not found: $file"
    return 2
  fi
  log "using $cc -f $file (timeout=${TIMEOUT}s)"

  # 1. Optional: stop ingress (caddy) first so new external traffic
  #    is rejected before the backends drain. Only present in prod.
  if (( SKIP_INGRESS == 0 )); then
    stop_compose_service "$file" "$cc" cloudflared || return 1
    stop_compose_service "$file" "$cc" caddy || return 1
  else
    log "skipping ingress stop (--skip-ingress)"
  fi

  # 2. frontend next (no drain) — pulls external browsers off the app.
  stop_compose_service "$file" "$cc" frontend || return 1

  # 3. Optional DB backup before the backend stops. If SQLite lives in a
  #    named volume we invoke sqlite3 inside the backend container; if
  #    it lives on the host (dev compose bind mount), we try host path.
  if (( DO_BACKUP )); then
    backup_db_best_effort
  fi

  # 4. backend replicas — lifecycle.py drain.  The compose stop -t value
  #    is the hard SIGKILL deadline; 40s is enough for the 30s in-flight
  #    drain + 10s buffer, matching the systemd unit.
  for svc in backend backend-a backend-b; do
    stop_compose_service "$file" "$cc" "$svc" || return 1
  done

  # 5. workers (dev compose only, via profile)
  stop_compose_service "$file" "$cc" worker --profile workers || return 1

  # 6. datastore containers, when present in local compose fixtures.
  for svc in postgres pg-primary; do
    stop_compose_service "$file" "$cc" "$svc" || return 1
  done

  # 7. observability sidecars (prod only)
  for svc in prometheus grafana; do
    stop_compose_service "$file" "$cc" "$svc" --profile observability || return 1
  done

  # 8. idempotency: clear any Exited residue from a prior hard-failed run so a
  #    rerun converges to a clean stack (§3.3).
  cleanup_compose_residue "$file" "$cc"

  # 9. verify
  verify_compose_down "$file" "$cc"
}

compose_service_exists() {
  local file="$1" cc="$2" service="$3"
  shift 3
  $cc -f "$file" "$@" ps --services 2>/dev/null | grep -qx "$service"
}

compose_service_container() {
  local file="$1" cc="$2" service="$3"
  shift 3
  $cc -f "$file" "$@" ps -q "$service" 2>/dev/null | head -n 1
}

container_running() {
  local container="$1"
  [[ "$(docker inspect --format='{{.State.Running}}' "$container" 2>/dev/null || echo false)" == "true" ]]
}

wait_container_exit() {
  local container="$1" grace="$2" started now
  started=$(date +%s)
  while container_running "$container"; do
    now=$(date +%s)
    if (( now - started >= grace )); then
      return 1
    fi
    sleep 1
  done
  return 0
}

stop_compose_service() {
  local file="$1" cc="$2" service="$3"
  shift 3
  if ! compose_service_exists "$file" "$cc" "$service" "$@"; then
    return 0
  fi

  local container grace t0
  t0=$(date +%s)
  container=$(compose_service_container "$file" "$cc" "$service" "$@")
  grace=$(service_grace "$service")
  if [[ -z "$container" ]] || ! container_running "$container"; then
    log "already exited: $service"
    jsonl_stop_result "$service" "$grace" SIGTERM already_exited
    record_elapsed "$service" "$t0"
    return 0
  fi

  wait_pg_wal_safe "$file" "$cc" "$service" "$@" || return 1

  log "stopping $service (grace=${grace}s) …"
  if (( DRY_RUN )); then
    run docker kill --signal=TERM "$container"
    jsonl_stop_result "$service" "$grace" SIGTERM stopped
    record_elapsed "$service" "$t0"
    return 0
  fi

  docker kill --signal=TERM "$container" >/dev/null
  if wait_container_exit "$container" "$grace"; then
    jsonl_stop_result "$service" "$grace" SIGTERM stopped
    record_elapsed "$service" "$t0"
    return 0
  fi

  warn "$service still running after ${grace}s — sending SIGKILL"
  docker kill --signal=KILL "$container" >/dev/null
  if wait_container_exit "$container" 1; then
    jsonl_stop_result "$service" "$grace" SIGKILL stopped
    record_elapsed "$service" "$t0"
    return 0
  fi

  err "$service did not stop after SIGKILL"
  return 1
}

wait_pg_wal_safe() {
  local file="$1" cc="$2" service="$3"
  shift 3
  if [[ "$service" != "postgres" && "$service" != "pg-primary" ]]; then
    return 0
  fi

  local started now
  started=$(date +%s)
  log "waiting for $service WAL checkpoint and readiness (timeout=${PG_WAL_SAFETY_TIMEOUT}s) …"
  if ! run $cc -f "$file" "$@" exec -T "$service" psql -c "CHECKPOINT;"; then
    err "$service CHECKPOINT failed before shutdown"
    return 1
  fi

  while true; do
    if run $cc -f "$file" "$@" exec -T "$service" pg_isready >/dev/null 2>&1; then
      log "$service pg_isready OK after checkpoint"
      return 0
    fi
    now=$(date +%s)
    if (( now - started >= PG_WAL_SAFETY_TIMEOUT )); then
      err "$service pg_isready did not become OK within ${PG_WAL_SAFETY_TIMEOUT}s"
      return 1
    fi
    sleep 1
  done
}

# Idempotency residue cleanup (family8 §3.3). A prior invocation that had to
# SIGKILL a hung service leaves an `Exited` container behind. A rerun must
# converge to a clean stack, so we remove that residue with `rm -f -s` (stop
# if needed, then remove). Safe to call when there is nothing to remove — the
# exited list is simply empty.
cleanup_compose_residue() {
  local file="$1" cc="$2"
  local exited svc
  exited=$($cc -f "$file" ps --all --services --filter status=exited 2>/dev/null || true)
  [[ -z "$exited" ]] && return 0
  while IFS= read -r svc; do
    [[ -z "$svc" ]] && continue
    log "removing exited residue: $svc (rerun-idempotency, §3.3)"
    run $cc -f "$file" rm -f -s "$svc" >/dev/null 2>&1 || true
  done <<< "$exited"
}

verify_compose_down() {
  local file="$1" cc="$2"
  # §3.4 verification loop: poll until every in-scope service has left the
  # `running` state, up to VERIFY_TIMEOUT, then emit a single structured
  # evidence line. Any service still running when the budget expires is a
  # failure (exit 1). `ps --format json` isn't portable across compose v1/v2,
  # so we parse `ps --services --filter status=running`.
  local start deadline running
  start=$(date +%s)
  deadline=$(( start + VERIFY_TIMEOUT ))
  while :; do
    running=$($cc -f "$file" ps --all --services --filter status=running 2>/dev/null || true)
    if (( SKIP_INGRESS == 1 )); then
      # ingress is intentionally left up (rolling-restart flows); exclude it
      # before judging instead of skipping verification wholesale.
      running=$(printf '%s\n' "$running" | grep -vE '^(caddy|cloudflared)$' || true)
    fi
    running=$(printf '%s' "$running" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')
    if [[ -z "$running" ]]; then
      emit_verification_evidence compose 0 ""
      log "compose stack is down"
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      break
    fi
    sleep 2
  done
  emit_verification_evidence compose 1 "$running"
  err "verification timeout — still running: $running"
  return 1
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
  # --dry-run surfaces the per-service-class grace budgets up front so an
  # operator can confirm the §3.2 wire-up without a live stop (OP-1748 AC).
  if (( DRY_RUN )); then
    print_class_budgets
  fi
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
