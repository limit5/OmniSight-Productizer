#!/usr/bin/env bash
#
# OmniSight deploy — single-machine WSL/systemd flow + G2 rolling mode.
#
# Usage:
#   scripts/deploy.sh staging
#   scripts/deploy.sh prod v0.2.0                      # check out the tag first
#   scripts/deploy.sh prod v0.2.0 rolling              # G2 #3 HA-02 rolling restart
#   scripts/deploy.sh --strategy blue-green prod v0.2.0 # G3 HA-03 blue-green cutover
#   scripts/deploy.sh --rollback                        # G3 HA-03 row 1356 秒級切回 previous color
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
#   are supplied the flag wins.
#
# Rollback flag (G3 HA-03 TODO row 1356):
#   `--rollback` is the "秒級切回 previous color" instant-fallback path.
#   It does NOT run git fetch / pip install / pnpm build / systemctl
#   restart / docker compose up — the old color's container has been
#   kept warm by the row-1355 ceremony for exactly this purpose, so all
#   we do is flip the symlink back and (optionally) reload Caddy. This
#   is why rollback is measured in seconds, not minutes. The rollback
#   block runs before any positional-env validation so it works even
#   without supplying a trailing `prod`/`staging` arg — the operator's
#   muscle memory at 3am shouldn't be "which env was I deploying?".
#
#   Pre-flight gates (rollback refuses and exits non-zero unless each
#   passes, so we never point Caddy at a dead upstream):
#     (a) blue-green primitive present (exit 5)
#     (b) `previous_color` breadcrumb exists (exit 2)
#     (c) retention window still open — now <= previous_retention_until
#         (exit 8; bypass with OMNISIGHT_ROLLBACK_FORCE=1 — DANGEROUS)
#     (d) previous color's host-port /readyz returns 200 (exit 3;
#         bypass with OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1 — DANGEROUS,
#         only if you're about to recreate the container manually)
#
#   Exit codes (--rollback):
#     0 — symlink flipped back, previous color is now active
#     2 — no previous_color recorded (never had a cutover)
#     3 — previous color's /readyz dead (container pruned?)
#     5 — blue-green primitive / state dir missing
#     8 — retention window expired (24 h default)
#
#   Dry-run: OMNISIGHT_BLUEGREEN_DRY_RUN=1 prints the plan and exits 0
#   without any symlink mutation.
#
# Blue-green ceremony (G3 HA-03 TODO row 1355):
#   `--strategy blue-green` runs the full cutover ceremony:
#     1. Recreate STANDBY container with new image; wait for /readyz.
#     2. Pre-cut smoke (`scripts/prod_smoke_test.py` on standby host port)
#        — smoke fail = exit 6, NO symlink flip, active color unchanged.
#     3. Atomic cutover via `scripts/bluegreen_switch.sh set-active`
#        (rename(2) over the `deploy/blue-green/active_upstream.caddy` symlink).
#     4. Caddy reload (via `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD`).
#     5. Record retention breadcrumbs (cutover_timestamp + previous_retention_until).
#     6. 5-minute `/readyz` observation window; consecutive failures =
#        exit 7 (operator runs `scripts/deploy.sh --rollback` — row 1356).
#     7. OLD color's container stays warm for 24 h so rollback is instant.
#   Tunables: OMNISIGHT_BLUEGREEN_{SMOKE_TIMEOUT,OBSERVE_SECONDS,
#     OBSERVE_INTERVAL,OBSERVE_MAX_FAILURES,RETENTION_HOURS}
#   Escape hatches: OMNISIGHT_BLUEGREEN_DRY_RUN=1 / OMNISIGHT_BLUEGREEN_SKIP_SMOKE=1
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

# ── Flag parsing (G3 #1 / TODO row 1353, #4 / row 1356) ────────────────
# GNU-style flags are stripped first so the downstream positional
# contract (ENV / GIT_REF / STRATEGY_ARG = $1 / $2 / $3) is unchanged.
# `--strategy <value>` / `--strategy=<value>` and `--rollback` are
# parsed here; everything else bubbles back up as a positional so
# future flags can slot in next to them without a rewrite.
STRATEGY_FLAG=""
ROLLBACK_FLAG=0
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
    --rollback)
      ROLLBACK_FLAG=1
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
      echo "usage: scripts/deploy.sh [--strategy rolling|systemd|blue-green] [--rollback] <env> [git-ref] [rolling|systemd|blue-green]" >&2
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

