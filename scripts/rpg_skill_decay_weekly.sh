#!/usr/bin/env bash
# OP-217 -- weekly RPG.W12 skill XP decay sweep.
#
# Companion to deploy/systemd/rpg-skill-decay.{service,timer}. Wraps
# scripts/rpg_skill_decay.py so the systemd unit has a small,
# auditable surface that does the venv / log-routing / exit-code
# discipline. Idempotent: re-runs in the same day are safe because
# decay is week-quantised by ``apply_decay`` and only rows with a
# non-zero week delta change.
#
# Usage:
#   rpg_skill_decay_weekly.sh                 # decay-now (UTC clock)
#   rpg_skill_decay_weekly.sh --dry-run       # log-only, no DB write
#   rpg_skill_decay_weekly.sh --as-of ISO     # backdate for replay
#
# Exit codes:
#   0  -- sweep completed (zero or more rows touched)
#   1  -- DB or Python error (logged; systemd retries per timer)
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)

LOG_DIR=${OMNISIGHT_SKILL_DECAY_LOG_DIR:-${HOME}/work/sora/logs/rpg-skill-decay}
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/run.log"

PYTHON_BIN=${OMNISIGHT_PYTHON:-/usr/bin/python3}

cd "${REPO_ROOT}"
echo "[$(date -Is)] rpg_skill_decay start argv=$*" >>"${LOG_FILE}"

if "${PYTHON_BIN}" -m scripts.rpg_skill_decay "$@" >>"${LOG_FILE}" 2>&1; then
    echo "[$(date -Is)] rpg_skill_decay ok" >>"${LOG_FILE}"
    exit 0
else
    rc=$?
    echo "[$(date -Is)] rpg_skill_decay FAILED rc=${rc}" >>"${LOG_FILE}"
    exit 1
fi
