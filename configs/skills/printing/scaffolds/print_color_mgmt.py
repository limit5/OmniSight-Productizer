"""Print Color Management — OmniSight Print Pipeline (C20).

ICC profile selection and application for print output.
Selects the correct profile based on paper type and ink combination,
applies color space conversion (sRGB/AdobeRGB → CMYK) via rendering intent.

Usage:
    from print_color_mgmt import PrintColorManager

    mgr = PrintColorManager(profile_dir="/path/to/profiles")
    profile = mgr.select_profile(paper="glossy", ink="cmyk_photo")
    cmyk_data = mgr.apply_profile(rgb_raster, profile)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class PrintProfile:
    paper_type: str
    ink_set: str
    icc_path: Path
    rendering_intent: str  # perceptual | relative_colorimetric | saturation | absolute_colorimetric


class PrintColorManager:
    def __init__(self, profile_dir: str | Path) -> None:
        self._profile_dir = Path(profile_dir)
        self._profiles: dict[tuple[str, str], PrintProfile] = {}

    def register_profile(self, paper: str, ink: str, icc_file: str,
                         intent: str = "perceptual") -> PrintProfile:
        prof = PrintProfile(
            paper_type=paper,
            ink_set=ink,
            icc_path=self._profile_dir / icc_file,
            rendering_intent=intent,
        )
        self._profiles[(paper, ink)] = prof
        return prof

    def select_profile(self, paper: str, ink: str) -> Optional[PrintProfile]:
        return self._profiles.get((paper, ink))

    def apply_profile(self, rgb_data: bytes, profile: PrintProfile) -> bytes:
        """Apply ICC profile to convert RGB raster → CMYK.

        Stub: real implementation uses lcms2 / Pillow ICC transform.
        This stub performs a naive RGB→CMYK conversion for testing.
        """
        if len(rgb_data) % 3 != 0:
            raise ValueError("RGB data must be multiple of 3 bytes")

        cmyk_pixels = bytearray()
        for i in range(0, len(rgb_data), 3):
            r, g, b = rgb_data[i], rgb_data[i + 1], rgb_data[i + 2]
            c = 255 - r
            m = 255 - g
            y = 255 - b
            k = min(c, m, y)
            if k == 255:
                c = m = y = 0
            else:
                c = ((c - k) * 255) // (255 - k)
                m = ((m - k) * 255) // (255 - k)
                y = ((y - k) * 255) // (255 - k)
            cmyk_pixels.extend([c, m, y, k])
        return bytes(cmyk_pixels)

    def list_profiles(self) -> list[PrintProfile]:
        return list(self._profiles.values())
