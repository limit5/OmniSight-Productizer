#!/usr/bin/env bash
# [OP-939] Daily release conductor cron skeleton.
#
# Enumerates new SemVer JIRA fixVersions, runs the OP-868 milestone
# acceptance check read-only, and invokes G1 only when the release is
# ready and no RELEASE-vX.Y.Z META exists yet.

set -euo pipefail

REPO_ROOT="${RELEASE_CONDUCTOR_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DIR="${RELEASE_CONDUCTOR_STATE_DIR:-${REPO_ROOT}/logs/release-conductor/state}"
AUDIT_LOG="${RELEASE_CONDUCTOR_AUDIT_LOG:-${REPO_ROOT}/logs/release-conductor/audit.jsonl}"
AGENT_CLASS="${OMNISIGHT_JIRA_AGENT_CLASS:-subscription-codex}"
SEMVER_RE='^v[0-9]+\.[0-9]+\.[0-9]+(-((rc|beta|alpha)[0-9]+))?$'
MAX_CONSECUTIVE_FAILURES=3

mkdir -p "$STATE_DIR" "$(dirname "$AUDIT_LOG")"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

version_key() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'
}

counter_file() {
  printf '%s/%s.failures' "$STATE_DIR" "$(version_key "$1")"
}

read_counter() {
  local path
  path="$(counter_file "$1")"
  if [[ -f "$path" ]]; then
    tr -dc '0-9' < "$path"
  else
    printf '0'
  fi
}

write_counter() {
  printf '%s\n' "$2" > "$(counter_file "$1")"
}

reset_counter() {
  rm -f "$(counter_file "$1")"
}

append_audit() {
  local event="$1"
  local version="$2"
  local detail="${3:-}"
  python3 - "$AUDIT_LOG" "$event" "$version" "$detail" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
row = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "event": sys.argv[2],
    "fixVersion": sys.argv[3],
}
if sys.argv[4]:
    row["detail"] = sys.argv[4]
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
PY
}

notify_operator() {
  local event="$1"
  local version="$2"
  local detail="${3:-}"
  append_audit "$event" "$version" "$detail"
  if [[ -n "${RELEASE_CONDUCTOR_NOTIFY_CMD:-}" ]]; then
    RELEASE_CONDUCTOR_EVENT="$event" \
      RELEASE_CONDUCTOR_VERSION="$version" \
      RELEASE_CONDUCTOR_DETAIL="$detail" \
      bash -c "$RELEASE_CONDUCTOR_NOTIFY_CMD"
  else
    log "operator notification pending G6: event=${event} version=${version} detail=${detail}"
  fi
}

query_fix_versions() {
  if [[ -n "${RELEASE_CONDUCTOR_FIXVERSIONS_CMD:-}" ]]; then
    bash -c "$RELEASE_CONDUCTOR_FIXVERSIONS_CMD"
    return
  fi

  PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$AGENT_CLASS" <<'PY'
import re
import sys

from backend.agents import jira_dispatch

agent_class = sys.argv[1]
client = jira_dispatch.make_client(agent_class)
jql = (
    f'project = "{client.project_key}" '
    'AND fixVersion is not EMPTY '
    'AND created >= -7d '
    'ORDER BY created DESC'
)
payload = jira_dispatch._request(
    client,
    "POST",
    "/search/jql",
    {"jql": jql, "fields": ["fixVersions"], "maxResults": 500},
)
seen = set()
semver = re.compile(r"^v\d+\.\d+\.\d+(?:-(?:rc|beta|alpha)\d+)?$")
for issue in payload.get("issues", []):
    fields = issue.get("fields") or {}
    for row in fields.get("fixVersions") or []:
        name = str(row.get("name") or "")
        if semver.match(name) and name not in seen:
            seen.add(name)
            print(name)
PY
}

