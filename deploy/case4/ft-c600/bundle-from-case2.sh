#!/usr/bin/env bash
# [OP-1955] Build the Case 4 FT-C600 BSP/SDK reference bundle from Case 2.

set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: bundle-from-case2.sh --case2-root PATH [options]

Required:
  --case2-root PATH       local checkout of limit5/UVCCamera_Qt

Options:
  --out-dir PATH          output directory (default: dist/case4/ft-c600)
  --bundle-version VALUE  version label (default: git short SHA or timestamp)
  --artifact-glob GLOB    Case 2 artifact glob, relative to --case2-root
                           (default: build/UVCCamera*)
  --signing-key PATH      private key for openssl sha256 signature
  --unsigned              write archive + sha256 only; explicit local smoke mode
  -h, --help              show this help

Environment:
  CASE2_UVCCAMERA_QT_ROOT same as --case2-root
  BSP_BUNDLE_SIGNING_KEY  same as --signing-key
EOF
}

die() {
	printf 'bundle-from-case2: %s\n' "$*" >&2
	exit 1
}

need_cmd() {
	command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

json_escape() {
	python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"
}

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
bundle_src_dir="$repo_root/deploy/case4/ft-c600"
case2_root="${CASE2_UVCCAMERA_QT_ROOT:-}"
out_dir="$repo_root/dist/case4/ft-c600"
bundle_version=""
artifact_glob="build/UVCCamera*"
signing_key="${BSP_BUNDLE_SIGNING_KEY:-}"
unsigned=0

while (($# > 0)); do
	case "$1" in
		--case2-root)
			(($# >= 2)) || die "--case2-root requires a value"
			case2_root="$2"
			shift 2
			;;
		--out-dir)
			(($# >= 2)) || die "--out-dir requires a value"
			out_dir="$2"
			shift 2
			;;
		--bundle-version)
			(($# >= 2)) || die "--bundle-version requires a value"
			bundle_version="$2"
			shift 2
			;;
		--artifact-glob)
			(($# >= 2)) || die "--artifact-glob requires a value"
			artifact_glob="$2"
			shift 2
			;;
		--signing-key)
			(($# >= 2)) || die "--signing-key requires a value"
			signing_key="$2"
			shift 2
			;;
		--unsigned)
			unsigned=1
			shift
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

[[ -n "$case2_root" ]] || die "--case2-root or CASE2_UVCCAMERA_QT_ROOT is required"
[[ -d "$case2_root" ]] || die "Case 2 root does not exist: $case2_root"
[[ -f "$bundle_src_dir/LICENSES.spdx.json" ]] || die "missing LICENSES.spdx.json"
[[ -d "$bundle_src_dir/sdk-headers" ]] || die "missing sdk-headers directory"
if [[ "$unsigned" -eq 0 ]]; then
	[[ -n "$signing_key" ]] || die "--signing-key or BSP_BUNDLE_SIGNING_KEY is required; pass --unsigned only for local smoke"
	[[ -f "$signing_key" ]] || die "signing key does not exist: $signing_key"
fi

need_cmd find
need_cmd python3
need_cmd sha256sum
need_cmd tar
if [[ "$unsigned" -eq 0 ]]; then
	need_cmd openssl
fi

case2_abs="$(CDPATH= cd -- "$case2_root" && pwd)"
out_abs="$(mkdir -p "$out_dir" && CDPATH= cd -- "$out_dir" && pwd)"
workdir="$(mktemp -d)"
cleanup() {
	rm -rf "$workdir"
}
trap cleanup EXIT

if [[ -z "$bundle_version" ]]; then
	if git -C "$case2_abs" rev-parse --short=12 HEAD >/dev/null 2>&1; then
		bundle_version="case2-$(git -C "$case2_abs" rev-parse --short=12 HEAD)"
	else
		bundle_version="case2-$(date -u +%Y%m%d%H%M%S)"
	fi
fi

case2_ref="unknown"
if git -C "$case2_abs" rev-parse HEAD >/dev/null 2>&1; then
	case2_ref="$(git -C "$case2_abs" rev-parse HEAD)"
fi

mapfile -t artifacts < <(
	cd "$case2_abs"
	find . -path "./$artifact_glob" -type f -print | sort
)
if ((${#artifacts[@]} == 0)); then
	die "no Case 2 artifacts matched '$artifact_glob' under $case2_abs"
fi

bundle_name="omnisight-case4-ft-c600-bsp-${bundle_version}"
bundle_root="$workdir/$bundle_name"
mkdir -p \
	"$bundle_root/bin/uvccamera_qt" \
	"$bundle_root/bsp/dtb" \
	"$bundle_root/bsp/kernel" \
	"$bundle_root/checksums" \
	"$bundle_root/licenses" \
	"$bundle_root/runbook" \
	"$bundle_root/sdk/include" \
	"$bundle_root/stitching"

cp "$bundle_src_dir"/sdk-headers/*.h "$bundle_root/sdk/include/"
cp "$bundle_src_dir/LICENSES.spdx.json" "$bundle_root/licenses/LICENSES.spdx.json"

for artifact in "${artifacts[@]}"; do
	rel="${artifact#./}"
	dest="$bundle_root/bin/uvccamera_qt/${rel//\//__}"
	cp "$case2_abs/$rel" "$dest"
done

cat >"$bundle_root/bsp/kernel/README.txt" <<'EOF'
FT-C600 Case 4 reference bundle:

This deliverable re-exports the Case 2 UVCCamera_Qt artifacts for the
Fullhan MC6358 / FT-C600 EVK. It does not introduce a new kernel image.
Use the customer BSP already validated for the Case 2 FT-C600 hardware.
EOF

cat >"$bundle_root/bsp/dtb/README.txt" <<'EOF'
FT-C600 Case 4 reference bundle:

No new DTB is emitted by this reference package. Keep using the Case 2
FT-C600 board configuration until a hardware-specific C4-E stitching
ticket replaces this bundle with a native BSP image.
EOF

cat >"$bundle_root/runbook/README.txt" <<'EOF'
FT-C600 customer bring-up checklist:

1. Flash the existing Case 2 FT-C600 BSP image that matches the shipped
   UVCCamera_Qt artifact.
2. Install bin/uvccamera_qt/* onto the customer application partition.
3. Install sdk/include/*.h into the application SDK include path.
4. Confirm /run/uvc-xu-dispatcher.sock exists after boot.
5. Run the Phase 0 dispatcher lookup smoke with vendor_id=ft-c600.
6. Record the archive .sha256 and .sig files in the customer delivery log.
EOF

cat >"$bundle_root/stitching/pipeline.json" <<EOF
{
  "case": "case4-phase1a-c4-e",
  "evk": "ft-c600",
  "mode": "case2-reference-reexport",
  "source_repo": "https://github.com/limit5/UVCCamera_Qt.git",
  "source_ref": $(json_escape "$case2_ref"),
  "dispatcher_guid_token": "ft-c600-xu-guid",
  "notes": [
    "This bundle preserves the Case 2 single-device UVCCamera_Qt path.",
    "Native multi-camera stitching remains a later C4-E hardware exercise."
  ]
}
EOF

{
	printf '{\n'
	printf '  "bundle": %s,\n' "$(json_escape "$bundle_name")"
	printf '  "evk": "ft-c600",\n'
	printf '  "case2_repo": "https://github.com/limit5/UVCCamera_Qt.git",\n'
	printf '  "case2_ref": %s,\n' "$(json_escape "$case2_ref")"
	printf '  "artifacts": [\n'
	for i in "${!artifacts[@]}"; do
		rel="${artifacts[$i]#./}"
		comma=","
		[[ "$i" == "$((${#artifacts[@]} - 1))" ]] && comma=""
		printf '    %s%s\n' "$(json_escape "$rel")" "$comma"
	done
	printf '  ]\n'
	printf '}\n'
} >"$bundle_root/manifest.json"

(
	cd "$bundle_root"
	find . -type f -print0 | sort -z | xargs -0 sha256sum
) >"$bundle_root/checksums/SHA256SUMS"

archive="$out_abs/${bundle_name}.tar.gz"
tar --sort=name \
	--mtime='UTC 2026-06-03' \
	--owner=0 --group=0 --numeric-owner \
	-C "$workdir" -czf "$archive" "$bundle_name"
sha256sum "$archive" >"$archive.sha256"

if [[ "$unsigned" -eq 0 ]]; then
	openssl dgst -sha256 -sign "$signing_key" -out "$archive.sig" "$archive"
fi

printf 'bundle_archive=%s\n' "$archive"
printf 'bundle_sha256=%s\n' "$archive.sha256"
if [[ "$unsigned" -eq 0 ]]; then
	printf 'bundle_signature=%s\n' "$archive.sig"
fi
