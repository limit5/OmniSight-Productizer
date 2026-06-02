# RV1126 uses the 32-bit ARMv7-A Cortex-A7 Linaro toolchain from P0.A.1b.
# Keep this file disjoint from RK35xx aarch64 bbappends.

COMPATIBLE_MACHINE:rv1126 = "rv1126"

TARGET_CC_ARCH:append:rv1126 = " -march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard -mcpu=cortex-a7"

OMNISIGHT_CAMERA_SOC:rv1126 = "rv1126"
OMNISIGHT_CAMERA_TARGET_TRIPLE:rv1126 = "arm-linux-gnueabihf"
OMNISIGHT_CAMERA_SYSROOT:rv1126 = "/opt/omnisight/toolchains/rockchip-rv1126-armhf/sysroot"
