#!/usr/bin/env bash
# scripts/sync_sora_bridge.sh — OP-798 Phase A.
#
# Keeps the long-lived /home/user/sora-bridge/ working tree in sync with
# origin/develop and bounces the gerrit-jira-bridge.service ONLY if a
# file the daemon actually imports changed. Wired via the
# sora-bridge-sync.timer (5 min cadence).
#
# Why a separate clone exists at all:
#   The bridge daemon must run from a tree that is NEVER touched by the
#   runner / dev worktrees (which constantly switch branches and reset
#   state). Sharing the main checkout would race the daemon against
#   every runner tick. Today's failure mode (OP-798) was the inverse:
#   isolation works, but with no automated sync the clone drifted 233
#   commits behind and silently kept running pre-OP-743 code for ~7h.
#
# Behaviour (in order):
#   1. Refuse to run if BRIDGE_DIR is missing or not a git repo.
#   2. If working tree is dirty (operator debug edits) → log + exit 0.
#      Do NOT touch the tree. Dirty state is a SAFE skip, not a failure.
#   3. git fetch origin develop. fetch failure → counted as a failure.
#   4. If HEAD == origin/develop → reset failure counter, exit 0.
#   5. Otherwise compute diff, decide whether any RELEVANT file changed
#      (i.e. would actually affect the daemon's runtime behaviour).
#   6. git pull --ff-only. Non-FF → log + exit non-zero (no force).
#   7. If relevant changed → systemctl --user restart $BRIDGE_UNIT and
#      bump the on-disk restart counter. Else → log "synced_no_restart".
#   8. After 3 consecutive sync failures emit a P0 line to
#      operator_notifier (defence-in-depth; the watchdog T2 catches the
#      same case on a different signal).
#
# The only side-effects on a healthy run are (a) a fast-forward pull,
# (b) optionally one systemctl --user restart, and (c) appending to the
# log + state files. Everything else is informational.
#
# Env overrides (all optional — defaults match the production layout):
#   BRIDGE_DIR     default /home/user/sora-bridge
#   BRIDGE_REMOTE  default origin
#   BRIDGE_BRANCH  default develop
#   BRIDGE_UNIT    default gerrit-jira-bridge.service
#   SYNC_LOG       default /home/user/work/sora/logs/bridge/sync.log
#   SYNC_STATE     default /home/user/work/sora/logs/bridge/sync-state.json
#   SYNC_DRY_RUN   default 0 (1 = log decisions, skip pull + restart)
#
# Exit codes:
#   0  no-op, dirty-skip, synced-no-restart, restart-success
#   2  unrecoverable config error (missing dir / not a repo)
#   3  fetch / pull failure (counted toward the consecutive-failure budget)
#   4  systemctl restart failure (counted)
#
# JSON log records use {ts, event, ...} so journald readers and the
# operator_notifier tail can grep by event= without extra parsing.

set -uo pipefail

BRIDGE_DIR="${BRIDGE_DIR:-/home/user/sora-bridge}"
BRIDGE_REMOTE="${BRIDGE_REMOTE:-origin}"
BRIDGE_BRANCH="${BRIDGE_BRANCH:-develop}"
BRIDGE_UNIT="${BRIDGE_UNIT:-gerrit-jira-bridge.service}"
SYNC_LOG="${SYNC_LOG:-/home/user/work/sora/logs/bridge/sync.log}"
SYNC_STATE="${SYNC_STATE:-/home/user/work/sora/logs/bridge/sync-state.json}"
SYNC_DRY_RUN="${SYNC_DRY_RUN:-0}"
SYNC_FAIL_ALERT_THRESHOLD="${SYNC_FAIL_ALERT_THRESHOLD:-3}"

# RELEVANT_FILES: any change touching one of these paths (exact match,
# repo-relative) triggers a daemon restart. Docs / tests / unrelated
# backend code do NOT. Keep this list tight — every entry here is a
# restart trigger and restarts cost ~5s of stream-events downtime.
#
# Why these five:
#   gerrit_jira_bridge.py     — daemon entrypoint logic
#   jira_dispatch.py          — JIRA transition + comment helpers
#   db.py                     — DB session / engine config
#   git_accounts.py           — Gerrit/JIRA cred lookup at bridge start
#   run_gerrit_jira_bridge.py — the actual ExecStart target
RELEVANT_FILES=(
    "backend/agents/gerrit_jira_bridge.py"
    "backend/agents/jira_dispatch.py"
    "backend/db.py"
    "backend/git_accounts.py"
    "scripts/run_gerrit_jira_bridge.py"
)

mkdir -p "$(dirname "$SYNC_LOG")" "$(dirname "$SYNC_STATE")"

