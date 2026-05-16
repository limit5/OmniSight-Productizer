#!/bin/bash
# OP-1137 / Sprint Boreas-C2 — Ephemeral cycle wrapper.
#
# Per-cycle lifecycle (replaces /tmp/runner_wrapper.sh which used shared
# .git via `git worktree add`):
#
#   1. mktemp a fresh workspace at $WORKSPACE_ROOT/$INSTANCE/run-XXXXXX
#   2. `git clone --reference $MIRROR_DIR --depth=50` into it (5-10s)
#   3. Set OMNISIGHT_{CODEX,CLAUDE}_WORKTREE = workspace so the runner's
#      CLI invocation chooses this directory as both cwd + worktree
#   4. Invoke `python3 -u $workspace/auto-runner-jira.py` — runner reads
#      itself from inside the ephemeral clone, so its capability_matrix /
#      jira_dispatch / etc. are guaranteed in lockstep with the worktree
#      (closes the OP-1126/1124 overnight bug class).
#   5. rm -rf the workspace on any exit (trap EXIT covers SIGTERM, normal
#      completion, internal error — but NOT SIGKILL; orphan sweep
#      OP-1139 handles that residual class).
#   6. Sleep 30s between cycles, repeat.
#
# Companion: scripts/runner-mirror/setup.sh + runner-mirror-fetch.timer
# (OP-1136) keep the bare mirror fresh so step 2 stays fast.
#
# Usage:
#   bash scripts/runner-wrapper/run-ephemeral.sh <instance> <agent_class> [<ignored>]
#
#   Third positional arg (worktree_path) is accepted for back-compat with
#   the old wrapper signature but IGNORED — workspace is auto-created.

set -u

INSTANCE="$1"
CLASS="$2"
# $3 deliberately ignored (was worktree_path in legacy wrapper)

LOG="/tmp/runner-$INSTANCE.log"

# Auto-detect bot identity from agent_class. Operator can still pin any
# of these via the matching OMNISIGHT_* env var (e.g., for testing or
# per-instance accounts when those land).
case "$CLASS" in
    subscription-codex|api-openai)
        DEFAULT_BOT="codex-bot"
        ;;
    *)
        DEFAULT_BOT="claude-bot"
        ;;
esac

WORKSPACE_ROOT="${OMNISIGHT_RUNNER_WORKSPACE_ROOT:-/tmp/runner-workspaces}"
MIRROR_DIR="${OMNISIGHT_GIT_MIRROR_DIR:-$HOME/git-mirror/omnisight.git}"
GERRIT_URL="${OMNISIGHT_GERRIT_URL:-ssh://${DEFAULT_BOT}@sora.services:29418/omnisight/OmniSight-Productizer}"
SSH_KEY="${OMNISIGHT_GERRIT_SSH_KEY:-$HOME/.config/omnisight/gerrit-${DEFAULT_BOT}-ed25519}"
CLONE_DEPTH="${OMNISIGHT_RUNNER_CLONE_DEPTH:-50}"
CYCLE_SLEEP_S="${OMNISIGHT_RUNNER_CYCLE_SLEEP_S:-30}"

mkdir -p "$WORKSPACE_ROOT/$INSTANCE"

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ephemeral wrapper started ===" >> "$LOG"
echo "    instance=$INSTANCE class=$CLASS" >> "$LOG"
echo "    workspace_root=$WORKSPACE_ROOT/$INSTANCE" >> "$LOG"
echo "    mirror=$MIRROR_DIR" >> "$LOG"

# Pre-flight: mirror must exist
if [[ ! -d "$MIRROR_DIR" ]] || ! git -C "$MIRROR_DIR" rev-parse --is-bare-repository >/dev/null 2>&1; then
    echo "[ephemeral-wrapper] ERROR: mirror not at $MIRROR_DIR — run scripts/runner-mirror/setup.sh first" >> "$LOG"
    exit 2
