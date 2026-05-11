#!/usr/bin/env bash
# OP-886 — cut an accelerated hotfix branch from a Gerrit change.
#
# Usage:
#   scripts/hotfix_cut.sh --from-change 123 --target main \
#     --human-plus2 alice --smoke-command 'scripts/prod_smoke_test.py https://prod' \
#     --deploy-command 'scripts/deploy-prod.sh --branch hotfix/v0.3.1'
#
# Conflict exit code: 20 (HotfixCherryPickConflict)
# Smoke exit code:   30 (HotfixSmokeFailed)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FROM_CHANGE=""
TARGET=""
REMOTE="${OMNISIGHT_HOTFIX_REMOTE:-gerrit}"
HUMAN_PLUS2=""
SMOKE_COMMAND=""
DEPLOY_COMMAND=""
CUT_ONLY=false
DRY_RUN=false

err() { echo "hotfix_cut: $*" >&2; exit 2; }
log() { echo "hotfix_cut: $*" >&2; }

usage() {
    sed -n '3,11p' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-change) FROM_CHANGE="${2:-}"; shift 2;;
        --target) TARGET="${2:-}"; shift 2;;
        --repo) REPO="$(cd "${2:-}" && pwd)"; shift 2;;
        --remote) REMOTE="${2:-}"; shift 2;;
        --human-plus2) HUMAN_PLUS2="${2:-}"; shift 2;;
        --smoke-command) SMOKE_COMMAND="${2:-}"; shift 2;;
        --deploy-command) DEPLOY_COMMAND="${2:-}"; shift 2;;
        --cut-only) CUT_ONLY=true; shift;;
        --dry-run) DRY_RUN=true; shift;;
        -h|--help) usage; exit 0;;
        *) err "unknown arg: $1";;
    esac
done

[[ -n "$FROM_CHANGE" ]] || err "--from-change is required"
[[ -n "$TARGET" ]] || err "--target is required"

git_c() {
    git -C "$REPO" "$@"
}

run_or_print() {
    if [[ "$DRY_RUN" == "true" ]]; then
        printf '[dry-run]' >&2
        printf ' %q' "$@" >&2
        printf '\n' >&2
    else
        "$@" >&2
    fi
}

json_event() {
    local event="$1" code="$2" branch="$3" change="$4" detail="$5"
    printf '{"event":"%s","code":"%s","hotfix_branch":"%s","from_change":"%s","detail":"%s"}\n' \
        "$event" "$code" "$branch" "$change" "${detail//\"/\\\"}"
}

resolve_change_commit() {
    local change="$1" ref last_two patchset
    if git_c rev-parse --verify --quiet "${change}^{commit}" >/dev/null; then
        git_c rev-parse --verify "${change}^{commit}"
        return 0
    fi
    [[ "$change" =~ ^[0-9]+$ ]] || err "--from-change must be a Gerrit number or local commit"
    last_two=$(printf '%02d' "$((10#$change % 100))")
    ref=$(git_c ls-remote "$REMOTE" "refs/changes/${last_two}/${change}/*" \
        | awk '{print $2}' \
        | sort -t/ -k5,5n \
        | tail -1)
    [[ -n "$ref" ]] || err "no Gerrit patchset ref found for change $change on remote $REMOTE"
    patchset="${ref##*/}"
    log "fetching change $change patchset $patchset from $REMOTE"
    run_or_print git -C "$REPO" fetch "$REMOTE" "$ref"
    if [[ "$DRY_RUN" == "true" ]]; then
        printf 'FETCH_HEAD\n'
    else
        git_c rev-parse --verify FETCH_HEAD
    fi
}

latest_semver_tag() {
    git_c tag --list 'v[0-9]*.[0-9]*.[0-9]*' --merged "$TARGET" \
        | sed -E 's/^v([0-9]+)\.([0-9]+)\.([0-9]+)$/\1 \2 \3 &/' \
        | sort -n -k1,1 -k2,2 -k3,3 \
        | awk 'END {print $4}'
}

next_patch_version() {
    local tag="$1" major minor patch
    [[ "$tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || err "no SemVer tag found on $TARGET"
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    patch="${BASH_REMATCH[3]}"
    printf 'v%s.%s.%s\n' "$major" "$minor" "$((10#$patch + 1))"
}

abort_cherry_pick_if_needed() {
    if [[ -f "$REPO/.git/CHERRY_PICK_HEAD" ]]; then
        git_c cherry-pick --abort >/dev/null 2>&1 || true
    fi
}

change_commit="$(resolve_change_commit "$FROM_CHANGE")"
git_c rev-parse --verify "$TARGET^{commit}" >/dev/null || err "target ref not found: $TARGET"

source_tag="$(latest_semver_tag)"
next_version="$(next_patch_version "$source_tag")"
hotfix_branch="hotfix/${next_version}"

if [[ "$CUT_ONLY" != "true" ]]; then
    [[ -n "$HUMAN_PLUS2" ]] || err "--human-plus2 is required unless --cut-only is used"
    [[ -n "$SMOKE_COMMAND" ]] || err "--smoke-command is required unless --cut-only is used"
fi
[[ -n "$DEPLOY_COMMAND" ]] || DEPLOY_COMMAND="scripts/deploy-prod.sh --branch ${hotfix_branch}"

log "cutting $hotfix_branch from $TARGET for change $FROM_CHANGE ($change_commit)"
if git_c show-ref --verify --quiet "refs/heads/${hotfix_branch}"; then
    log "branch already exists: $hotfix_branch"
    run_or_print git -C "$REPO" checkout "$hotfix_branch"
else
    run_or_print git -C "$REPO" checkout -b "$hotfix_branch" "$TARGET"
fi

if git_c merge-base --is-ancestor "$change_commit" HEAD 2>/dev/null; then
    log "change commit is already reachable from $hotfix_branch"
elif git_c log --format=%B "$TARGET..HEAD" | grep -Fq "(cherry picked from commit ${change_commit})"; then
    log "change commit was already cherry-picked onto $hotfix_branch"
elif ! run_or_print git -C "$REPO" cherry-pick -x "$change_commit"; then
    abort_cherry_pick_if_needed
    json_event "hotfix.refused" "HotfixCherryPickConflict" "$hotfix_branch" "$FROM_CHANGE" \
        "manual merge ticket required before retry"
    exit 20
fi

if [[ "$CUT_ONLY" == "true" ]]; then
    json_event "hotfix.branch_cut" "ok" "$hotfix_branch" "$FROM_CHANGE" "source_tag=${source_tag}"
    exit 0
fi

log "reduced gate accepted: human_plus2=${HUMAN_PLUS2}; D1 milestone acceptance skipped"
if ! bash -lc "$SMOKE_COMMAND"; then
    json_event "hotfix.refused" "HotfixSmokeFailed" "$hotfix_branch" "$FROM_CHANGE" \
        "smoke command failed; production promotion blocked"
    exit 30
fi

approved_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
deadline="$(date -u -d "${approved_at} + 30 minutes" +%Y-%m-%dT%H:%M:%SZ)"
log "approved_at=${approved_at}; prod deploy deadline=${deadline}"
run_or_print bash -lc "$DEPLOY_COMMAND"
json_event "hotfix.deployed" "ok" "$hotfix_branch" "$FROM_CHANGE" "deploy_deadline=${deadline}"