_now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# Emit a single-line JSON record to both stderr (→ journald via the
# .service unit) and to $SYNC_LOG. event= is always first so grep is
# trivial.
_log() {
    local event="$1"; shift
    local payload="{\"ts\":\"$(_now)\",\"event\":\"$event\""
    while [[ $# -gt 0 ]]; do
        local k="$1" v="$2"; shift 2
        # naive JSON-string escape: backslash + double-quote only.
        # We only emit values we control (refs, paths, integers).
        v="${v//\\/\\\\}"
        v="${v//\"/\\\"}"
        payload+=",\"$k\":\"$v\""
    done
    payload+="}"
    printf '%s\n' "$payload" >> "$SYNC_LOG"
    printf '%s\n' "$payload" >&2
}

# State file holds {consecutive_failures, restarts, last_synced_sha,
# last_alert_at}. We hand-write JSON to keep the script stdlib-only
# (busybox bash + git is the runtime contract; no jq required).
_state_read_int() {
    local key="$1" default="$2"
    [[ -f "$SYNC_STATE" ]] || { printf '%s' "$default"; return; }
    # grep returns 1 when the key is absent — under set -e that would
    # abort the script; pipefail is also disabled at script scope.
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
    local consecutive_failures="$1" restarts="$2" last_sha="$3" last_alert_at="$4"
    cat > "$SYNC_STATE" <<EOF
{
  "consecutive_failures": $consecutive_failures,
  "restarts": $restarts,
  "last_synced_sha": "$last_sha",
  "last_alert_at": "$last_alert_at",
  "updated_at": "$(_now)"
}
EOF
}

CONSECUTIVE_FAILURES="$(_state_read_int consecutive_failures 0)"
RESTARTS="$(_state_read_int restarts 0)"
LAST_SHA="$(_state_read_str last_synced_sha "")"
LAST_ALERT_AT="$(_state_read_str last_alert_at "")"

_record_failure() {
    local code="$1" detail="$2"
    CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
    _state_write "$CONSECUTIVE_FAILURES" "$RESTARTS" "$LAST_SHA" "$LAST_ALERT_AT"
    _log sync_failure code "$code" detail "$detail" \
        consecutive_failures "$CONSECUTIVE_FAILURES"
    if [[ "$CONSECUTIVE_FAILURES" -ge "$SYNC_FAIL_ALERT_THRESHOLD" ]]; then
        _alert_operator "$code" "$detail"
    fi
}

# Best-effort operator notification. We re-use the same notifier the
# bridge + watchdog already use (operator_notifier P0). Missing
# notifier module is silently tolerated — the journald + sync.log line
# is still authoritative; this is a defence-in-depth fan-out.
_alert_operator() {
    local code="$1" detail="$2"
    local repo_root
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if ! command -v python3 >/dev/null 2>&1; then
        _log alert_skipped reason "no_python3"
        return
    fi
    local payload
    payload="$(printf '%s' "$detail" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"
    PYTHONPATH="$repo_root" python3 - "$code" "$payload" "$CONSECUTIVE_FAILURES" <<'PY' || _log alert_dispatch_failed code "$code"
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
        code="sora_bridge_sync_failed",
        detail=detail,
        sync_failure_code=code,
        consecutive_failures=consecutive,
        ticket="OP-798",
    )
except Exception as exc:
    print(f"alert_dispatch_failed code={code} exc={exc!r}", file=sys.stderr)
    sys.exit(1)
PY
    LAST_ALERT_AT="$(_now)"
    _state_write "$CONSECUTIVE_FAILURES" "$RESTARTS" "$LAST_SHA" "$LAST_ALERT_AT"
}

_reset_failure() {
    if [[ "$CONSECUTIVE_FAILURES" -ne 0 ]]; then
        _log sync_recovered prior_consecutive_failures "$CONSECUTIVE_FAILURES"
    fi
    CONSECUTIVE_FAILURES=0
}

# ── 1. Sanity: bridge dir present + is a git repo ──────────────────
if [[ ! -d "$BRIDGE_DIR/.git" ]]; then
    _log sync_aborted reason "bridge_dir_not_a_git_repo" path "$BRIDGE_DIR"
    exit 2
fi

cd "$BRIDGE_DIR"

# ── 2. Skip if working tree is dirty ───────────────────────────────
# Operator debug edits (live patches, prints, ad-hoc instrumentation)
# live in the working tree. We MUST NOT clobber them. Skip + warn.
if ! git diff --quiet || ! git diff --cached --quiet; then
    _log sync_skipped_dirty path "$BRIDGE_DIR"
    # A dirty tree is intentional operator state, not a sync failure.
    # Reset consecutive_failures so a long debug session doesn't tip
    # the script into spurious P0 alerts.
    _reset_failure
    _state_write "$CONSECUTIVE_FAILURES" "$RESTARTS" "$LAST_SHA" "$LAST_ALERT_AT"
    exit 0
fi

# ── 3. Fetch ───────────────────────────────────────────────────────
if ! git fetch --quiet "$BRIDGE_REMOTE" "$BRIDGE_BRANCH" 2>/tmp/.sync_sora_bridge.fetch.err; then
    _record_failure "fetch_failed" "$(tr -d '\n' </tmp/.sync_sora_bridge.fetch.err | head -c 240)"
    rm -f /tmp/.sync_sora_bridge.fetch.err
    exit 3
fi
rm -f /tmp/.sync_sora_bridge.fetch.err

LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
REMOTE_SHA="$(git rev-parse "$BRIDGE_REMOTE/$BRIDGE_BRANCH" 2>/dev/null || echo "")"

if [[ -z "$LOCAL_SHA" || -z "$REMOTE_SHA" ]]; then
    _record_failure "rev_parse_failed" "local=$LOCAL_SHA remote=$REMOTE_SHA"
    exit 3
fi

# ── 4. Already in sync ─────────────────────────────────────────────
if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
    _log sync_no_change head "$LOCAL_SHA"
    _reset_failure
    LAST_SHA="$LOCAL_SHA"
    _state_write "$CONSECUTIVE_FAILURES" "$RESTARTS" "$LAST_SHA" "$LAST_ALERT_AT"
    exit 0
fi

# ── 5. Decide whether any relevant file changed ────────────────────
CHANGED_PATHS_RAW="$(git diff --name-only "$LOCAL_SHA" "$REMOTE_SHA" 2>/dev/null || true)"
RELEVANT_HIT=""
while IFS= read -r changed; do
    [[ -z "$changed" ]] && continue
    for rel in "${RELEVANT_FILES[@]}"; do
        if [[ "$changed" == "$rel" ]]; then
            RELEVANT_HIT+="$changed "
            break
        fi
    done
done <<< "$CHANGED_PATHS_RAW"
RELEVANT_HIT="${RELEVANT_HIT% }"

# ── 6. Fast-forward pull (no merge, no rebase) ─────────────────────
if [[ "$SYNC_DRY_RUN" == "1" ]]; then
    _log sync_dry_run from "$LOCAL_SHA" to "$REMOTE_SHA" relevant_changed "${RELEVANT_HIT:-none}"
    _reset_failure
    _state_write "$CONSECUTIVE_FAILURES" "$RESTARTS" "$LAST_SHA" "$LAST_ALERT_AT"
    exit 0
fi

if ! git pull --ff-only --quiet "$BRIDGE_REMOTE" "$BRIDGE_BRANCH" 2>/tmp/.sync_sora_bridge.pull.err; then
    _record_failure "pull_not_fast_forward" \
        "$(tr -d '\n' </tmp/.sync_sora_bridge.pull.err | head -c 240)"
    rm -f /tmp/.sync_sora_bridge.pull.err
    exit 3
fi
rm -f /tmp/.sync_sora_bridge.pull.err

POST_SHA="$(git rev-parse HEAD)"
_log sync_pulled from "$LOCAL_SHA" to "$POST_SHA"
LAST_SHA="$POST_SHA"

# ── 7. Restart only if a relevant file changed ─────────────────────
if [[ -n "$RELEVANT_HIT" ]]; then
    if systemctl --user restart "$BRIDGE_UNIT" 2>/tmp/.sync_sora_bridge.restart.err; then
        RESTARTS=$((RESTARTS + 1))
        _log sync_restarted unit "$BRIDGE_UNIT" relevant "$RELEVANT_HIT" \
            restart_count "$RESTARTS"
        _reset_failure
        _state_write "$CONSECUTIVE_FAILURES" "$RESTARTS" "$LAST_SHA" "$LAST_ALERT_AT"
        rm -f /tmp/.sync_sora_bridge.restart.err
        exit 0
    else
        _record_failure "systemctl_restart_failed" \
            "$(tr -d '\n' </tmp/.sync_sora_bridge.restart.err | head -c 240)"
        rm -f /tmp/.sync_sora_bridge.restart.err
        exit 4
    fi
fi

_log sync_no_restart_needed from "$LOCAL_SHA" to "$POST_SHA" \
    reason "no_relevant_files_changed"
_reset_failure
_state_write "$CONSECUTIVE_FAILURES" "$RESTARTS" "$LAST_SHA" "$LAST_ALERT_AT"
exit 0
