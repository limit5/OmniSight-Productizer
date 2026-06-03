# Qualcomm QCS6490 uses the aarch64 toolchain/sysroot from P0.A.2.
# Keep this file disjoint from RK35xx and RV1126 bbappends.

COMPATIBLE_MACHINE_qcs6490 = "qcs6490"

PACKAGE_ARCH_qcs6490 = "${MACHINE_ARCH}"

OMNISIGHT_CAMERA_SOC_qcs6490 = "qcs6490"
OMNISIGHT_CAMERA_TARGET_TRIPLE_qcs6490 = "aarch64-linux-gnu"
OMNISIGHT_CAMERA_SYSROOT_qcs6490 = "/opt/omnisight/toolchains/qualcomm-qcs6490-aarch64/sysroot"

TARGET_CFLAGS_append_qcs6490 = " --sysroot=${OMNISIGHT_CAMERA_SYSROOT}"
TARGET_LDFLAGS_append_qcs6490 = " --sysroot=${OMNISIGHT_CAMERA_SYSROOT}"
