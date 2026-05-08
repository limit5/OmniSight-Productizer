#!/bin/bash
# ref-update-tier-classify.sh — server-side Gerrit hook (OP-805 / G3).
#
# WHEN: invoked by the Gerrit ``hooks`` plugin after every patchset upload.
# WHERE: install at ``<gerrit-site>/hooks/patchset-created`` (recommended;
#        async, fires after the change record exists, so we can post a
#        review label) — the runbook ``deploy/gerrit/install-tier-hook.md``
#        explains why ``patchset-created`` is preferred over the literal
#        ``ref-update`` event for this purpose.
#
# WHAT: enforces ADR-0005 §4 layer 1 (path force-upgrade). Computes the
#       Tier (s/m/l/x) from the patchset's changed paths via G2's
#       classifier (in-process or HTTPS) and writes the ``Tier`` label
#       on the patchset. Monotonicity-respecting: never demotes a higher
#       existing label (reviewer promote-only).
#
# AUDIT: every classification + override attempt is logged to
#        ``$GERRIT_HOOK_AUDIT_LOG`` (default /var/log/gerrit-tier-hook.log)
#        with weekly rotation.
#
# OUT OF SCOPE: cooldown enforcement (G5 / a separate ticket).
#
# Required env (set in /etc/default/gerrit-tier-hook or systemd dropin):
#   GERRIT_HOOK_SSH_HOST        SSH endpoint Gerrit listens on (e.g. localhost)
#   GERRIT_HOOK_SSH_PORT        SSH port (default 29418)
#   GERRIT_HOOK_SSH_USER        Gerrit account that posts the label
#                               (typically ``tier-bot`` — must be in
#                               ``ai-reviewer-bots`` and have ACL to
#                               vote on the ``Tier`` label).
#   GERRIT_HOOK_SSH_KEY         Path to the private key for SSH_USER
# Optional env:
#   GERRIT_HOOK_BACKEND_PYTHONPATH   Path to backend repo clone for
#                                    in-process tier_classifier import
#   GERRIT_HOOK_BACKEND_URL          HTTPS fallback (e.g. https://api.sora.services)
#   GERRIT_HOOK_BACKEND_API_KEY      Bot API key for the HTTPS fallback
#   GERRIT_HOOK_AUDIT_LOG            Audit log path (default /var/log/gerrit-tier-hook.log)
#   GERRIT_HOOK_DRY_RUN=1            Skip the ``gerrit review`` SSH call
#                                    (synthetic-test mode); still logs.
#
# Hook plugin args (Gerrit passes these as ``--key value`` pairs):
#   patchset-created: --change <id> --change-url ... --commit <sha>
#                     --project <p> --branch <b> --patchset <n> ...
#   ref-update:       --project <p> --refname <r> --uploader <u>
#                     --oldrev <sha> --newrev <sha>
#
# Exit code: always 0. The patchset is already committed by the time
# this hook fires; rejecting here would only confuse operators. All
# error paths default the tier to ``m`` (deny-by-default) and log
# loudly so the operator notices.

set -u

HOOK_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_WRAPPER="${SCRIPT_DIR}/lib/classify_tier_via_backend.py"

AUDIT_LOG="${GERRIT_HOOK_AUDIT_LOG:-/var/log/gerrit-tier-hook.log}"

log() {
    # Single-line key=value audit format so logrotate-friendly grep works.
    # Mirrors classify_tier_via_backend.py's logging format.
    local ts
    ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    printf '%s INFO %s %s\n' "$ts" "$HOOK_NAME" "$*" >> "$AUDIT_LOG" 2>/dev/null || true
    printf '%s INFO %s %s\n' "$ts" "$HOOK_NAME" "$*" >&2
}

log_err() {
    local ts
    ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    printf '%s ERROR %s %s\n' "$ts" "$HOOK_NAME" "$*" >> "$AUDIT_LOG" 2>/dev/null || true
    printf '%s ERROR %s %s\n' "$ts" "$HOOK_NAME" "$*" >&2
}

# ── Parse Gerrit hook args ────────────────────────────────────────────
CHANGE_ID=""
COMMIT_SHA=""
PROJECT=""
PATCHSET=""
REFNAME=""
NEWREV=""
OLDREV=""

