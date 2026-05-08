#!/usr/bin/env bash
# OP-763 D2 — image retention policy enforcer.
#
# Driven by `.github/workflows/build-images.yml`'s `retention` job
# on a weekly cron. Runs locally too (with a GH_TOKEN scoped to
# `packages: write` on the org) for ad-hoc cleanup.
#
# Policy:
#   1. Tagged versions are kept forever (never deleted by this script).
#   2. Untagged versions younger than ${MAX_AGE_DAYS} days are kept.
#   3. The last ${MIN_KEEP} untagged versions are kept regardless of
#      age — a hard floor against clock-skew or runaway-build wipes.
#
# Required env:
#   GH_TOKEN       — gh CLI auth, packages: write scope.
#   PACKAGE_NAME   — e.g. "omnisight-backend"
#   OWNER          — github org or user (repo owner)
#
# Optional env:
#   MIN_KEEP       — default 20
#   MAX_AGE_DAYS   — default 30
#   DRY_RUN        — set to "1" to print decisions without deleting

set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${PACKAGE_NAME:?PACKAGE_NAME is required}"
: "${OWNER:?OWNER is required}"

MIN_KEEP="${MIN_KEEP:-20}"
MAX_AGE_DAYS="${MAX_AGE_DAYS:-30}"
DRY_RUN="${DRY_RUN:-0}"

# Lowercase: GHCR namespaces are case-insensitive but the API path is
# canonicalised lowercase.
owner_lc="$(printf '%s' "$OWNER" | tr '[:upper:]' '[:lower:]')"

# Discover whether this is a user package or an org package — the
# REST endpoints differ.
if gh api "users/${owner_lc}" --silent 2>/dev/null; then
  api_root="users/${owner_lc}"
else
  api_root="orgs/${owner_lc}"
fi

# Fetch ALL versions (paginated). Each version has:
#   id              — int64 used for the delete URL
#   metadata.container.tags  — list of tag strings (empty == untagged)
#   updated_at      — ISO-8601 timestamp
versions_json="$(gh api --paginate \
  "${api_root}/packages/container/${PACKAGE_NAME}/versions" \
  -q '[.[] | {id, tags: .metadata.container.tags, updated_at}]')"

if [ -z "$versions_json" ] || [ "$versions_json" = "null" ]; then
  echo "no versions found for ${PACKAGE_NAME} — nothing to prune"
  exit 0
fi

# Compute the cutoff timestamp once (epoch seconds).
cutoff="$(date -u -d "${MAX_AGE_DAYS} days ago" +%s)"

# Sort untagged versions by updated_at DESC (newest first), then
# slice off the first MIN_KEEP — those are the floor and never get
# deleted. Of the remainder, delete those older than cutoff.
candidates="$(printf '%s' "$versions_json" | jq -c \
  --argjson cutoff "$cutoff" \
  --argjson minkeep "$MIN_KEEP" \
  '
  [.[] | select((.tags | length) == 0)]                # untagged only
  | sort_by(.updated_at) | reverse                      # newest first
  | .[$minkeep:]                                        # drop the floor
  | map(select((.updated_at | fromdateiso8601) < $cutoff))
  ')"

count="$(printf '%s' "$candidates" | jq 'length')"
echo "package=${PACKAGE_NAME} candidates_to_delete=${count} (min_keep=${MIN_KEEP}, max_age_days=${MAX_AGE_DAYS})"

if [ "$count" -eq 0 ]; then
  exit 0
fi

printf '%s' "$candidates" | jq -c '.[]' | while IFS= read -r row; do
  vid="$(printf '%s' "$row" | jq -r '.id')"
  vupd="$(printf '%s' "$row" | jq -r '.updated_at')"
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY_RUN would delete version_id=${vid} updated_at=${vupd}"
  else
    echo "deleting version_id=${vid} updated_at=${vupd}"
    gh api -X DELETE "${api_root}/packages/container/${PACKAGE_NAME}/versions/${vid}"
  fi
done
