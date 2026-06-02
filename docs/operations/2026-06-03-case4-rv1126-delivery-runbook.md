# Case 4 RV1126 customer delivery runbook -- ATK-DLRV1126

**Date filed:** 2026-06-03
**Ticket:** OP-1953 (Case 4 Phase 1A / C4-B.C)
**Parent:** OP-1928 (ATK-DLRV1126 per-EVK Sub-EPIC)
**Status:** customer-runnable template; physical-board evidence attaches after EVK arrival

---

## Why this runbook exists

Case 4 Phase 1A ships one customer BSP/SDK deliverable per EVK. This file is
the ATK-DLRV1126 customer handoff for the Rockchip RV1126 armhf target. It
combines the per-EVK BSP archive layout, stitching-pipeline handoff, Rockchip
`rkdeveloptool` flash sequence, ARMv7-A boot quirks, checksum/signature
generation, and the customer-side acceptance record.

This runbook mirrors the operator-facing shape of
`docs/operations/2026-06-02-case2-uvccamera-qt-product-feed-runbook.md`, but
the acceptance target is a signed BSP archive and a real ATK-DLRV1126 boot
instead of a product-feed database row.

**Scope lock:** this runbook is RV1126-only. Do not reuse it for RK3588,
QCS6490, Genio 1200, or FT-C600 deliverables without filing that EVK's own
C4-X.C ticket.

---

## Step 0 -- Confirm the delivery inputs

Start from a release workspace that already contains the RV1126 Phase 1A
outputs:

```bash
export BSP_ROOT=${CASE4_RV1126_BSP_ROOT}
export RELEASE_ID=${CASE4_RELEASE_ID}
export RV1126_UART_DEV=/dev/serial/by-id/${ATK_DLRV1126_UART_ID}
export RKDEVELOPTOOL_BIN=${RKDEVELOPTOOL_BIN:-rkdeveloptool}
```

Required BSP archive layout:

| Path | Required content |
| --- | --- |
| `images/loader/rv1126_loader.bin` | Rockchip RV1126 USB loader for `rkdeveloptool db` |
| `images/update.img` | Customer flash image written by `rkdeveloptool wl` |
| `images/update.img.sha256` | SHA256 for the flash image |
| `boot/` | Kernel image, DTB, U-Boot/SPL inputs used to build `update.img` |
| `rootfs/` | Rootfs manifest or rootfs image reference |
| `sdk/include/` | Customer-visible headers for the Phase 1A API |
| `sdk/examples/` | Minimal customer sample app for the stitching feed |
| `stitching/calibration/` | Intrinsic/extrinsic calibration bundle |
| `stitching/pipeline/` | RV1126 stitching pipeline config and model references |
| `licenses/manifest.spdx.json` | License manifest for open-source and vendor BSP inputs |
| `delivery/acceptance-template.md` | Customer acceptance checklist copied from this runbook |

If any required path is missing, stop the delivery and fix the C4-B packaging
ticket before flashing hardware. Do not invent substitute paths in the
customer archive.

---

## Step 1 -- Build and sign the customer BSP archive

Create a deterministic archive from the BSP root:

```bash
tar --sort=name --mtime="@0" --owner=0 --group=0 --numeric-owner \
  -C "${BSP_ROOT}" \
  -czf "case4-rv1126-${RELEASE_ID}.tar.gz" .
```

Generate the checksum and signature:

```bash
tools/case4/sign_bsp_armhf.sh \
  --board atk-dlrv1126 \
  --archive "case4-rv1126-${RELEASE_ID}.tar.gz"
```

Expected outputs:

| Output | Purpose |
| --- | --- |
| `case4-rv1126-${RELEASE_ID}.tar.gz.sha256` | Customer checksum verification |
| `case4-rv1126-${RELEASE_ID}.tar.gz.sig` | Cosign blob signature |
| `case4-rv1126-${RELEASE_ID}.tar.gz.delivery-manifest` | Human-readable manifest with board, arch, checksum, and signature path |

The signing helper is fail-closed: it never writes secrets, requires a real
archive path, and uses `BSP_COSIGN_KEY` or `COSIGN_KEY` for key-based signing.
Use `--dry-run` only for preflight; dry-run output is not customer evidence.

---

## Step 2 -- Customer verifies the archive

On the customer host:

```bash
sha256sum -c "case4-rv1126-${RELEASE_ID}.tar.gz.sha256"
cosign verify-blob \
  --key "${OMNISIGHT_BSP_COSIGN_PUB}" \
  --signature "case4-rv1126-${RELEASE_ID}.tar.gz.sig" \
  "case4-rv1126-${RELEASE_ID}.tar.gz"
```

Expected:

- `sha256sum` prints `OK`.
- `cosign verify-blob` exits `0`.
- The customer keeps the archive, `.sha256`, `.sig`, and `.delivery-manifest`
  together in the same release folder.

If signature verification fails, do not flash the board. Ask the operator for a
fresh signed archive and include the failing command output in the OP-1953
customer acceptance comment.

---

## Step 3 -- Enter ATK-DLRV1126 Maskrom mode

Cable the board before applying power:

1. Connect serial UART to the customer host.
2. Connect the ATK-DLRV1126 USB OTG port to the flashing host.
3. Hold the board's MASKROM/RECOVERY button.
4. Apply power.
5. Release the button only after `rkdeveloptool ld` reports Maskrom.

