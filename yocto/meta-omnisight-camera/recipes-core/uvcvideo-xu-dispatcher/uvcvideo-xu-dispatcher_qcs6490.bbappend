# Qualcomm QCS6490 uses the aarch64 toolchain/sysroot from P0.A.2.
# Keep this file disjoint from RK35xx and RV1126 bbappends.

COMPATIBLE_MACHINE:qcs6490 = "qcs6490"

PACKAGE_ARCH:qcs6490 = "${MACHINE_ARCH}"

OMNISIGHT_CAMERA_SOC:qcs6490 = "qcs6490"
OMNISIGHT_CAMERA_TARGET_TRIPLE:qcs6490 = "aarch64-linux-gnu"
OMNISIGHT_CAMERA_SYSROOT:qcs6490 = "/opt/omnisight/toolchains/qualcomm-qcs6490-aarch64/sysroot"

TARGET_CFLAGS:append:qcs6490 = " --sysroot=${OMNISIGHT_CAMERA_SYSROOT}"
TARGET_LDFLAGS:append:qcs6490 = " --sysroot=${OMNISIGHT_CAMERA_SYSROOT}"
