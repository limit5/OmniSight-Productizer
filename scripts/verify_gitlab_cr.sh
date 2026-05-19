#!/usr/bin/env bash
# OP-1514 — Verify GitLab Container Registry has fresh image artifacts.
#
# Part of META OP-1478 (Image artifact pipeline activation,
# scope:op-1478-activation-2026-05-19). Companion to OP-1511's
# gitlab-runner.service template. Once the runner is registered and a
# `v*` tag pipeline ships images into GitLab CR, this script becomes the
# daily heartbeat that confirms the CR is alive AND has expected images.
# Matches the existing pattern of gerrit-jira-bridge-watchdog,
# auto-promote-develop and the compliance-ledger-daily-export timers.
#
# Usage:
#   scripts/verify_gitlab_cr.sh
#
# Exit codes:
#   0  — at least one registry repo exists AND its latest tag was
#        updated within OMNISIGHT_GITLAB_CR_FRESHNESS_HOURS (default 48).
#        Emits a single-line JSON status="all_ok" on stderr.
#   1  — registry empty (status="cr_empty"), all tags stale
#        (status="stale"), or an upstream API failure
#        (status="api_error" / "auth_failed" / "no_token" /
#        "missing_dependency"). On status="stale" the script also pages
#        the operator_notifier bridge at severity=DEGRADED so the daily
#        timer surfaces silent CR drops in addition to the journal
#        line. Pure tooling failures (missing curl/jq, missing token
#        file) do NOT page — they exit 1 with the structured status so
#        the systemd unit's Restart=on-failure semantics catch them.
#
# Env vars:
#   OMNISIGHT_GITLAB_API_URL          GitLab API base URL.
#                                     Default: https://sora.services:49154
#   OMNISIGHT_GITLAB_PROJECT_PATH     URL-encoded project path used in
#                                     /api/v4/projects/:id/registry/...
#                                     Default: omnisight%2FOmniSight-Productizer
#   OMNISIGHT_GITLAB_TOKEN_FILE       Override token path (test hook).
#                                     Default: ~/.config/omnisight/gitlab-claude-token
#   OMNISIGHT_GITLAB_CR_FRESHNESS_HOURS  Tag freshness threshold.
#                                     Default: 48
#   OMNISIGHT_GITLAB_CR_NOTIFY        Set to 0 to suppress the
#                                     operator_notifier page on stale
#                                     state (used by smoke tests).
#                                     Default: 1
#   OMNISIGHT_REPO_ROOT               Repo root (for PYTHONPATH when
#                                     dispatching the notifier).
#                                     Default: directory containing
#                                     this script's parent.
#
# Why bash (not Python): the systemd unit runs as a oneshot timer and
# the verify path is curl + jq + one date comparison. The notifier
# fan-out is delegated to backend.agents.operator_notifier so the
# severity/dedup/ACK behaviour stays consistent with the rest of the
# program (OP-722 / OP-755).

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_DEFAULT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="${OMNISIGHT_REPO_ROOT:-$REPO_ROOT_DEFAULT}"

GITLAB_API_URL="${OMNISIGHT_GITLAB_API_URL:-https://sora.services:49154}"
PROJECT_PATH="${OMNISIGHT_GITLAB_PROJECT_PATH:-omnisight%2FOmniSight-Productizer}"
TOKEN_FILE="${OMNISIGHT_GITLAB_TOKEN_FILE:-${HOME}/.config/omnisight/gitlab-claude-token}"
FRESHNESS_HOURS="${OMNISIGHT_GITLAB_CR_FRESHNESS_HOURS:-48}"
NOTIFY_ENABLED="${OMNISIGHT_GITLAB_CR_NOTIFY:-1}"

# ── JSON status emitter ───────────────────────────────────────────
#
# One JSON line per run, written to stderr so the systemd
# StandardError=journal redirect captures it as a single structured
# event. jq -c keeps it parseable by promtail / journalctl -o cat
# downstream.

emit_status() {
    local status="$1"; shift
    local message="$1"; shift
    # Remaining args are jq --arg key val pairs.
    local jq_args=(
        --arg status "$status"
        --arg message "$message"
        --arg api_url "$GITLAB_API_URL"
        --arg project "$PROJECT_PATH"
        --arg freshness_hours "$FRESHNESS_HOURS"
        --arg ts "$(date -u +%FT%TZ)"
    )
    while [ $# -gt 1 ]; do
        jq_args+=(--arg "$1" "$2")
        shift 2
    done
    if command -v jq >/dev/null 2>&1; then
        jq -cn "${jq_args[@]}" \
            '{status:$status, message:$message, api_url:$api_url, project:$project, freshness_hours:$freshness_hours, ts:$ts} + (
                {} + ($ARGS.named | with_entries(select(.key | IN("status","message","api_url","project","freshness_hours","ts") | not)))
            )' >&2
    else
        # jq missing — fall back to a literal one-liner so the script
        # still emits *something* parseable for the journal.
        printf '{"status":"%s","message":"%s","api_url":"%s","project":"%s","freshness_hours":"%s","ts":"%s"}\n' \
            "$status" "$message" "$GITLAB_API_URL" "$PROJECT_PATH" "$FRESHNESS_HOURS" "$(date -u +%FT%TZ)" >&2
    fi
}

