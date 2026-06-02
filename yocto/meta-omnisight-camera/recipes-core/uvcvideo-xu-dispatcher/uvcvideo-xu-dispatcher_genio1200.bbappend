# OP-1962 / P0.C.2d: MediaTek Genio 1200 overlay for uvcvideo-xu-dispatcher.
# Keep this file disjoint from Rockchip bbappends; meta-mediatek owns the BSP
# kernel defconfig fragments that are staged through virtual/kernel below.

MEDIATEK_GENIO1200_TARGET_TRIPLE ?= "aarch64-linux-gnu"
MEDIATEK_GENIO1200_SYSROOT ?= "${RECIPE_SYSROOT}"

COMPATIBLE_MACHINE_genio1200 = "genio1200"

PACKAGE_ARCH_genio1200 = "${MACHINE_ARCH}"

DEPENDS_append_genio1200 = " virtual/kernel"

do_configure[depends]_append_genio1200 = " virtual/kernel:do_shared_workdir"

export MEDIATEK_GENIO1200_TARGET_TRIPLE
export MEDIATEK_GENIO1200_SYSROOT

EXTRA_OECMAKE_append_genio1200 = " -DOMNISIGHT_CAMERA_SOC=genio1200 -DOMNISIGHT_CAMERA_TARGET_TRIPLE=${MEDIATEK_GENIO1200_TARGET_TRIPLE} -DOMNISIGHT_CAMERA_SYSROOT=${MEDIATEK_GENIO1200_SYSROOT}"

TARGET_CFLAGS_append_genio1200 = " --sysroot=${MEDIATEK_GENIO1200_SYSROOT}"
TARGET_LDFLAGS_append_genio1200 = " --sysroot=${MEDIATEK_GENIO1200_SYSROOT}"
