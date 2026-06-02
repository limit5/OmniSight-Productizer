# Case 4 RK3588 delivery runbook — ATK-DLRK3588 BSP handoff

**Date filed:** 2026-06-03
**Ticket:** OP-1950 (Case 4 Phase 1A / C4-A.C)
**Parent:** OP-1927 (C4-A ATK-DLRK3588 per-EVK Sub-EPIC)
**Status:** operator-runnable after the RK3588 BSP archive and flash image are built

---

## Why this runbook exists

Case 4 Phase 1A C4-A delivers the ATK-DLRK3588 customer BSP package as a
per-EVK artifact, not as a shared Rockchip bundle. This runbook closes
C4-A.C by defining the customer delivery package, the cosign/checksum
signing step, the flash path, and the customer-side bring-up acceptance
checklist.

It mirrors the Case 2 T2C.2 handoff pattern: the productizer records the
operator-verifiable delivery contract in docs, while the concrete
artifact action is a small shell helper. The existing Phase 0 substrate is
reused directly:

- `tools/embedded/flash_rk3588.sh` — ATK-DLRK3588 rkdeveloptool flash wrapper
- `tools/embedded/serial_smoke.sh` — UART prompt + UVC enumeration + vendor-dispatch smoke
- `tools/case4/sign_bsp.sh` — BSP delivery checksum, manifest, and cosign helper

---

## Delivery package contract

The C4-A BSP delivery directory is named:

```text
omnisight-case4-atk-dlrk3588-bsp-<version>/
```

It contains:

```text
flash/
  atk-dlrk3588.img
  rk3588-loader.bin
bsp/
  kernel/Image
  dtb/atk-dlrk3588.dtb
  rootfs/rootfs.ext4
  modules/
stitching/
  bin/omnisight-rk3588-stitch
  config/atk-dlrk3588-cameras.yaml
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
tar -C "${RELEASE_ROOT}" -czf \
  "omnisight-case4-atk-dlrk3588-bsp-${VERSION}.tar.gz" \
  "omnisight-case4-atk-dlrk3588-bsp-${VERSION}"
```

The archive must remain EVK-specific. Do not add RV1126, QCS6490,
Genio, or FT-C600 artifacts to this package.

---

## Step 0 — Pre-flight inventory

Confirm the operator host has the required tools:

```bash
command -v sha256sum
command -v cosign
command -v rkdeveloptool
command -v stty
```

Confirm the unsigned inputs are present:

```bash
test -f "${BSP_ARCHIVE}"
test -f "${FLASH_IMAGE}"
test -f "${RK3588_LOADER}"
```

Expected names:

| artifact | expected filename |
|---|---|
| BSP archive | `omnisight-case4-atk-dlrk3588-bsp-<version>.tar.gz` |
| Flash image | `atk-dlrk3588.img` |
| Rockchip loader | `rk3588-loader.bin` |

---

## Step 1 — Sign and checksum the delivery artifacts

Use the self-managed cosign key. Do not inline the password into the
command or into any committed file.

```bash
export COSIGN_PASSWORD="$(cat ~/.config/omnisight/cosign/cosign-password.txt)"

tools/case4/sign_bsp.sh \
  --evk atk-dlrk3588 \
  --version "${VERSION}" \
  --archive "${BSP_ARCHIVE}" \
  --image "${FLASH_IMAGE}" \
  --out-dir "${DELIVERY_OUT}" \
  --cosign-key ~/.config/omnisight/cosign/cosign.key \
  --public-key deploy/cosign/cosign.pub
```

Expected output files:

```text
${DELIVERY_OUT}/omnisight-case4-atk-dlrk3588-bsp-<version>.tar.gz
${DELIVERY_OUT}/atk-dlrk3588.img
${DELIVERY_OUT}/atk-dlrk3588-delivery-manifest.json
${DELIVERY_OUT}/SHA256SUMS
${DELIVERY_OUT}/omnisight-case4-atk-dlrk3588-bsp-<version>.tar.gz.sig
${DELIVERY_OUT}/atk-dlrk3588.img.sig
${DELIVERY_OUT}/cosign-verify.log
```

Failure policy:

- Missing `COSIGN_PASSWORD` means the operator has not loaded the key
  secret; stop and fix local signing setup.
- Missing `deploy/cosign/cosign.pub` means the verification key was not
  checked out; stop and restore the repository state.
- Any `cosign verify-blob` failure blocks delivery. Do not hand off an
  archive that only has SHA256 without a valid signature.

---

## Step 2 — Stage the customer bundle