# ── Operator-notifier dispatch (DEGRADED only) ────────────────────
#
# The notifier integration only fires on status="stale" — the case
# where the CR is reachable but the pipeline has stopped producing
# fresh images. cr_empty is the *pre-O2* expected state and AC 3
# explicitly requires we NOT page on it.

fire_degraded() {
    local code="$1"; shift
    local message="$1"; shift
    local hours_old="$1"; shift
    local latest_tag="$1"; shift

    if [ "$NOTIFY_ENABLED" != "1" ]; then
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        echo "WARN: python3 missing — cannot dispatch operator_notifier page" >&2
        return 0
    fi

    PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
        python3 - <<PY 2>&1 || echo "WARN: operator_notifier dispatch failed (non-fatal)" >&2
import os, sys
try:
    from backend.agents.operator_notifier import Severity, notify
except Exception as exc:
    print(f"WARN: notifier import failed: {exc}", file=sys.stderr)
    sys.exit(0)
notify(
    Severity.DEGRADED,
    "${code}",
    message="${message}",
    context={
        "api_url": "${GITLAB_API_URL}",
        "project": "${PROJECT_PATH}",
        "freshness_hours": "${FRESHNESS_HOURS}",
        "hours_since_last_tag": "${hours_old}",
        "latest_tag": "${latest_tag}",
        "runbook": "docs/operations/gitlab-cr-monitor.md",
    },
)
PY
}

# ── Dependency / token preflight ──────────────────────────────────

for tool in curl jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        emit_status "missing_dependency" "$tool not installed on host" \
            tool "$tool"
        exit 1
    fi
done

if [ ! -r "$TOKEN_FILE" ]; then
    emit_status "no_token" "token file missing or unreadable" \
        token_file "$TOKEN_FILE"
    exit 1
fi

TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
if [ -z "$TOKEN" ]; then
    emit_status "no_token" "token file empty" token_file "$TOKEN_FILE"
    exit 1
fi

# ── API: list registry repositories ───────────────────────────────

REPOS_URL="${GITLAB_API_URL}/api/v4/projects/${PROJECT_PATH}/registry/repositories"
REPOS_BODY="$(mktemp)"
trap 'rm -f "$REPOS_BODY" "${TAGS_BODY:-}"' EXIT

REPOS_HTTP="$(curl -sS -o "$REPOS_BODY" -w '%{http_code}' \
    --max-time 30 \
    --header "PRIVATE-TOKEN: ${TOKEN}" \
    "$REPOS_URL" || echo "000")"

case "$REPOS_HTTP" in
    200) ;;
    401|403)
        emit_status "auth_failed" "GitLab API rejected token" \
            http_status "$REPOS_HTTP"
        exit 1
        ;;
    *)
        emit_status "api_error" "GitLab registry/repositories request failed" \
            http_status "$REPOS_HTTP" url "$REPOS_URL"
        exit 1
        ;;
esac

REPO_COUNT="$(jq 'length' < "$REPOS_BODY" 2>/dev/null || echo 0)"
if ! [[ "$REPO_COUNT" =~ ^[0-9]+$ ]] || [ "$REPO_COUNT" -eq 0 ]; then
    emit_status "cr_empty" "no registry repositories — CR not yet populated" \
        repo_count "${REPO_COUNT:-0}"
    exit 1
fi

# Pick the first repo ID. The omnisight-productizer project ships one
# repo per image (backend / frontend / bridge) and the daily check only
# needs to confirm that *some* repo has a fresh tag — the per-image
# matrix is exercised by the build pipeline itself, not by the monitor.
REPO_ID="$(jq -r '.[0].id' < "$REPOS_BODY")"
REPO_NAME="$(jq -r '.[0].name // .[0].path // "unknown"' < "$REPOS_BODY")"

if [ -z "$REPO_ID" ] || [ "$REPO_ID" = "null" ]; then
    emit_status "api_error" "first repository missing id field" \
        repo_name "$REPO_NAME"
    exit 1
