#!/bin/bash
# Mock cross-compilation script for E2E testing.
# Simulates a firmware build process with stages and potential errors.

set -e

MANIFEST="${1:-test_fixtures/hardware_manifest.yaml}"
OUTPUT_DIR="build/output"
ERROR_MODE="${MOCK_ERROR:-none}"  # none | compile_error | link_error

echo "[BUILD] OmniSight Firmware Build System v1.0"
echo "[BUILD] Manifest: $MANIFEST"
echo "[BUILD] Target: aarch64-linux-gnu"
echo ""

# Stage 1: Parse manifest
echo "[1/5] Parsing hardware manifest..."
if [ ! -f "$MANIFEST" ]; then
    echo "[ERROR] Manifest not found: $MANIFEST"
    exit 1
fi
sleep 0.3
SENSOR=$(grep "model:" "$MANIFEST" | head -1 | awk -F'"' '{print $2}')
echo "       Sensor: $SENSOR"
echo "       [OK]"
echo ""

# Stage 2: Generate driver skeleton
echo "[2/5] Generating driver skeleton..."
mkdir -p "$OUTPUT_DIR"
sleep 0.3
cat > "$OUTPUT_DIR/imx335_driver.c" << 'CEOF'
/* Auto-generated IMX335 sensor driver */
#include <linux/module.h>
#include <linux/i2c.h>

#define IMX335_I2C_ADDR  0x1A
#define IMX335_REG_STANDBY  0x3000
#define IMX335_REG_STREAM   0x3001

static int imx335_probe(struct i2c_client *client) {
    dev_info(&client->dev, "IMX335 sensor probed at 0x%02x\n", client->addr);
    return 0;
}

static struct i2c_driver imx335_driver = {
    .driver = { .name = "imx335" },
    .probe = imx335_probe,
};
module_i2c_driver(imx335_driver);
MODULE_LICENSE("GPL");
CEOF
echo "       Generated: $OUTPUT_DIR/imx335_driver.c"
echo "       [OK]"
echo ""

# Stage 3: Compile
echo "[3/5] Compiling driver module..."
sleep 0.5
if [ "$ERROR_MODE" = "compile_error" ]; then
    echo "       $OUTPUT_DIR/imx335_driver.c:15:5: error: implicit declaration of function 'i2c_register_driver'"
    echo "       $OUTPUT_DIR/imx335_driver.c:15:5: error: [-Werror=implicit-function-declaration]"
    echo "[BUILD FAILED] Compilation error — 2 errors, 0 warnings"
    exit 2
fi
echo "       Compiling imx335_driver.c -> imx335_driver.o"
echo "       [OK] 0 errors, 0 warnings"
echo ""

# Stage 4: Link
echo "[4/5] Linking kernel module..."
sleep 0.3
if [ "$ERROR_MODE" = "link_error" ]; then
    echo "       ERROR: undefined reference to 'v4l2_subdev_init'"
    echo "[BUILD FAILED] Link error — missing v4l2 symbols"
    exit 3
fi
touch "$OUTPUT_DIR/imx335.ko"
echo "       Linked: $OUTPUT_DIR/imx335.ko (mock)"
echo "       [OK]"
echo ""

# Stage 5: Package
echo "[5/5] Packaging artifacts..."
sleep 0.2
echo "       imx335.ko         (kernel module)"
echo "       imx335_driver.c   (source)"
echo "       build.log         (this output)"
echo "       [OK]"
echo ""
echo "[BUILD COMPLETE] All stages passed."
echo "  Sensor:   $SENSOR"
echo "  Output:   $OUTPUT_DIR/"
echo "  Module:   imx335.ko"
