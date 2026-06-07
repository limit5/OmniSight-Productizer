# Bench inventory -- Wave 1+2 embedded bring-up board map

**Date filed:** 2026-06-05
**Ticket:** OP-2086 (Phase 0 / P0.E.2 L05)
**Status:** inventory skeleton for Wave 1+2 `.run` tickets to fill

---

## Why this document exists

Phase 0 embedded bring-up needs one stable bench inventory before the Wave 1
RK3588 first-light run and Wave 2 RV1126, QCS6490, and Genio 1200 runs start
adding hardware-specific evidence. This file centralizes the physical bench
topology, USB identity placeholders, IP reservations, SSH fingerprints, and
quick-flash recipe slots so each `.run` ticket updates the same table instead
of scattering board facts across ticket comments.

**Scope lock:** this file is documentation only. It does not change flash
scripts, usbipd setup, board firmware, serial tooling, DHCP configuration,
credentials, SSH keys, CI, backend, frontend, database, Gerrit, security,
tests, or embedded implementation files.

---

## 1. Bench topology

The Phase 0 bench is a Windows-hosted USB/IP setup with the Linux flashing and
serial tools running inside WSL2.

```text
Windows bench host
  |
  | USB: UART adapters, OTG / Maskrom / BootROM / fastboot cables, UVC devices
  v
usbipd-win
  |
  | usbipd bind / attach --wsl <busid>
  v
WSL2 operator shell
  |
  | /dev/ttyUSB<N>, /dev/serial/by-id/*, lsusb, dmesg, flash wrappers
  |
  +-- ATK-DLRK3588           (Wave 1 / RK3588 first-light)
  +-- ATK-DLRV1126           (Wave 2 / RV1126)
  +-- Radxa Dragon Q6A       (Wave 2 / QCS6490)
  +-- MediaTek Genio 1200-EVK (Wave 2 / Genio 1200)
```

Keep Windows-side serial monitors and vendor flashing tools closed while a USB
device is attached to WSL2. Record the Windows `usbipd list` busid and the WSL2
`lsusb` line in the board's `.run` ticket before flashing.

---

## 2. Per-board inventory table

Fill only observed values. Leave placeholders as `TBD by <ticket>` until the
matching first-light `.run` ticket captures real bench evidence.

| board | VID:PID maskrom/boot | VID:PID runtime | expected ttyUSB | baudrate | power | ethernet MAC | DHCP-reserved IP | boot media | ssh key fingerprint | flash script | Yocto MACHINE |
| --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| ATK-DLRK3588 | TBD by Wave 1 RK3588 `.run` | TBD by Wave 1 RK3588 `.run` | `/dev/ttyUSB<N>` or `/dev/serial/by-id/*rk3588*` | 1500000 | External bench PSU or board supply, power-cycle for Maskrom | TBD by Wave 1 RK3588 `.run` | TBD by Wave 1 RK3588 `.run` | `atk-dlrk3588.img` / board eMMC or SD, final slot TBD | TBD by Wave 1 RK3588 `.run` | `tools/embedded/flash_rk3588.sh` | `rockchip-rk3588` |
| ATK-DLRV1126 | TBD by Wave 2 RV1126 `.run` | TBD by Wave 2 RV1126 `.run` | `/dev/ttyUSB<N>` or `/dev/serial/by-id/*rv1126*` | 1500000 | External bench PSU or board supply, power-cycle for Maskrom | TBD by Wave 2 RV1126 `.run` | TBD by Wave 2 RV1126 `.run` | `images/update.img` / board flash target TBD | TBD by Wave 2 RV1126 `.run` | `tools/embedded/flash_rv1126.sh` | `rockchip-rv1126` |
| Radxa-Dragon-Q6A | TBD by Wave 2 QCS6490 `.run` | TBD by Wave 2 QCS6490 `.run` | `/dev/ttyUSB<N>` or `/dev/serial/by-id/*q6a*` | 115200 | External bench PSU or board supply, fastboot / EDL sequence per operator guide | TBD by Wave 2 QCS6490 `.run` | TBD by Wave 2 QCS6490 `.run` | `qcs6490-fastboot.img` or EDL program set, final slot TBD | TBD by Wave 2 QCS6490 `.run` | `tools/embedded/flash_qualcomm.sh` | `qualcomm-qcs6490` |
| MediaTek-Genio-1200-EVK | TBD by Wave 2 Genio `.run` | TBD by Wave 2 Genio `.run` | `/dev/ttyUSB<N>` or `/dev/serial/by-id/*genio*` | 921600 | External bench PSU or board supply, BootROM sequence per NDA-cleared manual | TBD by Wave 2 Genio `.run` | TBD by Wave 2 Genio `.run` | `genio1200.wic.img` / eMMC or SD, final slot TBD | TBD by Wave 2 Genio `.run` | `tools/embedded/flash_mediatek.sh` | `mediatek-genio1200` |

Update rules for `.run` tickets:

1. Replace `TBD` only with observed values from the physical bench.
2. Keep private serial numbers, credentials, and full private key material out
   of the file; use public fingerprints only.
