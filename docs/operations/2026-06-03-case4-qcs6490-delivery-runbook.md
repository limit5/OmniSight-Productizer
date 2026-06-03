# Case 4 QCS6490 delivery runbook -- Radxa Dragon Q6A BSP handoff

**Date filed:** 2026-06-03
**Ticket:** OP-1969 (Case 4 Phase 1A / C4-C.C)
**Parent:** OP-1926 (Case 4 META; C4-C per-EVK Sub-EPIC to be filed)
**Status:** operator-runnable after the QCS6490 BSP archive and flash image are built; physical-board evidence attaches after post-HIL

---

## Why this runbook exists

Case 4 Phase 1A C4-C delivers the Radxa Dragon Q6A customer BSP package as a
per-EVK artifact, not as a shared Qualcomm bundle. This runbook closes C4-C.C
by defining the customer delivery package, the cosign/checksum signing step,
the fastboot/EDL flash paths, and the customer-side bring-up acceptance
checklist.

It mirrors the C4-A/C4-B/C4-E wave-1 delivery shape and the Case 2 T2C.2
handoff pattern: the productizer records the operator-verifiable delivery
contract in docs, while the concrete artifact action is a small shell helper.
The existing Phase 0 substrate is reused directly:

- `tools/embedded/flash_qualcomm.sh` -- Radxa Dragon Q6A fastboot/EDL wrapper
- `tools/embedded/serial_smoke.sh` -- UART prompt + UVC enumeration + vendor-dispatch smoke
- `tools/case4/sign_bsp_qcs6490.sh` -- BSP delivery checksum, manifest, and cosign helper

**Scope lock:** this runbook is Radxa Dragon Q6A / Qualcomm QCS6490 only. Do
not reuse it for RK3588, RV1126, Genio 1200, or FT-C600 deliverables without
filing that EVK's own C4-X.C ticket.

---

## Delivery package contract

The C4-C BSP delivery directory is named:

```text
omnisight-case4-radxa-dragon-q6a-bsp-<version>/
```

It contains:

```text
flash/
  qcs6490-fastboot.img
  edl/
    prog_firehose_ddr.elf
    rawprogram.xml
    patch.xml
bsp/
  boot/Image
  dtb/radxa-dragon-q6a.dtb
  rootfs/rootfs.ext4
  modules/
stitching/
  bin/omnisight-qcs6490-stitch
  config/radxa-dragon-q6a-cameras.yaml
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
  "omnisight-case4-radxa-dragon-q6a-bsp-${VERSION}.tar.gz" \
  "omnisight-case4-radxa-dragon-q6a-bsp-${VERSION}"
```

The archive must remain EVK-specific. Do not add RK3588, RV1126, Genio,
FT-C600, or generic Qualcomm SDK artifacts to this package.

---

## Step 0 -- Pre-flight inventory

Confirm the operator host has the required tools:

```bash
command -v sha256sum
command -v cosign
command -v fastboot
command -v qdl
command -v stty
```

Confirm the unsigned inputs are present:

```bash
test -f "${BSP_ARCHIVE}"
test -f "${FLASH_IMAGE}"
test -f "${FIREHOSE}"
test -f "${RAWPROGRAM_XML}"
test -f "${PATCH_XML}"
```

Expected names:

| artifact | expected filename |
| --- | --- |
| BSP archive | `omnisight-case4-radxa-dragon-q6a-bsp-<version>.tar.gz` |
| Fastboot flash image | `qcs6490-fastboot.img` |
| EDL firehose | `prog_firehose_ddr.elf` |
| EDL rawprogram | `rawprogram.xml` |
| EDL patch XML | `patch.xml` |

---

## Step 1 -- Sign and checksum the delivery artifacts

Use the self-managed cosign key. Do not inline the password into the command or
into any committed file.

```bash
export COSIGN_PASSWORD="$(cat ~/.config/omnisight/cosign/cosign-password.txt)"

tools/case4/sign_bsp_qcs6490.sh \
  --evk radxa-dragon-q6a \
  --version "${VERSION}" \
  --archive "${BSP_ARCHIVE}" \
  --image "${FLASH_IMAGE}" \
  --out-dir "${DELIVERY_OUT}" \
  --cosign-key ~/.config/omnisight/cosign/cosign.key \
  --public-key deploy/cosign/cosign.pub
```

Expected output files:

```text
${DELIVERY_OUT}/omnisight-case4-radxa-dragon-q6a-bsp-<version>.tar.gz
${DELIVERY_OUT}/qcs6490-fastboot.img
${DELIVERY_OUT}/radxa-dragon-q6a-delivery-manifest.json
${DELIVERY_OUT}/SHA256SUMS
${DELIVERY_OUT}/omnisight-case4-radxa-dragon-q6a-bsp-<version>.tar.gz.sig
${DELIVERY_OUT}/qcs6490-fastboot.img.sig
${DELIVERY_OUT}/cosign-verify.log
```

Failure policy:

- Missing `COSIGN_PASSWORD` means the operator has not loaded the key secret;
  stop and fix local signing setup.
- Missing `deploy/cosign/cosign.pub` means the verification key was not checked
  out; stop and restore the repository state.
- Any `cosign verify-blob` failure blocks delivery. Do not hand off an archive
  that only has SHA256 without a valid signature.

---

## Step 2 -- Stage the customer bundle

`tools/case4/sign_bsp_qcs6490.sh` stages the archive, image, manifest,
checksums, signatures, and public key into `${DELIVERY_OUT}`. Copy only these
files from `${DELIVERY_OUT}` into the customer delivery location:

```text
omnisight-case4-radxa-dragon-q6a-bsp-<version>.tar.gz
qcs6490-fastboot.img
radxa-dragon-q6a-delivery-manifest.json
SHA256SUMS
omnisight-case4-radxa-dragon-q6a-bsp-<version>.tar.gz.sig
qcs6490-fastboot.img.sig
cosign.pub
```

