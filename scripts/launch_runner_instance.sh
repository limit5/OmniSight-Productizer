#!/usr/bin/env bash
# launch_runner_instance.sh — bring up one OmniSight runner instance (OP-783).
#
# Usage:
#   scripts/launch_runner_instance.sh <bot-username> [agent_class]
#   scripts/launch_runner_instance.sh codex-bot-2
#   scripts/launch_runner_instance.sh claude-bot-3 subscription-claude
#   scripts/launch_runner_instance.sh codex-bot                  # default instance
#
# Idempotent:
#   * If the worktree already exists, it is reused.
#   * If the tmux session for this bot is already running, the script
#     reports status + exits 0 without spawning a duplicate.
#   * Re-running after a clean teardown re-creates everything.
#
# The runner process this launches reads OMNISIGHT_RUNNER_INSTANCE_ID and
# resolves per-bot JIRA creds, Gerrit SSH key, backpressure state, and
# idempotency DB via backend.agents.jira_dispatch helpers.
#
# Per OP-783 acceptance criteria (e). See docs/operations/multi-instance-runner-runbook.md
# for end-to-end ops.

set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "" ]]; then
  cat <<USAGE >&2
usage: $0 <bot-username> [agent_class]

  bot-username:
    codex-bot              default-instance codex (legacy)
    codex-bot-N            codex instance N  (N >= 2)
    claude-bot             default-instance claude (legacy)
    claude-bot-N           claude instance N (N >= 2)

  agent_class (optional, auto-detected from bot-username prefix):
    subscription-codex     codex-bot* (default)
    subscription-claude    claude-bot*

See docs/operations/multi-instance-runner-provisioning.md for how to
provision a new bot account end-to-end.
USAGE
  exit 64
fi

BOT_USERNAME="$1"
AGENT_CLASS_OVERRIDE="${2:-}"

# ── Derive agent_class + instance_id from bot username ────────────────
case "$BOT_USERNAME" in
  codex-bot)        BASE="codex-bot";  INSTANCE_ID="default" ;;
  codex-bot-*)      BASE="codex-bot";  INSTANCE_ID="${BOT_USERNAME#codex-bot-}" ;;
  claude-bot)       BASE="claude-bot"; INSTANCE_ID="default" ;;
  claude-bot-*)     BASE="claude-bot"; INSTANCE_ID="${BOT_USERNAME#claude-bot-}" ;;
  *)
    echo "ERROR: bot-username must start with 'codex-bot' or 'claude-bot' (got '$BOT_USERNAME')" >&2
    exit 64
    ;;
esac

if [[ -n "$AGENT_CLASS_OVERRIDE" ]]; then
  AGENT_CLASS="$AGENT_CLASS_OVERRIDE"
else
  case "$BASE" in
    codex-bot)  AGENT_CLASS="subscription-codex" ;;
    claude-bot) AGENT_CLASS="subscription-claude" ;;
  esac
fi

# ── Resolve per-instance paths (must match backend.agents.jira_dispatch) ──
CRED_DIR="$HOME/.config/omnisight"
if [[ "$INSTANCE_ID" == "default" ]]; then
  case "$BASE" in
    codex-bot)
      JIRA_ENV="$CRED_DIR/jira-codex.env"
      JIRA_TOKEN="$CRED_DIR/jira-codex-token"
      ;;
    claude-bot)
      JIRA_ENV="$CRED_DIR/jira-claude.env"
      JIRA_TOKEN="$CRED_DIR/jira-claude-token"
      ;;
  esac
else
  JIRA_ENV="$CRED_DIR/jira-${BOT_USERNAME}.env"
  JIRA_TOKEN="$CRED_DIR/jira-${BOT_USERNAME}-token"
fi
SSH_KEY="$CRED_DIR/gerrit-${BOT_USERNAME}-ed25519"
IDEM_DB="$CRED_DIR/idem-keys-${BOT_USERNAME}.db"
BACKPRESSURE_STATE="/tmp/runner-backpressure-${BOT_USERNAME}.state"

if [[ "$INSTANCE_ID" == "default" ]]; then
  case "$BASE" in
    codex-bot)  WORKTREE="$REPO/../OmniSight-codex-worktree" ;;
    claude-bot) WORKTREE="$REPO/../OmniSight-claude-worktree" ;;
  esac
else
  WORKTREE="$REPO/../OmniSight-${BOT_USERNAME}-worktree"
fi
WORKTREE="$(cd "$(dirname "$WORKTREE")" && pwd)/$(basename "$WORKTREE")"

LOG_DIR="$HOME/work/sora/logs/runner"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${BOT_USERNAME}-$(date -u +%Y%m%dT%H%M%SZ).log"
TMUX_SESSION="runner-${BOT_USERNAME}"

# ── Output helpers ────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_OK= C_WARN= C_ERR= C_OFF=
fi
ok()   { printf '  %s[OK]%s   %s\n' "$C_OK"   "$C_OFF" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '  %s[FAIL]%s %s\n' "$C_ERR"  "$C_OFF" "$*" >&2; exit 1; }

