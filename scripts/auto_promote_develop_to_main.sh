#!/usr/bin/env bash
# OP-877 D5 — daily develop -> main auto-promote cron.
#
# Behaviour:
#   * Read the most recent `release_milestone_checker.py` event log
#     (D1 / OP-868 acceptance signal for the current fixVersion).
#   * If acceptance is GREEN: fast-forward `main` to `develop` and push
#     to Gerrit; record a `promoted` row in the `release_audit` table.
#   * If acceptance is NOT green: log + no-op + record
#     `milestone_not_accepted` (cron exits 0 — non-fatal per error catalog).
#   * If develop/main have diverged (FF impossible): refuse +
#     `ff_not_possible` audit row + non-zero exit (operator alert).
#   * If Gerrit refuses the push: `push_rejected` audit row + non-zero
#     exit (retry next tick).
#
# Environment overrides (test seam — production cron uses defaults):
#   OP877_REPO            git repo path                 (default: /home/user/sora-bridge)
#   OP877_REMOTE          gerrit remote name            (default: gerrit)
#   OP877_SOURCE_BRANCH   source branch                 (default: develop)
#   OP877_TARGET_BRANCH   target branch                 (default: main)
#   OP877_EVENT_LOG       release-milestone event log   (default: /home/user/work/sora/logs/release-milestone/systemd.log)
#   OP877_AUDIT_DB        sqlite or postgres URL        (default: $OMNISIGHT_DATABASE_URL)
#   OP877_AUDIT_DB_KIND   "sqlite" | "postgres" | "skip" (auto-detected from URL prefix)
#   OP877_NOW             ISO-8601 UTC timestamp override (default: $(date -u))
set -euo pipefail

REPO="${OP877_REPO:-/home/user/sora-bridge}"
REMOTE="${OP877_REMOTE:-gerrit}"
SOURCE_BRANCH="${OP877_SOURCE_BRANCH:-develop}"
TARGET_BRANCH="${OP877_TARGET_BRANCH:-main}"
EVENT_LOG="${OP877_EVENT_LOG:-/home/user/work/sora/logs/release-milestone/systemd.log}"
AUDIT_DB="${OP877_AUDIT_DB:-${OMNISIGHT_DATABASE_URL:-}}"
NOW="${OP877_NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

# Auto-detect DB kind unless overridden. "skip" suppresses audit write
# (used by the alembic-apply test which sets up its own DB).
if [ -z "${OP877_AUDIT_DB_KIND:-}" ]; then
    case "${AUDIT_DB}" in
        sqlite:*|*.sqlite|*.db|*.sqlite3) OP877_AUDIT_DB_KIND="sqlite" ;;
        postgres:*|postgresql:*)          OP877_AUDIT_DB_KIND="postgres" ;;
        "")                               OP877_AUDIT_DB_KIND="skip" ;;
        *)                                OP877_AUDIT_DB_KIND="skip" ;;
    esac
fi

log() { printf '[OP-877] %s %s\n' "${NOW}" "$*"; }

# sql_quote ARG  ->  single-quote-escaped SQL literal on stdout.
sql_quote() {
    local v="${1//\'/\'\'}"
    printf "'%s'" "${v}"
}

# record_audit OUTCOME FIX_VERSION DEVELOP_SHA MAIN_SHA DETAIL_JSON
record_audit() {
    local outcome="$1" fix_version="${2:-}" develop_sha="${3:-}" main_sha="${4:-}" detail="${5:-{\}}"
    local fix_version_sql
    if [ -z "${fix_version}" ]; then
        fix_version_sql="NULL"
    else
        fix_version_sql="$(sql_quote "${fix_version}")"
    fi
    case "${OP877_AUDIT_DB_KIND}" in
        sqlite)
            local db_file="${AUDIT_DB#sqlite:}"
            db_file="${db_file#//}"
            sqlite3 "${db_file}" "INSERT INTO release_audit (ts, outcome, fix_version, develop_sha, main_sha, detail) VALUES ($(sql_quote "${NOW}"), $(sql_quote "${outcome}"), ${fix_version_sql}, $(sql_quote "${develop_sha}"), $(sql_quote "${main_sha}"), $(sql_quote "${detail}"));"
            ;;
        postgres)
            PGPASSWORD="${PGPASSWORD:-}" psql "${AUDIT_DB}" -v ON_ERROR_STOP=1 \
                -c "INSERT INTO release_audit (outcome, fix_version, develop_sha, main_sha, detail) VALUES ($(sql_quote "${outcome}"), ${fix_version_sql}, $(sql_quote "${develop_sha}"), $(sql_quote "${main_sha}"), $(sql_quote "${detail}"));"
            ;;
        skip)
            log "audit write skipped (OP877_AUDIT_DB_KIND=skip)"
            ;;
    esac
}

