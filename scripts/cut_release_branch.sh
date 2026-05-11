#!/usr/bin/env bash
# [OP-880] Cut a release branch, generate changelog, tag, and push.
#
# Usage:
#   scripts/cut_release_branch.sh --version v1.2.3
#   scripts/cut_release_branch.sh --version v1.2.3 --approve-tag
#   scripts/cut_release_branch.sh --version v1.2.3 --dry-run

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
REPO="$ROOT"
VERSION=""
MAIN_REF="main"
GERRIT_REMOTE="gerrit"
GITHUB_REMOTE="origin"
APPROVE_TAG=false
OVERRIDE_EXISTING=false
DRY_RUN=false
CHANGELOG_JIRA=false
CHANGELOG_TEMPLATE_ONLY=false
PUSH_PREFIX=""

err() {
  echo "cut_release_branch: $*" >&2
  exit 1
}

usage() {
  sed -n '2,12p' "$0" >&2
}

git_run() {
  git -C "$REPO" "$@"
}

git_maybe() {
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[dry-run] git -C %q' "$REPO"
    printf ' %q' "$@"
    printf '\n'
  else
    git_run "$@"
  fi
}

remote_exists() {
  git_run remote get-url "$1" >/dev/null 2>&1
}

remote_ref_exists() {
  local remote=$1
  local ref=$2
  remote_exists "$remote" && git_run ls-remote --exit-code "$remote" "$ref" >/dev/null 2>&1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:-}"
      shift 2
      ;;
    --main-ref)
      MAIN_REF="${2:-}"
      shift 2
      ;;
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --gerrit-remote)
      GERRIT_REMOTE="${2:-}"
      shift 2
      ;;
    --github-remote)
      GITHUB_REMOTE="${2:-}"
      shift 2
      ;;
    --approve-tag)
      APPROVE_TAG=true
      shift
      ;;
    --override-existing)
      OVERRIDE_EXISTING=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --changelog-jira)
      CHANGELOG_JIRA=true
      shift
      ;;
    --changelog-template-only)
      CHANGELOG_TEMPLATE_ONLY=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "unknown arg: $1"
      ;;
  esac
done

[[ -n "$VERSION" ]] || err "--version is required"
[[ "$VERSION" =~ ^v[0-9]+(\.[0-9]+){1,2}(-[0-9A-Za-z][0-9A-Za-z.-]*)?$ ]] \
  || err "--version must look like vX.Y.Z or a release candidate such as v0.99-rc"
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || err "--repo must point at a git working tree"
command -v python3 >/dev/null 2>&1 || err "python3 not found in PATH"

BRANCH="release/$VERSION"
TAG="$VERSION"
MAIN_SHA=$(git_run rev-parse --verify "$MAIN_REF^{commit}") \
  || err "main ref '$MAIN_REF' does not resolve to a commit"

if git_run show-ref --verify --quiet "refs/heads/$BRANCH" \
    || remote_ref_exists "$GERRIT_REMOTE" "refs/heads/$BRANCH" \
    || remote_ref_exists "$GITHUB_REMOTE" "refs/heads/$BRANCH"; then
  if [[ "$OVERRIDE_EXISTING" != "true" ]]; then
    err "ReleaseBranchExists: $BRANCH already exists; rerun with --override-existing to replace the branch ref"
  fi
fi

if git_run rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null \
    || remote_ref_exists "$GERRIT_REMOTE" "refs/tags/$TAG" \
    || remote_ref_exists "$GITHUB_REMOTE" "refs/tags/$TAG"; then
  if [[ "$OVERRIDE_EXISTING" != "true" ]]; then
    err "TagAlreadyExists: $TAG already exists; rerun with --override-existing to replace the tag ref"
  fi
fi

if [[ "$APPROVE_TAG" != "true" && "$DRY_RUN" != "true" ]]; then
  echo "Manual gate: type '$TAG' to approve creating and pushing tag $TAG" >&2
  read -r APPROVAL
  [[ "$APPROVAL" == "$TAG" ]] || err "operator approval mismatch; tag was not created"
fi

if [[ "$OVERRIDE_EXISTING" == "true" ]]; then
  PUSH_PREFIX="+"
fi

git_maybe branch -f "$BRANCH" "$MAIN_SHA"
git_maybe checkout "$BRANCH"

CHANGELOG_ARGS=(--version "$VERSION" --output "$REPO/CHANGELOG.md" --allow-template)
if [[ "$CHANGELOG_JIRA" == "true" ]]; then
  CHANGELOG_ARGS+=(--jira)
fi
if [[ "$CHANGELOG_TEMPLATE_ONLY" == "true" ]]; then
  CHANGELOG_ARGS+=(--template-only)
fi

if [[ "$DRY_RUN" == "true" ]]; then
  printf '[dry-run] python3 %q' "$ROOT/scripts/auto_changelog.py"
  printf ' %q' "${CHANGELOG_ARGS[@]}"
  printf '\n'
else
  if ! CHANGELOG_JSON=$(python3 "$ROOT/scripts/auto_changelog.py" "${CHANGELOG_ARGS[@]}"); then
    echo "cut_release_branch: ChangelogGenFailed; retrying with manual template" >&2
    CHANGELOG_JSON=$(python3 "$ROOT/scripts/auto_changelog.py" \
      --version "$VERSION" \
      --output "$REPO/CHANGELOG.md" \
      --template-only)
  fi
  echo "$CHANGELOG_JSON"
  if ! git_run diff --quiet -- CHANGELOG.md; then
    git_maybe add CHANGELOG.md
    git_maybe commit -m "docs(release): update changelog for $VERSION"
  fi
fi

git_maybe tag -f -a "$TAG" -m "Release $TAG"

remote_exists "$GERRIT_REMOTE" || err "Gerrit remote '$GERRIT_REMOTE' is not configured"
remote_exists "$GITHUB_REMOTE" || err "GitHub remote '$GITHUB_REMOTE' is not configured"

git_maybe push "$GERRIT_REMOTE" "${PUSH_PREFIX}refs/heads/$BRANCH:refs/heads/$BRANCH"
git_maybe push "$GERRIT_REMOTE" "${PUSH_PREFIX}refs/tags/$TAG:refs/tags/$TAG"
git_maybe push "$GITHUB_REMOTE" "${PUSH_PREFIX}refs/heads/$BRANCH:refs/heads/$BRANCH"
git_maybe push "$GITHUB_REMOTE" "${PUSH_PREFIX}refs/tags/$TAG:refs/tags/$TAG"

echo "cut_release_branch: created $BRANCH and $TAG from $MAIN_REF ($MAIN_SHA)"
