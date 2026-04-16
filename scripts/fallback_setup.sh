#!/usr/bin/env bash
# N9 — one-shot bootstrap for the framework fallback branches.
#
# Idempotent: re-running on a host where the branches already exist
# locally is a no-op (and prints a notice). Pushing to `origin` is left
# to the operator (requires push credentials this script intentionally
# does not assume).
#
# Reads the canonical list of fallback branches from
# `.fallback/manifests/*.toml` so adding a new fallback only means
# committing a new manifest — no edit here required.
#
# Usage:
#   bash scripts/fallback_setup.sh                # create missing branches
#   bash scripts/fallback_setup.sh --dry-run      # report what would happen

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

DRY_RUN=0
if [ "${1-}" = "--dry-run" ]; then
    DRY_RUN=1
fi

if [ ! -d ".fallback/manifests" ]; then
    echo "::error::.fallback/manifests not found — run from repo root with N9 committed."
    exit 2
fi

BASE_REF="$(git rev-parse HEAD)"
echo "[N9] base ref: ${BASE_REF}"

create_branch() {
    local name="$1"
    if git show-ref --verify --quiet "refs/heads/${name}"; then
        echo "[N9] branch already exists locally: ${name} (no-op)"
        return 0
    fi
    if [ "$DRY_RUN" = "1" ]; then
        echo "[N9] (dry-run) would create ${name} at ${BASE_REF}"
        return 0
    fi
    git branch "${name}" "${BASE_REF}"
    echo "[N9] created local branch ${name} at ${BASE_REF}"
}

# Extract the [branch].name field from each manifest. Pure shell + grep
# to avoid a Python/jq dep — matches the stdlib-only philosophy used by
# the rest of the N1-N8 tooling.
for manifest in .fallback/manifests/*.toml; do
    branch_name="$(awk '
        /^\[branch\]/ { in_branch = 1; next }
        /^\[/         { in_branch = 0 }
        in_branch && /^name[[:space:]]*=/ {
            sub(/^name[[:space:]]*=[[:space:]]*"/, "")
            sub(/".*$/, "")
            print
            exit
        }
    ' "$manifest")"
    if [ -z "$branch_name" ]; then
        echo "::warning::manifest ${manifest} has no [branch].name — skipping"
        continue
    fi
    create_branch "$branch_name"
done

cat <<'EOF'

[N9] local fallback branches ready.

Next steps for the operator (one-shot, requires push credentials):

    git push -u origin compat/nextjs-15
    git push -u origin compat/pydantic-v2

After the push, .github/workflows/fallback-branches.yml will pick up
the new refs automatically (push trigger) and start the weekly cron
maintenance loop.
EOF
