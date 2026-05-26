#!/usr/bin/env bash
# OP-1739 — release_preflight.sh — make the implicit prod-deploy
# prerequisites EXPLICIT and print the exact next command.
#
# WHAT THIS IS: a READ-ONLY preflight + guide. It ASSERTS the ~10
# prerequisites that today live as tribal knowledge ("cosign must be on
# PATH", "the backup passphrase must be exported", "the prod .env tag must
# be tag-shaped, not a digest", "the promote bundle digests must match the
# candidate", "the staging gate must be green"), reports PASS/FAIL for each
# in a single checklist, and — only when everything passes — prints the
# exact `scripts/deploy-prod.sh` command to run next, with the validated
# backend/frontend digests already filled in from the promote bundle.
#
# WHAT THIS IS NOT (per OP-1739 MUST NOT): an orchestrator. It NEVER
# executes promote/deploy/backup, never pushes to prod, never mutates a
# running service, and never builds or wires the (unmounted) path-B router.
# It chains the EXISTING verified scripts read-only and reports — that is
# the whole job. The operator runs the printed next command themselves.
#
# Usage:
#   scripts/release_preflight.sh [--candidate <tag>] [--bundle <path>] \
#                                [--env-file <path>] [--compose <path>]
#
# Exit codes:
#   0  — every prerequisite PASSED; the next command is printed.
#   1  — at least one prerequisite FAILED (each is still reported, never
#        silently skipped); the next command is withheld until it is fixed.
#   2  — bad invocation.
#
# Configuration (flags override env vars override defaults):
#   --candidate <tag>  OMNISIGHT_CANDIDATE_TAG    candidate release tag the
#                      promote bundle must match (default: OMNISIGHT_IMAGE_TAG
#                      from the prod .env).
#   --bundle <path>    OMNISIGHT_PROMOTE_BUNDLE   promote bundle JSON built by
#                      scripts/build_promote_bundle.py (default: the newest
#                      artifacts/bundle-*.json).
#   --env-file <path>  OMNISIGHT_PROD_ENV_FILE    prod .env (default: <repo>/.env).
#   --compose <path>   OMNISIGHT_PREFLIGHT_COMPOSE_FILE  (default:
#                      <repo>/docker-compose.prod.yml).
#
# Test/operator seams (point the chained tools elsewhere without editing):
#   OMNISIGHT_BACKUP_DR_ENV            backup-dr.env path (default
#                                      /etc/omnisight/backup-dr.env).
#   OMNISIGHT_PREFLIGHT_DOCKER         docker binary (default `docker`).
#   OMNISIGHT_PREFLIGHT_GITLAB_CR_CMD  GitLab CR check (default
#                                      <scriptdir>/verify_gitlab_cr.sh).
#   OMNISIGHT_PREFLIGHT_STAGING_GATE_CMD  staging gate (default
#                                      `python3 <scriptdir>/staging_gate.py --suite canary`).
#   COSIGN_BIN                         explicit cosign path (OP-1736 contract).
#
# Why no `set -e`: this is a reporting tool — a single failing check must
# NOT abort the run, or later prerequisites would be silently skipped (the
# exact failure mode OP-1739 calls out). We run EVERY check, tally, then
# decide the exit code. Mirrors verify_gitlab_cr.sh's `set -uo pipefail`.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Colour (TTY only; tests capture, so stay plain there) ─────────────
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; NC=''
fi

# ── Config (flags > env > defaults) ───────────────────────────────────
CANDIDATE_TAG="${OMNISIGHT_CANDIDATE_TAG:-}"
BUNDLE_FILE="${OMNISIGHT_PROMOTE_BUNDLE:-}"
PROD_ENV_FILE="${OMNISIGHT_PROD_ENV_FILE:-${REPO_ROOT}/.env}"
COMPOSE_FILE="${OMNISIGHT_PREFLIGHT_COMPOSE_FILE:-${REPO_ROOT}/docker-compose.prod.yml}"
BACKUP_DR_ENV="${OMNISIGHT_BACKUP_DR_ENV:-/etc/omnisight/backup-dr.env}"
DOCKER_BIN="${OMNISIGHT_PREFLIGHT_DOCKER:-docker}"

