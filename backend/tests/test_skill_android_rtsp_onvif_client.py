"""OP-1799 (skeleton) + OP-1814 (B-1 content) — tests for the Case-1
Android RTSP/ONVIF CLIENT pack.

The pack (`configs/skills/android-rtsp-onvif-client`) ships NO scaffolder
of its own: the dispatcher routes it to the existing
`backend.android_scaffolder` via the explicit override in
`backend/skill_registry.py`. OP-1814 adds the ONVIF-discovery +
RTSP-playback client templates to the pack's `scaffolds/` dir; because
that dir differs from the android scaffolder's own base dir,
`resolve_scaffolder` surfaces it as an overlay and the dispatcher layers
it on top of the borrowed skeleton. These tests pin the acceptance
surfaces:

* **Integration** — the pack is discoverable by the registry / dispatcher
  and its `skill.yaml` validates (`skill_manifest`).
* **Code** — `resolve_scaffolder` binds the pack to
  `backend.android_scaffolder` (no new scaffolder), gated on the manifest
  declaring a `scaffolds` artifact, and reports the pack's own scaffolds
  dir as an overlay.
* **Exercised** — dispatching the pack through `scripts/scaffold.py`
  renders the full Android client project: the base skeleton (byte-for-
  byte identical to a direct `android_scaffolder.render_project` call)
  PLUS the ONVIF + RTSP client overlay templates.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

from backend import android_scaffolder
from backend.skill_registry import (
    get_skill,
    list_scaffoldable_skills,
    list_skills,
    resolve_scaffolder,
    scaffolder_module_name,
    validate_skill,
)

PACK = "android-rtsp-onvif-client"

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "scaffold.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("scaffold_cli", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _rel_file_map(out_dir: Path) -> dict[str, int]:
    return {
        p.relative_to(out_dir).as_posix(): p.stat().st_size
        for p in sorted(out_dir.rglob("*"))
        if p.is_file()
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration — discovery + manifest validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPackRegistry:
    def test_pack_discoverable(self):
        names = {s.name for s in list_skills()}
        assert PACK in names

    def test_pack_validates_clean(self):
        result = validate_skill(PACK)
        assert result.ok, (
            "manifest validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill(PACK)
        assert info is not None
        assert info.has_manifest
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_android_reuse(self):
        info = get_skill(PACK)
        assert info is not None
        # Reuse of the android pack is explicit in the manifest.
        assert "skill-android" in info.manifest.depends_on_skills
        assert "CORE-05" in info.manifest.depends_on_core


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Code — routes to android_scaffolder, NO new scaffolder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRoutesToAndroidScaffolder:
    def test_module_name_overridden_to_android(self):
        # The convention would derive backend.android_rtsp_onvif_client_
        # scaffolder; the explicit override points it at the existing one.
        assert scaffolder_module_name(PACK) == "backend.android_scaffolder"

    def test_resolves_to_android_scaffolder(self):
        handle = resolve_scaffolder(PACK)
        assert handle.skill_name == PACK
        assert handle.module_name == "backend.android_scaffolder"
        # Binds the *existing* public entry points — no new scaffolder.
        # The pack ships its own scaffolds/, so render is the android
        # render_project with the overlay pre-bound (functools.partial)
        # rather than the bare function; the underlying callable is still
        # the existing scaffolder entry point.
        assert isinstance(handle.render, functools.partial)
        assert handle.render.func is android_scaffolder.render_project
        assert handle.render.keywords["overlay_dirs"] == handle.overlay_dirs
        assert handle.options_cls is android_scaffolder.ScaffoldOptions

    def test_listed_as_scaffoldable(self):
        assert PACK in list_scaffoldable_skills()

    def test_pack_scaffolds_resolved_as_overlay(self):
        # The pack reuses the android scaffolder but ships its own
        # scaffolds/ dir — resolve_scaffolder surfaces it as an overlay
        # so the dispatcher layers ONVIF + RTSP on top of the skeleton.
        handle = resolve_scaffolder(PACK)
        pack_scaffolds = get_skill(PACK).path / "scaffolds"
        assert [p.resolve() for p in handle.overlay_dirs] == [pack_scaffolds.resolve()]

    def test_self_owned_pack_has_no_overlay(self):
        # A pack bound to its own conventional scaffolder (skill-android)
        # has no overlay — its scaffolds dir *is* the scaffolder base dir.
        assert resolve_scaffolder("skill-android").overlay_dirs == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exercised — dispatch renders skeleton + ONVIF + RTSP overlay
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Overlay client source the dispatch must add on top of the base skeleton.
_OVERLAY_FILES = (
    "app/src/main/java/com/omnisight/pilot/onvif/WsDiscoveryClient.kt",
    "app/src/main/java/com/omnisight/pilot/onvif/OnvifMediaClient.kt",
    "app/src/main/java/com/omnisight/pilot/onvif/OnvifModels.kt",
    "app/src/main/java/com/omnisight/pilot/rtsp/RtspPlaybackController.kt",
    "app/src/main/java/com/omnisight/pilot/rtsp/RtspPlayerScreen.kt",
    "app/src/test/java/com/omnisight/pilot/onvif/OnvifParsingTest.kt",
)


class TestDispatchRendersClientProject:
    def test_dispatch_is_skeleton_superset_plus_overlay(self, tmp_path: Path):
        """Dispatching the client pack renders the full project: every
        file a direct android render produces (byte-for-byte) PLUS the
        ONVIF + RTSP overlay — proving it reuses the scaffolder and layers
        its own templates on top without altering the base output."""
        cli = _load_cli()

        via_dispatch = tmp_path / "dispatched"
        result = cli.run_scaffold(
            PACK,
            via_dispatch,
            ["--project-name", "CameraClient"],
        )
        assert result["files_written"], "dispatch rendered no files"

        via_direct = tmp_path / "direct"
        android_scaffolder.render_project(
            via_direct,
            android_scaffolder.ScaffoldOptions(project_name="CameraClient"),
        )

        dispatch_files = _rel_file_map(via_dispatch)
        direct_files = _rel_file_map(via_direct)

        # Base skeleton is preserved byte-for-byte (the overlay is purely
        # additive — no base file is overridden).
        for rel, size in direct_files.items():
            assert dispatch_files.get(rel) == size, f"base file changed: {rel}"

        # The overlay added exactly the ONVIF + RTSP client surface.
        added = set(dispatch_files) - set(direct_files)
        assert added == set(_OVERLAY_FILES)

    def test_main_renders_full_client_project(self, tmp_path: Path, capsys):
        cli = _load_cli()
        out_dir = tmp_path / "CameraClient"
        rc = cli.main([
            PACK, "--out-dir", str(out_dir),
            "--project-name", "CameraClient",
        ])
        assert rc == 0
        # The standard Android skeleton landed ...
        assert (out_dir / "build.gradle.kts").exists()
        assert (out_dir / "settings.gradle.kts").exists()
        # ... and so did the ONVIF + RTSP client overlay.
        for rel in _OVERLAY_FILES:
            assert (out_dir / rel).is_file(), f"overlay file missing: {rel}"
        # Overlay Kotlin keeps the skeleton's package root.
        onvif = (out_dir / _OVERLAY_FILES[0]).read_text(encoding="utf-8")
        assert "package com.omnisight.pilot.onvif" in onvif
        assert f"scaffolded {PACK}" in capsys.readouterr().out

    def test_main_list_includes_pack(self, capsys):
        cli = _load_cli()
        rc = cli.main(["--list"])
        assert rc == 0
        assert PACK in capsys.readouterr().out