while [ $# -gt 0 ]; do
    case "$1" in
        --change)        CHANGE_ID="${2:-}"; shift 2 ;;
        --commit)        COMMIT_SHA="${2:-}"; shift 2 ;;
        --project)       PROJECT="${2:-}"; shift 2 ;;
        --patchset)      PATCHSET="${2:-}"; shift 2 ;;
        --refname)       REFNAME="${2:-}"; shift 2 ;;
        --newrev)        NEWREV="${2:-}"; shift 2 ;;
        --oldrev)        OLDREV="${2:-}"; shift 2 ;;
        # Ignore any args we don't care about (Gerrit adds many).
        --*)             shift 2 ;;
        *)               shift ;;
    esac
done

# ref-update mode: derive change-id and commit from refname/newrev.
# Magic ref for patchset push: refs/changes/NN/<change-num>/<patchset>
if [ -z "$COMMIT_SHA" ] && [ -n "$NEWREV" ]; then
    COMMIT_SHA="$NEWREV"
fi
if [ -z "$CHANGE_ID" ] && [ -n "$REFNAME" ]; then
    case "$REFNAME" in
        refs/changes/*/*/*)
            # change-num is the third path segment; ``gerrit review``
            # accepts the numeric form too.
            CHANGE_ID="$(echo "$REFNAME" | awk -F/ '{print $4}')"
            ;;
    esac
fi

if [ -z "$COMMIT_SHA" ] || [ -z "$CHANGE_ID" ]; then
    log_err "skipped reason=missing-args change_id=${CHANGE_ID} commit=${COMMIT_SHA} refname=${REFNAME}"
    exit 0
fi

# ── Load changed file list (git diff-tree) ────────────────────────────
# The hook runs with the Gerrit site's git repo CWD set to the project's
# git dir. Per ticket spec: ``git diff-tree --no-commit-id --name-only -r <commit>``.
PATHS_FILE="$(mktemp -t gerrit-tier-hook.XXXXXX)"
trap 'rm -f "$PATHS_FILE"' EXIT

if ! git diff-tree --no-commit-id --name-only -r "$COMMIT_SHA" > "$PATHS_FILE" 2>/dev/null; then
    log_err "skipped reason=git-diff-tree-failed change_id=${CHANGE_ID} commit=${COMMIT_SHA}"
    exit 0
fi

PATH_COUNT="$(wc -l < "$PATHS_FILE" | tr -d ' ')"
if [ "$PATH_COUNT" = "0" ]; then
    # Merge commits with no diff against parent — skip silently.
    log "skipped reason=no-paths change_id=${CHANGE_ID} commit=${COMMIT_SHA}"
    exit 0
fi

# ── Run the classifier ────────────────────────────────────────────────
# Allow the operator to point PYTHONPATH at the backend clone for
# in-process classification (Strategy 1 in the wrapper).
if [ -n "${GERRIT_HOOK_BACKEND_PYTHONPATH:-}" ]; then
    export PYTHONPATH="${GERRIT_HOOK_BACKEND_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}"
fi
export GERRIT_HOOK_AUDIT_LOG="$AUDIT_LOG"

# The wrapper writes its own structured audit lines to $AUDIT_LOG; we
# only capture its stderr to the log when the file handler isn't active
# (e.g. permission error) — otherwise we'd double-log.
PY_STDERR="$(mktemp -t gerrit-tier-hook-stderr.XXXXXX)"
COMPUTED_TIER="$(python3 "$PY_WRAPPER" --paths-stdin --audit-log "$AUDIT_LOG" < "$PATHS_FILE" 2>"$PY_STDERR")"
if [ -s "$PY_STDERR" ]; then
    cat "$PY_STDERR" >> "$AUDIT_LOG" 2>/dev/null || true
fi
rm -f "$PY_STDERR"
COMPUTED_TIER="$(printf '%s' "$COMPUTED_TIER" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"

case "$COMPUTED_TIER" in
    s|m|l|x) ;;
    *)
        log_err "classifier_returned_invalid value=${COMPUTED_TIER} change_id=${CHANGE_ID} — defaulting to m"
        COMPUTED_TIER="m"
        ;;