Check enumeration:

```bash
"${RKDEVELOPTOOL_BIN}" ld
```

Expected: exactly one Rockchip Maskrom device for the target board. If multiple
Rockchip devices appear, unplug non-target boards and retry enumeration before
running any write command.

---

## Step 4 -- Flash the RV1126 image

Unpack the signed archive and flash with the Phase 0 RV1126 wrapper:

```bash
tar -xzf "case4-rv1126-${RELEASE_ID}.tar.gz" -C "${BSP_ROOT}"

tools/embedded/flash_rv1126.sh \
  --board atk-dlrv1126 \
  --loader "${BSP_ROOT}/images/loader/rv1126_loader.bin" \
  --image "${BSP_ROOT}/images/update.img" \
  --offset 0x40 \
  --yes
```

Expected:

- The wrapper refuses any board id other than `atk-dlrv1126`.
- `rkdeveloptool db` downloads the RV1126 loader.
- `rkdeveloptool wl 0x40` writes `images/update.img`.
- `rkdeveloptool rd` reboots the board.

Do not use RK3588 flash offsets or aarch64 images here. RV1126 is ARMv7-A
armhf; keeping the loader, rootfs, and customer SDK on `arm-linux-gnueabihf`
is part of the delivery contract.

---

## Step 5 -- Watch ARMv7-A boot

Monitor serial immediately after reset:

```bash
tools/embedded/serial_smoke.sh \
  --soc rv1126 \
  --serial "${RV1126_UART_DEV}" \
  --uvc-node /dev/video0 \
  --dispatcher-socket /run/uvc-xu-dispatcher.sock \
  --timeout-seconds 180
```

RV1126-specific boot checks:

| Check | Expected value |
| --- | --- |
| Architecture | 32-bit ARMv7-A / armhf, not aarch64 |
| CPU line | Cortex-A7 class CPU appears in kernel boot output |
| Rootfs ABI | `arm-linux-gnueabihf` userland binaries run without loader errors |
| Kernel | Linux reaches normal init without emergency shell |
| Camera node | Target camera appears under `/sys/class/video4linux` |
| Dispatcher | `/run/uvc-xu-dispatcher.sock` exists after boot |

If Linux boots but the stitching feed does not start, capture serial output,
`dmesg | tail -100`, `/sys/class/video4linux` listing, and the relevant
`stitching/pipeline/` config name before escalating.

---

## Step 6 -- Exercise the customer stitching handoff

Run the delivered sample app from the SDK folder:

```bash
cd "${BSP_ROOT}/sdk/examples"
./rv1126_stitching_sample \
  --config "${BSP_ROOT}/stitching/pipeline/customer-rv1126.toml" \
  --calibration "${BSP_ROOT}/stitching/calibration/customer-rv1126.json" \
  --frames 300
```

Expected customer-side acceptance:

- The sample app starts from the delivered headers and config files.
- The camera feed opens from the flashed image without host-side rebuilds.
- The stitching pipeline processes 300 frames without fatal errors.
- Output frame size, timestamp source, and dropped-frame count are recorded in
  `delivery/acceptance-template.md`.

The exact frame-rate threshold belongs to the C4-B packaging evidence or the
customer contract. This runbook only requires that the delivered BSP archive
contains a runnable RV1126 stitching path and records the observed result.

---

## Step 7 -- Record delivery acceptance

Attach or comment the following evidence on the customer delivery ticket:

| Evidence | Required value |
| --- | --- |
| Board | `ATK-DLRV1126` |
| SoC / ABI | `Rockchip RV1126 / armhf / ARMv7-A` |
| Archive | `case4-rv1126-${RELEASE_ID}.tar.gz` |
| Checksum | Full SHA256 line from `.sha256` |
| Signature | `.sig` path plus `cosign verify-blob` exit `0` |
| Flash command | Full `tools/embedded/flash_rv1126.sh ... --board atk-dlrv1126 ...` command |
| Flash result | Final `rkdeveloptool rd` or wrapper exit code |
| Serial log | Boot banner, kernel command line, rootfs-ready line |
| Dispatcher result | RV1126 adapter response or deferred reason |
| Stitching sample | Config name, frame count, dropped-frame count, pass/fail |

All rows populated = customer-side delivery acceptance for C4-B.C.

---

## What this runbook DOES NOT do

- Does NOT create the BSP contents. C4-B packaging owns the archive inputs.
- Does NOT sign with a checked-in private key. Keep signing material in the
  operator vault and pass it through `BSP_COSIGN_KEY` or `COSIGN_KEY`.
- Does NOT flash any EVK other than ATK-DLRV1126.
- Does NOT use RK3588/aarch64 boot assumptions.
- Does NOT replace real-hardware acceptance. The physical EVK run is recorded
  when the ATK-DLRV1126 board is present.

---

## Cross-references

- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md`
  -- Case 4 Phase 1A structure; A8 identifies ATK-DLRV1126 as C4-B.
- `docs/operations/2026-06-XX-embedded-bringup-sop.md` -- Phase 0 RV1126
  flash and serial smoke template.
- `tools/embedded/flash_rv1126.sh` -- ATK-DLRV1126 rkdeveloptool wrapper.
- `tools/case4/sign_bsp_armhf.sh` -- checksum and signature helper for this
  RV1126 armhf customer archive.