3. Use `/dev/serial/by-id/...` for stable UART paths when Linux exposes one.
4. Keep DHCP reservations in the operator network source of truth; this table
   records the reservation outcome, not the router configuration.

---

## 3. Per-board quick-flash recipes

These recipes are placeholders for the first-light tickets. Keep the 5-step
shape stable so later evidence can be compared board by board.

### ATK-DLRK3588

1. Hold the Maskrom / recovery button and power-cycle the board.
2. On Windows, run `usbipd list`, then `usbipd attach --wsl --busid <busid>`.
3. In WSL2, run `dmesg | grep ttyUSB` and record the UART device path.
4. Run `tools/embedded/flash_rk3588.sh --device <busid>` with the final loader
   and image arguments from the Wave 1 RK3588 `.run` ticket.
5. Release Maskrom, reset the board, then open `tio -b 1500000 /dev/ttyUSB<N>`.

### ATK-DLRV1126

1. Hold the Maskrom / recovery button and power-cycle the board.
2. On Windows, run `usbipd list`, then `usbipd attach --wsl --busid <busid>`.
3. In WSL2, run `dmesg | grep ttyUSB` and record the UART device path.
4. Run `tools/embedded/flash_rv1126.sh` with loader, image, and board arguments
   from the Wave 2 RV1126 `.run` ticket.
5. Release Maskrom, reset the board, then open `tio -b 1500000 /dev/ttyUSB<N>`.

### Radxa Dragon Q6A

1. Put the board into fastboot or EDL mode per the operator-approved guide.
2. On Windows, run `usbipd list`, then `usbipd attach --wsl --busid <busid>`.
3. In WSL2, run `dmesg | grep ttyUSB` and record the UART device path.
4. Run `tools/embedded/flash_qualcomm.sh` with fastboot or EDL arguments from
   the Wave 2 QCS6490 `.run` ticket.
5. Reset the board, then open `tio -b 115200 /dev/ttyUSB<N>`.

### MediaTek Genio 1200-EVK

1. Put the board into BootROM download mode per the NDA-cleared EVK manual.
2. On Windows, run `usbipd list`, then `usbipd attach --wsl --busid <busid>`.
3. In WSL2, run `dmesg | grep ttyUSB` and record the UART device path.
4. Run `tools/embedded/flash_mediatek.sh` with preloader, config, and image
   arguments from the Wave 2 Genio `.run` ticket.
5. Reset the board, then open `tio -b 921600 /dev/ttyUSB<N>`.

---

## 4. First board on bench cold start

Use this runbook for the first physical board in a new bench session.

1. **Inventory the host.** On Windows, run `usbipd list` and note every UART,
   OTG, fastboot, BootROM, and UVC device that is already connected.
2. **Attach only the target board.** Bind and attach the selected busid to WSL2,
   then confirm `lsusb` shows exactly the intended flash/download device.
3. **Map serial before flashing.** In WSL2, run `dmesg | grep ttyUSB` and
   `ls -l /dev/serial/by-id/`; record the stable UART path when available.
4. **Flash once.** Run the board's flash wrapper with explicit device
   arguments. If it fails, capture the command and last 50 output lines before
   retrying.
5. **Open serial immediately after reset.** Use the baud rate from the table,
   keep the terminal running through reboot, and capture the first boot log.
6. **Reserve network identity.** Once Linux boots, record Ethernet MAC and
   request or confirm the DHCP-reserved IP outside this repository.
7. **Record SSH identity.** After SSH starts, record the host-key fingerprint
   only; do not commit private keys or operator account credentials.
8. **Run sanity checks.** Capture `uname -a`, `/proc/device-tree/compatible`,
   `lsusb`, `ip addr`, and the serial-smoke command used by the board's SOP.

Evidence from this cold start updates section 2 and the matching quick-flash
recipe only after the operator confirms the values are stable.

---

## 5. Known gotchas index

- `docs/operations/2026-06-XX-embedded-bringup-sop.md` -- L02 embedded bring-up
  flow, SoC rows, flash wrapper names, and physical-board evidence table.
- `docs/operations/2026-06-05-bench-serial-console-sop.md` -- L04 UART
  discovery, baud rates, `tio` commands, and serial log naming.
- `docs/operations/2026-06-03-case4-rk3588-delivery-runbook.md` -- RK3588
  Maskrom, signed image, and customer acceptance context.
- `docs/operations/2026-06-03-case4-rv1126-delivery-runbook.md` -- RV1126
  Maskrom, ARMv7-A, and `rkdeveloptool` offset context.
- `docs/operations/2026-06-03-case4-qcs6490-delivery-runbook.md` -- Radxa
  Dragon Q6A fastboot / EDL split and QCS6490 acceptance context.
- `docs/operations/2026-06-03-case4-genio1200-delivery-runbook.md` -- MediaTek
  BootROM / Genio flash context and NDA boundary reminders.
- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md`
  -- Substrate context for Phase 0 design sections 3.4 and 4.7.
