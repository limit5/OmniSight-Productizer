#!/usr/bin/env bash
# OP-2496 - Board-level thermal-zone temperature monitor.
#
# Enumerates every /sys/class/thermal/thermal_zone*/ on the running system
# and emits a single JSON document on stdout listing each zone's type name
# and its current temperature in degrees Celsius. Companion to
# rk3588_board_health.sh (OP-2495); that tool reports a single curated
# CPU/SoC zone, this one dumps all of them for board bring-up telemetry.
#
# Sources per zone:
#   /sys/class/thermal/thermal_zoneN/type   short name, e.g. "cpu-thermal"
#   /sys/class/thermal/thermal_zoneN/temp   temperature in milli-degC (int)
#
# Output JSON shape (single line):
#   {"thermal_zones":[{"name":"cpu-thermal","temp":45.5,"unit":"°C"},
#                     {"name":"gpu-thermal","temp":38.2,"unit":"°C"}]}
#
# If a zone's temp file is unreadable or does not contain an integer, the
# entry is still emitted with "temp": null so downstream consumers can
# distinguish "zone exists but sensor is unhealthy" from "zone missing".
# If the type file itself is unreadable the zone directory basename is used
# as a fallback name (e.g. "thermal_zone3").
#
# Usage:
#   thermal_zones.sh
#   thermal_zones.sh --sysfs-root <path>
#
# --sysfs-root exists so the tool can be exercised against a synthetic
# fixture tree without touching the real /sys.
#
# Installed to /usr/local/bin/thermal_zones.sh on target images.

set -euo pipefail

SYSFS_ROOT="/sys"

usage() {
  sed -n '2,32p' "$0"
}

die() {
  echo "thermal_zones.sh: $*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --sysfs-root)
      [[ $# -ge 2 ]] || die "--sysfs-root requires a path"
      SYSFS_ROOT="$2"
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

thermal_root="$SYSFS_ROOT/class/thermal"
[[ -d "$thermal_root" ]] || die "thermal root missing: $thermal_root"

shopt -s nullglob
zone_dirs=("$thermal_root"/thermal_zone*)
shopt -u nullglob

# Sort by trailing zone index so the output order is stable across runs
# (glob order is filesystem-dependent on some kernels).
if ((${#zone_dirs[@]} > 1)); then
  IFS=$'\n' zone_dirs=($(printf '%s\n' "${zone_dirs[@]}" | sort -V))
  unset IFS
fi

# JSON string escape for the "name" field. Zone type names are usually
# tame ([a-z0-9-]) but a defensive escape costs nothing.
escape_json() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  printf '%s' "$s"
}

entries=()
for zone_dir in "${zone_dirs[@]}"; do
  type_file="$zone_dir/type"
  temp_file="$zone_dir/temp"

  if [[ -r "$type_file" ]]; then
    name="$(tr -d '\0' <"$type_file" | tr -d '\n')"
  else
    name=""
  fi
  # Fall back to the directory basename if type is missing or empty so the
  # entry still carries some identifier the operator can act on.
  [[ -n "$name" ]] || name="$(basename "$zone_dir")"

  temp_json="null"
  if [[ -r "$temp_file" ]]; then
    temp_milli="$(tr -d '[:space:]' <"$temp_file" 2>/dev/null || true)"
    if [[ "$temp_milli" =~ ^-?[0-9]+$ ]]; then
      # milli-degC -> degC with one decimal; awk keeps this portable.
      temp_json="$(awk -v m="$temp_milli" 'BEGIN { printf "%.1f", m/1000 }')"
    fi
  fi

  name_json="$(escape_json "$name")"
  # Unit is emitted as a JSON \u escape (U+00B0 DEGREE SIGN + 'C') so the
  # output is pure ASCII and safe for any downstream tokeniser regardless
  # of the caller's locale.
  entries+=("$(printf '{"name":"%s","temp":%s,"unit":"\\u00b0C"}' "$name_json" "$temp_json")")
done

# Join array with commas without spawning a subshell per element.
joined=""
for ((i = 0; i < ${#entries[@]}; i++)); do
  if ((i == 0)); then
    joined="${entries[i]}"
  else
    joined="$joined,${entries[i]}"
  fi
done

printf '{"thermal_zones":[%s]}\n' "$joined"
