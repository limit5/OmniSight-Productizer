#!/usr/bin/env bash
# [OP-1132] Post-reboot production smoke test.
#
# One run emits one JSONL row to OMNISIGHT_SMOKE_LOG. Failures also page
# through backend.agents.operator_notifier.

set -u

REPO_ROOT="${OMNISIGHT_SMOKE_REPO_ROOT:-/home/user/work/sora/OmniSight-Productizer}"
BASE_URL="${OMNISIGHT_SMOKE_BASE_URL:-http://localhost:8000}"
BASE_URL="${BASE_URL%/}"
LOG_PATH="${OMNISIGHT_SMOKE_LOG:-/var/log/omnisight-smoke-test.log}"
RUNNER_LOG_GLOB="${OMNISIGHT_SMOKE_RUNNER_LOG_GLOB:-/home/user/work/sora/logs/runner/*.log}"
RUNNER_MAX_AGE_SECONDS="${OMNISIGHT_SMOKE_RUNNER_MAX_AGE_SECONDS:-300}"
CURL_BIN="${CURL_BIN:-curl}"
PSQL_BIN="${PSQL_BIN:-psql}"
SSH_BIN="${SSH_BIN:-ssh}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

declare -a CHECK_NAMES=()
declare -a CHECK_STATUSES=()
declare -a CHECK_DETAILS=()
declare -a FAILED_CHECKS=()

_now_iso() {
	/bin/date -u +"%Y-%m-%dT%H:%M:%SZ"
}

_run_check() {
	local name="$1"
	local hook_var="$2"
	local default_cmd="$3"
	local output
	local status

	CHECK_NAMES+=("$name")
	if [[ -n "${!hook_var:-}" ]]; then
		output="$(bash -c "${!hook_var}" 2>&1)"
		status=$?
	elif declare -F "$default_cmd" >/dev/null; then
		output="$("$default_cmd" 2>&1)"
		status=$?
	else
		output="$(bash -c "$default_cmd" 2>&1)"
		status=$?
	fi

	if [[ "$status" -eq 0 ]]; then
		CHECK_STATUSES+=("pass")
		CHECK_DETAILS+=("${output:-ok}")
		return 0
	fi

	CHECK_STATUSES+=("fail")
	CHECK_DETAILS+=("${output:-exit $status}")
	FAILED_CHECKS+=("$name")
	return 1
}