meta_exists() {
  local version="$1"
  if [[ -n "${RELEASE_CONDUCTOR_META_EXISTS_CMD:-}" ]]; then
    RELEASE_CONDUCTOR_VERSION="$version" bash -c "$RELEASE_CONDUCTOR_META_EXISTS_CMD"
    return
  fi

  PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$AGENT_CLASS" "$version" <<'PY'
import sys

from backend.agents import jira_dispatch
from scripts.instantiate_release_meta import find_existing_meta_key

try:
    client = jira_dispatch.make_client(sys.argv[1])
    key, status = find_existing_meta_key(client, sys.argv[2])
except Exception as exc:
    print(f"MetaLookupFailed: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc
raise SystemExit(0 if key and status else 1)
PY
}

acceptance_green() {
  local version="$1"
  if [[ -n "${RELEASE_CONDUCTOR_ACCEPTANCE_CMD:-}" ]]; then
    RELEASE_CONDUCTOR_VERSION="$version" bash -c "$RELEASE_CONDUCTOR_ACCEPTANCE_CMD"
    return
  fi

  PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$AGENT_CLASS" "$version" "$AUDIT_LOG" <<'PY'
import sys
from pathlib import Path

from backend.agents import jira_dispatch
from scripts.milestone_check import (
    DispatchMilestoneJiraClient,
    SshMilestoneGerritClient,
    check_one,
)

client = jira_dispatch.make_client(sys.argv[1])
report = check_one(
    sys.argv[2],
    jira=DispatchMilestoneJiraClient(client),
    gerrit=SshMilestoneGerritClient.from_env(),
    audit_log=Path(sys.argv[3]),
)
raise SystemExit(0 if report.get("ready") is True else 1)
PY
}

instantiate_meta() {
  local version="$1"
  if [[ -n "${RELEASE_CONDUCTOR_INSTANTIATE_CMD:-}" ]]; then
    RELEASE_CONDUCTOR_VERSION="$version" bash -c "$RELEASE_CONDUCTOR_INSTANTIATE_CMD"
    return
  fi

  python3 "$REPO_ROOT/scripts/instantiate_release_meta.py" --version "$version"
}

handle_version() {
  local version="$1"
  local failures
  failures="$(read_counter "$version")"

  if (( failures >= MAX_CONSECUTIVE_FAILURES )); then
    notify_operator "Backoff3Consecutive" "$version" "suppressed after ${failures} consecutive acceptance failures"
    return 0
  fi

  if meta_exists "$version"; then
    append_audit "ExistingMetaSuppress" "$version" "RELEASE-${version} META already exists"
    reset_counter "$version"
    log "skip ${version}: RELEASE META already exists"
    return 0
  elif [[ $? -ne 1 ]]; then
    append_audit "MetaLookupFailed" "$version" "existing META lookup failed; retry tomorrow"
    log "skip ${version}: existing META lookup failed"
    return 0
  fi

  if ! acceptance_green "$version"; then
    failures=$((failures + 1))
    write_counter "$version" "$failures"
    append_audit "MilestoneAcceptanceCheckFailed" "$version" "consecutive_failures=${failures}"
    log "skip ${version}: milestone acceptance not green (${failures}/${MAX_CONSECUTIVE_FAILURES})"
    if (( failures >= MAX_CONSECUTIVE_FAILURES )); then
      notify_operator "Backoff3Consecutive" "$version" "suppressed after ${failures} consecutive acceptance failures"
    fi
    return 0
  fi

  reset_counter "$version"
  if instantiate_meta "$version"; then
    append_audit "ReleaseMetaInstantiated" "$version" "G1 instantiate_release_meta.py completed"
    notify_operator "ReleaseMetaInstantiated" "$version" "RELEASE-${version} META instantiated"
    return 0
  fi

  append_audit "InstantiateMetaFailed" "$version" "G1 instantiate_release_meta.py failed; retry tomorrow"
  log "instantiate failed for ${version}; retrying tomorrow"
  return 0
}

main() {
  local versions=()
  local version
  while IFS= read -r version; do
    [[ -n "$version" ]] || continue
    if [[ "$version" =~ $SEMVER_RE ]]; then
      versions+=("$version")
    else
      append_audit "FixVersionSkipped" "$version" "not a vX.Y.Z semver fixVersion"
    fi
  done < <(query_fix_versions)

  if (( ${#versions[@]} == 0 )); then
    append_audit "NoFixVersions" "" "no vX.Y.Z fixVersions created in last 7 days"
    log "no recent semver fixVersions"
    return 0
  fi

  for version in "${versions[@]}"; do
    handle_version "$version"
  done
}

main "$@"
