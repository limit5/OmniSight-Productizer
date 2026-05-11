#!/usr/bin/env bash
# scripts/sync_omnisight_main.sh — OP-837.
#
# Keeps the runner's main checkout (/home/user/work/sora/OmniSight-Productizer/)
# on gerrit/develop. Sister of sync_sora_bridge.sh (OP-798) — same shape,
# different target, no daemon to restart.
#
# Why this exists:
#   The auto-runner-jira.py + its imports (backend.agents.jira_dispatch,
#   runner_workspace_safety, etc.) are read from the main repo path. When
#   that path drifts behind develop, runner code stays stale and merged
#   fixes (OP-827 typed exceptions, OP-832 area validation, OP-836
#   workspace safety) silently never apply. 2026-05-11 incident:
#   OP-829 entered a 5x revert loop for ~16 minutes because the OP-832
#   fix shipped on develop wasn't reachable by the running runner.
#
# Behaviour (in order):
#   1. Refuse to run if MAIN_DIR is missing or not a git repo.
#   2. If working tree dirty AND auto-stash is OFF → log + exit 0
#      (operator's draft state is sacred; skip is the safe default).
#      AUTO_STASH=1 → stash --include-untracked, fetch, ff-pull,
#      stash pop. On pop conflict → leave stash + log + alert.
#   3. git fetch gerrit develop. Failure → counted toward the
#      consecutive-failure budget.
#   4. If HEAD == gerrit/develop → reset failure counter, exit 0.
#   5. Compute diff, decide whether any RUNNER-CODE file changed.
#   6. git pull --ff-only. Non-FF → log + exit non-zero (no force).
#   7. Always log; if RUNNER-CODE changed → emit "runner-code-changed"
#      structured log line so the operator's tail can pick it up.
#   8. After 3 consecutive sync failures → P0 notifier (defence-in-depth,
#      same shape as OP-798).
#
# Env overrides:
#   MAIN_DIR         default /home/user/work/sora/OmniSight-Productizer
#   MAIN_REMOTE      default gerrit
#   MAIN_BRANCH      default develop
#   SYNC_LOG         default ~/.local/state/omnisight-main-sync/sync.log
#   SYNC_STATE       default ~/.local/state/omnisight-main-sync/state.json
#   SYNC_DRY_RUN     default 0 (1 = log decisions, skip pull)
#   AUTO_STASH       default 0 (1 = stash --include-untracked + pop)
#   SYNC_FAIL_ALERT_THRESHOLD  default 3
#
# Exit codes:
#   0  no-op, dirty-skip, synced
#   2  unrecoverable config (missing dir / not a repo)
#   3  fetch / pull failure (counted)
#   4  stash-pop conflict (auto-stash mode only)

set -uo pipefail

MAIN_DIR="${MAIN_DIR:-/home/user/work/sora/OmniSight-Productizer}"
MAIN_REMOTE="${MAIN_REMOTE:-gerrit}"
MAIN_BRANCH="${MAIN_BRANCH:-develop}"
SYNC_LOG="${SYNC_LOG:-$HOME/.local/state/omnisight-main-sync/sync.log}"
SYNC_STATE="${SYNC_STATE:-$HOME/.local/state/omnisight-main-sync/state.json}"
SYNC_DRY_RUN="${SYNC_DRY_RUN:-0}"
AUTO_STASH="${AUTO_STASH:-0}"
SYNC_FAIL_ALERT_THRESHOLD="${SYNC_FAIL_ALERT_THRESHOLD:-3}"

# RUNNER_CODE_FILES: a change touching one of these is grep-flagged so
# operator can spot when runner code ships from develop. NOT a restart
# trigger (runner is a bash loop in tmux; next tick reads disk fresh).
RUNNER_CODE_FILES=(
    "auto-runner-jira.py"
    "auto-runner-codex.py"
    "auto-runner-sdk.py"
    "auto-runner-multi.py"
    "backend/agents/jira_dispatch.py"
    "backend/agents/runner_handlers.py"
    "backend/agents/runner_workspace_safety.py"
    "backend/agents/scheduler.py"
    "backend/agents/runner_failure_classifier.py"
    "backend/agents/orphan_salvage.py"
    "backend/agents/circuit_breaker.py"
)

mkdir -p "$(dirname "$SYNC_LOG")" "$(dirname "$SYNC_STATE")"

_now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

