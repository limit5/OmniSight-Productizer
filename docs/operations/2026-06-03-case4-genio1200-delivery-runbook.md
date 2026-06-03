# Case 4 Genio 1200 delivery runbook -- MediaTek Genio 1200-EVK BSP handoff

**Date filed:** 2026-06-03
**Ticket:** OP-1972 (Case 4 Phase 1A / C4-D.C)
**Parent:** OP-1926 (Case 4 META; C4-D per-EVK Sub-EPIC to be filed)
**Status:** customer-runnable template; physical-board evidence attaches after
post-HIL MediaTek Genio 1200-EVK smoke

---

## Why this runbook exists

Case 4 Phase 1A C4-D delivers the MediaTek Genio 1200-EVK customer BSP package
as a per-EVK artifact, not as a shared MediaTek bundle. This runbook closes the
C4-D.C delivery lane by defining the customer delivery package, the
cosign/checksum signing step, the `mtk-brom` / `genio-flash` handoff, and the
customer-side bring-up acceptance checklist.

It mirrors the C4-A/B/E delivery shape and the Case 2 T2C.2 handoff pattern:
the productizer records the operator-verifiable delivery contract in docs,
while the concrete artifact action is a small shell helper.

**Scope lock:** this runbook is MediaTek Genio 1200-EVK only. Do not reuse it
for ATK-DLRK3588, ATK-DLRV1126, Radxa Dragon Q6A, FT-C600, or another
MediaTek board without filing that EVK's own C4-X.C ticket.

---

## Delivery package contract

The C4-D BSP delivery directory is named:

```text
omnisight-case4-mediatek-genio1200-bsp-<version>/
```

It contains:

```text
flash/
  genio1200.wic.img
  mtk-brom-preloader.bin
  genio-flash.json
bsp/
  kernel/Image
  dtb/genio1200-evk.dtb
  rootfs/rootfs.ext4
  modules/
stitching/
  bin/omnisight-genio1200-stitch
  config/genio1200-evk-cameras.yaml
sdk/
  include/
  lib/
  samples/
licenses/
  SPDX.json
  vendor-blobs.md
acceptance/
  bringup-checklist.md
```

Archive the directory as:

```bash
tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 \
  --numeric-owner \
  -C "${RELEASE_ROOT}" \
  -czf "omnisight-case4-mediatek-genio1200-bsp-${VERSION}.tar.gz" \
  "omnisight-case4-mediatek-genio1200-bsp-${VERSION}"
```

The archive must remain EVK-specific. Do not add Rockchip, Qualcomm, or FT-C600
artifacts to this package.

---

## Step 0 -- Pre-flight inventory

Confirm the operator host has the required tools:

```bash
command -v sha256sum
command -v cosign
command -v mtk-brom
command -v genio-flash
command -v stty
```

Confirm the unsigned inputs are present:

```bash
test -f "${BSP_ARCHIVE}"
test -f "${FLASH_IMAGE}"
test -f "${MTK_BROM_PRELOADER}"
test -f "${GENIO_FLASH_CONFIG}"
```

Expected names:

| artifact | expected filename |
|---|---|
| BSP archive | `omnisight-case4-mediatek-genio1200-bsp-<version>.tar.gz` |
| Flash image | `genio1200.wic.img` |
| MediaTek BootROM preloader | `mtk-brom-preloader.bin` |
| Genio flash config | `genio-flash.json` |

If any required path is missing, stop the delivery and fix the C4-D packaging
ticket before flashing hardware. Do not invent substitute paths in the
customer archive.

---

## Step 1 -- Sign and checksum the delivery artifacts

Use the self-managed cosign key. Do not inline the password into the command
or into any committed file.

```bash
export COSIGN_PASSWORD="$(cat ~/.config/omnisight/cosign/cosign-password.txt)"

tools/case4/sign_bsp_genio1200.sh \
  --evk mediatek-genio1200 \
  --version "${VERSION}" \
  --archive "${BSP_ARCHIVE}" \
  --image "${FLASH_IMAGE}" \
  --out-dir "${DELIVERY_OUT}" \
  --cosign-key ~/.config/omnisight/cosign/cosign.key \
  --public-key deploy/cosign/cosign.pub
```

