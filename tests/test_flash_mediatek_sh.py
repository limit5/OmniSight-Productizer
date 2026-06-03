"""OP-1964 contract checks for tools/embedded/flash_mediatek.sh."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "embedded" / "flash_mediatek.sh"
RK3588_SCRIPT = REPO_ROOT / "tools" / "embedded" / "flash_rk3588.sh"
RV1126_SCRIPT = REPO_ROOT / "tools" / "embedded" / "flash_rv1126.sh"
QUALCOMM_SCRIPT = REPO_ROOT / "tools" / "embedded" / "flash_qualcomm.sh"


class FlashMediatekScriptTest(unittest.TestCase):
    def test_script_exists_and_is_valid_bash(self) -> None:
        self.assertTrue(SCRIPT.exists(), "flash_mediatek.sh is missing")
        self.assertTrue(SCRIPT.stat().st_mode & 0o111, "flash_mediatek.sh must be executable")

        proc = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_requires_genio1200_board_acknowledgement(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--dry-run",
                "--device",
                "0e8d:0003",
                "--scatter",
                "/tmp/MT8195_Android_scatter.txt",
                "--image-dir",
                "/tmp/genio1200-deploy",
                "--download-agent",
                "/tmp/DA_BR.bin",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 2)
        self.assertIn("--board must be genio1200-evk", proc.stderr)

    def test_requires_explicit_bootrom_device(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--dry-run",
                "--board",
                "genio1200-evk",
                "--scatter",
                "/tmp/MT8195_Android_scatter.txt",
                "--image-dir",
                "/tmp/genio1200-deploy",
                "--download-agent",
                "/tmp/DA_BR.bin",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 2)
        self.assertIn("--device is required", proc.stderr)

    def test_dry_run_prints_genio_flash_scatter_plan_without_usb_writes(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--dry-run",
                "--board",
                "genio1200-evk",
                "--device",
                "0e8d:0003",
                "--scatter",
                "/tmp/MT8195_Android_scatter.txt",
                "--image-dir",
                "/tmp/genio1200-deploy",
                "--download-agent",
                "/tmp/DA_BR.bin",
                "flash",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would verify exactly one MediaTek BootROM USB device matching: 0e8d:0003", proc.stdout)
        self.assertIn(
            "[dry-run] genio-flash --device 0e8d:0003 --scatter /tmp/MT8195_Android_scatter.txt --image-dir /tmp/genio1200-deploy --download-agent /tmp/DA_BR.bin",
            proc.stdout,
        )

    def test_dry_run_prints_mtk_brom_scatter_plan(self) -> None:
        proc = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--dry-run",
                "--board",
                "genio1200-evk",
                "--device",
                "MediaTek Inc.",
                "--scatter",
                "/tmp/MT8195_Android_scatter.txt",
                "--image-dir",
                "/tmp/genio1200-deploy",
                "--download-agent",
                "/tmp/DA_BR.bin",
                "--flash-bin",
                "mtk-brom",
                "--tool-mode",
                "mtk-brom",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "[dry-run] mtk-brom flash --device MediaTek\\ Inc. --scatter /tmp/MT8195_Android_scatter.txt --image-dir /tmp/genio1200-deploy --download-agent /tmp/DA_BR.bin",
            proc.stdout,
        )

    def test_sibling_flash_scripts_stay_byte_identical(self) -> None:
        rk3588_before = RK3588_SCRIPT.read_bytes()
        rv1126_before = RV1126_SCRIPT.read_bytes()
        qualcomm_before = QUALCOMM_SCRIPT.read_bytes()

        subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(RK3588_SCRIPT.read_bytes(), rk3588_before)
        self.assertEqual(RV1126_SCRIPT.read_bytes(), rv1126_before)
        self.assertEqual(QUALCOMM_SCRIPT.read_bytes(), qualcomm_before)


if __name__ == "__main__":
    unittest.main()
