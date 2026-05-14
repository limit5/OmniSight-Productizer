#!/bin/bash
# scripts/save-codex-review.sh
#
# Save codex review output from /tmp into docs/audit/codex-reviews/
# before WSL restart wipes /tmp. Idempotent. Refuses to overwrite without --force.
#
# Born: 2026-05-14. Trigger: /tmp/phase31{e,g,h,i,j}-codex-review-final.txt
# and /tmp/s12g-codex-review-final-retry.txt were referenced by S12 specs but
# were gone after the host-reboot incident. /tmp on WSL is volatile.
#
# Usage:
#   scripts/save-codex-review.sh /tmp/g-a-v2-codex-review-2026-05-14.txt
#   scripts/save-codex-review.sh /tmp/*codex*review*.txt          # batch
#   scripts/save-codex-review.sh --force /tmp/foo.txt              # overwrite
#   scripts/save-codex-review.sh --git-add /tmp/foo.txt            # also stage
#
# Naming: source file basename is preserved. If destination exists and content
# differs and --force is NOT set, the script exits non-zero with a diff hint.
# If content is identical, exits 0 with "already saved".

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "/home/user/work/sora/OmniSight-Productizer")"
DEST_DIR="${REPO_ROOT}/docs/audit/codex-reviews"

FORCE=0
GIT_ADD=0
FILES=()

# Parse flags + collect files
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)   FORCE=1; shift ;;
        --git-add) GIT_ADD=1; shift ;;
        --help|-h)
            head -n 25 "$0" | sed -n '/^#/p' | sed 's/^# //; s/^#//'
            exit 0
            ;;
        --*)
            echo "ERROR: unknown flag: $1" >&2
            exit 2
            ;;
        *)
            FILES+=("$1"); shift
            ;;
    esac
done

if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "ERROR: no input files. Usage: $0 [--force] [--git-add] <file>..." >&2
    exit 2
fi

mkdir -p "$DEST_DIR"

saved=0
skipped=0
conflicts=0
staged=()

for src in "${FILES[@]}"; do
    if [[ ! -f "$src" ]]; then
        echo "SKIP (not a file): $src" >&2
        skipped=$((skipped + 1))
        continue
    fi
    base="$(basename "$src")"
    dst="${DEST_DIR}/${base}"

    if [[ -f "$dst" ]]; then
        if cmp -s "$src" "$dst"; then
            echo "ALREADY SAVED (identical): $base"
            skipped=$((skipped + 1))
            continue
        fi
        if [[ $FORCE -eq 1 ]]; then
            cp "$src" "$dst"
            echo "OVERWROTE: $base"
            saved=$((saved + 1))
            [[ $GIT_ADD -eq 1 ]] && staged+=("$dst")
        else
            echo "CONFLICT (use --force to overwrite, or diff manually): $base" >&2
            echo "  src: $src" >&2
            echo "  dst: $dst" >&2
            echo "  diff first 5 lines:" >&2
            diff "$dst" "$src" 2>/dev/null | head -5 | sed 's/^/    /' >&2 || true
            conflicts=$((conflicts + 1))
        fi
        continue
    fi

    cp "$src" "$dst"
    echo "SAVED: $base"
    saved=$((saved + 1))
    [[ $GIT_ADD -eq 1 ]] && staged+=("$dst")
done

if [[ ${#staged[@]} -gt 0 ]]; then
    git -C "$REPO_ROOT" add -- "${staged[@]}"
    echo ""
    echo "Staged ${#staged[@]} file(s) in git. Review with: git -C $REPO_ROOT diff --cached --stat"
fi

echo ""
echo "Summary: $saved saved, $skipped skipped, $conflicts conflict(s)"
[[ $conflicts -gt 0 ]] && exit 1
exit 0