fi
if [[ ! -f "$SSH_KEY" ]]; then
    echo "[ephemeral-wrapper] ERROR: SSH key not found at $SSH_KEY" >> "$LOG"
    exit 2
fi

export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"

cycle=0
while true; do
    cycle=$((cycle + 1))

    # Workspace: fresh per cycle
    workspace=$(mktemp -d "$WORKSPACE_ROOT/$INSTANCE/run-XXXXXX")
    # trap covers normal exit, SIGTERM, SIGINT, and the while-loop's `continue`.
    # SIGKILL of this script itself orphans the workspace — handled by OP-1139 sweeper.
    cleanup() { [[ -n "${workspace:-}" && -d "$workspace" ]] && rm -rf "$workspace"; }
    trap cleanup EXIT INT TERM

    echo "" >> "$LOG"
    echo "=== $(date '+%H:%M:%S') $INSTANCE cycle #$cycle start workspace=$workspace ===" >> "$LOG"

    # Clone from local bare mirror with --reference for fast object reuse.
    # Shallow (--depth=50) keeps the working tree minimal; full history
    # is available through the alternates pointer back to the mirror.
    clone_start=$(date +%s)
    if ! git clone --reference "$MIRROR_DIR" --depth="$CLONE_DEPTH" \
            "$GERRIT_URL" "$workspace" >> "$LOG" 2>&1; then
        echo "[ephemeral-wrapper] cycle #$cycle clone failed; retry next tick" >> "$LOG"
        rm -rf "$workspace"
        trap - EXIT INT TERM
        sleep "$CYCLE_SLEEP_S"
        continue
    fi
    clone_elapsed=$(( $(date +%s) - clone_start ))
    echo "[ephemeral-wrapper] cycle #$cycle clone completed in ${clone_elapsed}s" >> "$LOG"

    # OP-729: runner pre-flight asserts extensions.worktreeConfig=true on
    # its repo (cross-runner bot identity race fix). Each ephemeral clone
    # is fresh, so this config must be re-set every cycle. We also pin
    # user.name / user.email so git commits the runner makes carry the
    # right identity even if no global git config is set.
    git -C "$workspace" config core.repositoryformatversion 1
    git -C "$workspace" config extensions.worktreeConfig true
    git_user_name="${OMNISIGHT_GIT_USER_NAME:-${DEFAULT_BOT}}"
    git_user_email="${OMNISIGHT_GIT_USER_EMAIL:-rt3628+${DEFAULT_BOT}@gmail.com}"
    git -C "$workspace" config user.name "$git_user_name"
    git -C "$workspace" config user.email "$git_user_email"

    # Invoke runner Python from inside the ephemeral clone.
    # OMNISIGHT_{CODEX,CLAUDE}_WORKTREE pinned to the workspace so the
    # runner's CLI invocation uses this dir as both cwd + worktree.
    # OMNISIGHT_RUNNER_EPHEMERAL=1 is a hint flag — auto-runner-jira.py
    # (per OP-1138 / C3) can use it to assert workspace==REPO invariant.
    cd "$workspace"
    PATH="/home/user/.nvm/versions/node/v24.14.1/bin:$PATH" \
    OMNISIGHT_RUNNER_CLASS="$CLASS" \
    OMNISIGHT_RUNNER_EPHEMERAL=1 \
    OMNISIGHT_RUNNER_LABEL_CLAIM_LEGACY=1 \
    OMNISIGHT_CODEX_WORKTREE="$workspace" \
    OMNISIGHT_CLAUDE_WORKTREE="$workspace" \
    python3 -u "$workspace/auto-runner-jira.py" >> "$LOG" 2>&1
    rc=$?
    cd - >/dev/null 2>&1 || true

    echo "=== $(date '+%H:%M:%S') $INSTANCE cycle #$cycle end rc=$rc ===" >> "$LOG"

    cleanup
    trap - EXIT INT TERM

    sleep "$CYCLE_SLEEP_S"
done
