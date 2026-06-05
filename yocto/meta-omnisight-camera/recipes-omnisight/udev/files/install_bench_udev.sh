#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES_FILE="${SCRIPT_DIR}/99-omnisight-bench.rules"
TARGET_DIR="/etc/udev/rules.d"

sudo cp "${RULES_FILE}" "${TARGET_DIR}/99-omnisight-bench.rules"
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "bench udev rules installed; add yourself to dialout group: sudo usermod -aG dialout $USER"
