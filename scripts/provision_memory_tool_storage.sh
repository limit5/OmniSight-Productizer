#!/usr/bin/env bash
# OP-900: provision durable Memory Tool fleet directories.

set -euo pipefail

ROOT="${OMNISIGHT_MEMORY_TOOL_ROOT:-/var/omnisight/memory}"
SERVICE_USER="${OMNISIGHT_BACKEND_SERVICE_USER:-omnisight}"
SERVICE_GROUP="${OMNISIGHT_BACKEND_SERVICE_GROUP:-$SERVICE_USER}"
FLEETS="${OMNISIGHT_MEMORY_TOOL_FLEETS:-claude codex merger}"

install_dir() {
    local dir="$1"

    if [ "$(id -u)" -eq 0 ]; then
        install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$dir"
    else
        mkdir -p "$dir"
        chmod 0750 "$dir"
        printf 'warning: not root; skipped chown %s:%s for %s\n' \
            "$SERVICE_USER" "$SERVICE_GROUP" "$dir" >&2
    fi
}

install_dir "$ROOT"

for fleet in $FLEETS; do
    case "$fleet" in
        claude|codex|merger)
            install_dir "$ROOT/$fleet"
            ;;
        *)
            printf 'refusing unknown memory fleet: %s\n' "$fleet" >&2
            exit 2
            ;;
    esac
done

printf 'memory tool storage provisioned at %s for fleets: %s\n' "$ROOT" "$FLEETS"
