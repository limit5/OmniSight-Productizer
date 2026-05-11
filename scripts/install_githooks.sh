#!/usr/bin/env bash
# Install project-tracked Git hooks for this checkout.

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$repo_root" ]; then
  echo "install_githooks: not inside a git repository" >&2
  exit 1
fi

hook_dir="$repo_root/.githooks"
post_commit="$hook_dir/post-commit"

if [ ! -f "$post_commit" ]; then
  echo "install_githooks: missing $post_commit" >&2
  exit 1
fi

chmod +x "$post_commit"
git -C "$repo_root" config core.hooksPath .githooks

configured="$(git -C "$repo_root" config --get core.hooksPath)"
if [ "$configured" != ".githooks" ]; then
  echo "install_githooks: core.hooksPath expected .githooks, got $configured" >&2
  exit 1
fi

echo "Installed OmniSight git hooks: core.hooksPath=.githooks"