# Chained-tool commands as arrays so test seams can substitute a stub.
read -r -a GITLAB_CR_CMD <<<"${OMNISIGHT_PREFLIGHT_GITLAB_CR_CMD:-bash ${SCRIPT_DIR}/verify_gitlab_cr.sh}"
read -r -a STAGING_GATE_CMD <<<"${OMNISIGHT_PREFLIGHT_STAGING_GATE_CMD:-python3 ${SCRIPT_DIR}/staging_gate.py --suite canary}"

usage() {
    sed -n '2,49p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --candidate) CANDIDATE_TAG="${2:-}"; shift 2 ;;
        --candidate=*) CANDIDATE_TAG="${1#--candidate=}"; shift ;;
        --bundle) BUNDLE_FILE="${2:-}"; shift 2 ;;
        --bundle=*) BUNDLE_FILE="${1#--bundle=}"; shift ;;
        --env-file) PROD_ENV_FILE="${2:-}"; shift 2 ;;
        --env-file=*) PROD_ENV_FILE="${1#--env-file=}"; shift ;;
        --compose) COMPOSE_FILE="${2:-}"; shift 2 ;;
        --compose=*) COMPOSE_FILE="${1#--compose=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "release_preflight: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# Image-digest shape, reused for the bundle digest check (matches
# scripts/check_deploy_ref.sh's DIGEST_RE — sha256 + 64 lowercase hex).
DIGEST_RE='^sha256:[0-9a-f]{64}$'

# ── Checklist accounting ──────────────────────────────────────────────
# Each result is "STATUS<TAB>label<TAB>detail", where detail is optional
# remediation context (newline-joined) rendered indented under the line —
# buffered, not echoed inline, so the checklist stays a clean block.
PASS_COUNT=0
FAIL_COUNT=0
declare -a RESULTS=()

pass() { PASS_COUNT=$((PASS_COUNT + 1)); RESULTS+=("PASS"$'\t'"$1"$'\t'"${2:-}"); }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); RESULTS+=("FAIL"$'\t'"$1"$'\t'"${2:-}"); }

# Resolved during the bundle check, used to build the next command.
BUNDLE_BACKEND_DIGEST=""
BUNDLE_FRONTEND_DIGEST=""

# Read a KEY=value from a dotenv-style file (last assignment wins).
env_get() {
    local key="$1" file="$2"
    [ -r "$file" ] || return 0
    grep -E "^${key}=" "$file" 2>/dev/null | tail -1 | cut -d= -f2-
}

# ── Check 1: cosign resolvable (OP-1736 self-location contract) ───────
# Mirrors scripts/verify_image_signature.sh: honour $COSIGN_BIN, else PATH,
# else probe the common install dirs. This is the prerequisite probe — the
# actual signature verification is chained via verify_image_bundle.py at
# promote time; here we only assert the operator won't hit "cosign: command
# not found" mid-cut.
check_cosign() {
    local dirs=("${HOME:-}/bin" "/usr/local/bin" "${HOME:-}/go/bin")
    if [ -n "${COSIGN_BIN:-}" ]; then
        if [ -x "$COSIGN_BIN" ]; then
            pass "cosign resolvable (\$COSIGN_BIN=${COSIGN_BIN})"
        else
            fail "cosign: \$COSIGN_BIN=${COSIGN_BIN} is set but not executable"
        fi
        return
    fi
    if command -v cosign >/dev/null 2>&1; then
        pass "cosign resolvable on PATH ($(command -v cosign))"
        return
    fi
    local d
    for d in "${dirs[@]}"; do
        if [ -x "${d}/cosign" ]; then
            pass "cosign resolvable in ${d} (not on PATH; prepend before deploy)"
            return
        fi
    done
    fail "cosign not found on PATH or in ${dirs[*]} — set COSIGN_BIN (https://docs.sigstore.dev/cosign/installation/)"
}