# ── Rollback fast-path (G3 #4 / TODO row 1356) ────────────────────────
# `--rollback` deliberately short-circuits the ENV requirement, the git
# checkout, the db backup, the pip/pnpm build, and the strategy
# dispatch. The previous color's container was kept warm by row 1355's
# ceremony precisely so rollback is a single-rename(2) symlink flip —
# measured in seconds, not minutes. Putting this block BEFORE the
# `ENV` check is what lets operators type `scripts/deploy.sh --rollback`
# with no other args at 3am.
if [[ "$ROLLBACK_FLAG" == "1" ]]; then
  ROOT=$(cd "$(dirname "$0")/.." && pwd)
  cd "$ROOT"

  rblog() { printf '\033[35m[rollback]\033[0m %s\n' "$*"; }

  # Honour OMNISIGHT_BLUEGREEN_DIR the same way bluegreen_switch.sh does
  # so contract tests can sandbox the state dir without mutating the
  # committed repo. Default is $ROOT/deploy/blue-green (production).
  BLUEGREEN_STATE_DIR="${OMNISIGHT_BLUEGREEN_DIR:-$ROOT/deploy/blue-green}"
  BLUEGREEN_SWITCH="$ROOT/scripts/bluegreen_switch.sh"

  # (a) Primitive present?
  if [[ ! -x "$BLUEGREEN_SWITCH" || ! -d "$BLUEGREEN_STATE_DIR" ]]; then
    echo "[rollback] blue-green primitive missing (expected $BLUEGREEN_SWITCH + $BLUEGREEN_STATE_DIR) — nothing to roll back (has the G3 ceremony ever run on this host?)" >&2
    exit 5
  fi

  RB_ACTIVE_FILE="$BLUEGREEN_STATE_DIR/active_color"
  RB_PREV_FILE="$BLUEGREEN_STATE_DIR/previous_color"
  RB_RETENTION_FILE="$BLUEGREEN_STATE_DIR/previous_retention_until"

  # (b) previous_color breadcrumb?
  if [[ ! -f "$RB_PREV_FILE" ]]; then
    echo "[rollback] no previous_color recorded at $RB_PREV_FILE — there is no prior color to roll back to (has a cutover happened on this host?)" >&2
    exit 2
  fi
  RB_PREV_COLOR=$(tr -d '[:space:]' < "$RB_PREV_FILE")
  if [[ "$RB_PREV_COLOR" != "blue" && "$RB_PREV_COLOR" != "green" ]]; then
    echo "[rollback] invalid previous_color '$RB_PREV_COLOR' in $RB_PREV_FILE (expected blue|green)" >&2
    exit 2
  fi

  if [[ ! -f "$RB_ACTIVE_FILE" ]]; then
    echo "[rollback] no active_color state at $RB_ACTIVE_FILE — state dir looks uninitialised" >&2
    exit 5
  fi
  RB_CURR_COLOR=$(tr -d '[:space:]' < "$RB_ACTIVE_FILE")

  # No-op guard: if active already equals previous (e.g. two rollbacks
  # in quick succession without an intervening cutover), we're already
  # where we'd flip to. Bail cleanly rather than ping-pong.
  if [[ "$RB_CURR_COLOR" == "$RB_PREV_COLOR" ]]; then
    rblog "already on $RB_CURR_COLOR (previous_color=$RB_PREV_COLOR) — nothing to roll back (no-op)"
    exit 0
  fi

  # Map color → host port (mirrors blue-green arm + G2 compose topology).
  case "$RB_PREV_COLOR" in
    blue)  RB_PREV_PORT=8000 ;;
    green) RB_PREV_PORT=8001 ;;
    *)     echo "[rollback] unknown color '$RB_PREV_COLOR'" >&2; exit 2 ;;
  esac

  rblog "plan: $RB_CURR_COLOR → $RB_PREV_COLOR (previous host port :$RB_PREV_PORT)"

  # (c) Retention window. 24 h default (row 1355 breadcrumb). Bypass
  # via OMNISIGHT_ROLLBACK_FORCE=1 for the rare case where an operator
  # wants to try anyway (e.g. after manually recreating the old color).
  if [[ -f "$RB_RETENTION_FILE" ]]; then
    RB_RETENTION_UNTIL=$(tr -d '[:space:]' < "$RB_RETENTION_FILE")
    RB_NOW=$(date +%s)
    if [[ "$RB_RETENTION_UNTIL" =~ ^[0-9]+$ ]] && (( RB_NOW > RB_RETENTION_UNTIL )); then
      if [[ "${OMNISIGHT_ROLLBACK_FORCE:-0}" != "1" ]]; then
        RB_RETENTION_ISO=$(date -u -d "@$RB_RETENTION_UNTIL" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
          || python3 -c "import datetime,sys; print(datetime.datetime.utcfromtimestamp(int(sys.argv[1])).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$RB_RETENTION_UNTIL")
        echo "[rollback] retention window EXPIRED (ended $RB_RETENTION_ISO, now=$RB_NOW) — previous color ($RB_PREV_COLOR) may have been pruned. Rolling back blind would point Caddy at a dead upstream." >&2
        echo "[rollback]   bypass: OMNISIGHT_ROLLBACK_FORCE=1 (DANGEROUS — only if you verified $RB_PREV_COLOR is still live)" >&2
        exit 8
      fi
      echo "[rollback] WARN: retention window expired but OMNISIGHT_ROLLBACK_FORCE=1 is set — proceeding at operator's risk" >&2
    fi
  else
    echo "[rollback] WARN: no previous_retention_until breadcrumb at $RB_RETENTION_FILE — proceeding without retention gate (pre-row-1355 state dir?)" >&2
  fi

  # Dry-run exit point — everything above is read-only state inspection;
  # from here on we'd actually flip the symlink + write breadcrumbs.
  if [[ "${OMNISIGHT_BLUEGREEN_DRY_RUN:-0}" == "1" ]]; then
    rblog "OMNISIGHT_BLUEGREEN_DRY_RUN=1 — plan printed, no symlink / Caddy changes"
    exit 0
  fi

  # (d) /readyz pre-flight — we refuse to flip to a dead upstream.
  # The whole point of the 24 h warm-standby is instant rollback; if
  # the container isn't responding, flipping the symlink would just
  # shift the outage to the "rolled back" pretense. Bypass is for the
  # rare case where the operator is about to `docker compose up
  # backend-<prev>` right after this runs.
  if [[ "${OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT:-0}" != "1" ]]; then
    rb_ready_url="http://localhost:${RB_PREV_PORT}/readyz"
    if ! curl -sf -m 5 "$rb_ready_url" >/dev/null 2>&1; then
      rb_prev_svc="backend-a"; [[ "$RB_PREV_COLOR" == "green" ]] && rb_prev_svc="backend-b"
      echo "[rollback] previous color ($RB_PREV_COLOR :$RB_PREV_PORT) /readyz NOT responding — rolling back would point Caddy at a dead upstream." >&2
      echo "[rollback]   triage: docker compose -f docker-compose.prod.yml logs --tail=200 $rb_prev_svc" >&2
      echo "[rollback]   bypass: OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1 (DANGEROUS — only if you're about to recreate $rb_prev_svc manually)" >&2
      exit 3
    fi
    rblog "pre-flight: previous color ($RB_PREV_COLOR :$RB_PREV_PORT) /readyz OK"
  else
    echo "[rollback] WARN: OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1 — skipping /readyz pre-flight (DANGEROUS)" >&2
  fi

  # ── ATOMIC SYMLINK FLIP ───────────────────────────────────────────
  # Delegate to the G3 #2 primitive — rename(2) over the symlink. This
  # is THE cutover (everything above is read-only; everything below is
  # post-flip audit). Passing OMNISIGHT_BLUEGREEN_DIR through so the
  # primitive targets the same sandbox we just validated.
  rblog "ATOMIC ROLLBACK → $RB_PREV_COLOR (was: $RB_CURR_COLOR)"
  OMNISIGHT_BLUEGREEN_DIR="$BLUEGREEN_STATE_DIR" "$BLUEGREEN_SWITCH" rollback

  # Optional Caddy reload — same delegation pattern as the row-1355 arm.
  BLUEGREEN_CADDY_RELOAD_CMD="${OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD:-}"
  if [[ -n "$BLUEGREEN_CADDY_RELOAD_CMD" ]]; then
    rblog "reloading Caddy ($BLUEGREEN_CADDY_RELOAD_CMD)"
    if ! bash -c "$BLUEGREEN_CADDY_RELOAD_CMD"; then
      echo "[rollback] WARN: Caddy reload command failed — symlink is already on $RB_PREV_COLOR, but the running Caddy may still see the old upstream. Run the reload command by hand." >&2
    fi
  else
    rblog "OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD not set — reload Caddy manually (e.g. docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile)"
  fi

  # Audit breadcrumb — row 1357 runbook + any future timeline reconstructor
  # reads this to know when the rollback happened. Same atomic tmp-then-mv
  # pattern as the row-1355 ceremony so a concurrent reader never sees a
  # half-written file.
  rb_ts=$(date +%s)
  printf '%s\n' "$rb_ts" > "$BLUEGREEN_STATE_DIR/rollback_timestamp.tmp.$$"
  mv -f "$BLUEGREEN_STATE_DIR/rollback_timestamp.tmp.$$" "$BLUEGREEN_STATE_DIR/rollback_timestamp"

  rblog "rollback complete: now active=$RB_PREV_COLOR (:$RB_PREV_PORT). Old color ($RB_CURR_COLOR) still running as new standby — investigate logs before next cutover."
  exit 0