_db_check_cmd() {
	local dsn="${OMNISIGHT_DATABASE_URL:-${DATABASE_URL:-}}"
	if [[ -z "$dsn" ]]; then
		echo "OMNISIGHT_DATABASE_URL or DATABASE_URL is required" >&2
		return 2
	fi
	if [[ "$dsn" == postgresql+asyncpg://* ]]; then
		dsn="postgresql://${dsn#postgresql+asyncpg://}"
	fi
	"$PSQL_BIN" "$dsn" -v ON_ERROR_STOP=1 -Atc \
		"SELECT version_num FROM alembic_version LIMIT 1;" >/dev/null
}

_jira_check_cmd() {
	local base="${OMNISIGHT_JIRA_URL:-${JIRA_BASE_URL:-https://soraapp.atlassian.net}}"
	local url="${OMNISIGHT_SMOKE_JIRA_PROBE_URL:-${base%/}/rest/api/3/myself}"
	local auth_header="${OMNISIGHT_JIRA_AUTH_HEADER:-}"
	if [[ -z "$auth_header" && -n "${JIRA_EMAIL:-}" && -n "${JIRA_API_TOKEN:-}" ]]; then
		auth_header="Authorization: Basic $(printf '%s' "${JIRA_EMAIL}:${JIRA_API_TOKEN}" | base64 | tr -d '\n')"
	fi
	if [[ -z "$auth_header" && -n "${JIRA_BEARER_TOKEN:-}" ]]; then
		auth_header="Authorization: Bearer ${JIRA_BEARER_TOKEN}"
	fi
	if [[ -z "$auth_header" ]]; then
		echo "JIRA auth is required (set OMNISIGHT_JIRA_AUTH_HEADER, JIRA_EMAIL+JIRA_API_TOKEN, or JIRA_BEARER_TOKEN)" >&2
		return 2
	fi
	"$CURL_BIN" -fsS -o /dev/null -H "$auth_header" "$url"
}

_gerrit_check_cmd() {
	local host="${OMNISIGHT_GERRIT_SSH_HOST:-codex-bot@sora.services}"
	local port="${OMNISIGHT_GERRIT_SSH_PORT:-29418}"
	local key="${OMNISIGHT_GIT_SSH_KEY_PATH:-}"
	local args=(-o BatchMode=yes -o ConnectTimeout=10 -p "$port")
	if [[ -n "$key" ]]; then
		args+=(-i "$key")
	fi
	"$SSH_BIN" "${args[@]}" "$host" gerrit version >/dev/null
}

_runner_activity_check_cmd() {
	local now
	local newest=0
	local mtime
	now="$(/bin/date +%s)"
	shopt -s nullglob
	for path in $RUNNER_LOG_GLOB; do
		mtime="$(stat -c %Y "$path" 2>/dev/null || true)"
		if [[ "$mtime" =~ ^[0-9]+$ && "$mtime" -gt "$newest" ]]; then
			newest="$mtime"
		fi
	done
	shopt -u nullglob
	if [[ "$newest" -eq 0 ]]; then
		echo "no runner cycle logs matched $RUNNER_LOG_GLOB" >&2
		return 1
	fi
	if (( now - newest > RUNNER_MAX_AGE_SECONDS )); then
		echo "newest runner log is $((now - newest))s old; limit ${RUNNER_MAX_AGE_SECONDS}s" >&2
		return 1
	fi
}

_append_row() {
	local status="$1"
	local tmp
	tmp="$(mktemp)"
	"$PYTHON_BIN" - "$status" "$(_now_iso)" "${#CHECK_NAMES[@]}" >"$tmp" <<'PY'
import json
import os
import sys

status = sys.argv[1]
timestamp = sys.argv[2]
count = int(sys.argv[3])
names = os.environ["OMNISIGHT_SMOKE_CHECK_NAMES"].split("\x1f") if count else []
statuses = os.environ["OMNISIGHT_SMOKE_CHECK_STATUSES"].split("\x1f") if count else []
details = os.environ["OMNISIGHT_SMOKE_CHECK_DETAILS"].split("\x1f") if count else []
failed = os.environ["OMNISIGHT_SMOKE_FAILED_CHECKS"].split("\x1f")
if failed == [""]:
    failed = []

print(json.dumps({
    "timestamp": timestamp,
    "status": status,
    "failed_checks": failed,
    "checks": {
        name: {"status": check_status, "detail": detail[-500:]}
        for name, check_status, detail in zip(names, statuses, details)
    },
}, sort_keys=True))
PY
	mkdir -p "$(dirname "$LOG_PATH")"
	cat "$tmp" >>"$LOG_PATH"
	rm -f "$tmp"
}

_notify_failure() {
	local payload
	payload="$("$PYTHON_BIN" - <<'PY'
import json
import os

failed = os.environ["OMNISIGHT_SMOKE_FAILED_CHECKS"].split("\x1f")
if failed == [""]:
    failed = []
print(json.dumps({
    "failed_checks": failed,
    "log_path": os.environ["OMNISIGHT_SMOKE_LOG"],
    "base_url": os.environ["OMNISIGHT_SMOKE_BASE_URL"],
}, sort_keys=True))
PY
)"
	if [[ -n "${OMNISIGHT_SMOKE_NOTIFY_CMD:-}" ]]; then
		OMNISIGHT_SMOKE_FAILURE_JSON="$payload" bash -c "$OMNISIGHT_SMOKE_NOTIFY_CMD"
		return
	fi
	PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
	OMNISIGHT_SMOKE_FAILURE_JSON="$payload" "$PYTHON_BIN" - <<'PY'
import json
import os

from backend.agents.operator_notifier import Severity, get_default_notifier

context = json.loads(os.environ["OMNISIGHT_SMOKE_FAILURE_JSON"])
notifier = get_default_notifier()
notifier.notify(
    Severity.CRITICAL,
    "prod_smoke_test_failed",
    "Production post-reboot smoke test failed: " + ", ".join(context["failed_checks"]),
    context=context,
    scope="prod-smoke-test",
    root_cause_key="post-reboot-smoke-test",
)
notifier.flush_all()
PY
}

export OMNISIGHT_SMOKE_BASE_URL="$BASE_URL"
export OMNISIGHT_SMOKE_LOG="$LOG_PATH"

_run_check "livez" "OMNISIGHT_SMOKE_LIVEZ_CMD" \
	"\"$CURL_BIN\" -fsS -o /dev/null \"$BASE_URL/livez\"" || true
_run_check "readyz" "OMNISIGHT_SMOKE_READYZ_CMD" \
	"\"$CURL_BIN\" -fsS -o /dev/null \"$BASE_URL/readyz\"" || true
_run_check "db_alembic_version" "OMNISIGHT_SMOKE_DB_CMD" "_db_check_cmd" || true
_run_check "jira_reachability" "OMNISIGHT_SMOKE_JIRA_CMD" "_jira_check_cmd" || true
_run_check "gerrit_ssh" "OMNISIGHT_SMOKE_GERRIT_CMD" "_gerrit_check_cmd" || true
_run_check "runner_activity" "OMNISIGHT_SMOKE_RUNNER_ACTIVITY_CMD" "_runner_activity_check_cmd" || true

export OMNISIGHT_SMOKE_CHECK_NAMES
export OMNISIGHT_SMOKE_CHECK_STATUSES
export OMNISIGHT_SMOKE_CHECK_DETAILS
export OMNISIGHT_SMOKE_FAILED_CHECKS
OMNISIGHT_SMOKE_CHECK_NAMES="$(printf '%s\x1f' "${CHECK_NAMES[@]}")"
OMNISIGHT_SMOKE_CHECK_NAMES="${OMNISIGHT_SMOKE_CHECK_NAMES%$'\x1f'}"
OMNISIGHT_SMOKE_CHECK_STATUSES="$(printf '%s\x1f' "${CHECK_STATUSES[@]}")"
OMNISIGHT_SMOKE_CHECK_STATUSES="${OMNISIGHT_SMOKE_CHECK_STATUSES%$'\x1f'}"
OMNISIGHT_SMOKE_CHECK_DETAILS="$(printf '%s\x1f' "${CHECK_DETAILS[@]}")"
OMNISIGHT_SMOKE_CHECK_DETAILS="${OMNISIGHT_SMOKE_CHECK_DETAILS%$'\x1f'}"
OMNISIGHT_SMOKE_FAILED_CHECKS="$(printf '%s\x1f' "${FAILED_CHECKS[@]}")"
OMNISIGHT_SMOKE_FAILED_CHECKS="${OMNISIGHT_SMOKE_FAILED_CHECKS%$'\x1f'}"

if [[ "${#FAILED_CHECKS[@]}" -eq 0 ]]; then
	_append_row "pass"
	echo "omnisight smoke test passed"
	exit 0
fi

_append_row "fail"
_notify_failure
echo "omnisight smoke test failed: ${FAILED_CHECKS[*]}" >&2
exit 1
