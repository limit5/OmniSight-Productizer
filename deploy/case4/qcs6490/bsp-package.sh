#!/usr/bin/env bash
# OP-1968 C4-C.B - Radxa Dragon Q6A BSP/SDK package builder.
#
# Usage:
#   deploy/case4/qcs6490/bsp-package.sh \
#     --kernel <Image> \
#     --dtb <radxa-dragon-q6a.dtb> \
#     --stitching-bin <uvc-stitching> \
#     --version <release-id> \
#     --output-dir <dir> \
#     --signing-key <cosign.key>
#
# The package layout is:
#   case4-qcs6490-bsp-sdk-<version>/
#     boot/Image
#     boot/radxa-dragon-q6a.dtb
#     usr/bin/uvc-stitching
#     sdk/include/*.h
#     licenses/LICENSES.spdx.json
#     RUNBOOK.md
#     manifest.json
#     CHECKSUMS.sha256
#
# Production packages are signed with `cosign sign-blob`. Local validation may
# pass --allow-unsigned; the unsigned mode still emits checksums and fails if
# any payload input is missing.

set -euo pipefail

usage() {
  sed -n '2,35p' "$0"
}

die() {
  echo "bsp-package.sh: $*" >&2
  exit 2
}

log() {
  echo "bsp-package.sh: $*" >&2
}

need_file() {
  local label="$1"
  local path="$2"
  [ -n "$path" ] || die "$label is required"
  [ -f "$path" ] || die "$label does not exist: $path"
}

need_dir() {
  local label="$1"
  local path="$2"
  [ -n "$path" ] || die "$label is required"
  [ -d "$path" ] || die "$label does not exist: $path"
}

copy_mode() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  install -m "$mode" "$src" "$dst"
}

sha256_file() {
  local path="$1"
  sha256sum "$path" | awk '{print $1}'
}

write_runbook() {
  local path="$1"
  cat >"$path" <<'EOF'
# Radxa Dragon Q6A BSP/SDK Customer Runbook

## Contents

- `boot/Image` - QCS6490 Linux kernel image.
- `boot/radxa-dragon-q6a.dtb` - Radxa Dragon Q6A device tree blob.
- `usr/bin/uvc-stitching` - multi-camera UVC stitching pipeline binary.
- `sdk/include/` - customer SDK headers for application integration.
- `licenses/LICENSES.spdx.json` - SPDX package/license manifest with NDA blob attribution.
- `CHECKSUMS.sha256` - payload integrity checksums.

## Bring-up

1. Flash the BSP image using the Phase 0 QCS6490 flash flow for Radxa Dragon Q6A.
2. Boot the EVK and confirm the kernel reports the packaged DTB.
3. Copy `usr/bin/uvc-stitching` to the target rootfs if it is not already baked into the image.
4. Build the customer application against `sdk/include`.
5. Run `uvc-stitching --mode grid --device /dev/video0` or the customer-selected topology.
6. Compare payload checksums against `CHECKSUMS.sha256` before customer acceptance.

## Acceptance

Customer-side acceptance requires a real Radxa Dragon Q6A EVK boot, visible UVC
camera enumeration, a successful stitching pipeline run, and checksum/signature
verification of the delivered archive. NDA-covered Qualcomm BSP blobs remain
inside the customer delivery boundary named in `licenses/LICENSES.spdx.json`.
EOF
}

write_manifest() {
  local path="$1"
  local version="$2"
  local kernel_sha="$3"
  local dtb_sha="$4"
  local stitching_sha="$5"
  local licenses_sha="$6"

  cat >"$path" <<EOF
{
  "package": "case4-qcs6490-bsp-sdk",
  "ticket": "OP-1968",
  "evk": "Radxa Dragon Q6A",
  "soc": "Qualcomm QCS6490",
  "arch": "aarch64",
  "version": "$version",
  "nda_required": true,
  "artifacts": [
    {
      "path": "boot/Image",
      "role": "kernel",
      "sha256": "$kernel_sha"
    },
    {
      "path": "boot/radxa-dragon-q6a.dtb",
      "role": "device-tree",
      "sha256": "$dtb_sha"
    },
    {
      "path": "usr/bin/uvc-stitching",
      "role": "stitching-pipeline",
      "sha256": "$stitching_sha"
    },
    {
      "path": "licenses/LICENSES.spdx.json",
      "role": "license-manifest",
      "sha256": "$licenses_sha"
    }
  ]
}
EOF
}

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
script_dir="$repo_root/deploy/case4/qcs6490"

kernel=""
dtb=""
stitching_bin=""
sdk_headers="$script_dir/sdk-headers"
license_manifest="$script_dir/LICENSES.spdx.json"
output_dir="$repo_root/dist/case4/qcs6490"
version=""
signing_key=""
allow_unsigned=false
keep_workdir=false