# ── Check 2: backup passphrase available (env or backup-dr.env) ───────
# deploy-prod.sh Step 1b takes an AES-256 encrypted pre-deploy backup that
# is unprotected without OMNISIGHT_BACKUP_PASSPHRASE.
check_backup_passphrase() {
    if [ -n "${OMNISIGHT_BACKUP_PASSPHRASE:-}" ]; then
        pass "OMNISIGHT_BACKUP_PASSPHRASE present in environment"
        return
    fi
    if [ -r "$BACKUP_DR_ENV" ] && grep -qE '^OMNISIGHT_BACKUP_PASSPHRASE=.+' "$BACKUP_DR_ENV"; then
        pass "OMNISIGHT_BACKUP_PASSPHRASE available via ${BACKUP_DR_ENV}"
        return
    fi
    fail "OMNISIGHT_BACKUP_PASSPHRASE not in env and not set in ${BACKUP_DR_ENV} — the encrypted pre-deploy backup (deploy-prod.sh Step 1b) would be unprotected"
}

# ── Check 3: GitLab CR login present (chain verify_gitlab_cr.sh) ──────
check_gitlab_cr() {
    local out rc=0
    out="$("${GITLAB_CR_CMD[@]}" 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "GitLab CR reachable + fresh (${GITLAB_CR_CMD[0]##*/})"
    else
        fail "GitLab CR check failed (exit ${rc}) — registry login/token or freshness problem" "${out##*$'\n'}"
    fi
}

# ── Check 4: prod .env exists + readable ──────────────────────────────
check_env_file() {
    if [ -r "$PROD_ENV_FILE" ]; then
        pass "prod env file present (${PROD_ENV_FILE})"
    else
        fail "prod env file missing/unreadable: ${PROD_ENV_FILE}"
    fi
}

# ── Check 5: OMNISIGHT_REGISTRY set (compose + bundle need it) ────────
check_registry() {
    local reg
    reg="$(env_get OMNISIGHT_REGISTRY "$PROD_ENV_FILE")"
    if [ -n "$reg" ]; then
        pass "OMNISIGHT_REGISTRY set (${reg})"
    else
        fail "OMNISIGHT_REGISTRY unset in ${PROD_ENV_FILE} — docker compose config will fail-closed (\${OMNISIGHT_REGISTRY:?registry required})"
    fi
}

# ── Check 6: OMNISIGHT_IMAGE_TAG is tag-shaped, not a digest ──────────
# A digest-shaped value here would trip the ${OMNISIGHT_IMAGE_TAG} tag path
# in docker-compose.prod.yml (the tag path expects e.g. v0.6.2, never
# sha256:...). Digest pinning is done via OMNISIGHT_{BACKEND,FRONTEND}_IMAGE_REF.
check_image_tag_shape() {
    local tag
    tag="$(env_get OMNISIGHT_IMAGE_TAG "$PROD_ENV_FILE")"
    if [ -z "$tag" ]; then
        fail "OMNISIGHT_IMAGE_TAG unset in ${PROD_ENV_FILE} — the tag path is fail-closed (\${OMNISIGHT_IMAGE_TAG:?image tag required})"
        return
    fi
    case "$tag" in
        sha256:*|*@sha256:*)
            fail "OMNISIGHT_IMAGE_TAG='${tag}' is digest-shaped — the tag path expects a tag (e.g. v0.6.2); pin digests via OMNISIGHT_{BACKEND,FRONTEND}_IMAGE_REF instead"
            ;;
        *)
            pass "OMNISIGHT_IMAGE_TAG is tag-shaped (${tag})"
            ;;
    esac
}

