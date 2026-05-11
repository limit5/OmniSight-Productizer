#!/usr/bin/env bash
# OP-864 D2 — retention policy GC for the self-hosted Docker registry.
#
# Driven by `.github/workflows/image-build.yml`'s `retention` job on
# a weekly cron. Also runnable interactively against the production
# registry by an operator (with REGISTRY_USERNAME/REGISTRY_PASSWORD
# in env, or anonymous if the registry allows it).
#
# Policy (OP-864 ticket AC #4):
#   keep last 30 days OR last 20 tags — WHICHEVER MORE.
#
# Concretely:
#   1. Fetch all tags of <namespace>/<repo>.
#   2. For each tag, resolve the manifest's image-config "created"
#      timestamp.
#   3. Sort tags by created DESC (newest first).
#   4. Mark the top MIN_KEEP (default 20) as "keep" regardless of age.
#   5. Of the remainder, mark "keep" if created within MAX_AGE_DAYS
#      (default 30).
#   6. Delete everything else via the Registry HTTP API v2 DELETE
#      /v2/<name>/manifests/<digest> endpoint.
#
# Required env:
#   REGISTRY        — host:port of the registry (default: registry.sora.services:5000)
#   IMAGE_PATH      — namespace/repo (default: omnisight/runner)
#
# Optional env:
#   MIN_KEEP        — default 20
#   MAX_AGE_DAYS    — default 30
#   REGISTRY_USER   — basic-auth username
#   REGISTRY_PASS   — basic-auth password
#   DRY_RUN         — "1" to print decisions without deleting
#
# Errors emitted (mapping to OP-864 catalog):
#   RetentionGCFailed — any non-recoverable error while listing or
#                       deleting; the script exits 1 and the workflow
#                       opens an alert via the audit-log poller.

set -euo pipefail

REGISTRY="${REGISTRY:-registry.sora.services:5000}"
IMAGE_PATH="${IMAGE_PATH:-omnisight/runner}"
MIN_KEEP="${MIN_KEEP:-20}"
MAX_AGE_DAYS="${MAX_AGE_DAYS:-30}"
DRY_RUN="${DRY_RUN:-0}"

err() { printf '❌ gc_image_registry: %s\n' "$*" >&2; }
log() { printf '→ gc_image_registry: %s\n' "$*" >&2; }

# Numeric env sanity — non-int values would otherwise expand into
# arithmetic later and silently delete everything.
case "$MIN_KEEP" in *[!0-9]*|"") err "MIN_KEEP must be a positive integer (got '${MIN_KEEP}')"; exit 1;; esac
case "$MAX_AGE_DAYS" in *[!0-9]*|"") err "MAX_AGE_DAYS must be a positive integer (got '${MAX_AGE_DAYS}')"; exit 1;; esac

for bin in curl jq; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    err "missing dependency: $bin"
    exit 1
  fi
done

# Auth header — registry typically uses basic auth; tokens too if
# the registry is fronted by a Bearer-token issuer. We support the
# simple basic-auth case (sora.services:5000) here.
AUTH_ARGS=()
if [[ -n "${REGISTRY_USER:-}" ]] && [[ -n "${REGISTRY_PASS:-}" ]]; then
  AUTH_ARGS=(-u "${REGISTRY_USER}:${REGISTRY_PASS}")
fi

