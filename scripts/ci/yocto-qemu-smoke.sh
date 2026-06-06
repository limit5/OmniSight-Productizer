#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-omnisight-camera-image}"
MACHINE_NAME="${MACHINE_NAME:-qemuarm64}"

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel)}"
POKY_DIR="${POKY_DIR:-${REPO_ROOT}/poky}"
META_OPENEMBEDDED_DIR="${META_OPENEMBEDDED_DIR:-${REPO_ROOT}/meta-openembedded}"
OMNISIGHT_LAYER_DIR="${OMNISIGHT_LAYER_DIR:-${REPO_ROOT}/yocto/meta-omnisight-camera}"
BUILD_DIR="${BUILD_DIR:-${REPO_ROOT}/build-${MACHINE_NAME}}"
SSTATE_DIR="${SSTATE_DIR:-${REPO_ROOT}/.yocto-cache/sstate-cache}"
DL_DIR="${DL_DIR:-${REPO_ROOT}/.yocto-cache/downloads}"

for required_dir in "$POKY_DIR" "$META_OPENEMBEDDED_DIR" "$OMNISIGHT_LAYER_DIR"; do
	if [ ! -d "$required_dir" ]; then
		echo "missing required directory: $required_dir" >&2
		exit 1
	fi
done

mkdir -p "$BUILD_DIR" "$SSTATE_DIR" "$DL_DIR"

# shellcheck source=/dev/null
source "$POKY_DIR/oe-init-build-env" "$BUILD_DIR" >/dev/null

bitbake-layers add-layer \
	"$META_OPENEMBEDDED_DIR/meta-oe" \
	"$OMNISIGHT_LAYER_DIR"

cat >>conf/local.conf <<EOF

# OP-2084 qemuarm64 Yocto smoke gate.
MACHINE = "${MACHINE_NAME}"
SSTATE_DIR = "${SSTATE_DIR}"
DL_DIR = "${DL_DIR}"
EOF

bitbake "$IMAGE_NAME"

IMAGE_ROOTFS="$(bitbake -e "$IMAGE_NAME" | sed -n 's/^IMAGE_ROOTFS="\(.*\)"$/\1/p' | tail -n 1)"
DEPLOY_DIR_IMAGE="$(bitbake -e "$IMAGE_NAME" | sed -n 's/^DEPLOY_DIR_IMAGE="\(.*\)"$/\1/p' | tail -n 1)"

if [ -z "$IMAGE_ROOTFS" ] || [ ! -d "$IMAGE_ROOTFS" ]; then
	echo "image rootfs was not produced for $IMAGE_NAME" >&2
	exit 1
fi

if [ -z "$DEPLOY_DIR_IMAGE" ] || ! find "$DEPLOY_DIR_IMAGE" -maxdepth 1 -type f -name "${IMAGE_NAME}-${MACHINE_NAME}.*" | grep -q .; then
	echo "deploy image artifact was not produced for $IMAGE_NAME on $MACHINE_NAME" >&2
	exit 1
fi

if [ ! -x "$IMAGE_ROOTFS/usr/bin/uvc-xu-dispatcher" ] && \
	[ ! -x "$IMAGE_ROOTFS/usr/bin/uvcvideo-xu-dispatcher" ]; then
	echo "dispatcher binary was not installed into $IMAGE_ROOTFS/usr/bin" >&2
	exit 1
fi

echo "Yocto qemuarm64 smoke passed: $IMAGE_NAME rootfs and dispatcher binary produced"
