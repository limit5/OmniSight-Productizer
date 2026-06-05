"""OP-2075 - uvcvideo-xu-dispatcher Yocto source resolution."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RECIPE = (
    REPO_ROOT
    / "yocto"
    / "meta-omnisight-camera"
    / "recipes-core"
    / "uvcvideo-xu-dispatcher"
    / "uvcvideo-xu-dispatcher.bb"
)
SOURCE_TREE = REPO_ROOT / "src" / "embedded" / "uvc-xu-dispatcher"


def _recipe_text() -> str:
    return RECIPE.read_text(encoding="utf-8")


def _assignment(name: str, text: str) -> str:
    match = re.search(rf"^{re.escape(name)}\s*(?:\?=|=)\s*\"([^\"]*)\"$", text, re.MULTILINE)
    assert match is not None, f"{name} assignment is missing"
    return match.group(1)


def test_dispatcher_recipe_uses_externalsrc_instead_of_file_fetch() -> None:
    text = _recipe_text()

    assert _assignment("SRC_URI", text) == ""
    assert "file://uvcvideo-xu-dispatcher" not in text
    assert re.search(r"^inherit\s+.*\bexternalsrc\b", text, re.MULTILINE)
    assert _assignment("S", text) == "${EXTERNALSRC}"
    assert _assignment("EXTERNALSRC", text) == "${OMNISIGHT_UVC_XU_DISPATCHER_EXTERNALSRC}"


def test_dispatcher_externalsrc_points_at_in_tree_source() -> None:
    text = _recipe_text()
    externalsrc = _assignment("OMNISIGHT_UVC_XU_DISPATCHER_EXTERNALSRC", text)

    assert externalsrc == "${THISDIR}/../../../../src/embedded/uvc-xu-dispatcher"
    resolved = (RECIPE.parent / externalsrc.removeprefix("${THISDIR}/")).resolve()
    assert resolved == SOURCE_TREE.resolve()
    assert (resolved / "main.c").is_file()