# Detect plaintext (registries on :5000 typically run HTTP unless
# fronted by a TLS terminator). Honour the explicit scheme if the
# caller passed one in REGISTRY, otherwise default to https.
if [[ "$REGISTRY" == http://* || "$REGISTRY" == https://* ]]; then
  REGISTRY_URL="$REGISTRY"
else
  REGISTRY_URL="https://${REGISTRY}"
fi

V2_BASE="${REGISTRY_URL}/v2/${IMAGE_PATH}"

# `--accept` matches the v2 manifest schema *and* the OCI manifest
# schema; some registries return one or the other depending on what
# was pushed.
MANIFEST_ACCEPT='application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json'

curl_get() {
  curl --silent --show-error --fail-with-body "${AUTH_ARGS[@]}" -H "Accept: ${MANIFEST_ACCEPT}" "$@"
}

# ── 1. list all tags ───────────────────────────────────────────────
log "listing tags from ${V2_BASE}/tags/list"
tags_json="$(curl_get "${V2_BASE}/tags/list" || true)"
if [[ -z "$tags_json" ]]; then
  err "could not list tags (RetentionGCFailed) — registry unreachable or unauthorised"
  exit 1
fi

mapfile -t tags < <(printf '%s' "$tags_json" | jq -r '.tags[]?')
total="${#tags[@]}"
log "found ${total} tags"

if [[ "$total" -eq 0 ]]; then
  log "nothing to GC"
  exit 0
fi

# ── 2. resolve created timestamp per tag ───────────────────────────
# For each tag, HEAD the manifest to get its digest, then GET the
# config blob and parse `.created`. We stream into a TSV
# (created_epoch \t tag \t digest) for the sort step.
tmp_index="$(mktemp)"
trap 'rm -f "$tmp_index"' EXIT

for tag in "${tags[@]}"; do
  digest="$(curl --silent --show-error --fail-with-body "${AUTH_ARGS[@]}" \
              -I -H "Accept: ${MANIFEST_ACCEPT}" \
              "${V2_BASE}/manifests/${tag}" \
              | tr -d '\r' \
              | awk -F': ' 'tolower($1)=="docker-content-digest" {print $2}')"

  if [[ -z "$digest" ]]; then
    log "warn: could not resolve digest for tag '${tag}' (skipping)"
    continue
  fi

  manifest="$(curl_get "${V2_BASE}/manifests/${tag}" || true)"
  if [[ -z "$manifest" ]]; then
    log "warn: could not fetch manifest for tag '${tag}' (skipping)"
    continue
  fi

  config_digest="$(printf '%s' "$manifest" | jq -r '.config.digest // empty')"
  created_iso=""
  if [[ -n "$config_digest" ]]; then
    config_json="$(curl_get "${V2_BASE}/blobs/${config_digest}" || true)"
    created_iso="$(printf '%s' "$config_json" | jq -r '.created // empty')"
  fi

  if [[ -z "$created_iso" ]]; then
    # Fallback: use the manifest's history[0].created if present, or
    # treat as epoch 0 so the tag is the OLDEST and gets pruned
    # first (after the floor).
    created_iso="$(printf '%s' "$manifest" | jq -r '.history[0].v1Compatibility // empty' | jq -r '.created // empty' 2>/dev/null || true)"
  fi

  if [[ -z "$created_iso" ]]; then
    created_epoch=0
  else
    created_epoch="$(date -u -d "$created_iso" +%s 2>/dev/null || echo 0)"
  fi

  printf '%s\t%s\t%s\n' "$created_epoch" "$tag" "$digest" >> "$tmp_index"
done

# ── 3-5. decide keep / delete ──────────────────────────────────────
cutoff="$(date -u -d "${MAX_AGE_DAYS} days ago" +%s)"

# Sort by created_epoch DESC. The first MIN_KEEP are kept (floor).
# Of the rest, those with created_epoch ≥ cutoff are kept (age window).
# The remainder are deleted.
deletions="$(sort -k1,1nr "$tmp_index" \
  | awk -v floor="$MIN_KEEP" -v cutoff="$cutoff" '
      BEGIN { OFS="\t" }
      {
        line_no = NR
        if (line_no <= floor) {
          # floor — always keep
          next
        }
        if ($1 >= cutoff) {
          # within age window — keep
          next
        }
        print $2, $3
      }
    ')"

if [[ -z "$deletions" ]]; then
  log "nothing to delete (floor=${MIN_KEEP}, max_age_days=${MAX_AGE_DAYS})"
  exit 0
fi

del_count="$(printf '%s\n' "$deletions" | wc -l | tr -d ' ')"
log "deletion candidates: ${del_count} (floor=${MIN_KEEP}, max_age_days=${MAX_AGE_DAYS})"

# ── 6. delete via DELETE /v2/<name>/manifests/<digest> ────────────
while IFS=$'\t' read -r tag digest; do
  [[ -z "$tag" ]] && continue
  if [[ "$DRY_RUN" = "1" ]]; then
    log "DRY_RUN would delete tag=${tag} digest=${digest}"
    continue
  fi
  log "deleting tag=${tag} digest=${digest}"
  if ! curl --silent --show-error --fail-with-body "${AUTH_ARGS[@]}" \
            -X DELETE "${V2_BASE}/manifests/${digest}" >/dev/null; then
    err "delete failed for ${tag} (${digest}) — RetentionGCFailed"
    exit 1
  fi
done <<< "$deletions"

log "GC complete"
