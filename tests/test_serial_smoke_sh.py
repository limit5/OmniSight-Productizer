"""OP-1945 contract checks for tools/embedded/serial_smoke.sh."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "embedded" / "serial_smoke.sh"


class SerialSmokeScriptTest(unittest.TestCase):
    def test_script_exists_and_is_valid_bash(self) -> None:
        self.assertTrue(SCRIPT.exists(), "serial_smoke.sh is missing")
        self.assertTrue(SCRIPT.stat().st_mode & 0o111, "serial_smoke.sh must be executable")

        proc = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_requires_explicit_device(self) -> None:
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--timeout", "1"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 2)
        self.assertIn("--device is required", proc.stderr)

    def test_runs_hardware_checks_over_uart_not_host_shell(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('exec 3<>"$DEVICE"', body)
        self.assertIn('stty -F "$DEVICE" "$BAUD"', body)
        self.assertIn('target_run lsusb "lsusb"', body)
        self.assertIn('target_run video_enum "ls /dev/video*"', body)
        self.assertIn('target_run dispatch_verify "curl -fsS $(target_quote "$DISPATCH_URL")"', body)
        self.assertIn("http://localhost:8000/dispatch?vendor=ft-c600", body)

    def test_help_documents_no_implicit_default_device(self) -> None:
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertIn("--device PATH", proc.stdout)
        self.assertIn("Must be explicit", proc.stdout)
        self.assertIn("/dev/ttyUSB0", proc.stdout)
        self.assertIn(os.linesep, proc.stdout)


if __name__ == "__main__":
    unittest.main()