# ── Check 7: docker compose config resolves ───────────────────────────
check_compose_config() {
    if ! command -v "$DOCKER_BIN" >/dev/null 2>&1; then
        fail "docker binary '${DOCKER_BIN}' not found — cannot validate ${COMPOSE_FILE} interpolation"
        return
    fi
    local out rc=0
    out="$("$DOCKER_BIN" compose --env-file "$PROD_ENV_FILE" -f "$COMPOSE_FILE" config 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "docker compose config resolves (${COMPOSE_FILE##*/})"
    else
        fail "docker compose config FAILED for ${COMPOSE_FILE} (exit ${rc}) — unresolved env var or invalid override" "${out##*$'\n'}"
    fi
}

# ── Check 8 + 9: promote bundle digests well-formed + match candidate ─
check_promote_bundle() {
    # Default to the newest artifacts/bundle-*.json when not specified.
    if [ -z "$BUNDLE_FILE" ]; then
        BUNDLE_FILE="$(ls -1t "${REPO_ROOT}"/artifacts/bundle-*.json 2>/dev/null | head -1 || true)"
    fi
    if [ -z "$BUNDLE_FILE" ] || [ ! -r "$BUNDLE_FILE" ]; then
        fail "promote bundle not found/readable (${BUNDLE_FILE:-<none>}) — build it: scripts/build_promote_bundle.py --candidate-tag <tag> --out artifacts/bundle-<tag>.json"
        fail "promote bundle ↔ candidate match — cannot check without a bundle"
        return
    fi

    # Extract backend/frontend (digest, tag) — python3 for robust JSON.
    local parsed
    parsed="$(python3 - "$BUNDLE_FILE" <<'PY' 2>/dev/null
import json, sys
try:
    b = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"__ERR__\t{exc}")
    sys.exit(0)
imgs = b.get("images", {})
for name in ("backend", "frontend"):
    e = imgs.get(name, {}) or {}
    print(f"{name}\t{e.get('digest','')}\t{e.get('tag','')}")
PY
)"
    if [ -z "$parsed" ] || printf '%s' "$parsed" | grep -q '^__ERR__'; then
        fail "promote bundle ${BUNDLE_FILE} is not valid JSON / unreadable"
        fail "promote bundle ↔ candidate match — cannot check (bundle unparseable)"
        return
    fi

    # Candidate to match against: explicit, else the prod .env tag.
    local candidate="$CANDIDATE_TAG"
    [ -z "$candidate" ] && candidate="$(env_get OMNISIGHT_IMAGE_TAG "$PROD_ENV_FILE")"

    local digests_ok=1 match_ok=1 match_checked=0
    local name digest tag digest_detail="" match_detail=""
    while IFS=$'\t' read -r name digest tag; do
        [ -z "$name" ] && continue
        if [[ "$digest" =~ $DIGEST_RE ]]; then
            case "$name" in
                backend) BUNDLE_BACKEND_DIGEST="$digest" ;;
                frontend) BUNDLE_FRONTEND_DIGEST="$digest" ;;
            esac
        else
            digests_ok=0
            digest_detail+="${digest_detail:+$'\n'}${name}: digest '${digest:-<missing>}' is not sha256:<64 hex>"
        fi
        if [ -n "$candidate" ]; then
            match_checked=1
            if [ "$tag" != "$candidate" ]; then
                match_ok=0
                match_detail+="${match_detail:+$'\n'}${name}: bundle tag '${tag:-<none>}' != candidate '${candidate}'"
            fi
        fi
    done <<<"$parsed"

    if [ "$digests_ok" -eq 1 ] && [ -n "$BUNDLE_BACKEND_DIGEST" ] && [ -n "$BUNDLE_FRONTEND_DIGEST" ]; then
        pass "promote bundle digests well-formed (backend + frontend)"
    else
        fail "promote bundle digests malformed/incomplete in ${BUNDLE_FILE##*/}" "$digest_detail"
    fi

    if [ "$match_checked" -eq 0 ]; then
        fail "promote bundle ↔ candidate match — no candidate tag (pass --candidate or set OMNISIGHT_IMAGE_TAG)"
    elif [ "$match_ok" -eq 1 ]; then
        pass "promote bundle matches candidate '${candidate}'"
    else
        fail "promote bundle does NOT match candidate '${candidate}'" "$match_detail"
    fi
}