_log() {
    local event="$1"; shift
    local payload="{\"ts\":\"$(_now)\",\"event\":\"$event\""
    while [[ $# -gt 0 ]]; do
        local k="$1" v="$2"; shift 2
        v="${v//\\/\\\\}"
        v="${v//\"/\\\"}"
        payload+=",\"$k\":\"$v\""
    done
    payload+="}"
    printf '%s\n' "$payload" >> "$SYNC_LOG"
    printf '%s\n' "$payload" >&2
}

_state_read_int() {
    local key="$1" default="$2"
    [[ -f "$SYNC_STATE" ]] || { printf '%s' "$default"; return; }
    local line
    line="$(grep -oE "\"$key\"[[:space:]]*:[[:space:]]*[0-9]+" "$SYNC_STATE" | tail -n1)" || true
    if [[ -z "$line" ]]; then printf '%s' "$default"; return; fi
    printf '%s' "$line" | grep -oE '[0-9]+' | tail -n1
}
_state_read_str() {
    local key="$1" default="$2"
    [[ -f "$SYNC_STATE" ]] || { printf '%s' "$default"; return; }
    local line
    line="$(grep -oE "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$SYNC_STATE" | tail -n1)" || true
    if [[ -z "$line" ]]; then printf '%s' "$default"; return; fi
    printf '%s' "$line" | sed -E 's/.*"([^"]*)"$/\1/'
}
_state_write() {
    local consecutive_failures="$1" last_sha="$2" last_alert_at="$3"
    cat > "$SYNC_STATE" <<EOF
{
  "consecutive_failures": $consecutive_failures,
  "last_synced_sha": "$last_sha",
  "last_alert_at": "$last_alert_at",
  "updated_at": "$(_now)"
}
EOF
}

CONSECUTIVE_FAILURES="$(_state_read_int consecutive_failures 0)"
LAST_SHA="$(_state_read_str last_synced_sha "")"
LAST_ALERT_AT="$(_state_read_str last_alert_at "")"

_record_failure() {
    local code="$1" detail="$2"
    CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
    _state_write "$CONSECUTIVE_FAILURES" "$LAST_SHA" "$LAST_ALERT_AT"
    _log sync_failure code "$code" detail "$detail" \
        consecutive_failures "$CONSECUTIVE_FAILURES"
    if [[ "$CONSECUTIVE_FAILURES" -ge "$SYNC_FAIL_ALERT_THRESHOLD" ]]; then
        _alert_operator "$code" "$detail"
    fi
}

_alert_operator() {
    local code="$1" detail="$2"
    if ! command -v python3 >/dev/null 2>&1; then
        _log alert_skipped reason "no_python3"
        return
    fi
    local payload
    payload="$(printf '%s' "$detail" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"
    PYTHONPATH="$MAIN_DIR" python3 - "$code" "$payload" "$CONSECUTIVE_FAILURES" <<'PY' || _log alert_dispatch_failed code "$code"
import json
import sys

code, detail_json, consecutive = sys.argv[1], sys.argv[2], int(sys.argv[3])
detail = json.loads(detail_json)
try:
    from backend.agents.operator_notifier import notify
except Exception:
    print(f"alert_skipped reason=operator_notifier_unavailable code={code}", file=sys.stderr)
    sys.exit(0)
try:
    notify(
        "P0",
        code="omnisight_main_sync_failed",
        detail=detail,
        sync_failure_code=code,
        consecutive_failures=consecutive,
        ticket="OP-837",
    )
except Exception as exc:
    print(f"alert_dispatch_failed code={code} exc={exc!r}", file=sys.stderr)
    sys.exit(1)
PY
    LAST_ALERT_AT="$(_now)"
    _state_write "$CONSECUTIVE_FAILURES" "$LAST_SHA" "$LAST_ALERT_AT"
}

_reset_failure() {
    if [[ "$CONSECUTIVE_FAILURES" -ne 0 ]]; then
        _log sync_recovered prior_consecutive_failures "$CONSECUTIVE_FAILURES"
    fi
    CONSECUTIVE_FAILURES=0
}

# ── 1. Sanity ──────────────────────────────────────────────────────
if [[ ! -d "$MAIN_DIR/.git" ]]; then
    _log sync_aborted reason "main_dir_not_a_git_repo" path "$MAIN_DIR"
    exit 2
fi

cd "$MAIN_DIR"

# ── 2. Dirty tree handling ─────────────────────────────────────────
TREE_IS_DIRTY=0
if ! git diff --quiet || ! git diff --cached --quiet; then
    TREE_IS_DIRTY=1
fi
# Untracked files also count as "dirty" for our purposes (auto-stash
# without --include-untracked would silently skip them, which is exactly
# how OP-837's predecessor incident lost draft work).
HAS_UNTRACKED=0
if [[ -n "$(git ls-files --others --exclude-standard | head -n 1)" ]]; then
    HAS_UNTRACKED=1
fi

if [[ "$TREE_IS_DIRTY" == "1" || "$HAS_UNTRACKED" == "1" ]]; then
    if [[ "$AUTO_STASH" != "1" ]]; then
        _log sync_skipped_dirty path "$MAIN_DIR" \
            tree_dirty "$TREE_IS_DIRTY" untracked "$HAS_UNTRACKED"
        _reset_failure
        _state_write "$CONSECUTIVE_FAILURES" "$LAST_SHA" "$LAST_ALERT_AT"
        exit 0
    fi
    # AUTO_STASH=1 path. Push a named stash so post-pop traceability is
    # easy. --include-untracked because dirty drafts include audit docs
    # / new spec files we must preserve.
    STASH_NAME="auto-sync-$(_now)"
    if ! git stash push --include-untracked -m "$STASH_NAME" >/dev/null 2>&1; then
        _record_failure "stash_push_failed" "auto_stash mode could not stash"
        exit 3
    fi
    STASHED=1
    _log sync_auto_stashed name "$STASH_NAME"
else
    STASHED=0
fi

# ── 3. Fetch ───────────────────────────────────────────────────────
if ! git fetch --quiet "$MAIN_REMOTE" "$MAIN_BRANCH" 2>/tmp/.sync_omnisight_main.fetch.err; then
    _record_failure "fetch_failed" "$(tr -d '\n' </tmp/.sync_omnisight_main.fetch.err | head -c 240)"
    rm -f /tmp/.sync_omnisight_main.fetch.err
    if [[ "$STASHED" == "1" ]]; then
        git stash pop --quiet >/dev/null 2>&1 || _log stash_pop_failed phase "post_fetch_failure"
    fi
    exit 3
fi
rm -f /tmp/.sync_omnisight_main.fetch.err

LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
REMOTE_SHA="$(git rev-parse "$MAIN_REMOTE/$MAIN_BRANCH" 2>/dev/null || echo "")"

if [[ -z "$LOCAL_SHA" || -z "$REMOTE_SHA" ]]; then
    _record_failure "rev_parse_failed" "local=$LOCAL_SHA remote=$REMOTE_SHA"
    if [[ "$STASHED" == "1" ]]; then
        git stash pop --quiet >/dev/null 2>&1 || _log stash_pop_failed phase "post_rev_parse_failure"
    fi
    exit 3
fi

# ── 4. Already in sync ─────────────────────────────────────────────
if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
    _log sync_no_change head "$LOCAL_SHA"
    _reset_failure
    LAST_SHA="$LOCAL_SHA"
    _state_write "$CONSECUTIVE_FAILURES" "$LAST_SHA" "$LAST_ALERT_AT"
    if [[ "$STASHED" == "1" ]]; then
        git stash pop --quiet >/dev/null 2>&1 || _log stash_pop_failed phase "no_change"
    fi
    exit 0
fi

# ── 5. Detect runner-code changes (informational) ──────────────────
CHANGED_PATHS_RAW="$(git diff --name-only "$LOCAL_SHA" "$REMOTE_SHA" 2>/dev/null || true)"
RUNNER_CODE_HIT=""
while IFS= read -r changed; do
    [[ -z "$changed" ]] && continue
    for rel in "${RUNNER_CODE_FILES[@]}"; do
        if [[ "$changed" == "$rel" ]]; then
            RUNNER_CODE_HIT+="$changed "
            break
        fi
    done
done <<< "$CHANGED_PATHS_RAW"
RUNNER_CODE_HIT="${RUNNER_CODE_HIT% }"

# ── 6. Fast-forward pull ──────────────────────────────────────────
if [[ "$SYNC_DRY_RUN" == "1" ]]; then
    _log sync_dry_run from "$LOCAL_SHA" to "$REMOTE_SHA" \
        runner_code_changed "${RUNNER_CODE_HIT:-none}"
    _reset_failure
    _state_write "$CONSECUTIVE_FAILURES" "$LAST_SHA" "$LAST_ALERT_AT"
    if [[ "$STASHED" == "1" ]]; then
        git stash pop --quiet >/dev/null 2>&1 || _log stash_pop_failed phase "dry_run"
    fi
    exit 0
fi

if ! git pull --ff-only --quiet "$MAIN_REMOTE" "$MAIN_BRANCH" 2>/tmp/.sync_omnisight_main.pull.err; then
    _record_failure "pull_not_fast_forward" \
        "$(tr -d '\n' </tmp/.sync_omnisight_main.pull.err | head -c 240)"
    rm -f /tmp/.sync_omnisight_main.pull.err
    if [[ "$STASHED" == "1" ]]; then
        git stash pop --quiet >/dev/null 2>&1 || _log stash_pop_failed phase "post_non_ff"
    fi
    exit 3
fi
rm -f /tmp/.sync_omnisight_main.pull.err

# ── 7. Restore stash if we made one ────────────────────────────────
if [[ "$STASHED" == "1" ]]; then
    if ! git stash pop --quiet 2>/dev/null; then
        # Conflict on pop — leave stash for operator review.
        _log stash_pop_conflict stash_name "$STASH_NAME"
        # Don't exit non-zero — the pull itself succeeded; this is a
        # follow-up operator-action signal, not a sync failure.
        _record_failure "stash_pop_conflict" "stash $STASH_NAME has conflicts; operator must resolve"
        exit 4
    fi
fi

# ── 8. Success path ────────────────────────────────────────────────
_reset_failure
LAST_SHA="$REMOTE_SHA"
_state_write "$CONSECUTIVE_FAILURES" "$LAST_SHA" "$LAST_ALERT_AT"

if [[ -n "$RUNNER_CODE_HIT" ]]; then
    _log sync_runner_code_changed from "$LOCAL_SHA" to "$REMOTE_SHA" \
        files "$RUNNER_CODE_HIT"
else
    _log sync_no_runner_code_change from "$LOCAL_SHA" to "$REMOTE_SHA"
fi

exit 0
