#!/usr/bin/env bash
# OP-1022: provision shared + per-runner memory scratch directories.

set -euo pipefail

ROOT="${OMNISIGHT_MEMORY_ROOT:-/var/omnisight/memory}"
OWNER="${OMNISIGHT_MEMORY_OWNER:-${SUDO_USER:-${USER:-}}}"
if [ -z "$OWNER" ]; then
    OWNER="$(id -un)"
fi
GROUP="${OMNISIGHT_MEMORY_GROUP:-}"
if [ -z "$GROUP" ]; then
    GROUP="$(id -gn "$OWNER" 2>/dev/null || id -gn)"
fi

DIRS="
shared
instance-claude-1
instance-claude-2
instance-codex-1
instance-codex-2
"

install_dir() {
    local dir="$1"

    if [ "$(id -u)" -eq 0 ]; then
        install -d -m 0750 -o "$OWNER" -g "$GROUP" "$dir"
    else
        mkdir -p "$dir"
        chmod 0750 "$dir"
        printf 'warning: not root; skipped chown %s:%s for %s\n' \
            "$OWNER" "$GROUP" "$dir" >&2
    fi
}

install_dir "$ROOT"

for rel in $DIRS; do
    install_dir "$ROOT/$rel"
done

printf 'memory dirs provisioned at %s owner=%s group=%s mode=0750\n' \
    "$ROOT" "$OWNER" "$GROUP"
