#!/bin/bash
# OP-1136 / Sprint Boreas-C1 — Health check for the local bare mirror.
#
# Exits:
#   0 — mirror exists, was fetched within the freshness window
#   2 — mirror is stale (last fetch > MAX_AGE_SECONDS ago)
#   3 — mirror does not exist
#   4 — mirror exists but isn't a bare repo / is corrupt
#
# Output is single-line JSON for consumption by Boreas-A7 smoke-test
# (OP-1132) and the periodic timer's audit log.
#
# Usage:
#   bash scripts/runner-mirror/health.sh
#   OMNISIGHT_GIT_MIRROR_MAX_AGE_SECONDS=300 bash scripts/runner-mirror/health.sh

set -uo pipefail

MIRROR_DIR="${OMNISIGHT_GIT_MIRROR_DIR:-$HOME/git-mirror/omnisight.git}"
MAX_AGE_SECONDS="${OMNISIGHT_GIT_MIRROR_MAX_AGE_SECONDS:-300}"  # 5 min default

emit() {
    # $1 = status, $2 = exit_code, $3 = message (optional)
    local age_field=""
    if [[ -n "${last_fetch_age:-}" ]]; then
        age_field=", \"last_fetch_age_seconds\": $last_fetch_age"
    fi
    printf '{"status": "%s", "mirror_dir": "%s", "max_age_seconds": %d%s, "message": "%s"}\n' \
        "$1" "$MIRROR_DIR" "$MAX_AGE_SECONDS" "$age_field" "${3:-ok}"
    exit "$2"
}

[[ -d "$MIRROR_DIR" ]] || emit "missing" 3 "mirror dir does not exist; run scripts/runner-mirror/setup.sh"

if ! git -C "$MIRROR_DIR" rev-parse --is-bare-repository >/dev/null 2>&1; then
    emit "corrupt" 4 "mirror dir exists but is not a bare git repository"
fi

# Last fetch time approximated by FETCH_HEAD mtime
fetch_head="$MIRROR_DIR/FETCH_HEAD"
if [[ ! -f "$fetch_head" ]]; then
    emit "no_fetch_yet" 2 "mirror has never been fetched (no FETCH_HEAD)"
fi

last_fetch_epoch=$(stat -c %Y "$fetch_head" 2>/dev/null || stat -f %m "$fetch_head" 2>/dev/null)
now_epoch=$(date +%s)
last_fetch_age=$((now_epoch - last_fetch_epoch))

if (( last_fetch_age > MAX_AGE_SECONDS )); then
    emit "stale" 2 "last fetch was ${last_fetch_age}s ago (>${MAX_AGE_SECONDS}s threshold)"
fi

emit "ok" 0 "mirror fresh"
