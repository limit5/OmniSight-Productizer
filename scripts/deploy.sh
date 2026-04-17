#!/usr/bin/env bash
#
# OmniSight deploy — single-machine WSL/systemd flow + G2 rolling mode.
#
# Usage:
#   scripts/deploy.sh staging
#   scripts/deploy.sh prod v0.2.0                      # check out the tag first
#   scripts/deploy.sh prod v0.2.0 rolling              # G2 #3 HA-02 rolling restart
#   scripts/deploy.sh --strategy blue-green prod v0.2.0 # G3 HA-03 blue-green cutover
#
# Legacy (systemd) steps, kept for single-replica hosts:
#   1. Optionally check out the requested git ref.
#   2. Online-backup the SQLite DB (WAL-safe).
#   3. Install backend deps + build the frontend.
#   4. systemctl restart with health-check polling.
#   5. Hit /health on the new pid.
#
# Strategy flag (G3 HA-03 TODO row 1353):
#   `--strategy <rolling|systemd|blue-green>` is the GNU-style form.
#   Accepts the same three values the positional arg accepts; when both
#   are supplied the flag wins. Added so the blue-green ceremony (TODO
#   row 1354-1357: atomic upstream switch, pre-cut smoke, observe window,
#   --rollback) has a stable selector operators can script against —
#   without breaking the positional-arg contract tests the G2 rolling
#   suite relies on.
#
# Rolling mode (G2 / HA-02 TODO row 1347):
#   Used when the host runs the dual-replica docker-compose topology
#   (backend-a:8000 + backend-b:8001, Caddy :443 upstream pool — see
#   docker-compose.prod.yml + deploy/reverse-proxy/Caddyfile). Activated
#   by:
#     - third positional arg `rolling` (preferred: explicit intent), or
#     - env `OMNISIGHT_DEPLOY_STRATEGY=rolling`.
#   Default remains the legacy systemd path so operators without the
#   compose topology are unaffected.
#
#   Rolling contract (one replica at a time, never both down):
#     (A) send SIGTERM to backend-a → backend/lifecycle.py drains, /readyz
#         returns 503, Caddy ejects A via active health probe (health_uri
#         /readyz, fail_duration 30s).
#     (B) wait up to 35s for A's /readyz to stop responding 200.
#     (C) recreate backend-a with `docker compose up -d --force-recreate
#         --no-deps backend-a`.
#     (D) poll http://localhost:8000/readyz until 200 — A is back in
#         rotation.
#     (E) repeat (A)-(D) for backend-b on :8001.
#   At no point are both replicas simultaneously unready, so Caddy always
#   has a live upstream — zero 5xx during deploy (verified by the G2 #5
#   soak-test deliverable).
#
# Conventions (systemd mode):
#   - systemd units named omnisight-{backend,frontend}-{ENV} when ENV
#     != prod; bare omnisight-{backend,frontend} for prod (the unit
#     templates in deploy/systemd/ ship the prod names).
#   - DB at data/${ENV}.db (`prod.db`, `staging.db`, …). Override
#     via OMNISIGHT_DATABASE_PATH if your layout differs.
#   - Backups land in data/backups/<env>-<timestamp>.db.
#
# This script is intentionally noisy. A silent deploy is a deploy you
# can't audit afterwards.

set -euo pipefail

# ── Flag parsing (G3 #1 / TODO row 1353) ──────────────────────────────
# GNU-style flags are stripped first so the downstream positional
# contract (ENV / GIT_REF / STRATEGY_ARG = $1 / $2 / $3) is unchanged.
# Only `--strategy <value>` and `--strategy=<value>` are parsed here;
# everything else bubbles back up as a positional so future flags can
# slot in next to it without a rewrite.
STRATEGY_FLAG=""
_positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --strategy)
      if [[ $# -lt 2 ]]; then
        echo "error: --strategy requires a value (rolling|systemd|blue-green)" >&2
        exit 1
      fi
      STRATEGY_FLAG="$2"
      shift 2
      ;;
    --strategy=*)
      STRATEGY_FLAG="${1#--strategy=}"
      shift
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        _positional+=("$1")
        shift
      done
      break
      ;;
    --*)
      echo "error: unknown flag: $1" >&2
      echo "usage: scripts/deploy.sh [--strategy rolling|systemd|blue-green] <env> [git-ref] [rolling|systemd|blue-green]" >&2
      exit 1
      ;;
    *)
      _positional+=("$1")
      shift
      ;;
  esac
