#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

fail_json() {
    local event="$1"
    local details="$2"
    local code="${3:-1}"
    python3 - "$event" "$details" <<'PY' >&2
import json
import sys

print(json.dumps({"event": sys.argv[1], "details": sys.argv[2]}, sort_keys=True))
PY
    exit "${code}"
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

# Reserved fail-build exit codes (family5 §4.2 / family6 §9.2). These are the
# build-time enforcement points for the image-surfacing contract; do not
# repurpose 78/90/91/92.
GIT_REF_INVALID_EXIT=91   # git_ref is not a full 40-char hex SHA
MULTI_HEAD_EXIT=92        # alembic head count != 1 (zero or multi-head)

assert_git_ref_40() {
    # Fail the build (exit 91) unless $1 is exactly 40 hex chars, per the
    # git_ref invariant in family5 §4.2 (not a short hash, tag, or branch).
    local ref="$1"
    if [[ ! "${ref}" =~ ^[0-9a-fA-F]{40}$ ]]; then
        fail_json \
            "git_ref_not_40_char" \
            "git_ref must be a full 40-char hex SHA per family5 §4.2; got '${ref}' (len=${#ref})" \
            "${GIT_REF_INVALID_EXIT}"
    fi
}

assert_single_head() {
    # Fail the build (exit 92) unless the newline-separated head list in $1
    # contains exactly one head, per family6 §9 / ADR-0036. Multi-head must be
    # converged with `alembic merge` before the image can be built.
    local heads="$1"
    local count
    count="$(printf '%s\n' "${heads}" | awk 'NF {count++} END {print count + 0}')"
    if [[ "${count}" != "1" ]]; then
        fail_json \
            "multi_head_remediation" \
            "Expected exactly one Alembic head in the image; create a merge revision before building. heads=$(printf '%s' "${heads}" | tr '\n' ',')" \
            "${MULTI_HEAD_EXIT}"
    fi
}

# --check: dry/test mode for the fail-build guards (family5 §4 verification).
# Validates injected candidate values against the reserved exit codes WITHOUT
# resolving real git/alembic state or writing MANIFEST.json, so the exit-91 /
# exit-92 invariants are exercisable deterministically (see
# tests/test_bake_image_manifest_guards.py). git_ref is checked first, then the
# head count.
if [[ "${1:-}" == "--check" ]]; then
    assert_git_ref_40 "${BAKE_CHECK_GIT_REF-}"
    assert_single_head "${BAKE_CHECK_HEADS-}"
    printf '%s\n' '{"event": "guards_ok"}'
    exit 0
fi

image_sha="$(resolve_image_sha)"
git_ref="$(resolve_git_ref)"
build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
assert_git_ref_40 "${git_ref}"
heads="$(alembic_heads | awk 'NF {print $1}')"
assert_single_head "${heads}"

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