fi

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
  # G3 HA-03 blue-green cutover ceremony (TODO rows 1353-1357).
  #
  # Ceremony flow (row 1355):
  #   (1) Resolve active/standby colors from `bluegreen_switch.sh status`.
  #   (2) Re-create the standby container with the new image
  #       (`docker compose up -d --no-deps --force-recreate backend-<standby>`)
  #       and wait for `/readyz` on the standby host port.
  #   (3) Pre-cut smoke: run `scripts/prod_smoke_test.py` against the
  #       STANDBY replica (not the proxy) — every DAG runs through the
  #       fresh code *before* any client traffic touches it. Smoke fail
  #       → exit 6, NO cutover occurs, standby container left running
  #       so the operator can triage with `docker compose logs backend-<standby>`.
  #   (4) Atomic cutover via `bluegreen_switch.sh set-active <standby>` —
  #       rename(2)-based symlink flip, Caddy sees the new upstream on
  #       its next config reload. Optional reload via
  #       `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD` (operators running Caddy
  #       outside compose skip this and reload by hand).
  #   (5) Record retention breadcrumbs: `deploy/blue-green/cutover_timestamp`
  #       (Unix seconds of cutover) + `deploy/blue-green/previous_retention_until`
  #       (cutover + 24 h) so row 1356 rollback can verify it's still
  #       within the retention window and row 1357 runbook can surface
  #       when the old color becomes eligible for pruning.
  #   (6) 5-min observation: poll `/readyz` on the new active's host port
  #       every 15 s. More than `OMNISIGHT_BLUEGREEN_OBSERVE_MAX_FAILURES`
  #       consecutive failures → exit 7 (operator should run
  #       `deploy.sh --rollback`).
  #   (7) The OLD color's container is NEVER stopped — it stays warm,
  #       still passing `/readyz` probes, ready for instant rollback
  #       during the 24 h retention window.
  #
  # Exit codes (blue-green):
  #   0 — ceremony passed (smoke + cutover + 5-min observe all green)
  #   3 — standby failed to come up (before cutover — NO symlink flip)
  #   4 — compose file missing
  #   5 — blue-green primitive / state dir missing (can't resolve colors)
  #   6 — pre-cut smoke failed (before cutover — NO symlink flip)
  #   7 — 5-min observation window detected degradation (cutover DID
  #       happen, operator should run `deploy.sh --rollback`)
  #
  # Escape hatches:
  #   OMNISIGHT_BLUEGREEN_DRY_RUN=1   — print the plan, exit 0 before any
  #                                     docker / symlink mutation (used by
  #                                     contract tests + operator sanity check).
  #   OMNISIGHT_BLUEGREEN_SKIP_SMOKE=1 — SKIP the pre-cut smoke (DANGEROUS
  #                                     — only for local dev against fixtures
  #                                     without the full DAG runner).
  log "blue-green mode: compose=$COMPOSE_FILE (active/standby cutover)"
  if [[ ! -f "$ROOT/$COMPOSE_FILE" ]]; then
    echo "[deploy] blue-green: compose file '$COMPOSE_FILE' missing — cannot select active/standby color" >&2
    exit 4
  fi

  BLUEGREEN_SWITCH="$ROOT/scripts/bluegreen_switch.sh"
  BLUEGREEN_STATE_DIR="$ROOT/deploy/blue-green"
  if [[ ! -x "$BLUEGREEN_SWITCH" || ! -d "$BLUEGREEN_STATE_DIR" ]]; then
    echo "[deploy] blue-green: atomic switch primitive missing (expected $BLUEGREEN_SWITCH + $BLUEGREEN_STATE_DIR). Ship TODO row 1354 first." >&2
    exit 5
  fi

  # Tunables — all env-overridable so contract tests and operators can
  # shorten the observe window / skip smoke without editing this file.
  BLUEGREEN_SMOKE_TIMEOUT="${OMNISIGHT_BLUEGREEN_SMOKE_TIMEOUT:-300}"
  BLUEGREEN_OBSERVE_SECONDS="${OMNISIGHT_BLUEGREEN_OBSERVE_SECONDS:-300}"
  BLUEGREEN_OBSERVE_INTERVAL="${OMNISIGHT_BLUEGREEN_OBSERVE_INTERVAL:-15}"
  BLUEGREEN_OBSERVE_MAX_FAILURES="${OMNISIGHT_BLUEGREEN_OBSERVE_MAX_FAILURES:-3}"
  BLUEGREEN_RETENTION_HOURS="${OMNISIGHT_BLUEGREEN_RETENTION_HOURS:-24}"
  BLUEGREEN_STANDBY_READY_TIMEOUT="${OMNISIGHT_BLUEGREEN_STANDBY_READY_TIMEOUT:-${ROLL_READY_TIMEOUT}}"
  BLUEGREEN_CADDY_RELOAD_CMD="${OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD:-}"

  # Map color → compose service + host port (matches docker-compose.prod.yml
  # dual-replica topology G2 #2 / TODO row 1346).
  bluegreen_service_for_color() {
    case "$1" in
      blue)  echo "backend-a" ;;
      green) echo "backend-b" ;;
      *)     echo "" ;;
    esac
  }
  bluegreen_port_for_color() {
    case "$1" in
      blue)  echo 8000 ;;
      green) echo 8001 ;;
      *)     echo "" ;;
    esac
  }

  log "blue-green state (row 1354 primitive):"
  if ! "$BLUEGREEN_SWITCH" status | sed 's/^/  /' >&2; then
    echo "[deploy] blue-green: bluegreen_switch.sh status failed — state may need reconciliation" >&2
    exit 5
  fi

  # (1) Resolve colors. `bluegreen_switch.sh status` prints `active=<color>`
  # on stdout; we parse it so the ceremony knows which replica is the
  # pre-cut smoke target.
  status_out=$("$BLUEGREEN_SWITCH" status)
  BG_ACTIVE=$(printf '%s\n' "$status_out" | sed -n 's/^active=//p')
  BG_STANDBY=$(printf '%s\n' "$status_out" | sed -n 's/^standby=//p')
  if [[ -z "$BG_ACTIVE" || -z "$BG_STANDBY" ]]; then
    echo "[deploy] blue-green: could not parse active/standby from switch status output:" >&2
    echo "$status_out" | sed 's/^/  /' >&2
    exit 5
  fi
  BG_STANDBY_SVC=$(bluegreen_service_for_color "$BG_STANDBY")
  BG_STANDBY_PORT=$(bluegreen_port_for_color "$BG_STANDBY")
  BG_ACTIVE_PORT=$(bluegreen_port_for_color "$BG_ACTIVE")
  if [[ -z "$BG_STANDBY_SVC" || -z "$BG_STANDBY_PORT" || -z "$BG_ACTIVE_PORT" ]]; then
    echo "[deploy] blue-green: unknown color mapping (active=$BG_ACTIVE standby=$BG_STANDBY)" >&2
    exit 5
  fi

  log "blue-green plan: cutover $BG_ACTIVE (:$BG_ACTIVE_PORT, keep warm 24 h) → $BG_STANDBY (:$BG_STANDBY_PORT, $BG_STANDBY_SVC) via pre-cut smoke → atomic switch → ${BLUEGREEN_OBSERVE_SECONDS}s observe"

  if [[ "${OMNISIGHT_BLUEGREEN_DRY_RUN:-0}" == "1" ]]; then
    log "blue-green: OMNISIGHT_BLUEGREEN_DRY_RUN=1 — plan printed, no docker / symlink changes"
    log "blue-green: dry-run complete (standby=$BG_STANDBY would be recreated + smoked, then symlink flipped)"
    exit 0
  fi

  # (2) Re-create standby container with the new image. `--no-deps`
  # avoids touching the frontend / active replica; `--force-recreate`
  # ensures the new image + env take effect even if the tag is the
  # same as before.
  log "blue-green[$BG_STANDBY_SVC]: recreating standby container"
  docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "$BG_STANDBY_SVC"

  log "blue-green[$BG_STANDBY_SVC]: waiting for /readyz on :$BG_STANDBY_PORT (timeout: ${BLUEGREEN_STANDBY_READY_TIMEOUT}s)"
  standby_ready_url="http://localhost:${BG_STANDBY_PORT}/readyz"
  standby_ready=0
  standby_waited=0
  while (( standby_waited < BLUEGREEN_STANDBY_READY_TIMEOUT )); do
    if curl -sf -m 2 "$standby_ready_url" >/dev/null 2>&1; then
      standby_ready=1
      break
    fi
    sleep "$ROLL_POLL_INTERVAL"
    standby_waited=$((standby_waited + ROLL_POLL_INTERVAL))
  done
  if [[ "$standby_ready" != "1" ]]; then
    echo "[deploy] blue-green[$BG_STANDBY_SVC]: /readyz never returned 200 within ${BLUEGREEN_STANDBY_READY_TIMEOUT}s — aborting BEFORE cutover (active color unchanged)" >&2
    echo "[deploy]    triage: docker compose -f $COMPOSE_FILE logs --tail=200 $BG_STANDBY_SVC" >&2
    exit 3
  fi
  log "blue-green[$BG_STANDBY_SVC]: /readyz pass — standby is up on :$BG_STANDBY_PORT"

  # (3) Pre-cut smoke on standby. We point prod_smoke_test.py DIRECTLY
  # at the standby host port so the DAGs run through the fresh code
  # before Caddy routes any real user there. Bypassing the proxy is
  # deliberate: we want "does the new image work end-to-end?", not
  # "does the load balancer work?".
  if [[ "${OMNISIGHT_BLUEGREEN_SKIP_SMOKE:-0}" == "1" ]]; then
    echo "[deploy] blue-green: OMNISIGHT_BLUEGREEN_SKIP_SMOKE=1 — skipping pre-cut smoke (DANGEROUS — dev-only)" >&2
  else
    log "blue-green: pre-cut smoke on standby (scripts/prod_smoke_test.py → http://localhost:$BG_STANDBY_PORT, timeout ${BLUEGREEN_SMOKE_TIMEOUT}s)"
    if ! timeout "$BLUEGREEN_SMOKE_TIMEOUT" python3 "$ROOT/scripts/prod_smoke_test.py" "http://localhost:$BG_STANDBY_PORT"; then
      echo "[deploy] blue-green: PRE-CUT SMOKE FAILED — aborting BEFORE cutover (active color unchanged, standby left warm for triage)" >&2
      echo "[deploy]    triage: docker compose -f $COMPOSE_FILE logs --tail=200 $BG_STANDBY_SVC" >&2
      echo "[deploy]    triage: curl http://localhost:$BG_STANDBY_PORT/readyz" >&2
      exit 6
    fi
    log "blue-green: pre-cut smoke PASS — safe to cut over"
  fi

  # (4) Atomic cutover — the one-line rename(2) flip. Everything before
  # this point is reversible by just redeploying the same image; this
  # line is the actual traffic flip.
  log "blue-green: ATOMIC CUTOVER → $BG_STANDBY (was: $BG_ACTIVE)"
  "$BLUEGREEN_SWITCH" set-active "$BG_STANDBY"

  # Reload Caddy so the new symlink target takes effect. The command
  # varies by topology (docker compose service vs. host-installed
  # systemd unit vs. external LB), so we delegate to an operator-
  # supplied env override. If empty, emit a hint — the symlink is
  # already swapped, so a manual `caddy reload` is all that's left.
  if [[ -n "$BLUEGREEN_CADDY_RELOAD_CMD" ]]; then
    log "blue-green: reloading Caddy ($BLUEGREEN_CADDY_RELOAD_CMD)"
    if ! bash -c "$BLUEGREEN_CADDY_RELOAD_CMD"; then
      echo "[deploy] WARN: Caddy reload command failed — symlink is already on $BG_STANDBY, but the running Caddy may still see the old upstream. Run the reload command by hand." >&2
    fi
  else
    log "blue-green: OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD not set — reload Caddy manually (e.g. docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile)"
  fi

  # (5) Retention breadcrumbs — row 1356 rollback reads these to verify
  # the old color is still within the 24 h rollback window; row 1357
  # runbook surfaces them to the operator.
  cutover_ts=$(date +%s)
  retention_until=$((cutover_ts + BLUEGREEN_RETENTION_HOURS * 3600))
  retention_until_iso=$(date -u -d "@$retention_until" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
    || python3 -c "import datetime,sys; print(datetime.datetime.utcfromtimestamp(int(sys.argv[1])).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$retention_until")
  printf '%s\n' "$cutover_ts" > "$BLUEGREEN_STATE_DIR/cutover_timestamp.tmp.$$"
  mv -f "$BLUEGREEN_STATE_DIR/cutover_timestamp.tmp.$$" "$BLUEGREEN_STATE_DIR/cutover_timestamp"
  printf '%s\n' "$retention_until" > "$BLUEGREEN_STATE_DIR/previous_retention_until.tmp.$$"
  mv -f "$BLUEGREEN_STATE_DIR/previous_retention_until.tmp.$$" "$BLUEGREEN_STATE_DIR/previous_retention_until"
  log "blue-green: retention window — old color ($BG_ACTIVE) kept warm until $retention_until_iso (+${BLUEGREEN_RETENTION_HOURS}h). Do NOT prune $BG_ACTIVE's container before that."

  # (6) 5-minute observation window. Poll new active's /readyz; if we
  # hit OBSERVE_MAX_FAILURES consecutive failures we exit 7 so the
  # operator knows to run `deploy.sh --rollback` (row 1356).
  new_active_url="http://localhost:${BG_STANDBY_PORT}/readyz"
  log "blue-green: observation window — polling $new_active_url every ${BLUEGREEN_OBSERVE_INTERVAL}s for ${BLUEGREEN_OBSERVE_SECONDS}s"
  observe_elapsed=0
  observe_failures=0
  observe_checks=0
  while (( observe_elapsed < BLUEGREEN_OBSERVE_SECONDS )); do
    observe_checks=$((observe_checks + 1))
    if curl -sf -m 5 "$new_active_url" >/dev/null 2>&1; then
      observe_failures=0
    else
      observe_failures=$((observe_failures + 1))
      echo "[deploy] blue-green: observation probe #$observe_checks FAIL (consecutive=$observe_failures/$BLUEGREEN_OBSERVE_MAX_FAILURES)" >&2
      if (( observe_failures >= BLUEGREEN_OBSERVE_MAX_FAILURES )); then
        echo "[deploy] blue-green: observation window DETECTED DEGRADATION ($observe_failures consecutive /readyz failures) — run: scripts/deploy.sh --rollback" >&2
        exit 7
      fi
    fi
    sleep "$BLUEGREEN_OBSERVE_INTERVAL"
    observe_elapsed=$((observe_elapsed + BLUEGREEN_OBSERVE_INTERVAL))
  done
  log "blue-green: observation window PASS ($observe_checks probes over ${BLUEGREEN_OBSERVE_SECONDS}s, no sustained failure)"

  # (7) Old color container is intentionally left running — warm for
  # the 24 h retention window so row 1356 `deploy.sh --rollback` can
  # flip the symlink back in seconds without needing to recreate the
  # container. Operators running a disk-tight host can manually
  # `docker compose stop backend-<old>` after the retention_until
  # timestamp, or let row 1357 cron prune it.
  log "blue-green: deploy complete — new active is $BG_STANDBY (:$BG_STANDBY_PORT), old $BG_ACTIVE (:$BG_ACTIVE_PORT) kept warm for ${BLUEGREEN_RETENTION_HOURS}h rollback retention"
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