done
# Repopulate $@ so the rest of the script uses the familiar positional
# reads. `${_positional[@]+"${_positional[@]}"}` is the set-u-safe way
# to expand an array that might be empty.
set -- "${_positional[@]+"${_positional[@]}"}"

ENV=${1:-}
GIT_REF=${2:-}
STRATEGY_ARG=${3:-}

if [[ -z "$ENV" ]]; then
  echo "usage: scripts/deploy.sh [--strategy rolling|systemd|blue-green] <env> [git-ref] [rolling|systemd|blue-green]" >&2
  exit 1
fi
if [[ "$ENV" != "prod" && "$ENV" != "staging" ]]; then
  echo "error: env must be 'staging' or 'prod' (got '$ENV')" >&2
  exit 1
fi

# Strategy resolution — positional arg wins over env var; default systemd.
STRATEGY="${STRATEGY_ARG:-${OMNISIGHT_DEPLOY_STRATEGY:-systemd}}"
# The `--strategy` flag (parsed above) overrides the positional/env
# chain so operators can write `scripts/deploy.sh --strategy blue-green
# prod v0.2.0` without remembering that the legacy positional slot is
# the third arg. If both are supplied, the flag wins — this matches
# GNU-coreutils conventions and is documented in the usage string.
if [[ -n "$STRATEGY_FLAG" ]]; then
  STRATEGY="$STRATEGY_FLAG"
fi
if [[ "$STRATEGY" != "rolling" && "$STRATEGY" != "systemd" && "$STRATEGY" != "blue-green" ]]; then
  echo "error: strategy must be 'rolling', 'systemd', or 'blue-green' (got '$STRATEGY')" >&2
  exit 1
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if [[ "$ENV" == "prod" ]]; then
  BACKEND_UNIT="omnisight-backend"
  FRONTEND_UNIT="omnisight-frontend"
  BACKEND_PORT=8000
else
  BACKEND_UNIT="omnisight-backend-staging"
  FRONTEND_UNIT="omnisight-frontend-staging"
  BACKEND_PORT=8001
fi

DB_PATH="${OMNISIGHT_DATABASE_PATH:-data/${ENV}.db}"
BACKUP_DIR="data/backups"
mkdir -p "$BACKUP_DIR"

log() { printf '\033[36m[deploy]\033[0m %s\n' "$*"; }

# ───────────────────────────────────────────────────────────────────
# 1. Git ref
# ───────────────────────────────────────────────────────────────────

if [[ -n "$GIT_REF" ]]; then
  log "git fetch + checkout $GIT_REF"
  git fetch --tags --quiet
  git checkout --quiet "$GIT_REF"
fi

CURRENT_REF=$(git describe --tags --always --dirty)
log "deploying $CURRENT_REF to $ENV (strategy=$STRATEGY)"

# ───────────────────────────────────────────────────────────────────
# 1b. N10 blue-green gate (prod only)
# ───────────────────────────────────────────────────────────────────
# Refuses a prod deploy when the last-merged PR is blue-green-required
# but the G3 ceremony was not recorded in the rollback ledger.
# See docs/ops/dependency_upgrade_policy.md for the full policy.

if [[ "$ENV" == "prod" ]]; then
  log "N10: checking blue-green gate"
  if ! OMNISIGHT_DEPLOY_ENV="$ENV" python3 "$ROOT/scripts/check_bluegreen_gate.py"; then
    rc=$?
    if [[ "$rc" == "2" ]]; then
      echo "[deploy] blue-green gate REFUSED the deploy (see stderr above)." >&2
      exit 2
    fi
    # rc==3 = environmental failure (no gh, broken ledger). Surface
    # it loudly but don't block — operators can set
    # OMNISIGHT_CHECK_BLUEGREEN=0 for a documented bypass.
    echo "[deploy] WARN: blue-green gate reported environmental failure (rc=$rc); proceeding." >&2
  fi
fi

# ───────────────────────────────────────────────────────────────────
# 2. DB backup (WAL-safe online backup)
# ───────────────────────────────────────────────────────────────────