Before upload, verify the checksums from inside the staged directory:

```bash
sha256sum -c SHA256SUMS
cosign verify-blob \
  --key cosign.pub \
  --signature "omnisight-case4-radxa-dragon-q6a-bsp-${VERSION}.tar.gz.sig" \
  "omnisight-case4-radxa-dragon-q6a-bsp-${VERSION}.tar.gz"
cosign verify-blob \
  --key cosign.pub \
  --signature qcs6490-fastboot.img.sig \
  qcs6490-fastboot.img
```

Keep the EDL `firehose`, `rawprogram.xml`, and `patch.xml` files inside the
signed BSP archive unless the customer support channel explicitly asks for a
separate recovery bundle. If they are split out, add their SHA256 rows to the
customer evidence before handoff.

---

## Step 3 -- Flash the Radxa Dragon Q6A over fastboot

Put the board into fastboot mode using the Radxa Dragon Q6A operator guide, then
verify the host sees exactly one target board:

```bash
fastboot devices
```

Flash with the Phase 0 wrapper:

```bash
tools/embedded/flash_qualcomm.sh \
  --device "${QCS6490_FASTBOOT_SERIAL}" \
  --image "${FLASH_IMAGE}" \
  --partition "${QCS6490_FASTBOOT_PARTITION}" \
  --yes \
  fastboot
```

`QCS6490_FASTBOOT_SERIAL` must be a unique substring of the connected Radxa
Dragon Q6A fastboot serial. The wrapper refuses to auto-select among multiple
USB devices.

---

## Step 4 -- Recover or provision over EDL

Use EDL only for boards that require firehose provisioning or recovery. Put the
Radxa Dragon Q6A into EDL mode, then run:

```bash
tools/embedded/flash_qualcomm.sh \
  --device "${QCS6490_EDL_SERIAL}" \
  --firehose "${BSP_ROOT}/flash/edl/prog_firehose_ddr.elf" \
  --rawprogram "${BSP_ROOT}/flash/edl/rawprogram.xml" \
  --patch "${BSP_ROOT}/flash/edl/patch.xml" \
  --yes \
  edl
```

Expected:

- The wrapper passes the explicit serial/device identifier to `qdl` or `edl.py`.
- The firehose programmer, rawprogram XML, and patch XML come from the signed
  Radxa Dragon Q6A BSP archive.
- The final EDL command exits `0`; otherwise, keep the full command output with
  the customer acceptance evidence and do not declare the board provisioned.

---

## Step 5 -- Customer bring-up acceptance checklist

Run this checklist on the flashed Radxa Dragon Q6A before customer handoff.
Record the command output in the ticket or delivery evidence folder.

| check | command | pass condition |
| --- | --- | --- |
| Board boots | serial console at 115200 baud | login prompt or shell prompt appears |
| QCS6490 identity | `cat /proc/device-tree/compatible` | output includes a Radxa Dragon Q6A or QCS6490 compatible string |
| Kernel image | `uname -a` | expected BSP kernel version is present |
| UVC devices | `ls /dev/video*` | expected camera count appears |
| USB inventory | `lsusb` | attached UVC camera VID:PID values appear |
| Dispatcher | `systemctl status uvcvideo-xu-dispatcher` | service is active or the bundled launcher reports healthy |
| Stitching binary | `omnisight-qcs6490-stitch --version` | binary starts and prints the BSP version |
| Stitching dry run | `omnisight-qcs6490-stitch --config /opt/omnisight/radxa-dragon-q6a-cameras.yaml --frames 30 --output /tmp/stitch-smoke.yuv` | exits 0 and writes non-empty output |
| Serial smoke | `tools/embedded/serial_smoke.sh --soc qcs6490 --serial "${QCS6490_UART_DEV}"` | UART prompt, UVC enumeration, and vendor dispatch verified |
| Signature evidence | `sha256sum -c SHA256SUMS` and both `cosign verify-blob` commands | all verification commands exit 0 |

If the EVK has not completed post-HIL yet, mark only the real-board rows as
hardware-blocked and keep the signed bundle evidence. Do not mark C4-C.C
customer acceptance complete until the Radxa Dragon Q6A rows above pass on the
physical board.

---

## Step 6 -- Customer acceptance handoff text

Include this short note with the delivery:

```text
This Radxa Dragon Q6A package is EVK-specific. Verify SHA256SUMS, verify both
cosign signatures with cosign.pub, flash qcs6490-fastboot.img through the
documented fastboot flow or use the included EDL firehose recovery files, then
run acceptance/bringup-checklist.md on the booted EVK. Report any signature
failure, boot failure, missing /dev/video* node, or stitching dry-run failure
before integrating the SDK.
```

---

## What this runbook does not do

- Does not build the QCS6490 BSP, image, stitching binary, SDK headers, or EDL
  firehose package. Those are C4-C.A and C4-C.B deliverables.
- Does not cover ATK-DLRK3588, ATK-DLRV1126, MediaTek Genio 1200-EVK, or
  FT-C600.
- Does not bypass cosign verification for local-only delivery.
- Does not claim real-EVK acceptance from qemu, dry-run evidence, or unsigned
  recovery files.
- Does not modify backend, CI, database, devops, frontend, Gerrit, security,
  tests, or tooling domains.

---

## Cross-references

- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md` §4
- `docs/operations/2026-06-02-case2-uvccamera-qt-product-feed-runbook.md`
- `docs/operations/2026-06-XX-embedded-bringup-sop.md`
- `tools/embedded/flash_qualcomm.sh`
- `tools/embedded/serial_smoke.sh`
- `deploy/cosign/cosign.pub`