`tools/case4/sign_bsp.sh` stages the archive, image, manifest,
checksums, signatures, and public key into `${DELIVERY_OUT}`. Copy only
these files from `${DELIVERY_OUT}` into the customer delivery location:

```text
omnisight-case4-atk-dlrk3588-bsp-<version>.tar.gz
atk-dlrk3588.img
atk-dlrk3588-delivery-manifest.json
SHA256SUMS
omnisight-case4-atk-dlrk3588-bsp-<version>.tar.gz.sig
atk-dlrk3588.img.sig
cosign.pub
```

Before upload, verify the checksums from inside the staged directory:

```bash
sha256sum -c SHA256SUMS
cosign verify-blob \
  --key cosign.pub \
  --signature "omnisight-case4-atk-dlrk3588-bsp-${VERSION}.tar.gz.sig" \
  "omnisight-case4-atk-dlrk3588-bsp-${VERSION}.tar.gz"
cosign verify-blob \
  --key cosign.pub \
  --signature atk-dlrk3588.img.sig \
  atk-dlrk3588.img
```

---

## Step 3 — Flash the ATK-DLRK3588 EVK

Put the board into Maskrom mode:

1. Disconnect ATK-DLRK3588 power and USB OTG.
2. Hold the board recovery or Maskrom button.
3. Connect USB OTG to the flashing host, then apply power.
4. Release the button only after `rkdeveloptool ld` shows a Maskrom line.

Flash with the Phase 0 wrapper:

```bash
rkdeveloptool ld

tools/embedded/flash_rk3588.sh \
  --device "${RKDEVELOPTOOL_LD_MATCH}" \
  --loader "${RK3588_LOADER}" \
  --image "${FLASH_IMAGE}" \
  flash
```

`RKDEVELOPTOOL_LD_MATCH` must be a unique substring of the connected
ATK-DLRK3588 Maskrom line. The wrapper refuses to auto-select among
multiple USB devices.

---

## Step 4 — Customer bring-up acceptance checklist

Run this checklist on the flashed ATK-DLRK3588 before customer handoff.
Record the command output in the ticket or delivery evidence folder.

| check | command | pass condition |
|---|---|---|
| Board boots | serial console at 115200 baud | login prompt or shell prompt appears |
| RK3588 identity | `cat /proc/device-tree/compatible` | output includes an RK3588 board/device-tree compatible string |
| Kernel image | `uname -a` | expected BSP kernel version is present |
| UVC devices | `ls /dev/video*` | expected camera count appears |
| USB inventory | `lsusb` | attached UVC camera VID:PID values appear |
| Dispatcher | `systemctl status uvcvideo-xu-dispatcher` | service is active or the bundled launcher reports healthy |
| Stitching binary | `omnisight-rk3588-stitch --version` | binary starts and prints the BSP version |
| Stitching dry run | `omnisight-rk3588-stitch --config /opt/omnisight/atk-dlrk3588-cameras.yaml --frames 30 --output /tmp/stitch-smoke.yuv` | exits 0 and writes non-empty output |
| Serial smoke | `tools/embedded/serial_smoke.sh --device "${SERIAL_DEV}"` | UART prompt, UVC enumeration, and vendor dispatch verified |
| Signature evidence | `sha256sum -c SHA256SUMS` and both `cosign verify-blob` commands | all verification commands exit 0 |

If the EVK has not arrived yet, mark only the real-board rows as
hardware-blocked and keep the signed bundle evidence. Do not mark C4-A.C
customer acceptance complete until the ATK-DLRK3588 rows above pass on
the physical board.

---

## Step 5 — Customer acceptance handoff text

Include this short note with the delivery:

```text
This ATK-DLRK3588 package is EVK-specific. Verify SHA256SUMS, verify both
cosign signatures with cosign.pub, flash atk-dlrk3588.img through the
documented Maskrom flow, then run acceptance/bringup-checklist.md on the
booted EVK. Report any signature failure, boot failure, missing /dev/video*
node, or stitching dry-run failure before integrating the SDK.
```

---

## What this runbook does not do

- Does not build the RK3588 BSP, image, stitching binary, or SDK headers.
  Those are C4-A.A and C4-A.B deliverables.
- Does not cover ATK-DLRV1126, Radxa Dragon Q6A, MediaTek Genio 1200-EVK,
  or FT-C600.
- Does not bypass cosign verification for local-only delivery.
- Does not claim real-EVK acceptance from qemu or dry-run evidence.

---

## Cross-references

- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md` §4
- `docs/operations/2026-06-02-case2-uvccamera-qt-product-feed-runbook.md`
- `tools/embedded/flash_rk3588.sh`
- `tools/embedded/serial_smoke.sh`
- `deploy/cosign/cosign.pub`
