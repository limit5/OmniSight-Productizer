"""OP-2074 regression checks for uvcvideo-xu-dispatcher bbappend overrides."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BBAPPEND_DIR = (
    REPO_ROOT
    / "yocto"
    / "meta-omnisight-camera"
    / "recipes-core"
    / "uvcvideo-xu-dispatcher"
)
BBAPPENDS = (
    "uvcvideo-xu-dispatcher_%.bbappend",
    "uvcvideo-xu-dispatcher_rv1126.bbappend",
    "uvcvideo-xu-dispatcher_qcs6490.bbappend",
    "uvcvideo-xu-dispatcher_genio1200.bbappend",
)
KNOWN_MACHINES = ("rk3576", "rk3588", "rv1126", "qcs6490", "genio1200")
UNDERSCORE_OVERRIDE_RE = re.compile(
    rf"^[A-Za-z0-9_]+\[[^\]]+\]_append_({'|'.join(KNOWN_MACHINES)})\b"
    rf"|^[A-Za-z0-9_]+_append_({'|'.join(KNOWN_MACHINES)})\b"
    rf"|^[A-Za-z0-9_]+_({'|'.join(KNOWN_MACHINES)})\b",
    re.MULTILINE,
)


class UvcvideoXuDispatcherOverrideSyntaxTest(unittest.TestCase):
    def test_bbappends_use_colon_override_syntax_for_known_machines(self) -> None:
        for filename in BBAPPENDS:
            with self.subTest(filename=filename):
                content = (BBAPPEND_DIR / filename).read_text(encoding="utf-8")

                self.assertIsNone(
                    UNDERSCORE_OVERRIDE_RE.search(content),
                    f"{filename} contains deprecated underscore override syntax",
                )

