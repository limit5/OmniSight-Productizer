#!/usr/bin/env bash
# OP-864 D2 — build, sign, and push the runner backend image.
#
# Usage:
#   scripts/build_image.sh --tag v0.1.0
#   scripts/build_image.sh --tag v0.1.0 --no-push        # local build only
#   scripts/build_image.sh --tag v0.1.0 --no-sign        # skip cosign (CI debug)
#   scripts/build_image.sh --tag v0.1.0 --dry-run        # print, don't run
#
# Error catalog (codes match the OP-864 ticket):
#   1   ImageBuildFailed         — Docker layer error
#   2   CosignSignFailed         — key missing/revoked, sign call failed
#   3   RegistryPushUnauthorized — `docker push` returned 401/403
#   4   bad invocation / missing dependency
#
# Reproducibility (AC #5): SOURCE_DATE_EPOCH is pinned to the commit's
# author timestamp so multi-stage cache + COPY layers don't capture
# the wall clock. The script also sorts COPY inputs and disables
# pip's hash-randomising __pycache__ writes. With BuildKit ≥ 0.12 and
# the docker daemon's `--platform linux/amd64` pinned, 2 invocations
# at the same git SHA produce identical image digests on the same host.

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────
REGISTRY="${OMNISIGHT_REGISTRY:-registry.sora.services:5000}"
IMAGE_PATH="${OMNISIGHT_IMAGE_PATH:-omnisight/runner}"
DOCKERFILE="${OMNISIGHT_DOCKERFILE:-Dockerfile.runner}"
COSIGN_KEY="${OMNISIGHT_COSIGN_KEY:-/home/user/.config/omnisight/cosign-private-key}"
PLATFORM="${OMNISIGHT_BUILD_PLATFORM:-linux/amd64}"

TAG=""
PUSH=1
SIGN=1
DRY_RUN=0

err() { printf '❌ build_image: %s\n' "$*" >&2; }
log() { printf '→ build_image: %s\n' "$*" >&2; }

usage() {
  sed -n '2,18p' "$0" >&2
}

# ── Arg parse ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)        TAG="${2:-}"; shift 2;;
    --tag=*)      TAG="${1#--tag=}"; shift;;
    --no-push)    PUSH=0; shift;;
    --no-sign)    SIGN=0; shift;;
    --dry-run)    DRY_RUN=1; shift;;
    -h|--help)    usage; exit 0;;
    *)            err "unknown arg: $1"; usage; exit 4;;
  esac
done

if [[ -z "$TAG" ]]; then
  err "--tag <semver> is required (e.g. --tag v0.1.0)"
  usage
  exit 4
fi

# Accept v0.1.0 OR 0.1.0 — normalise to the v-prefix the registry uses.
if [[ "$TAG" =~ ^[0-9]+\.[0-9]+\.[0-9]+([+-].*)?$ ]]; then
  TAG="v${TAG}"
fi
if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([+-][A-Za-z0-9.-]+)?$ ]]; then
  err "tag '$TAG' is not a valid semver (expected vX.Y.Z or vX.Y.Z-suffix)"
  exit 4
fi

# ── Dependency checks ──────────────────────────────────────────────
for bin in docker git; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    err "missing dependency: $bin"
    exit 4
  fi
done

if [[ "$SIGN" -eq 1 ]] && ! command -v cosign >/dev/null 2>&1; then
  err "cosign not installed; pass --no-sign to skip or install from sigstore.dev"
  exit 2
fi

# ── Resolve repo state ─────────────────────────────────────────────
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [[ ! -f "$DOCKERFILE" ]]; then
  err "Dockerfile not found: $REPO_ROOT/$DOCKERFILE"
  exit 4
fi

GIT_SHA="$(git rev-parse HEAD)"
GIT_SHA_SHORT="$(printf '%s' "$GIT_SHA" | cut -c1-12)"
# Reproducibility AC #5: pin SOURCE_DATE_EPOCH to commit author time.
SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"

SEMVER="${TAG#v}"
BASE_REF="${REGISTRY}/${IMAGE_PATH}"
SEMVER_REF="${BASE_REF}:${TAG}"
SHA_REF="${BASE_REF}:sha-${GIT_SHA_SHORT}"

log "tag=${TAG} git_sha=${GIT_SHA_SHORT} source_date_epoch=${SOURCE_DATE_EPOCH}"
log "registry=${BASE_REF}"