esac

# ── Read existing Tier label (monotonicity) ───────────────────────────
SSH_HOST="${GERRIT_HOOK_SSH_HOST:-}"
SSH_PORT="${GERRIT_HOOK_SSH_PORT:-29418}"
SSH_USER="${GERRIT_HOOK_SSH_USER:-}"
SSH_KEY="${GERRIT_HOOK_SSH_KEY:-}"

ssh_gerrit() {
    if [ -z "$SSH_HOST" ] || [ -z "$SSH_USER" ]; then
        return 64  # config-missing sentinel
    fi
    local key_args=()
    if [ -n "$SSH_KEY" ]; then
        key_args=(-i "$SSH_KEY")
    fi
    ssh -o StrictHostKeyChecking=no -o BatchMode=yes \
        -p "$SSH_PORT" "${key_args[@]}" "${SSH_USER}@${SSH_HOST}" "$@"
}

EXISTING_TIER=""
if [ "${GERRIT_HOOK_DRY_RUN:-0}" != "1" ]; then
    QUERY_OUT="$(ssh_gerrit gerrit query --format=JSON --current-patch-set --all-approvals "change:${CHANGE_ID}" 2>/dev/null | head -n 1)"
    if [ -n "$QUERY_OUT" ]; then
        # Extract the highest Tier vote on the current patchset. Gerrit
        # returns approvals as an array of {type, value, ...}; we want
        # the one where type == "Tier" (the label name).
        EXISTING_TIER="$(printf '%s' "$QUERY_OUT" | python3 -c '
import json, sys
try:
    obj = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
ps = obj.get("currentPatchSet", {}) or {}
approvals = ps.get("approvals", []) or []
values = [str(a.get("value","")).lower() for a in approvals if a.get("type") == "Tier"]
# Tier label values are stored as strings "s"/"m"/"l"/"x".
for v in values:
    if v in ("s", "m", "l", "x"):
        sys.stdout.write(v); break
')"
    fi
fi

# ── Apply monotonicity (max(existing, computed)) ──────────────────────
tier_rank() {
    case "$1" in
        s) echo 1 ;;
        m) echo 2 ;;
        l) echo 3 ;;
        x) echo 4 ;;
        *) echo 0 ;;
    esac
}

FINAL_TIER="$COMPUTED_TIER"
if [ -n "$EXISTING_TIER" ]; then
    if [ "$(tier_rank "$EXISTING_TIER")" -gt "$(tier_rank "$COMPUTED_TIER")" ]; then
        FINAL_TIER="$EXISTING_TIER"
    fi
fi

# ── Audit + post the label ────────────────────────────────────────────
PATHS_PREVIEW="$(head -c 512 "$PATHS_FILE" | tr '\n' ',' | sed 's/,$//')"
log "classified change_id=${CHANGE_ID} project=${PROJECT} commit=${COMMIT_SHA} patchset=${PATCHSET} paths_count=${PATH_COUNT} paths=${PATHS_PREVIEW} computed_tier=${COMPUTED_TIER} existing_label=${EXISTING_TIER:-none} final_label=${FINAL_TIER}"

if [ "${GERRIT_HOOK_DRY_RUN:-0}" = "1" ]; then
    log "dry_run=1 skipping_gerrit_review change_id=${CHANGE_ID} final_label=${FINAL_TIER}"
    exit 0
fi

# Use the commit SHA as the review target — Gerrit's ``gerrit review``
# accepts ``<change>,<patchset>`` or a SHA. SHA is unambiguous and
# survives the ref-update path where we don't have --patchset.
REVIEW_TARGET="$COMMIT_SHA"
if [ -n "$PATCHSET" ] && [ -n "$CHANGE_ID" ]; then
    REVIEW_TARGET="${CHANGE_ID},${PATCHSET}"
fi

if ! ssh_gerrit gerrit review --label "Tier=${FINAL_TIER}" "$REVIEW_TARGET" >/dev/null 2>&1; then
    log_err "gerrit_review_failed change_id=${CHANGE_ID} target=${REVIEW_TARGET} final_label=${FINAL_TIER}"
    # Still exit 0 — see file header.
fi

exit 0
