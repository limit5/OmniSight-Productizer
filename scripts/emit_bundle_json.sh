#!/usr/bin/env bash
# OP-1513 / OP-1478-act-T1.3 — emit a real bundle.json for the GitLab CI
# build pipeline. The Dockerfiles (backend/frontend/bridge) accept
# BUNDLE_ID / BUNDLE_SHA build args and COPY bundle.json into /app so
# `/api/version` and image labels match what CI sealed.
#
# Inputs (env vars; all required unless noted):
#   GIT_SHA                 40-char lowercase commit SHA
#   GIT_REF                 full git ref, e.g. refs/tags/v0.5.0-rc6
#   IMAGE_DIGEST_BACKEND    sha256:<64 hex>
#   IMAGE_DIGEST_FRONTEND   sha256:<64 hex>
#   IMAGE_DIGEST_BRIDGE     sha256:<64 hex>
#   API_REQUIRED            minimum API version backend negotiates with (e.g. v1)
#   OPENAPI_HASH            sha256 hex of committed openapi.json (64 hex)
#   DB_MIGRATION_HEAD       single alembic head baked into the backend image
#
# Optional env vars (sensible defaults):
#   API_SUPPORTED               comma-separated list (default "v1,v2")
#   FRONTEND_BUILT_AGAINST_API  API version frontend built against (default "v1")
#   BUILD_TIME                  RFC 3339 / ISO 8601 UTC timestamp (default: now)
#   BUNDLE_ID                   override (default derived per AC format)
#
# Output: writes a single bundle.json document to stdout.
#
# bundle_id format (per OP-1513 AC):
#   <git_ref_short>+<YYYYMMDD>.sha<short_sha>
#   e.g. v0.5.0-rc6+20260520.sha7a3b4c
set -euo pipefail

die() {
    printf 'emit_bundle_json: %s\n' "$*" >&2
    exit 1
}

require() {
    local name="$1"
    if [[ -z "${!name:-}" ]]; then
        die "missing required env var: ${name}"
    fi
}

require GIT_SHA
require GIT_REF
require IMAGE_DIGEST_BACKEND
require IMAGE_DIGEST_FRONTEND
require IMAGE_DIGEST_BRIDGE
require API_REQUIRED
require OPENAPI_HASH
require DB_MIGRATION_HEAD

export API_SUPPORTED="${API_SUPPORTED:-v1,v2}"
export FRONTEND_BUILT_AGAINST_API="${FRONTEND_BUILT_AGAINST_API:-v1}"
export BUILD_TIME="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
export BUNDLE_ID="${BUNDLE_ID:-}"

python3 <<'PY'
import json
import os
import re
import sys

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
OPENAPI_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def die(msg: str) -> None:
    print(f"emit_bundle_json: {msg}", file=sys.stderr)
    sys.exit(1)


git_sha = os.environ["GIT_SHA"]
git_ref = os.environ["GIT_REF"]
backend_digest = os.environ["IMAGE_DIGEST_BACKEND"]
frontend_digest = os.environ["IMAGE_DIGEST_FRONTEND"]
bridge_digest = os.environ["IMAGE_DIGEST_BRIDGE"]
api_required = os.environ["API_REQUIRED"]
openapi_hash = os.environ["OPENAPI_HASH"]
db_migration_head = os.environ["DB_MIGRATION_HEAD"]
api_supported_raw = os.environ["API_SUPPORTED"]
frontend_built_against_api = os.environ["FRONTEND_BUILT_AGAINST_API"]
build_time = os.environ["BUILD_TIME"]
bundle_id_override = os.environ.get("BUNDLE_ID") or ""

if not SHA40_RE.match(git_sha):
    die(f"GIT_SHA must be a 40-char lowercase hex SHA (got: {git_sha!r})")
for name, value in (
    ("IMAGE_DIGEST_BACKEND", backend_digest),
    ("IMAGE_DIGEST_FRONTEND", frontend_digest),
    ("IMAGE_DIGEST_BRIDGE", bridge_digest),
):
    if not DIGEST_RE.match(value):
        die(f"{name} must look like 'sha256:<64 hex chars>' (got: {value!r})")
if not OPENAPI_HASH_RE.match(openapi_hash):
    die(f"OPENAPI_HASH must be a 64-char lowercase hex sha256 (got: {openapi_hash!r})")

# Ref tail: strip refs/tags/ or refs/heads/ prefix.
git_ref_short = git_ref.rsplit("/", 1)[-1] if "/" in git_ref else git_ref
short_sha = git_sha[:7]
# build_time is RFC 3339 'YYYY-MM-DDTHH:MM:SSZ'; first 10 chars = date.
build_date_compact = build_time[:10].replace("-", "")

bundle_id = bundle_id_override or f"{git_ref_short}+{build_date_compact}.sha{short_sha}"

api_supported = [v.strip() for v in api_supported_raw.split(",") if v.strip()]
if not api_supported:
    die("API_SUPPORTED must be a non-empty comma-separated list")

bundle = {
    "bundle_id": bundle_id,
    "git_ref": git_ref,
    "git_sha": git_sha,
    "build_time": build_time,
    "images": {
        "backend":  {"digest": backend_digest},
        "frontend": {"digest": frontend_digest},
        "bridge":   {"digest": bridge_digest},
    },
    "contracts": {
        "api_required": api_required,
        "api_supported": api_supported,
        "openapi_hash": openapi_hash,
        "db_migration_head": db_migration_head,
        "frontend_built_against_api": frontend_built_against_api,
    },
    "signatures": [],
}
print(json.dumps(bundle, sort_keys=True, indent=2))
PY
