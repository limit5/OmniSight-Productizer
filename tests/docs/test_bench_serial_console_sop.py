"""OP-2083 contract checks for the bench serial console SOP."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SOP = REPO_ROOT / "docs" / "operations" / "2026-06-05-bench-serial-console-sop.md"


def _doc() -> str:
    return SOP.read_text(encoding="utf-8")


def test_sop_exists_and_documents_required_tools() -> None:
    body = _doc()

    assert "sudo apt install tio minicom screen python3-serial" in body
    assert "## 1. Required tools" in body


def test_each_serial_board_has_baudrate_row() -> None:
    body = _doc()

    expected_rows = [
        "| ATK-DLRK3588 | `/dev/ttyUSB0` or `/dev/serial/by-id/*rk3588*` | 1500000 | 8N1 | no-flow | Rockchip default |",
        "| ATK-DLRV1126 | `/dev/ttyUSB0` or `/dev/serial/by-id/*rv1126*` | 1500000 | 8N1 | no-flow | Rockchip default |",
        "| Radxa Dragon Q6A | `/dev/ttyUSB0` or `/dev/serial/by-id/*q6a*` | 115200 | 8N1 | no-flow | Qualcomm bench default |",
        "| MediaTek Genio 1200-EVK | `/dev/ttyUSB0` or `/dev/serial/by-id/*genio*` | 921600 | 8N1 | no-flow | MediaTek EVK console |",
    ]
    for row in expected_rows:
        assert row in body

    assert "| FT-C600 | N/A | N/A | N/A | N/A | USB UVC device; no separate serial console |" in body


def test_discovery_and_terminal_invocations_are_documented() -> None:
    body = _doc()

    assert "dmesg | grep tty" in body
    assert "lsusb -tv" in body
    assert "tio -b 1500000 /dev/ttyUSB0" in body
    assert "minicom -b 1500000 -D /dev/ttyUSB0 -8 -w" in body
    assert "screen /dev/ttyUSB0 1500000" in body


def test_common_gotchas_lists_required_cases() -> None:
    body = _doc()
    gotchas = [
        "`Permission denied`: add the operator user to the `dialout` group",
        "Garbled output: the baud rate is wrong",
        "Serial port disappeared after `usbipd-win` attach",
        "Windows-side COM port hogging",
    ]

    assert "## 6. Common gotchas" in body
    for gotcha in gotchas:
        assert gotcha in body

    common_gotchas_section = body.split("## 6. Common gotchas", maxsplit=1)[1].split(
        "## 7. Log capture conventions",
        maxsplit=1,
    )[0]
    assert common_gotchas_section.count("\n- ") >= 3


def test_log_capture_conventions_are_documented() -> None:
    body = _doc()

    assert "tio -b 1500000 -l atk-dlrk3588-2026-06-05.log /dev/ttyUSB0" in body
    assert "bench-logs/<board>/<YYYY-MM-DD>-<phase>.log" in body
    assert "Attach Phase C and Phase D `.run` logs to the relevant JIRA ticket" in body