Expected output files:

```text
${DELIVERY_OUT}/omnisight-case4-mediatek-genio1200-bsp-<version>.tar.gz
${DELIVERY_OUT}/genio1200.wic.img
${DELIVERY_OUT}/mediatek-genio1200-delivery-manifest.json
${DELIVERY_OUT}/SHA256SUMS
${DELIVERY_OUT}/omnisight-case4-mediatek-genio1200-bsp-<version>.tar.gz.sig
${DELIVERY_OUT}/genio1200.wic.img.sig
${DELIVERY_OUT}/cosign-verify.log
${DELIVERY_OUT}/cosign.pub
```

Failure policy:

- Missing `COSIGN_PASSWORD` means the operator has not loaded the key secret;
  stop and fix local signing setup.
- Missing `deploy/cosign/cosign.pub` means the verification key was not
  checked out; stop and restore the repository state.
- Any `cosign verify-blob` failure blocks delivery. Do not hand off an
  archive that only has SHA256 without a valid signature.

---

## Step 2 -- Stage the customer bundle

`tools/case4/sign_bsp_genio1200.sh` stages the archive, image, manifest,
checksums, signatures, and public key into `${DELIVERY_OUT}`. Copy only these
files from `${DELIVERY_OUT}` into the customer delivery location:

```text
omnisight-case4-mediatek-genio1200-bsp-<version>.tar.gz
genio1200.wic.img
mediatek-genio1200-delivery-manifest.json
SHA256SUMS
omnisight-case4-mediatek-genio1200-bsp-<version>.tar.gz.sig
genio1200.wic.img.sig
cosign.pub
```

Before upload, verify the checksums from inside the staged directory:

```bash
sha256sum -c SHA256SUMS
cosign verify-blob \
  --key cosign.pub \
  --signature "omnisight-case4-mediatek-genio1200-bsp-${VERSION}.tar.gz.sig" \
  "omnisight-case4-mediatek-genio1200-bsp-${VERSION}.tar.gz"
cosign verify-blob \
  --key cosign.pub \
  --signature genio1200.wic.img.sig \
  genio1200.wic.img
```

---

## Step 3 -- Enter MediaTek BootROM download mode

Use only the NDA-cleared MediaTek Genio 1200-EVK board manual for button
names, DIP-switch positions, and USB port selection. Keep board serials and
operator-local device names in environment variables:

```bash
export GENIO1200_UART_DEV=/dev/serial/by-id/${GENIO1200_UART_ID}
export MTK_BROM_USB_MATCH=${GENIO1200_BROM_USB_MATCH}
```

Bench sequence:

1. Disconnect Genio 1200-EVK power and USB download cable.
2. Connect serial UART to the operator host.
3. Hold the BootROM / download-mode control documented in the NDA-cleared
   board manual.
4. Connect the USB download-mode port, then apply power.
5. Release the control only after `mtk-brom` reports one matching BootROM
   target.

Check enumeration:

```bash
mtk-brom list --match "${MTK_BROM_USB_MATCH}"
```

Expected: exactly one MediaTek Genio 1200-EVK BootROM target. If multiple
MediaTek devices appear, unplug non-target boards and retry enumeration before
running any write command.

---

## Step 4 -- Flash the Genio 1200 image

Unpack the signed archive and flash with the vendor-approved Genio flow:

```bash
tar -xzf "omnisight-case4-mediatek-genio1200-bsp-${VERSION}.tar.gz" \
  -C "${BSP_ROOT}"

mtk-brom download \
  --match "${MTK_BROM_USB_MATCH}" \
  --preloader "${BSP_ROOT}/flash/mtk-brom-preloader.bin"

genio-flash \
  --config "${BSP_ROOT}/flash/genio-flash.json" \
  --image "${BSP_ROOT}/flash/genio1200.wic.img" \
  --target mediatek-genio1200 \
  --yes
```

Expected:

- `mtk-brom download` loads only the Genio 1200-EVK preloader from the signed
  BSP archive.