# Parse latest milestone_ready / milestone_blocked record from event log.
# Echoes "<event>\t<fixVersion>" on stdout, or "missing\t" if no recent record.
latest_acceptance() {
    if [ ! -s "${EVENT_LOG}" ]; then
        printf 'missing\t\n'
        return
    fi
    python3 - "${EVENT_LOG}" <<'PYEOF'
import json, sys
path = sys.argv[1]
event, version = "missing", ""
with open(path, "r", encoding="utf-8", errors="replace") as fh:
    for raw in fh:
        raw = raw.strip()
        idx = raw.find("{")
        if idx < 0:
            continue
        try:
            rec = json.loads(raw[idx:])
        except json.JSONDecodeError:
            continue
        ev = rec.get("event", "")
        if ev in ("milestone_ready", "milestone_blocked"):
            event, version = ev, str(rec.get("fixVersion", ""))
print(f"{event}\t{version}")
PYEOF
}

main() {
    local accept_line event version
    accept_line="$(latest_acceptance)"
    event="${accept_line%%$'\t'*}"
    version="${accept_line#*$'\t'}"

    if [ "${event}" != "milestone_ready" ]; then
        log "MilestoneNotAccepted: latest event=${event:-<none>} version=${version:-<none>}; no-op"
        record_audit "milestone_not_accepted" "${version}" "" "" "{\"event\":\"${event}\"}"
        return 0
    fi

    local develop_sha main_sha main_only dev_only
    develop_sha="$(git -C "${REPO}" rev-parse "${SOURCE_BRANCH}")"
    main_sha="$(git -C "${REPO}" rev-parse "${TARGET_BRANCH}")"
    main_only="$(git -C "${REPO}" log --oneline "${SOURCE_BRANCH}..${TARGET_BRANCH}" || true)"
    dev_only="$(git -C "${REPO}" log --oneline "${TARGET_BRANCH}..${SOURCE_BRANCH}" || true)"

    if [ -n "${main_only}" ]; then
        log "GitFFNotPossible: ${TARGET_BRANCH} has commits absent from ${SOURCE_BRANCH}"
        record_audit "ff_not_possible" "${version}" "${develop_sha}" "${main_sha}" \
            "{\"main_only_count\":$(printf '%s' "${main_only}" | wc -l | tr -d ' ')}"
        return 2
    fi
    if [ -z "${dev_only}" ]; then
        log "Noop: ${TARGET_BRANCH} already at ${SOURCE_BRANCH}"
        record_audit "noop" "${version}" "${develop_sha}" "${main_sha}" "{}"
        return 0
    fi

    if ! git -C "${REPO}" push "${REMOTE}" "${SOURCE_BRANCH}:${TARGET_BRANCH}" >/tmp/op877-push.$$.log 2>&1; then
        local push_err
        push_err="$(tr '\n' ' ' </tmp/op877-push.$$.log | head -c 400 | sed 's/"/\\"/g')"
        rm -f /tmp/op877-push.$$.log
        log "MainPushRejected: gerrit refused push: ${push_err}"
        record_audit "push_rejected" "${version}" "${develop_sha}" "${main_sha}" "{\"err\":\"${push_err}\"}"
        return 3
    fi
    rm -f /tmp/op877-push.$$.log

    log "Promoted: ${TARGET_BRANCH} ${main_sha} -> ${develop_sha} for ${version}"
    record_audit "promoted" "${version}" "${develop_sha}" "${main_sha}" "{}"
}

main "$@"