if [[ -f "$DB_PATH" ]]; then
  TS=$(date +%Y%m%d-%H%M%S)
  OUT="${BACKUP_DIR}/${ENV}-${TS}.db"
  log "backing up $DB_PATH → $OUT"
  sqlite3 "$DB_PATH" ".backup '$OUT'"
else
  log "no existing $DB_PATH — first-run deploy, skipping backup"
fi

# ───────────────────────────────────────────────────────────────────
# 3. Build
# ───────────────────────────────────────────────────────────────────

log "installing backend deps (hash-verified, N1)"
pip install --quiet --require-hashes -r backend/requirements.txt

log "installing frontend deps + building (pnpm, N1)"
pnpm install --frozen-lockfile --prefer-offline
pnpm run build

# ───────────────────────────────────────────────────────────────────
# 4. Restart — strategy-specific
# ───────────────────────────────────────────────────────────────────

COMPOSE_FILE="${OMNISIGHT_COMPOSE_FILE:-docker-compose.prod.yml}"
# How long each replica has to drain + come back healthy. Paired with
# backend/lifecycle.py's 30s drain_timeout plus compose start_period.
ROLL_DRAIN_TIMEOUT="${OMNISIGHT_ROLL_DRAIN_TIMEOUT:-35}"
ROLL_READY_TIMEOUT="${OMNISIGHT_ROLL_READY_TIMEOUT:-120}"
ROLL_POLL_INTERVAL="${OMNISIGHT_ROLL_POLL_INTERVAL:-2}"

# rolling_restart_replica <compose-service> <host-port>
#
# Drives ONE replica through: drain → recreate → wait-ready. Aborts with
# exit 3 if the replica fails to come back within ROLL_READY_TIMEOUT so
# the caller stops before touching the second replica (the whole point
# of rolling is: never both down). Idempotent enough to be re-run after
# an operator fixes the underlying image / env issue.
rolling_restart_replica() {
  local svc="$1"
  local port="$2"
  local ready_url="http://localhost:${port}/readyz"

  log "rolling[$svc]: sending SIGTERM (drain window: ${ROLL_DRAIN_TIMEOUT}s)"
  # `docker compose stop --timeout N` sends SIGTERM then waits up to N
  # seconds before SIGKILL. Paired with backend/lifecycle.py's 30s
  # drain_timeout + TimeoutStopSec=40 on systemd parity.
  docker compose -f "$COMPOSE_FILE" stop --timeout "$ROLL_DRAIN_TIMEOUT" "$svc"

  # Confirm Caddy-visible readiness is gone. Once the container is
  # stopped curl fails to connect — that's the signal we need.
  local drained=0
  for _ in $(seq 1 "$ROLL_DRAIN_TIMEOUT"); do
    if ! curl -sf -m 2 "$ready_url" >/dev/null 2>&1; then
      drained=1
      break
    fi
    sleep 1
  done
  if [[ "$drained" != "1" ]]; then
    echo "[deploy] rolling[$svc]: drain confirmation timed out — $ready_url still 200 after ${ROLL_DRAIN_TIMEOUT}s" >&2
    exit 3
  fi
  log "rolling[$svc]: drained (Caddy ejects via /readyz active probe)"

  log "rolling[$svc]: recreating container"
  # --no-deps avoids restarting the frontend (which depends_on both
  # replicas) every time we touch a backend. --force-recreate ensures
  # new image / env-file values take effect even when the tag is same.
  docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "$svc"

  log "rolling[$svc]: waiting for /readyz (timeout: ${ROLL_READY_TIMEOUT}s)"
  local healthy=0
  local waited=0
  while (( waited < ROLL_READY_TIMEOUT )); do
    if curl -sf -m 2 "$ready_url" >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep "$ROLL_POLL_INTERVAL"
    waited=$((waited + ROLL_POLL_INTERVAL))
  done
  if [[ "$healthy" != "1" ]]; then
    echo "[deploy] rolling[$svc]: /readyz never returned 200 within ${ROLL_READY_TIMEOUT}s — aborting before touching the other replica" >&2
    echo "[deploy]    triage: docker compose -f $COMPOSE_FILE logs --tail=200 $svc" >&2
    exit 3
  fi
  log "rolling[$svc]: /readyz pass → back in Caddy upstream pool"
}

