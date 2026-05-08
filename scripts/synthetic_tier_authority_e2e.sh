#!/bin/bash
# OP-807 (G5) — synthetic Tier S/M/L/X end-to-end test against prod Gerrit.
#
# WHEN: invoked manually by the operator (or by the AC-verification job)
#       after the OP-805 Gerrit hook is installed on the live host.
#       NOT auto-scheduled — every run pushes 4 real patchsets to
#       prod Gerrit and abandons them on cleanup. Re-running is safe
#       (each PS goes to a unique synthetic ref) but operator-visible.
#
# WHAT: pushes one synthetic patchset per tier (s|m|l|x) such that the
#       changed-paths set forces the OP-805 hook to compute exactly
#       that tier. Then queries Gerrit for the resulting ``Tier`` label
#       and asserts the assigned value matches the expected one. Finally
#       abandons all four synthetic changes so they don't leak into the
#       open-PS list.
#
# WHY:  this is the live-system equivalent of a contract test — the
#       four-tier classifier on the Gerrit host has its own
#       configuration (PYTHONPATH or HTTPS classify-tier endpoint)
#       and the only way to know the operator wired it correctly is
#       to push real PSes and read back the labels. Unit tests verify
#       the classifier logic in isolation; this script verifies the
#       deployment.
#
# Dry-run mode (``--dry-run``): generate the commits but skip the
#   ``git push refs/for`` step, and skip the Gerrit query/abandon
#   steps. Used by CI to lint the script and verify the synthetic
#   commits build before risking a prod push.
#
# Required env (sane defaults for prod sora.services):
#   GERRIT_SSH_HOST          SSH host (default: sora.services)
#   GERRIT_SSH_PORT          SSH port (default: 29418)
#   GERRIT_SSH_USER          SSH user (default: claude-bot)
#   GERRIT_SSH_KEY           private key path (default: ~/.ssh/id_ed25519_claude_bot)
#   GERRIT_PROJECT           target project (default: OmniSight)
#   GERRIT_BRANCH            target branch for refs/for (default: develop)
#   POLL_TIMEOUT_S           seconds to wait for the hook to set the
#                            Tier label after push (default: 60)
#
# Exit codes:
#   0   all four tiers verified
#   1   at least one tier mismatched the expected value
#   2   prerequisite missing (no SSH access, no upstream remote, etc.)
#   3   Gerrit query failed for at least one synthetic change
#
# This script writes one summary line per tier to stdout in the format::
#
#   tier=<expected> path=<touched-file> change=<num> result=<assigned>
#                                                              status=<ok|fail>
#
# and a final ``ALL_TIERS_OK=1`` (or =0) line that the AC-verification
# job greps for.

set -u
set -o pipefail

# ── Configuration ────────────────────────────────────────────────────

GERRIT_SSH_HOST="${GERRIT_SSH_HOST:-sora.services}"
GERRIT_SSH_PORT="${GERRIT_SSH_PORT:-29418}"
GERRIT_SSH_USER="${GERRIT_SSH_USER:-claude-bot}"
GERRIT_SSH_KEY="${GERRIT_SSH_KEY:-$HOME/.ssh/id_ed25519_claude_bot}"
GERRIT_PROJECT="${GERRIT_PROJECT:-OmniSight}"
GERRIT_BRANCH="${GERRIT_BRANCH:-develop}"
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-60}"

DRY_RUN=0
case "${1:-}" in
    --dry-run|-n) DRY_RUN=1 ;;
    --help|-h)
        sed -n '2,40p' "$0"  # print the header block as usage
        exit 0
        ;;
esac

# Repo root is the worktree we're invoked from.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
    echo "synthetic_tier_authority_e2e: not inside a git worktree" >&2
    exit 2
fi
cd "$REPO_ROOT"

# ── Tier → synthetic touched-path lookup ─────────────────────────────
#
# Each entry is ``<tier>:<relative-path>``. The path is chosen so the
# tier-paths.yaml resolution rules produce *exactly* the expected tier:
#
#   s = whitelist match only — every touched path is in tiers.s.whitelist_globs
#   m = no force-upgrade and no whitelist match — fallback default
#   l = path matches tiers.l.force_upgrade_globs (alembic migration here)
#   x = path matches tiers.x.force_upgrade_globs (deploy/systemd here)
#
# For the M case we touch a backend/ path that's outside every
# whitelist and force-upgrade list (``backend/op807_synth_M_marker.txt``
# matches no glob in tier-paths.yaml, so it falls through to the M
# default). test_assets/ is deliberately AVOIDED — it's CLAUDE.md L1
# read-only ground truth. For the L and X cases we touch genuine
# Tier L / Tier X paths because force-upgrade is what we're verifying.
#
# The Tier L glob is ``backend/alembic/versions/*.py`` so the synthetic
# revision matches that glob but uses a marker (``op807_SYNTH_``) so
# operator cleanup can recognise stale uploads. The synthetic L file
# is NEVER applied (the change is abandoned within seconds and never
# reaches develop's alembic head).

TIER_S_PATH="backend/tests/op807_synth_S_marker.txt"
TIER_M_PATH="backend/op807_synth_M_marker.txt"       # no glob match → M default
TIER_L_PATH="backend/alembic/versions/op807_SYNTH_L_marker.py"
TIER_X_PATH="deploy/systemd/op807-synth-x.marker"

# ── Helpers ──────────────────────────────────────────────────────────

ssh_gerrit() {
    ssh -o StrictHostKeyChecking=no -o BatchMode=yes \
        -p "$GERRIT_SSH_PORT" -i "$GERRIT_SSH_KEY" \
        "${GERRIT_SSH_USER}@${GERRIT_SSH_HOST}" "$@"
}