fi

# ── API: list tags for the first repo ─────────────────────────────
#
# `?per_page=20` is enough — the cleanup policy in
# docs/operations/gitlab-cr-pull-credentials.md keeps at most 30 untagged
# variants and we only need the most recent of them. We sort client-side
# by updated_at to insulate against the API returning tags in an
# unspecified order.

TAGS_URL="${GITLAB_API_URL}/api/v4/projects/${PROJECT_PATH}/registry/repositories/${REPO_ID}/tags?per_page=20"
TAGS_BODY="$(mktemp)"

TAGS_HTTP="$(curl -sS -o "$TAGS_BODY" -w '%{http_code}' \
    --max-time 30 \
    --header "PRIVATE-TOKEN: ${TOKEN}" \
    "$TAGS_URL" || echo "000")"

case "$TAGS_HTTP" in
    200) ;;
    401|403)
        emit_status "auth_failed" "GitLab API rejected token on tag list" \
            http_status "$TAGS_HTTP" repo_id "$REPO_ID"
        exit 1
        ;;
    *)
        emit_status "api_error" "GitLab registry/repositories/:id/tags request failed" \
            http_status "$TAGS_HTTP" url "$TAGS_URL"
        exit 1
        ;;
esac

# The tags-list endpoint does NOT return `updated_at` per-tag; only the
# per-tag detail endpoint does. Walk each tag, fetch its detail, and
# keep the freshest. This is O(N) requests but the per_page=20 cap keeps
# N small and the daily cadence makes the API cost a non-issue.

TAG_NAMES="$(jq -r '.[].name' < "$TAGS_BODY")"
if [ -z "$TAG_NAMES" ]; then
    emit_status "cr_empty" "repository present but no tags published" \
        repo_id "$REPO_ID" repo_name "$REPO_NAME"
    exit 1
fi

LATEST_TAG=""
LATEST_TS=""
LATEST_EPOCH=0

while IFS= read -r tag; do
    [ -z "$tag" ] && continue
    # URL-encode the tag (versions like "v0.5.0-rc5" are already safe;
    # branch-style tags like "develop-latest" likewise. Skip the heavy
    # encoder and trust GitLab tag-name rules.)
    DETAIL_URL="${GITLAB_API_URL}/api/v4/projects/${PROJECT_PATH}/registry/repositories/${REPO_ID}/tags/${tag}"
    DETAIL="$(curl -sS --max-time 20 \
        --header "PRIVATE-TOKEN: ${TOKEN}" \
        "$DETAIL_URL")" || continue
    ts="$(printf '%s' "$DETAIL" | jq -r '.created_at // .updated_at // empty' 2>/dev/null)"
    [ -z "$ts" ] && continue
    # GNU date understands ISO 8601 directly.
    epoch="$(date -u -d "$ts" +%s 2>/dev/null || echo 0)"
    if [ "$epoch" -gt "$LATEST_EPOCH" ]; then
        LATEST_EPOCH="$epoch"
        LATEST_TS="$ts"
        LATEST_TAG="$tag"
    fi
done <<< "$TAG_NAMES"

if [ "$LATEST_EPOCH" -eq 0 ]; then
    emit_status "api_error" "no tag had a parseable created_at/updated_at" \
        repo_id "$REPO_ID" repo_name "$REPO_NAME"
    exit 1
fi

NOW_EPOCH="$(date -u +%s)"
AGE_SECONDS=$(( NOW_EPOCH - LATEST_EPOCH ))
AGE_HOURS=$(( AGE_SECONDS / 3600 ))
THRESHOLD_SECONDS=$(( FRESHNESS_HOURS * 3600 ))

if [ "$AGE_SECONDS" -gt "$THRESHOLD_SECONDS" ]; then
    emit_status "stale" "latest tag older than freshness threshold" \
        repo_id "$REPO_ID" \
        repo_name "$REPO_NAME" \
        latest_tag "$LATEST_TAG" \
        latest_updated_at "$LATEST_TS" \
        age_hours "$AGE_HOURS"
    fire_degraded \
        "gitlab_cr_stale" \
        "GitLab CR latest tag ${LATEST_TAG} is ${AGE_HOURS}h old (threshold ${FRESHNESS_HOURS}h)" \
        "$AGE_HOURS" \
        "$LATEST_TAG"
    exit 1
fi

emit_status "all_ok" "CR has fresh image" \
    repo_id "$REPO_ID" \
    repo_name "$REPO_NAME" \
    latest_tag "$LATEST_TAG" \
    latest_updated_at "$LATEST_TS" \
    age_hours "$AGE_HOURS"
exit 0