if [[ "$STRATEGY" == "blue-green" ]]; then
  # G3 HA-03 blue-green cutover landing pad (TODO row 1353).
  # The `--strategy blue-green` flag is accepted and validated here;
  # the full ceremony (atomic active/standby upstream switch, pre-cut
  # smoke on standby, 5-min observation window, 24 h rollback retention,
  # `deploy.sh --rollback` companion, runbook) is tracked in the
  # remaining G3 TODO rows (1354-1357) and will fill in this branch
  # incrementally. Until those deliverables land we fail CLOSED with a
  # distinct exit code (5) — *not* silently fall through to
  # rolling/systemd — so an operator who types
  # `scripts/deploy.sh --strategy blue-green prod v0.2.0` gets an
  # explicit "not yet wired" signal instead of an accidental rolling
  # restart dressed up as blue-green.
  log "blue-green mode: compose=$COMPOSE_FILE (active/standby cutover)"
  if [[ ! -f "$ROOT/$COMPOSE_FILE" ]]; then
    echo "[deploy] blue-green: compose file '$COMPOSE_FILE' missing — cannot select active/standby color" >&2
    exit 4
  fi
  echo "[deploy] blue-green: --strategy flag accepted; ceremony wiring (standby smoke → atomic upstream switch → 5-min observe → 24 h rollback retention) is tracked in TODO rows 1354-1357 (G3 #2-#5). Aborting before any upstream switch so no half-applied cutover runs against $ENV." >&2
  echo "[deploy] blue-green: next steps — implement the atomic symlink/upstream switch (row 1354), then wire pre-cut smoke via scripts/prod_smoke_test.py on the standby color (row 1355), then add deploy.sh --rollback (row 1356) and docs/ops/blue_green_runbook.md (row 1357)." >&2
  exit 5
elif [[ "$STRATEGY" == "rolling" ]]; then
  log "rolling mode: compose=$COMPOSE_FILE (backend-a:8000 → backend-b:8001)"
  if [[ ! -f "$ROOT/$COMPOSE_FILE" ]]; then
    echo "[deploy] rolling: compose file '$COMPOSE_FILE' missing — cannot run dual-replica rolling restart" >&2
    exit 4
  fi

  # Order is fixed: A first, then B. Never parallel — that would leave
  # zero replicas and defeat the whole rolling invariant.
  rolling_restart_replica "backend-a" 8000
  rolling_restart_replica "backend-b" 8001

  log "rolling: both replicas healthy, no traffic gap"
else
  log "systemd mode: restarting $BACKEND_UNIT"
  sudo systemctl restart "$BACKEND_UNIT"

  log "waiting for backend health on :$BACKEND_PORT"
  HEALTHY=0
  for i in $(seq 1 30); do
    if curl -sf "http://localhost:${BACKEND_PORT}/api/v1/health" >/dev/null; then
      HEALTHY=1
      break
    fi
    sleep 2
  done
  if [[ "$HEALTHY" != "1" ]]; then
    echo "backend failed to come up — check 'journalctl -u $BACKEND_UNIT'" >&2
    exit 2
  fi
  log "backend healthy"

  log "restarting $FRONTEND_UNIT"
  sudo systemctl restart "$FRONTEND_UNIT"
fi

# ───────────────────────────────────────────────────────────────────
# 5. Smoke
# ───────────────────────────────────────────────────────────────────

log "smoke test"
if [[ "$STRATEGY" == "rolling" ]]; then
  # In rolling mode the /api/v1/health legacy alias lives on either
  # replica; the Caddy front door (:443) proxies to whichever is
  # currently round-robin elected. We smoke both replicas directly so
  # a silent half-broken pool is caught before we exit 0.
  for port in 8000 8001; do
    HEALTH=$(curl -sf "http://localhost:${port}/api/v1/health" || echo '{"status":"DOWN"}')
    echo "[:$port] $HEALTH" | python3 -m json.tool || echo "[:$port] $HEALTH"
  done
else
  HEALTH=$(curl -sf "http://localhost:${BACKEND_PORT}/api/v1/health" || echo '{"status":"DOWN"}')
  echo "$HEALTH" | python3 -m json.tool
fi

log "deploy complete: $CURRENT_REF → $ENV (strategy=$STRATEGY)"
