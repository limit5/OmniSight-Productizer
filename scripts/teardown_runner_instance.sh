#!/usr/bin/env bash
# teardown_runner_instance.sh — clean shutdown of one runner instance (OP-783).
#
# Usage:
#   scripts/teardown_runner_instance.sh <bot-username>
#
# Idempotent:
#   * If the tmux session is not running, the script reports + exits 0.
#   * The worktree is NOT deleted (would clobber any in-flight uncommitted
#     work or salvage candidates). Operator removes manually if desired:
#         git worktree remove ../OmniSight-<bot>-worktree
#
# Per OP-783 acceptance criteria (e). Pairs with launch_runner_instance.sh.

set -Eeuo pipefail

if [[ "${1:-}" == "" ]]; then
  cat <<USAGE >&2
usage: $0 <bot-username>

  Stops the tmux session and clears the backpressure latch for one
  runner instance. The on-disk worktree, JIRA creds, and Gerrit SSH
  key are preserved for inspection / re-launch.
USAGE
  exit 64
fi

BOT_USERNAME="$1"
TMUX_SESSION="runner-${BOT_USERNAME}"
BACKPRESSURE_STATE="/tmp/runner-backpressure-${BOT_USERNAME}.state"

if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_OFF=$'\033[0m'
else
  C_OK= C_WARN= C_OFF=
fi
ok()   { printf '  %s[OK]%s   %s\n' "$C_OK"   "$C_OFF" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }

if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  tmux kill-session -t "$TMUX_SESSION"
  ok "tmux session '$TMUX_SESSION' stopped"
else
  warn "no tmux session '$TMUX_SESSION' to stop"
fi

if [[ -f "$BACKPRESSURE_STATE" ]]; then
  rm -f "$BACKPRESSURE_STATE"
  ok "cleared backpressure latch $BACKPRESSURE_STATE"
fi

printf '\nWorktree, creds, and idempotency DB preserved.\n'
printf 'To remove the worktree manually:\n'
printf '  git worktree remove ../OmniSight-%s-worktree\n' "$BOT_USERNAME"
