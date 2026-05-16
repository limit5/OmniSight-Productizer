#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="${OMNISIGHT_RUNNER_TMP_WRAPPER_DIR:-/tmp/runner_wrappers}"

mkdir -p "${TMP_DIR}"

for wrapper in claude-1.sh claude-2.sh codex-1.sh codex-2.sh; do
  ln -sfn "${SCRIPT_DIR}/${wrapper}" "${TMP_DIR}/${wrapper}"
done
