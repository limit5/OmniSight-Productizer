#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

fail_json() {
    local event="$1"
    local details="$2"
    python3 - "$event" "$details" <<'PY' >&2
import json
import sys

print(json.dumps({"event": sys.argv[1], "details": sys.argv[2]}, sort_keys=True))
PY
    exit 1
}

resolve_image_sha() {
    if [[ -n "${GITHUB_SHA:-}" ]]; then
        printf '%s\n' "${GITHUB_SHA}"
        return
    fi
    git rev-parse HEAD
}

resolve_git_ref() {
    if [[ -n "${GITHUB_REF_NAME:-}" ]]; then
        printf '%s\n' "${GITHUB_REF_NAME}"
        return
    fi
    git symbolic-ref HEAD
}

alembic_heads() {
    local alembic_config="${ALEMBIC_CONFIG:-alembic.ini}"
    cd "${repo_root}/backend"
    if command -v alembic >/dev/null 2>&1; then
        alembic -c "${alembic_config}" heads --resolve-dependencies
        return
    fi
    python3 -m alembic -c "${alembic_config}" heads --resolve-dependencies
}

image_sha="$(resolve_image_sha)"
git_ref="$(resolve_git_ref)"
build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
heads="$(alembic_heads | awk 'NF {print $1}')"
head_count="$(printf '%s\n' "${heads}" | awk 'NF {count++} END {print count + 0}')"

if [[ "${head_count}" != "1" ]]; then
    fail_json \
        "multi_head_remediation" \
        "Expected exactly one Alembic head in the image; create a merge revision before building. heads=$(printf '%s' "${heads}" | tr '\n' ',')"
fi

alembic_head_in_image="$(printf '%s\n' "${heads}" | awk 'NF {print; exit}')"

python3 - "$image_sha" "$build_time" "$git_ref" "$alembic_head_in_image" <<'PY'
import json
import sys

fields = {
    "image_sha": sys.argv[1],
    "build_time": sys.argv[2],
    "git_ref": sys.argv[3],
    "alembic_head_in_image": sys.argv[4],
}
print(json.dumps(fields, sort_keys=True))
PY
