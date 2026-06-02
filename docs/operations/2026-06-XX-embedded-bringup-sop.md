# Phase 0 embedded bring-up SOP -- per-SoC flash + serial smoke

**Date filed:** 2026-06-XX
**Ticket:** OP-1946 (Phase 0 / P0.D.3)
**Parent:** OP-1924 (Phase 0 P0.D Sub-EPIC)
**Status:** operator-runnable template; physical-board execution deferred until EVK is present

---

## Why this runbook exists

Phase 0 P0.D ships the physical-board bring-up contract for the embedded
camera platform: flash the target SoC image, watch serial boot, and confirm
the userspace UVC XU dispatcher responds after the board reaches Linux.

This document mirrors the operator-facing shape of
`docs/operations/2026-06-02-case2-uvccamera-qt-product-feed-runbook.md`
(Case 2 / T2C.2), but the execution target is per-SoC hardware instead of
the `git_accounts` product-feed row. It is the canonical P0.D.3 handoff until
P0.E.2 real-board acceptance replaces the template with observed board logs.

**Scope lock:** this SOP documents operator steps only. It does not create
flash scripts, serial-smoke scripts, Yocto recipes, daemon code, credentials,
or new external dependencies.

---

## Step 0 -- Pick the SoC row

Start from the supported SoC table and use only wave-1 rows for Phase 0
hardware bring-up unless the operator explicitly opens the OP-491 NDA path.

| SoC / EVK | Wave | Flash command | Serial default | Maskrom / boot-mode action | Expected dispatcher adapter |
| --- | --- | --- | --- | --- | --- |
| Rockchip RK3588 / ATK-DLRK3588 | 1 | `tools/embedded/flash_rk3588.sh` | `${RK3588_UART_DEV}` | Hold RECOVERY or MASKROM, connect USB OTG, release after `rkdeveloptool ld` sees the board | `rockchip-rk3588` |
| Rockchip RV1126 / ATK-DLRV1126 | 1 | `tools/embedded/flash_rv1126.sh` | `${RV1126_UART_DEV}` | Hold MASKROM, connect USB OTG, release after loader enumeration | `rockchip-rv1126` |
| Qualcomm QCS6490 / Radxa Dragon Q6A | 2 -- blocked on OP-491 | `tools/embedded/flash_qualcomm.sh` | `${QCS6490_UART_DEV}` | EDL or fastboot per NDA-cleared board manual | `qualcomm-qcs6490` |
| MediaTek Genio 1200-EVK | 2 -- blocked on OP-491 | `tools/embedded/flash_mediatek.sh` | `${GENIO1200_UART_DEV}` | BootROM download mode per NDA-cleared board manual | `mediatek-genio1200` |

If the board on the bench is not one of these rows, stop and file a
discovered-dependency note against the relevant Phase 0 / Case 4 ticket.
Adding a fifth vendor follows the Case 2 adapter pattern: update the external
`VendorAdapter` source, update the Python mirror / probe, then update the
Phase 0 dispatcher table.

---

## Step 1 -- Cable the bench

Prepare the host and board before flashing:

1. Connect the board's serial UART to the operator host.
2. Connect the board's USB OTG / download-mode port to the operator host.
3. Connect the camera or UVC test fixture required by the target SoC.
4. Keep power off until the maskrom / boot-mode step below asks for it.
5. Export the operator-local paths; do not write these into source files:

```bash
export SOC=rk3588
export UART_DEV=/dev/serial/by-id/${BOARD_UART_ID}
export IMAGE_PATH=${PHASE0_IMAGE_PATH}
export UVC_NODE=/dev/video0
export DISPATCHER_SOCKET=/run/uvc-xu-dispatcher.sock
```

Sanity check the host can see the serial adapter before entering maskrom:

```bash
test -e "${UART_DEV}"
stty -F "${UART_DEV}" 1500000 cs8 -cstopb -parenb -ixon -ixoff
```

For RV1126 boards, use the baud rate printed in the board manual if it differs
from the RK3588 default. Record the final baud rate in the P0.E.2 ticket
comment so the template can be narrowed after first hardware acceptance.

---

## Step 2 -- Enter maskrom / download mode

Use the SoC-specific row from Step 0:

### RK3588

```bash
rkdeveloptool ld
```

Expected before pressing the button: no Rockchip maskrom device, or only the
previous normal boot device. Hold RECOVERY or MASKROM, connect USB OTG, then
power the board. Re-run:

```bash
rkdeveloptool ld
```

Expected after pressing the button: one Rockchip loader or maskrom device.
If multiple devices appear, unplug every non-target Rockchip board and retry.

### RV1126

```bash
rkdeveloptool ld
```

Hold MASKROM, connect USB OTG, then power the board. RV1126 is a 32-bit ARMv7
target, so a successful USB enumeration does not imply the RK3588 image or
toolchain is valid. Keep `${SOC}=rv1126` through every later step.

### Wave-2 QCS6490 / Genio 1200

Do not run the Qualcomm or MediaTek download-mode commands from public notes.
Those boards are wave 2 and depend on OP-491 NDA-cleared mirrors and manuals.
When OP-491 opens the path, replace this section with the vendor-approved
EDL / BootROM sequence and keep credentials in `${VARS}` or the operator vault.

---

## Step 3 -- Flash the SoC image