# ── Check 10: staging gate evidence green (chain staging_gate.py) ─────
check_staging_gate() {
    local out rc=0
    out="$("${STAGING_GATE_CMD[@]}" 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        pass "staging gate evidence GREEN (${STAGING_GATE_CMD[*]##*/})"
    else
        fail "staging gate NOT green (exit ${rc}) — do not cut until staging is green" "${out##*$'\n'}"
    fi
}

# ── Run every check (order = operator's mental model of a cut) ────────
echo -e "\n${CYAN}${BOLD}━━━ OmniSight release preflight (OP-1739) — READ-ONLY ━━━${NC}\n"
echo "Prod env:  ${PROD_ENV_FILE}"
echo "Compose:   ${COMPOSE_FILE}"
echo "Bundle:    ${BUNDLE_FILE:-<auto-detect artifacts/bundle-*.json>}"
echo "Candidate: ${CANDIDATE_TAG:-<from OMNISIGHT_IMAGE_TAG>}"
echo ""

check_cosign
check_backup_passphrase
check_gitlab_cr
check_env_file
check_registry
check_image_tag_shape
check_compose_config
check_promote_bundle
check_staging_gate

# ── Checklist ─────────────────────────────────────────────────────────
echo -e "${BOLD}Preflight checklist:${NC}"
for r in "${RESULTS[@]}"; do
    status="${r%%$'\t'*}"
    rest="${r#*$'\t'}"
    label="${rest%%$'\t'*}"
    detail="${rest#*$'\t'}"
    [ "$detail" = "$rest" ] && detail=""   # no detail field present
    if [ "$status" = "PASS" ]; then
        echo -e "  ${GREEN}✅ PASS${NC}  ${label}"
    else
        echo -e "  ${RED}❌ FAIL${NC}  ${label}"
    fi
    if [ -n "$detail" ]; then
        while IFS= read -r dline; do
            [ -n "$dline" ] && echo "          ↳ ${dline}"
        done <<<"$detail"
    fi
done
echo ""
echo -e "  ${BOLD}${PASS_COUNT} passed, ${FAIL_COUNT} failed${NC} (of $((PASS_COUNT + FAIL_COUNT)) checks)"
echo ""

# ── Next command (only when clean) ────────────────────────────────────
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo -e "${GREEN}${BOLD}━━━ All prerequisites PASSED — next command ━━━${NC}\n"
    echo "  # 1) dry-run first (prints steps, executes nothing):"
    echo "  scripts/deploy-prod.sh --backend-digest=${BUNDLE_BACKEND_DIGEST} \\"
    echo "                         --frontend-digest=${BUNDLE_FRONTEND_DIGEST} --dry-run"
    echo ""
    echo "  # 2) then the real zero-downtime deploy:"
    echo "  scripts/deploy-prod.sh --backend-digest=${BUNDLE_BACKEND_DIGEST} \\"
    echo "                         --frontend-digest=${BUNDLE_FRONTEND_DIGEST}"
    echo ""
    echo -e "  ${YELLOW}This preflight executed NOTHING. Run the command above yourself.${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}━━━ ${FAIL_COUNT} prerequisite(s) FAILED — not ready to cut ━━━${NC}\n"
    echo "  Fix the ❌ items above and re-run this preflight."
    echo "  The deploy command is withheld until every prerequisite passes."
    exit 1
fi