while [ $# -gt 0 ]; do
  case "$1" in
    --kernel) kernel="${2:-}"; shift 2 ;;
    --kernel=*) kernel="${1#--kernel=}"; shift ;;
    --dtb) dtb="${2:-}"; shift 2 ;;
    --dtb=*) dtb="${1#--dtb=}"; shift ;;
    --stitching-bin) stitching_bin="${2:-}"; shift 2 ;;
    --stitching-bin=*) stitching_bin="${1#--stitching-bin=}"; shift ;;
    --sdk-headers) sdk_headers="${2:-}"; shift 2 ;;
    --sdk-headers=*) sdk_headers="${1#--sdk-headers=}"; shift ;;
    --license-manifest) license_manifest="${2:-}"; shift 2 ;;
    --license-manifest=*) license_manifest="${1#--license-manifest=}"; shift ;;
    --output-dir) output_dir="${2:-}"; shift 2 ;;
    --output-dir=*) output_dir="${1#--output-dir=}"; shift ;;
    --version) version="${2:-}"; shift 2 ;;
    --version=*) version="${1#--version=}"; shift ;;
    --signing-key) signing_key="${2:-}"; shift 2 ;;
    --signing-key=*) signing_key="${1#--signing-key=}"; shift ;;
    --allow-unsigned) allow_unsigned=true; shift ;;
    --keep-workdir) keep_workdir=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$version" ] || version="$(date -u +%Y%m%dT%H%M%SZ)"
case "$version" in
  *[!A-Za-z0-9._-]*|"") die "--version must contain only A-Z, a-z, 0-9, dot, underscore, or dash" ;;
esac

need_file "--kernel" "$kernel"
need_file "--dtb" "$dtb"
need_file "--stitching-bin" "$stitching_bin"
need_dir "--sdk-headers" "$sdk_headers"
need_file "--license-manifest" "$license_manifest"

if [ -z "$signing_key" ] && [ "$allow_unsigned" != true ]; then
  die "--signing-key is required for production packages; pass --allow-unsigned only for local validation"
fi
if [ -n "$signing_key" ]; then
  need_file "--signing-key" "$signing_key"
  command -v cosign >/dev/null 2>&1 || die "cosign not found; set PATH or pass --allow-unsigned for local validation"
fi

command -v sha256sum >/dev/null 2>&1 || die "sha256sum not found"
command -v tar >/dev/null 2>&1 || die "tar not found"

mkdir -p "$output_dir"
workdir="$(mktemp -d)"
package_name="case4-qcs6490-bsp-sdk-$version"
package_root="$workdir/$package_name"

cleanup() {
  if [ "$keep_workdir" = true ]; then
    log "kept workdir: $workdir"
  else
    rm -rf "$workdir"
  fi
}
trap cleanup EXIT

mkdir -p "$package_root/boot" \
  "$package_root/usr/bin" \
  "$package_root/sdk/include" \
  "$package_root/licenses"

copy_mode "$kernel" "$package_root/boot/Image" 0644
copy_mode "$dtb" "$package_root/boot/radxa-dragon-q6a.dtb" 0644
copy_mode "$stitching_bin" "$package_root/usr/bin/uvc-stitching" 0755
find "$sdk_headers" -maxdepth 1 -type f -name '*.h' -print0 |
  while IFS= read -r -d '' header; do
    copy_mode "$header" "$package_root/sdk/include/$(basename "$header")" 0644
  done
copy_mode "$license_manifest" "$package_root/licenses/LICENSES.spdx.json" 0644
write_runbook "$package_root/RUNBOOK.md"

header_count="$(find "$package_root/sdk/include" -maxdepth 1 -type f -name '*.h' | wc -l | tr -d ' ')"
[ "$header_count" -gt 0 ] || die "no SDK headers copied from $sdk_headers"

kernel_sha="$(sha256_file "$package_root/boot/Image")"
dtb_sha="$(sha256_file "$package_root/boot/radxa-dragon-q6a.dtb")"
stitching_sha="$(sha256_file "$package_root/usr/bin/uvc-stitching")"
licenses_sha="$(sha256_file "$package_root/licenses/LICENSES.spdx.json")"
write_manifest "$package_root/manifest.json" "$version" "$kernel_sha" "$dtb_sha" "$stitching_sha" "$licenses_sha"

(
  cd "$package_root"
  find . -type f ! -name CHECKSUMS.sha256 -print |
    LC_ALL=C sort | sed 's#^\./##' |
    xargs sha256sum >CHECKSUMS.sha256
)

archive="$output_dir/$package_name.tar.gz"
tar -C "$workdir" --sort=name --owner=0 --group=0 --numeric-owner \
  -czf "$archive" "$package_name"
sha256sum "$archive" >"$archive.sha256"

if [ -n "$signing_key" ]; then
  cosign sign-blob --key "$signing_key" --output-signature "$archive.sig" "$archive"
  log "wrote signed BSP package: $archive"
else
  log "wrote unsigned local BSP package: $archive"
fi