- `genio-flash` refuses any target id other than `mediatek-genio1200`.
- The flash command exits `0` only after the final write or verify step
  completes.
- The command does not require secrets, private URLs, or checked-in vendor
  credentials.

Do not use Qualcomm EDL/fastboot or Rockchip Maskrom commands here. Keeping the
BootROM loader, flash config, rootfs, and customer SDK on the Genio 1200
aarch64 lane is part of the delivery contract.

---

## Step 5 -- Customer bring-up acceptance checklist

Run this checklist on the flashed MediaTek Genio 1200-EVK before customer
handoff. Record the command output in the ticket or delivery evidence folder.

| check | command | pass condition |
|---|---|---|
| Board boots | serial console at the NDA-cleared baud rate | login prompt or shell prompt appears |
| Genio identity | `cat /proc/device-tree/compatible` | output includes a MediaTek Genio 1200 / EVK compatible string |
| Kernel image | `uname -a` | expected BSP kernel version is present |
| Flash provenance | `cat /etc/omnisight-release` | release id matches `${VERSION}` |
| Camera nodes | `ls /dev/video*` and media graph inspection if MIPI-CSI is used | expected camera count appears |
| USB inventory | `lsusb` when USB UVC cameras are attached | expected UVC camera VID:PID values appear |
| Dispatcher | `systemctl status uvcvideo-xu-dispatcher` | service is active or the bundled launcher reports healthy |
| Stitching binary | `omnisight-genio1200-stitch --version` | binary starts and prints the BSP version |
| Stitching dry run | `omnisight-genio1200-stitch --config /opt/omnisight/genio1200-evk-cameras.yaml --frames 30 --output /tmp/stitch-smoke.yuv` | exits 0 and writes non-empty output |
| Serial smoke | `tools/embedded/serial_smoke.sh --soc genio1200 --serial "${GENIO1200_UART_DEV}" --uvc-node /dev/video0 --dispatcher-socket /run/uvc-xu-dispatcher.sock --timeout-seconds 180` | UART prompt, camera enumeration, and vendor dispatch verified |
| Signature evidence | `sha256sum -c SHA256SUMS` and both `cosign verify-blob` commands | all verification commands exit 0 |

If the EVK is not on the HIL bench yet, mark only the real-board rows as
hardware-blocked and keep the signed bundle evidence. Do not mark C4-D.C
customer acceptance complete until the MediaTek Genio 1200-EVK rows above pass
on the physical board.

---

## Step 6 -- Customer acceptance handoff text

Include this short note with the delivery:

```text
This MediaTek Genio 1200-EVK package is EVK-specific. Verify SHA256SUMS,
verify both cosign signatures with cosign.pub, flash genio1200.wic.img through
the documented mtk-brom / genio-flash flow, then run
acceptance/bringup-checklist.md on the booted EVK. Report any signature
failure, BootROM download failure, boot failure, missing camera node, or
stitching dry-run failure before integrating the SDK.
```

---

## What this runbook DOES NOT do

- Does NOT build the Genio 1200 BSP, image, stitching binary, or SDK headers.
  Those are C4-D.A and C4-D.B deliverables.
- Does NOT cover ATK-DLRK3588, ATK-DLRV1126, Radxa Dragon Q6A, FT-C600, or
  any other MediaTek board.
- Does NOT bypass cosign verification for local-only delivery.
- Does NOT inline NDA-only MediaTek manual text, private vendor URLs, serial
  numbers, credentials, or tokens.
- Does NOT claim real-EVK acceptance from qemu or dry-run evidence.

---

## Cross-references

- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md`
  §4 -- Case 4 Phase 1A delivery structure.
- `docs/operations/2026-06-XX-embedded-bringup-sop.md` -- Phase 0 embedded
  bring-up SOP shape and physical-board evidence pattern.
- `docs/operations/2026-06-03-case4-rk3588-delivery-runbook.md` -- C4-A
  customer delivery package shape mirrored here.
- `tools/case4/sign_bsp_genio1200.sh` -- checksum and signature helper for
  this MediaTek Genio 1200-EVK customer archive.
