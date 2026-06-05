"""OP-2082 contract checks for OmniSight bench udev rules."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RULES = (
    REPO_ROOT
    / "yocto"
    / "meta-omnisight-camera"
    / "recipes-omnisight"
    / "udev"
    / "files"
    / "99-omnisight-bench.rules"
)


EXPECTED_USB_RULES = (
    ("2207", "350a", True),
    ("2207", "110c", False),
    ("05c6", "9008", False),
    ("18d1", "d00d", False),
    ("0e8d", "0003", False),
)

EXPECTED_UART_VENDORS = ("0403", "10c4", "1a86", "067b")


class BenchUdevRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(RULES.exists(), "99-omnisight-bench.rules is missing")
        self.rules_text = RULES.read_text(encoding="utf-8")
        self.rules_lines = [
            line
            for line in self.rules_text.splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def test_flash_usb_modes_have_expected_permissions(self) -> None:
        for vid, pid, expect_group in EXPECTED_USB_RULES:
            with self.subTest(vid=vid, pid=pid):
                line = self._find_rule(f'ATTR{{idVendor}}=="{vid}"', f'ATTR{{idProduct}}=="{pid}"')
                self.assertIn('SUBSYSTEM=="usb"', line)
                self.assertIn('MODE="0666"', line)
                if expect_group:
                    self.assertIn('GROUP="dialout"', line)

    def test_usb_uart_vendors_have_serial_console_permissions(self) -> None:
        for vid in EXPECTED_UART_VENDORS:
            with self.subTest(vid=vid):
                line = self._find_rule(f'ATTRS{{idVendor}}=="{vid}"')
                self.assertIn('SUBSYSTEM=="tty"', line)
                self.assertIn('MODE="0666"', line)
                self.assertIn('GROUP="dialout"', line)

    def _find_rule(self, *needles: str) -> str:
        matches = [
            line
            for line in self.rules_lines
            if all(needle in line for needle in needles)
        ]

        self.assertEqual(len(matches), 1, f"expected exactly one rule matching {needles}")
        return matches[0]


if __name__ == "__main__":
    unittest.main()
