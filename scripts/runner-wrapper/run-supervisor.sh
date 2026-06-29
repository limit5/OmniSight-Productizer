#!/usr/bin/env bash
# L1 runner-supervisor wrapper (OP-2488): clone develop fresh + run the
# supervisor once (--execute) + write a heartbeat the watchdog reads.
# Read-only observer (no code edits) so a shallow throwaway clone is enough;
# cloning fresh each firing means new rules merged to develop auto-activate.
set -uo pipefail
DEFAULT_BOT="claude-bot"
GERRIT_URL="${OMNISIGHT_GERRIT_URL:-ssh://${DEFAULT_BOT}@sora.services:29418/omnisight/OmniSight-Productizer}"
SSH_KEY="${OMNISIGHT_GERRIT_SSH_KEY:-$HOME/.config/omnisight/gerrit-${DEFAULT_BOT}-ed25519}"
MIRROR_DIR="${OMNISIGHT_RUNNER_MIRROR:-$HOME/git-mirror/omnisight.git}"
HEARTBEAT="${OMNISIGHT_SUPERVISOR_HEARTBEAT:-$HOME/.local/state/omnisight-supervisor/heartbeat}"
LOG="${OMNISIGHT_SUPERVISOR_LOG:-$HOME/work/sora/logs/supervisor/run.log}"
mkdir -p "$(dirname "$HEARTBEAT")" "$(dirname "$LOG")"
export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"
ws=$(mktemp -d /tmp/runner-supervisor.XXXXXX)
trap 'rm -rf "$ws"' EXIT
ts() { date '+%Y-%m-%dT%H:%M:%S'; }
if [[ -d "$MIRROR_DIR" ]]; then
  git clone --reference "$MIRROR_DIR" --depth=50 "$GERRIT_URL" "$ws" >>"$LOG" 2>&1 \
    || git clone --depth=50 "$GERRIT_URL" "$ws" >>"$LOG" 2>&1
else
  git clone --depth=50 "$GERRIT_URL" "$ws" >>"$LOG" 2>&1
fi
if [[ ! -f "$ws/backend/agents/runner_supervisor.py" ]]; then
  echo "$(ts) [supervisor-wrapper] ERROR: clone failed / module missing" >>"$LOG"; exit 1
fi
cd "$ws"
echo "$(ts) [supervisor-wrapper] running --execute" >>"$LOG"
PYTHONPATH="$ws" /usr/bin/python3 -m backend.agents.runner_supervisor --execute >>"$LOG" 2>&1
rc=$?
if [[ $rc -eq 0 ]]; then date +%s > "$HEARTBEAT"; echo "$(ts) [supervisor-wrapper] ok (heartbeat written)" >>"$LOG"; else
  echo "$(ts) [supervisor-wrapper] supervisor exited rc=$rc (heartbeat NOT updated)" >>"$LOG"; fi
exit $rc
