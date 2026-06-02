"""OP-1963 contract checks for tools/embedded/flash_qualcomm.sh."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "embedded" / "flash_qualcomm.sh"
RK3588_SCRIPT = REPO_ROOT / "tools" / "embedded" / "flash_rk3588.sh"
RV1126_SCRIPT = REPO_ROOT / "tools" / "embedded" / "flash_rv1126.sh"


class FlashQualcommScriptTest(unittest.TestCase):
    def test_script_exists_and_is_valid_bash(self) -> None:
        self.assertTrue(SCRIPT.exists(), "flash_qualcomm.sh is missing")
        self.assertTrue(SCRIPT.stat().st_mode & 0o111, "flash_qualcomm.sh must be executable")

        proc = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_requires_explicit_device(self) -> None:
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--dry-run", "--partition", "boot", "--image", "/tmp/boot.img"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 2)
        self.assertIn("--device is required", proc.stderr)

    def test_dry_run_prints_fastboot_plan_without_usb_writes(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--dry-run",
                "--device",
                "dragon-q6a-001",
                "--partition",
                "boot",
                "--image",
                "/tmp/boot.img",
                "fastboot",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would verify exactly one fastboot device matching: dragon-q6a-001", proc.stdout)
        self.assertIn("[dry-run] fastboot -s dragon-q6a-001 flash boot /tmp/boot.img", proc.stdout)
        self.assertIn("[dry-run] fastboot -s dragon-q6a-001 reboot", proc.stdout)

    def test_dry_run_prints_edl_plan_with_explicit_serial(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--dry-run",
                "--device",
                "dragon-q6a-edl",
                "--firehose",
                "/tmp/prog_firehose_ddr.elf",
                "--rawprogram",
                "/tmp/rawprogram0.xml",
                "--patch",
                "/tmp/patch0.xml",
                "edl",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "[dry-run] qdl --serial dragon-q6a-edl --firehose /tmp/prog_firehose_ddr.elf /tmp/rawprogram0.xml /tmp/patch0.xml",
            proc.stdout,
        )

    def test_sibling_wave1_flash_scripts_stay_byte_identical(self) -> None:
        rk3588_before = RK3588_SCRIPT.read_bytes()
        rv1126_before = RV1126_SCRIPT.read_bytes()

        subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(RK3588_SCRIPT.read_bytes(), rk3588_before)
        self.assertEqual(RV1126_SCRIPT.read_bytes(), rv1126_before)


if __name__ == "__main__":
    unittest.main()
