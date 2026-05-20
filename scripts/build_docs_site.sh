#!/usr/bin/env bash
#
# Build the MkDocs Material docs site into docs-site-dist/, which the prod
# Caddy container serves read-only at its :8081 block (CF Tunnel ingress
# docs.sora-dev.app). See ADR-0014 + deploy/systemd/omnisight-docs-build.*.
#
# Uses the squidfunk/mkdocs-material image so no host mkdocs install is
# needed (build env choice 6b). Periodic via omnisight-docs-build.timer.
#
# LIMITATION: builds from the CURRENT working tree's docs-site/docs/, not a
# clean develop checkout. Acceptable for this low-traffic internal docs site;
# if it ever needs to track develop-tip exactly, move the build to CI.
#
set -euo pipefail

REPO="/home/user/work/sora/OmniSight-Productizer"
IMG="squidfunk/mkdocs-material:9.7.6"
STAGE="$REPO/docs-site-dist.new"
LIVE="$REPO/docs-site-dist"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "[$(date -Is)] docs build start (img=$IMG)"

rm -rf "$STAGE"

# --user keeps output owned by the invoking user (the image defaults to root).
# NOT --strict: 5 known content warnings (1 orphan nav page + 4 cross-doc
# links to ADRs outside the curated subset) would abort the build; tracked
# as a separate follow-up, not a publish blocker.
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$REPO:/repo" -w /repo/docs-site \
  "$IMG" build --site-dir /repo/docs-site-dist.new

# Publish into the live dir the Caddy bind-mount serves. rsync writes each
# file to a temp name then renames it into place (atomic per file), so a
# concurrent HTTP read never sees a half-written file. --delete prunes pages
# removed since the last build; a transient 404 on a just-removed page is
# harmless for docs.
rsync -a --delete "$STAGE/" "$LIVE/"

echo "[$(date -Is)] docs build published to $LIVE"
