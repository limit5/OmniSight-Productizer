#!/usr/bin/env bash
# [OP-1942] Yocto scarthgap smoke for the OmniSight camera layer.

set -euo pipefail

usage() {
	cat <<'EOF'
usage: tools/embedded/yocto_smoke.sh

Environment:
  YOCTO_SMOKE_WORKDIR          default: /tmp/omnisight-yocto-smoke
  YOCTO_SMOKE_CAMERA_LAYER     default: $REPO/meta-omnisight-camera
  YOCTO_SMOKE_ROCKCHIP_LAYER   default: $REPO/meta-rockchip
  YOCTO_SMOKE_MACHINE          default: rk3588
EOF
}

repo_root() {
	cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

skip_or_fail() {
	local message="$1"
	if [[ "${CI:-}" == "true" || "${CI:-}" == "1" ]]; then
		printf 'SKIP: %s\n' "$message"
		exit 0
	fi
	printf 'ERROR: %s\n' "$message" >&2
	exit 2
}

require_path() {
	local path="$1"
	local label="$2"
	[[ -e "$path" ]] || skip_or_fail "$label not found at $path"
}

case "${1:-}" in
	-h|--help)
		usage
		exit 0
		;;
	"")
		;;
	*)
		usage >&2
		exit 2
		;;
esac

REPO_ROOT="$(repo_root)"
WORKDIR="${YOCTO_SMOKE_WORKDIR:-/tmp/omnisight-yocto-smoke}"
POKY_URL="${YOCTO_SMOKE_POKY_URL:-https://git.yoctoproject.org/poky}"
POKY_BRANCH="${YOCTO_SMOKE_POKY_BRANCH:-scarthgap}"
POKY_DIR="${WORKDIR}/poky"
BUILD_DIR="${WORKDIR}/build-rk3588"
CAMERA_LAYER="${YOCTO_SMOKE_CAMERA_LAYER:-${REPO_ROOT}/meta-omnisight-camera}"
ROCKCHIP_LAYER="${YOCTO_SMOKE_ROCKCHIP_LAYER:-${REPO_ROOT}/meta-rockchip}"
MACHINE="${YOCTO_SMOKE_MACHINE:-rk3588}"
IMAGE="core-image-minimal"
DISPATCHER_PATH="./usr/bin/uvcvideo-xu-dispatcher"

require_path "$CAMERA_LAYER" "meta-omnisight-camera layer"
require_path "$ROCKCHIP_LAYER" "meta-rockchip layer"

mkdir -p "$WORKDIR"
if [[ "${CI:-}" == "true" || "${CI:-}" == "1" ]] && [[ ! -d "$POKY_DIR/.git" ]]; then
	skip_or_fail "poky scarthgap checkout not installed at $POKY_DIR"
fi

if [[ ! -d "$POKY_DIR/.git" ]]; then
	git clone --depth 1 --branch "$POKY_BRANCH" "$POKY_URL" "$POKY_DIR"
else
	git -C "$POKY_DIR" fetch --depth 1 origin "$POKY_BRANCH"
	git -C "$POKY_DIR" checkout "$POKY_BRANCH"
	git -C "$POKY_DIR" reset --hard "origin/$POKY_BRANCH"
fi

require_path "$POKY_DIR/oe-init-build-env" "poky oe-init-build-env"

set +u
# shellcheck disable=SC1091
source "$POKY_DIR/oe-init-build-env" "$BUILD_DIR"
set -u

if ! grep -Fq "$CAMERA_LAYER" conf/bblayers.conf; then
	bitbake-layers add-layer "$CAMERA_LAYER"
fi
if ! grep -Fq "$ROCKCHIP_LAYER" conf/bblayers.conf; then
	bitbake-layers add-layer "$ROCKCHIP_LAYER"
fi

if grep -q '^MACHINE[[:space:]]*=' conf/local.conf; then
	sed -i.bak -E "s/^MACHINE[[:space:]]*=.*/MACHINE = \"${MACHINE}\"/" conf/local.conf
else
	printf '\nMACHINE = "%s"\n' "$MACHINE" >>conf/local.conf
fi

bitbake "$IMAGE"

ROOTFS_TAR="${BUILD_DIR}/tmp/deploy/images/${MACHINE}/${IMAGE}-${MACHINE}.rootfs.tar.xz"
require_path "$ROOTFS_TAR" "rootfs tarball"

if tar -tf "$ROOTFS_TAR" | grep -Eq "^${DISPATCHER_PATH#./}$|^${DISPATCHER_PATH}$"; then
	printf 'OK: %s contains /usr/bin/uvcvideo-xu-dispatcher\n' "$ROOTFS_TAR"
	exit 0
fi

printf 'ERROR: /usr/bin/uvcvideo-xu-dispatcher missing from %s\n' "$ROOTFS_TAR" >&2
exit 1
