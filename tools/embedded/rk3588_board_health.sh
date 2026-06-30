#!/usr/bin/env bash
# OP-2495 - RK3588 (ATK-DLRK3588) board-level health check.
#
# Reads the board model and the SoC/CPU thermal-zone temperature from the
# running system and emits a single JSON document on stdout, then exits.
# One-shot by design so it composes into the Phase 0 embedded bring-up SOP
# without managing a daemon lifecycle.
#
# Sources:
#   /proc/device-tree/model                          board model (NUL-terminated string)
#   /sys/class/thermal/thermal_zoneN/{type,temp}     thermal zones (temp is milli-degC)
#
# Output JSON shape (single line):
#   {"model":"Rockchip RK3588 ATK-DLRK3588 EVB","cpu_temp_celsius":47.2}
#
# Usage:
#   tools/embedded/rk3588_board_health.sh
#   tools/embedded/rk3588_board_health.sh --sysfs-root <path> --proc-root <path>
#
# The --sysfs-root / --proc-root overrides exist so the tool can be exercised
# against a synthetic fixture tree without touching the real /sys, /proc.

set -euo pipefail

PROC_ROOT="/proc"
SYSFS_ROOT="/sys"

usage() {
  sed -n '2,22p' "$0"
}

die() {
  echo "rk3588_board_health.sh: $*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --sysfs-root)
      [[ $# -ge 2 ]] || die "--sysfs-root requires a path"
      SYSFS_ROOT="$2"
      shift 2
      ;;
    --proc-root)
      [[ $# -ge 2 ]] || die "--proc-root requires a path"
      PROC_ROOT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

model_path="$PROC_ROOT/device-tree/model"
[[ -r "$model_path" ]] || die "model not readable: $model_path"
# device-tree/model is a NUL-terminated string; strip the trailing NUL(s).
model="$(tr -d '\0' <"$model_path")"
[[ -n "$model" ]] || die "empty model string at $model_path"

# RK3588 exposes several thermal zones: soc-thermal, bigcore0-thermal,
# bigcore1-thermal, littlecore-thermal, center-thermal, gpu-thermal,
# npu-thermal. Prefer the whole-die 'soc-thermal' zone; fall back to the
# first cpu/core-tagged zone; final fallback is thermal_zone0.
thermal_root="$SYSFS_ROOT/class/thermal"
zone_dir=""

shopt -s nullglob
zone_candidates=("$thermal_root"/thermal_zone*)
shopt -u nullglob

for candidate in "${zone_candidates[@]}"; do
  type_file="$candidate/type"
  [[ -r "$type_file" ]] || continue
  zone_type="$(tr -d '\0' <"$type_file" | tr -d '\n')"
  if [[ "$zone_type" == "soc-thermal" ]]; then
    zone_dir="$candidate"
    break
  fi
done

if [[ -z "$zone_dir" ]]; then
  for candidate in "${zone_candidates[@]}"; do
    type_file="$candidate/type"
    [[ -r "$type_file" ]] || continue
    zone_type="$(tr -d '\0' <"$type_file" | tr -d '\n')"
    case "$zone_type" in
      *cpu*|*core*)
        zone_dir="$candidate"
        break
        ;;
    esac
  done
fi

if [[ -z "$zone_dir" ]]; then
  zone_dir="$thermal_root/thermal_zone0"
fi

temp_file="$zone_dir/temp"
[[ -r "$temp_file" ]] || die "thermal temp not readable: $temp_file"
temp_milli="$(tr -d '[:space:]' <"$temp_file")"
[[ "$temp_milli" =~ ^-?[0-9]+$ ]] || die "thermal temp not numeric: '$temp_milli'"

# Convert milli-degC -> degC with one decimal place. awk gives portable
# fixed-point math without dragging in bc.
cpu_temp_celsius="$(awk -v m="$temp_milli" 'BEGIN { printf "%.1f", m/1000 }')"

# Escape characters that would otherwise break the JSON string.
# A C0 control char in /proc/device-tree/model is unusual but cheap to handle.
escape_json() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  printf '%s' "$s"
}

model_json="$(escape_json "$model")"

printf '{"model":"%s","cpu_temp_celsius":%s}\n' "$model_json" "$cpu_temp_celsius"
