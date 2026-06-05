"""OP-2081 - omnisight-camera-image Yocto image recipe."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RECIPE = (
    REPO_ROOT
    / "yocto"
    / "meta-omnisight-camera"
    / "recipes-core"
    / "images"
    / "omnisight-camera-image.bb"
)


def _recipe_text() -> str:
    return RECIPE.read_text(encoding="utf-8")


def _assignment(name: str, text: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\s*(?:\?=|=)\s*\"([^\"]*)\"$",
        text,
        re.MULTILINE,
    )
    assert match is not None, f"{name} assignment is missing"
    return match.group(1)


def _words(value: str) -> set[str]:
    return set(value.split())


def test_camera_image_recipe_declares_core_image_minimal_base() -> None:
    text = _recipe_text()

    assert _assignment("DESCRIPTION", text) == (
        "OmniSight camera reference image with UVC-XU dispatcher + v4l-utils"
    )
    assert _assignment("LICENSE", text) == "MIT"
    assert re.search(r"^inherit\s+.*\bcore-image\b", text, re.MULTILINE)
    assert re.search(
        r"^require\s+recipes-core/images/core-image-minimal\.bb$",
        text,
        re.MULTILINE,
    )


def test_camera_image_installs_uvc_stack_and_bringup_features() -> None:
    text = _recipe_text()

    image_install = _words(_assignment("IMAGE_INSTALL:append", text))
    assert {
        "uvcvideo-xu-dispatcher",
        "v4l-utils",
        "kernel-modules",
        "openssh-sftp-server",
        "udev",
    }.issubset(image_install)

    image_features = _words(_assignment("IMAGE_FEATURES:append", text))
    assert {"ssh-server-openssh", "debug-tweaks"}.issubset(image_features)
    assert _assignment("IMAGE_LINGUAS", text) == "en-us"
    assert _assignment("IMAGE_INSTALL:append:rk3588", text) == " kernel-modules"
