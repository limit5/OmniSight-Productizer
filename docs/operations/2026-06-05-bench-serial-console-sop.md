# Bench serial console SOP -- EVK UART discovery, baud rates, and logging

**Date filed:** 2026-06-05
**Ticket:** OP-2083 (Phase 0 / P0.E.2 L04)
**Status:** operator-runnable bench SOP

---

## Why this runbook exists

Phase 0 embedded bring-up needs a repeatable serial-console setup before
flash, boot, and UVC dispatcher evidence can be collected. This SOP records
the per-board UART settings, discovery commands, terminal invocations, and log
capture naming conventions so operators do not have to sweep ports and baud
rates during every bench session.

**Scope lock:** this file documents host-side serial-console operation only.
It does not change flash scripts, board firmware, USB/IP setup, dispatcher
code, credentials, or CI deployment behavior.

---

## 1. Required tools

Install the terminal clients and the Python serial package on the bench host:

```bash
sudo apt update
sudo apt install tio minicom screen python3-serial
```

Use `tio` first because it handles device reconnects cleanly. Keep `minicom`
and `screen` available as fallbacks when a customer or vendor note already
standardizes on one of them.

## 2. Per-board baud rate and UART config

Use 8 data bits, no parity, 1 stop bit, and no hardware or software flow
control for every serial-console row below.

| Board / fixture | UART device hint | Baud rate | UART config | Flow control | Notes |
| --- | --- | ---: | --- | --- | --- |
| ATK-DLRK3588 | `/dev/ttyUSB0` or `/dev/serial/by-id/*rk3588*` | 1500000 | 8N1 | no-flow | Rockchip default |
| ATK-DLRV1126 | `/dev/ttyUSB0` or `/dev/serial/by-id/*rv1126*` | 1500000 | 8N1 | no-flow | Rockchip default |
| Radxa Dragon Q6A | `/dev/ttyUSB0` or `/dev/serial/by-id/*q6a*` | 115200 | 8N1 | no-flow | Qualcomm bench default |
| MediaTek Genio 1200-EVK | `/dev/ttyUSB0` or `/dev/serial/by-id/*genio*` | 921600 | 8N1 | no-flow | MediaTek EVK console |
| FT-C600 | N/A | N/A | N/A | N/A | USB UVC device; no separate serial console |

Prefer `/dev/serial/by-id/...` when it exists. Use `/dev/ttyUSB0` only for
single-adapter bench sessions or while identifying the adapter after attach.

## 3. ttyUSB discovery

Attach the USB-UART adapter, then check the kernel messages:

```bash
dmesg | grep tty
```

Map the physical USB port to the Linux device path:

```bash
lsusb -tv
```

If several adapters are present, unplug the target board's adapter, run both
commands, reattach it, then run both commands again. Record the new `ttyUSB`
or `ttyACM` line in the run ticket before starting boot capture.

## 4. tio invocation per board

Use `tio` for normal bench capture. Replace `/dev/ttyUSB0` with the discovered
device path.

```bash
tio -b 1500000 /dev/ttyUSB0   # ATK-DLRK3588
tio -b 1500000 /dev/ttyUSB0   # ATK-DLRV1126
tio -b 115200 /dev/ttyUSB0    # Radxa Dragon Q6A
tio -b 921600 /dev/ttyUSB0    # MediaTek Genio 1200-EVK
```

Exit `tio` with `Ctrl-T q`. If the board resets during flashing, leave `tio`
running and let it reconnect to the same device path.

## 5. minicom and screen fallbacks

Use `minicom` when an operator needs capture controls or a saved profile:

```bash
minicom -b 1500000 -D /dev/ttyUSB0 -8 -w
```

Save stable profiles as `~/.minirc.<board>`, for example
`~/.minirc.atk-dlrk3588` or `~/.minirc.genio1200-evk`. Keep the profile values
aligned with the table in section 2: baud rate, 8N1, and no flow control.

Use `screen` only as a minimal fallback:

```bash
screen /dev/ttyUSB0 1500000
```

Exit `screen` with `Ctrl-A k`, then confirm with `y`.

## 6. Common gotchas

- `Permission denied`: add the operator user to the `dialout` group, then log
  out and back in before retrying.
- Garbled output: the baud rate is wrong; sweep only `115200`, `921600`, and
  `1500000` before suspecting board firmware.
- Serial port disappeared after `usbipd-win` attach: re-attach the device from
  Windows, then rerun `dmesg | grep tty`.
- Windows-side COM port hogging: close PuTTY, Tera Term, vendor flash tools,
  and any Windows serial monitor before attaching the USB device to Linux.

## 7. Log capture conventions

Use `tio` built-in logging for first-boot and reset evidence:

```bash
tio -b 1500000 -l atk-dlrk3588-2026-06-05.log /dev/ttyUSB0
```

Store durable bench logs under this repository-external naming scheme:

```text
bench-logs/<board>/<YYYY-MM-DD>-<phase>.log
```

Examples:

```text
bench-logs/atk-dlrk3588/2026-06-05-first-boot.log
bench-logs/genio1200-evk/2026-06-05-uvc-dispatcher-smoke.log
```

Attach Phase C and Phase D `.run` logs to the relevant JIRA ticket after each
bench run. Keep raw serial logs unchanged; add interpretation in the ticket
comment instead of editing the captured file.