# Returns the assigned Tier label letter (s|m|l|x) for a given change
# number, or empty string if the label is not yet present.
fetch_tier_label() {
    local change_num="$1"
    local out
    out="$(ssh_gerrit gerrit query --format=JSON --current-patch-set --all-approvals \
        "change:${change_num}" 2>/dev/null | head -n 1)" || return 1
    [ -z "$out" ] && return 0
    printf '%s' "$out" | python3 -c '
import json, sys
try:
    obj = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
ps = obj.get("currentPatchSet", {}) or {}
approvals = ps.get("approvals", []) or []
for a in approvals:
    if a.get("type") == "Tier":
        v = str(a.get("value","")).lower().strip()
        if v in ("s","m","l","x"):
            sys.stdout.write(v); break
'
}

abandon_change() {
    local change_num="$1"
    ssh_gerrit gerrit review --abandon "change:${change_num}" >/dev/null 2>&1 || true
}

# Polls Gerrit until ``fetch_tier_label`` returns non-empty or timeout.
poll_for_label() {
    local change_num="$1"
    local elapsed=0
    while [ "$elapsed" -lt "$POLL_TIMEOUT_S" ]; do
        local v
        v="$(fetch_tier_label "$change_num")" || true
        if [ -n "$v" ]; then
            printf '%s' "$v"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

# Pushes a synthetic single-file commit and returns the change number.
# Always pushes to a fresh topic so multiple runs don't collide.
push_synthetic_ps() {
    local tier="$1"
    local path="$2"
    local topic
    topic="op807-synth-${tier}-$(date -u +%Y%m%d-%H%M%S)-$$"

    mkdir -p "$(dirname "$path")"
    local body=""
    case "$path" in
        *.py)
            body=$'"""OP-807 synthetic Tier '"$tier"$' marker. Auto-abandoned by the e2e script."""\n'
            ;;
        *)
            body="# OP-807 synthetic Tier ${tier} marker (auto-abandoned)\n"
            ;;
    esac
    printf '%b' "$body" > "$path"

    git add "$path" >/dev/null
    git commit -q -m "[OP-807-SYNTH] tier-${tier} synthetic e2e marker

This commit is generated by scripts/synthetic_tier_authority_e2e.sh
to verify the Gerrit Tier-label hook (OP-805) classifies a change
that touches \`${path}\` as tier '${tier}'. The e2e script abandons
this change immediately after reading the assigned label."

    if [ "$DRY_RUN" = "1" ]; then
        # Reset working tree so the operator's repo isn't dirty after.
        git reset --hard HEAD~1 >/dev/null 2>&1 || true
        printf 'DRYRUN-SYNTH-%s\n' "$tier"
        return 0
    fi

    local push_out
    push_out="$(git push origin "HEAD:refs/for/${GERRIT_BRANCH}%topic=${topic}" 2>&1)" || {
        echo "$push_out" >&2
        return 1
    }
    # Gerrit prints a "remote: New Changes:" line containing the change URL
    # ending in /<change-number>. Parse out the numeric tail.
    local change_num
    change_num="$(printf '%s' "$push_out" \
        | grep -oE 'https?://[^ ]+/[0-9]+' \
        | head -n 1 \
        | grep -oE '[0-9]+$' || true)"
    # Reset the local commit either way — we don't want it on the
    # operator's branch after the script returns.
    git reset --hard HEAD~1 >/dev/null 2>&1 || true
    if [ -z "$change_num" ]; then
        echo "could not parse change number from gerrit push output" >&2
        echo "$push_out" >&2
        return 1
    fi
    printf '%s' "$change_num"
}

# ── Run all four tiers ───────────────────────────────────────────────

declare -a CHANGE_NUMS=()
declare -a EXPECTED_TIERS=("s" "m" "l" "x")
declare -a SYNTH_PATHS=("$TIER_S_PATH" "$TIER_M_PATH" "$TIER_L_PATH" "$TIER_X_PATH")
declare -a RESULTS=()

cleanup() {
    if [ "$DRY_RUN" = "1" ]; then
        return 0
    fi
    for cn in "${CHANGE_NUMS[@]}"; do
        if [ -n "$cn" ] && [ "${cn:0:8}" != "DRYRUN-S" ]; then
            abandon_change "$cn"
        fi
    done
}
trap cleanup EXIT

ALL_OK=1
for i in 0 1 2 3; do
    tier="${EXPECTED_TIERS[$i]}"
    synth_path="${SYNTH_PATHS[$i]}"
    cn="$(push_synthetic_ps "$tier" "$synth_path")" || {
        echo "tier=${tier} path=${synth_path} push_failed=1 status=fail"
        ALL_OK=0
        CHANGE_NUMS+=("")
        RESULTS+=("push-failed")
        continue
    }
    CHANGE_NUMS+=("$cn")
    if [ "$DRY_RUN" = "1" ]; then
        echo "tier=${tier} path=${synth_path} change=${cn} dry_run=1 status=ok"
        RESULTS+=("dry-run")
        continue
    fi
    assigned="$(poll_for_label "$cn")" || {
        echo "tier=${tier} path=${synth_path} change=${cn} result=NONE timeout=${POLL_TIMEOUT_S}s status=fail"
        ALL_OK=0
        RESULTS+=("timeout")
        continue
    }
    if [ "$assigned" = "$tier" ]; then
        echo "tier=${tier} path=${synth_path} change=${cn} result=${assigned} status=ok"
        RESULTS+=("ok")
    else
        echo "tier=${tier} path=${synth_path} change=${cn} result=${assigned} status=fail"
        ALL_OK=0
        RESULTS+=("mismatch")
    fi
done

echo "ALL_TIERS_OK=${ALL_OK}"
if [ "$ALL_OK" = "1" ]; then
    exit 0
else
    exit 1
fi