Run the flash wrapper that matches the selected SoC:

```bash
tools/embedded/flash_${SOC}.sh \
  --image "${IMAGE_PATH}" \
  --serial "${UART_DEV}"
```

Expected:

- The wrapper prints the detected SoC and refuses mismatched images.
- The wrapper uses the platform toolchain / sysroot selected by Phase 0 P0.A.
- The wrapper exits `0` only after the final write or verify step completes.
- The wrapper does not require secrets or tokens.

If the command exits non-zero, capture the last 50 lines of output and the
`rkdeveloptool ld` result in the JIRA ticket. Do not retry the same command
more than twice without explaining what changed between attempts.

---

## Step 4 -- Monitor serial boot

Start the serial smoke harness immediately after reset:

```bash
tools/embedded/serial_smoke.sh \
  --soc "${SOC}" \
  --serial "${UART_DEV}" \
  --uvc-node "${UVC_NODE}" \
  --dispatcher-socket "${DISPATCHER_SOCKET}" \
  --timeout-seconds 180
```

Expected serial milestones:

| Milestone | RK3588 expectation | RV1126 expectation |
| --- | --- | --- |
| Boot ROM / loader | Rockchip loader banner or U-Boot SPL | Rockchip loader banner or U-Boot SPL |
| Kernel | Linux kernel command line appears | Linux kernel command line appears |
| Rootfs | init system starts without emergency shell | init system starts without emergency shell |
| UVC | target camera appears under `/sys/class/video4linux` | target camera appears under `/sys/class/video4linux` |
| Dispatcher | `uvc-xu-dispatcher` socket exists | `uvc-xu-dispatcher` socket exists |

If the board reaches Linux but the UVC device does not appear, collect:

```bash
dmesg | tail -100
find /sys/class/video4linux -maxdepth 2 -type l -print
```

If the board never reaches Linux, keep the serial log and defer the dispatcher
check. That failure belongs to the P0.D flash / board lane, not P0.B daemon
logic.

---

## Step 5 -- Validate the UVC XU dispatcher

After serial smoke confirms the daemon socket exists, query the dispatcher for
the selected SoC adapter:

```bash
tools/embedded/serial_smoke.sh \
  --soc "${SOC}" \
  --serial "${UART_DEV}" \
  --uvc-node "${UVC_NODE}" \
  --dispatcher-socket "${DISPATCHER_SOCKET}" \
  --check-dispatcher-only
```

Expected:

- RK3588 returns the `rockchip-rk3588` adapter row.
- RV1126 returns the `rockchip-rv1126` adapter row.
- FT-C600 fixtures, when attached for cross-checking, return the FT-C600 GUID
  described in `docs/audit/2026-06-02-uvc-xu-userspace-dispatch-spike.md`.
- The check prints the device VID:PID, XU GUID, control selector set, and a
  pass/fail result.

Do not issue vendor control writes during P0.D.3 documentation verification.
Real XU SET coverage belongs to P0.B and P0.E hardware acceptance.

---

## Step 6 -- Record acceptance evidence

For each hardware run, attach or comment the following evidence on the active
P0.E.2 / real-board ticket:

| Evidence | Required value |
| --- | --- |
| SoC row | `rk3588`, `rv1126`, `qcs6490`, or `genio1200` |
| Flash command | Full command with `${VARS}` preserved |
| Flash result | exit code and final verify line |
| Serial log | first boot banner, kernel command line, rootfs-ready line |
| UVC node | `/sys/class/video4linux` path and `/dev/videoN` path |
| Dispatcher result | adapter id, GUID, selector list, pass/fail |
| Deferred checks | explicit reason, usually "waiting for P0.E.1 qemu smoke" or "waiting for EVK" |

This OP-1946 file can be verified by markdown/file sanity only. The physical
board exercise is intentionally deferred to P0.E.1 qemu e2e smoke and P0.E.2
real-board e2e smoke, per the Phase 0 design.

---

## What this runbook DOES NOT do

- Does NOT inline credentials, tokens, serial numbers, or private vendor URLs.
  Use `${VARS}` and operator vault references only.
- Does NOT bypass OP-491 for Qualcomm or MediaTek NDA-gated material.
- Does NOT create or modify flash scripts. P0.D.1a / P0.D.1b own those files.
- Does NOT create or modify `serial_smoke.sh`. P0.D.2 owns that file.
- Does NOT validate MIPI-CSI, media-controller graphs, UAC audio, PoE, PD3.0,
  or form-factor work. Phase 0 is USB-UVC control-plane scope only.
- Does NOT close real hardware acceptance. P0.E.2 owns the physical-board gate.

---

## Cross-references

- `docs/operations/2026-06-02-case2-uvccamera-qt-product-feed-runbook.md` --
  Case 2 / T2C.2 operator-runbook pattern mirrored here.
- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md`
  -- Phase 0 design doc v3; P0.D.3 scope in section 3; Gerrit #1370.
- `docs/audit/2026-06-02-uvc-xu-userspace-dispatch-spike.md` -- UVC XU UAPI
  spike and FT-C600 GUID encoding notes.
- OP-491 -- NDA mirror path for Qualcomm QCS6490 and MediaTek Genio 1200 wave 2.
- OP-1918 -- Phase 0 META that P0.E.1 qemu e2e smoke closes.