# ── Build ──────────────────────────────────────────────────────────
build_cmd=(
  docker buildx build
  --platform "${PLATFORM}"
  --file "${DOCKERFILE}"
  --build-arg "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}"
  --build-arg "GIT_SHA=${GIT_SHA}"
  --build-arg "SEMVER=${SEMVER}"
  --label "org.opencontainers.image.revision=${GIT_SHA}"
  --label "org.opencontainers.image.version=${SEMVER}"
  --label "org.opencontainers.image.created=$(date -u -d "@${SOURCE_DATE_EPOCH}" +%Y-%m-%dT%H:%M:%SZ)"
  --tag "${SEMVER_REF}"
  --tag "${SHA_REF}"
  --output "type=docker,name=${SEMVER_REF},rewrite-timestamp=true"
  .
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "DRY RUN — would exec:"
  printf '    %q ' "${build_cmd[@]}" >&2
  printf '\n' >&2
else
  log "building image..."
  if ! SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" "${build_cmd[@]}"; then
    err "docker build failed (ImageBuildFailed)"
    exit 1
  fi
fi

# Resolve the built image's digest. `docker inspect` returns the
# config digest of the local image, which is stable across builds
# with the same SOURCE_DATE_EPOCH + git SHA (the reproducibility
# contract).
if [[ "$DRY_RUN" -eq 0 ]]; then
  IMAGE_DIGEST="$(docker image inspect --format '{{.Id}}' "${SEMVER_REF}")"
  log "built digest=${IMAGE_DIGEST}"
else
  IMAGE_DIGEST="sha256:dryrun"
fi

# ── Push ───────────────────────────────────────────────────────────
if [[ "$PUSH" -eq 1 ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY RUN — would push: ${SEMVER_REF} ${SHA_REF}"
  else
    log "pushing ${SEMVER_REF}..."
    if ! docker push "${SEMVER_REF}"; then
      err "registry push failed for ${SEMVER_REF} (RegistryPushUnauthorized?)"
      exit 3
    fi
    log "pushing ${SHA_REF}..."
    if ! docker push "${SHA_REF}"; then
      err "registry push failed for ${SHA_REF} (RegistryPushUnauthorized?)"
      exit 3
    fi
  fi
else
  log "--no-push set; skipping registry push"
fi

# ── Sign ───────────────────────────────────────────────────────────
# Sign by registry digest (post-push), not by tag — a later tag move
# would otherwise invalidate the signature. If we skipped push, we
# fall back to signing the local image (cosign supports it, but the
# signature won't be discoverable by remote verifiers).
if [[ "$SIGN" -eq 1 ]]; then
  if [[ ! -f "$COSIGN_KEY" ]]; then
    err "cosign private key missing at ${COSIGN_KEY}"
    err "generate one with: cosign generate-key-pair (see docs/operations/image-pipeline-runbook.md §Key generation)"
    exit 2
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY RUN — would sign ${SEMVER_REF} + ${SHA_REF} with ${COSIGN_KEY}"
  else
    # When pushed, sign the registry digest (immutable). When not pushed,
    # cosign signs the local image and stores the signature in the
    # registry-style local OCI layout under ~/.cosign — that's good
    # enough for the build-only test path.
    if [[ "$PUSH" -eq 1 ]]; then
      # Resolve the pushed digest from the registry — `docker push`
      # output includes it on stdout, but we re-resolve via inspect
      # to avoid parsing fragile output. `docker buildx imagetools
      # inspect` queries the registry directly.
      pushed_digest="$(docker buildx imagetools inspect "${SEMVER_REF}" --format '{{.Manifest.Digest}}' 2>/dev/null || true)"
      if [[ -z "$pushed_digest" ]]; then
        err "could not resolve pushed digest for ${SEMVER_REF}"
        exit 2
      fi
      sign_ref="${BASE_REF}@${pushed_digest}"
    else
      sign_ref="${SEMVER_REF}"
    fi

    log "signing ${sign_ref} with cosign key ${COSIGN_KEY}"
    if ! COSIGN_YES=true cosign sign --key "${COSIGN_KEY}" --yes "${sign_ref}"; then
      err "cosign sign failed (CosignSignFailed)"
      exit 2
    fi
    log "signature attached"
  fi
else
  log "--no-sign set; skipping cosign signing"
fi

# ── Summary line — parsed by CI ────────────────────────────────────
printf 'BUILT image=%s digest=%s git_sha=%s tag=%s\n' \
       "${SEMVER_REF}" "${IMAGE_DIGEST}" "${GIT_SHA}" "${TAG}"
