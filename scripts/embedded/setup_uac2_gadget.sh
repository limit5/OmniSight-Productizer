#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Configure the OmniSight USB UAC2 gadget through kernel configfs (OP-1985).
# ALSA PCM routing is intentionally left to C5.A.2.

set -eu

CONFIGFS_ROOT="${CONFIGFS_ROOT:-/sys/kernel/config}"
GADGET_ROOT="${GADGET_ROOT:-$CONFIGFS_ROOT/usb_gadget}"
GADGET_NAME="${GADGET_NAME:-omnisight-uac2}"
GADGET_DIR="$GADGET_ROOT/$GADGET_NAME"
CONFIG_NAME="${CONFIG_NAME:-c.1}"
FUNCTION_NAME="${FUNCTION_NAME:-uac2.usb0}"
UDC_NAME="${UDC_NAME:-}"
VALIDATOR="${VALIDATOR:-/usr/libexec/omnisight/uac2-config}"

ID_VENDOR="${ID_VENDOR:-0x1d6b}"
ID_PRODUCT="${ID_PRODUCT:-0x0104}"
BCD_DEVICE="${BCD_DEVICE:-0x0100}"
BCD_USB="${BCD_USB:-0x0200}"
MANUFACTURER="${MANUFACTURER:-OmniSight}"
PRODUCT="${PRODUCT:-OmniSight UAC2 Conference Audio}"
SERIAL="${SERIAL:-0001}"
CONFIG_LABEL="${CONFIG_LABEL:-UAC2 stereo audio}"
MAX_POWER_MA="${MAX_POWER_MA:-250}"

UAC_SAMPLE_RATE="${UAC_SAMPLE_RATE:-48000}"
UAC_SAMPLE_SIZE="${UAC_SAMPLE_SIZE:-2}"
UAC_CHANNEL_MASK="${UAC_CHANNEL_MASK:-0x3}"

write_attr()
{
	path="$1"
	value="$2"

	if [ ! -e "$path" ]; then
		echo "setup_uac2_gadget: missing configfs attribute $path" >&2
		exit 1
	fi

	printf '%s\n' "$value" > "$path"
}

mount_configfs()
{
	if ! grep -qs " $CONFIGFS_ROOT configfs " /proc/mounts; then
		mkdir -p "$CONFIGFS_ROOT"
		mount -t configfs configfs "$CONFIGFS_ROOT"
	fi
}

load_modules()
{
	modprobe libcomposite
	modprobe usb_f_uac2
}

pick_udc()
{
	if [ -n "$UDC_NAME" ]; then
		printf '%s\n' "$UDC_NAME"
		return
	fi

	for udc_path in /sys/class/udc/*; do
		[ -e "$udc_path" ] || continue
		basename "$udc_path"
		return
	done
}

configure_gadget()
{
	udc="$1"

	if [ -z "$udc" ]; then
		echo "setup_uac2_gadget: no USB device controller found" >&2
		exit 1
	fi

	mkdir -p "$GADGET_DIR"
	write_attr "$GADGET_DIR/idVendor" "$ID_VENDOR"
	write_attr "$GADGET_DIR/idProduct" "$ID_PRODUCT"
	write_attr "$GADGET_DIR/bcdDevice" "$BCD_DEVICE"
	write_attr "$GADGET_DIR/bcdUSB" "$BCD_USB"

	mkdir -p "$GADGET_DIR/strings/0x409"
	write_attr "$GADGET_DIR/strings/0x409/serialnumber" "$SERIAL"
	write_attr "$GADGET_DIR/strings/0x409/manufacturer" "$MANUFACTURER"
	write_attr "$GADGET_DIR/strings/0x409/product" "$PRODUCT"

	mkdir -p "$GADGET_DIR/configs/$CONFIG_NAME/strings/0x409"
	write_attr "$GADGET_DIR/configs/$CONFIG_NAME/strings/0x409/configuration" \
		"$CONFIG_LABEL"
	write_attr "$GADGET_DIR/configs/$CONFIG_NAME/MaxPower" "$MAX_POWER_MA"

	mkdir -p "$GADGET_DIR/functions/$FUNCTION_NAME"
	write_attr "$GADGET_DIR/functions/$FUNCTION_NAME/c_chmask" "$UAC_CHANNEL_MASK"
	write_attr "$GADGET_DIR/functions/$FUNCTION_NAME/p_chmask" "$UAC_CHANNEL_MASK"
	write_attr "$GADGET_DIR/functions/$FUNCTION_NAME/c_srate" "$UAC_SAMPLE_RATE"
	write_attr "$GADGET_DIR/functions/$FUNCTION_NAME/p_srate" "$UAC_SAMPLE_RATE"
	write_attr "$GADGET_DIR/functions/$FUNCTION_NAME/c_ssize" "$UAC_SAMPLE_SIZE"
	write_attr "$GADGET_DIR/functions/$FUNCTION_NAME/p_ssize" "$UAC_SAMPLE_SIZE"

	if [ ! -e "$GADGET_DIR/configs/$CONFIG_NAME/$FUNCTION_NAME" ]; then
		ln -s "../../functions/$FUNCTION_NAME" \
			"$GADGET_DIR/configs/$CONFIG_NAME/$FUNCTION_NAME"
	fi

	current_udc="$(cat "$GADGET_DIR/UDC" 2>/dev/null || true)"
	if [ -z "$current_udc" ]; then
		printf '%s\n' "$udc" > "$GADGET_DIR/UDC"
	elif [ "$current_udc" != "$udc" ]; then
		echo "setup_uac2_gadget: gadget already bound to $current_udc" >&2
		exit 1
	fi
}

validate_gadget()
{
	if [ ! -x "$VALIDATOR" ]; then
		echo "setup_uac2_gadget: validator $VALIDATOR not installed; skipping" >&2
		return
	fi

	"$VALIDATOR" \
		--configfs-root "$GADGET_ROOT" \
		--gadget "$GADGET_NAME" \
		--function "$FUNCTION_NAME" \
		--config "$CONFIG_NAME"
}

mount_configfs
load_modules
configure_gadget "$(pick_udc)"
validate_gadget
