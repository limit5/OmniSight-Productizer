#!/usr/bin/env bash
# OP-1480 compatibility wrapper. The retention policy now lives in the
# testable Python implementation so tag classes can be handled safely.

set -euo pipefail

args=()
if [ -n "${PACKAGE_NAME:-}" ]; then
  args+=(--package "${PACKAGE_NAME}")
fi
if [ -n "${OWNER:-}" ]; then
  args+=(--owner "${OWNER}")
fi
if [ "${DRY_RUN:-0}" = "1" ]; then
  args+=(--dry-run)
fi

exec python3 scripts/enforce_image_retention.py "${args[@]}" "$@"
