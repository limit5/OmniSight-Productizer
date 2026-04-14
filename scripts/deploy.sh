#!/usr/bin/env bash
#
# OmniSight deploy — single-machine WSL/systemd flow.
#
# Usage:
#   scripts/deploy.sh staging
#   scripts/deploy.sh prod v0.2.0      # check out the tag first
#
# Steps:
#   1. Optionally check out the requested git ref.
#   2. Online-backup the SQLite DB (WAL-safe).
#   3. Install backend deps + build the frontend.
#   4. systemctl restart with health-check polling.
#   5. Hit /health on the new pid.
#
# Conventions:
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

ENV=${1:-}
GIT_REF=${2:-}

if [[ -z "$ENV" ]]; then
  echo "usage: scripts/deploy.sh <env> [git-ref]" >&2
  exit 1
fi
if [[ "$ENV" != "prod" && "$ENV" != "staging" ]]; then
  echo "error: env must be 'staging' or 'prod' (got '$ENV')" >&2
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
log "deploying $CURRENT_REF to $ENV"

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

log "installing backend deps"
pip install --quiet -r backend/requirements.txt

log "installing frontend deps + building"
npm ci --no-audit --prefer-offline
npm run build

# ───────────────────────────────────────────────────────────────────
# 4. Restart + health check
# ───────────────────────────────────────────────────────────────────

log "restarting $BACKEND_UNIT"
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

# ───────────────────────────────────────────────────────────────────
# 5. Smoke
# ───────────────────────────────────────────────────────────────────

log "smoke test"
HEALTH=$(curl -sf "http://localhost:${BACKEND_PORT}/api/v1/health" || echo '{"status":"DOWN"}')
echo "$HEALTH" | python3 -m json.tool

log "deploy complete: $CURRENT_REF → $ENV"