printf 'Launching runner: bot=%s class=%s instance_id=%s\n' \
  "$BOT_USERNAME" "$AGENT_CLASS" "$INSTANCE_ID"
printf '  worktree:        %s\n' "$WORKTREE"
printf '  jira-env:        %s\n' "$JIRA_ENV"
printf '  ssh-key:         %s\n' "$SSH_KEY"
printf '  idempotency-db:  %s\n' "$IDEM_DB"
printf '  tmux-session:    %s\n' "$TMUX_SESSION"

# ── Step 1: verify creds present ──────────────────────────────────────
[[ -f "$JIRA_ENV" ]]   || die "JIRA env file missing: $JIRA_ENV"
[[ -f "$JIRA_TOKEN" ]] || die "JIRA token file missing: $JIRA_TOKEN"
[[ -f "$SSH_KEY" ]]    || die "Gerrit SSH key missing: $SSH_KEY (see docs/operations/multi-instance-runner-provisioning.md)"
ok "credentials present"

# ── Step 2: idempotent — early exit if session already running ───────
command -v tmux >/dev/null 2>&1 || die "tmux not installed — run 'sudo apt install tmux'"

if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  warn "tmux session '$TMUX_SESSION' already running — leaving alone (idempotent re-run)"
  tmux list-sessions | grep -F "$TMUX_SESSION" || true
  exit 0
fi

# ── Step 3: ensure worktree exists ────────────────────────────────────
if [[ ! -d "$WORKTREE/.git" && ! -f "$WORKTREE/.git" ]]; then
  printf '  worktree missing; creating: %s\n' "$WORKTREE"
  branch="runner-${BOT_USERNAME}"
  if ! git -C "$REPO" worktree add -B "$branch" "$WORKTREE" HEAD; then
    die "failed to create worktree at $WORKTREE"
  fi
  ok "worktree created at $WORKTREE"
else
  ok "worktree exists at $WORKTREE"
fi

# ── Step 4: install commit-msg hook (idempotent) ─────────────────────
HOOK_DIR="$(git -C "$WORKTREE" rev-parse --git-common-dir)/hooks"
HOOK_PATH="$HOOK_DIR/commit-msg"
if [[ ! -s "$HOOK_PATH" ]]; then
  mkdir -p "$HOOK_DIR"
  if curl -fsSL "https://sora.services:29420/tools/hooks/commit-msg" -o "$HOOK_PATH"; then
    chmod +x "$HOOK_PATH"
    ok "commit-msg hook installed at $HOOK_PATH"
  else
    warn "could not download commit-msg hook (Gerrit unreachable?); runner will retry at first tick"
  fi
else
  ok "commit-msg hook present at $HOOK_PATH"
fi

# ── Step 5: pin worktree-local git identity to this bot ──────────────
BOT_EMAIL="rt3628+${BOT_USERNAME}@gmail.com"
git -C "$WORKTREE" config --worktree user.email "$BOT_EMAIL"
git -C "$WORKTREE" config --worktree user.name  "$BOT_USERNAME"
ok "worktree git identity: $BOT_USERNAME <$BOT_EMAIL>"

# ── Step 6: spawn runner under tmux ──────────────────────────────────
# tee'd log file makes the live session greppable from outside tmux.
ENV_PRELUDE=(
  "OMNISIGHT_RUNNER_CLASS=$AGENT_CLASS"
  "OMNISIGHT_RUNNER_INSTANCE_ID=$INSTANCE_ID"
  "OMNISIGHT_IDEMPOTENCY_DB_PATH=$IDEM_DB"
)

if [[ "$AGENT_CLASS" == "subscription-codex" ]]; then
  ENV_PRELUDE+=("OMNISIGHT_CODEX_WORKTREE=$WORKTREE")
else
  ENV_PRELUDE+=("OMNISIGHT_CLAUDE_WORKTREE=$WORKTREE")
fi

# Start a poll loop inside tmux so the runner re-ticks every 90s, matching
# the systemd RestartSec= cadence. Operator can `tmux attach -t runner-<bot>`
# to inspect, or tail the log file from outside.
ENV_LINE="$(printf '%s ' "${ENV_PRELUDE[@]}")"
RUNNER_CMD="while true; do \
  $ENV_LINE python3 $REPO/auto-runner-jira.py 2>&1 | tee -a $LOG_FILE; \
  echo '[launcher] tick exit; sleeping 90s before next tick' | tee -a $LOG_FILE; \
  sleep 90; \
done"

tmux new-session -d -s "$TMUX_SESSION" "bash -c '$RUNNER_CMD'"
ok "tmux session '$TMUX_SESSION' started; logs: $LOG_FILE"

printf '\nDone. Inspect with:\n'
printf '  tmux attach -t %s        # detach with Ctrl-b d\n' "$TMUX_SESSION"
printf '  tail -f %s\n' "$LOG_FILE"
printf '  scripts/teardown_runner_instance.sh %s   # stop\n' "$BOT_USERNAME"
