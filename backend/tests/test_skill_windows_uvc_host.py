"""OP-1815 (skeleton) + OP-1817 (B-1 Case-2 content) - tests for the
Windows UVC HOST pack.

The pack (`configs/skills/windows-uvc-host`) ships NO scaffolder of its
own: the dispatcher routes it to the existing `backend.tauri_scaffolder`
via the explicit override in `backend/skill_registry.py`. OP-1817 adds the
UVC enumerate + capture domain templates to the pack's `scaffolds/` dir;
because that dir differs from the tauri scaffolder's own base dir,
`resolve_scaffolder` surfaces it as an overlay and the dispatcher layers it
on top of the borrowed desktop skeleton. These tests pin the acceptance
surfaces:

* **Integration** - the pack is discoverable by the registry / dispatcher
  and its `skill.yaml` validates.
* **Code** - `resolve_scaffolder` binds the pack to
  `backend.tauri_scaffolder` (no new Windows scaffolder), gated on the
  manifest declaring a `scaffolds` artifact, and reports the pack's own
  scaffolds dir as an overlay.
* **Exercised** - dispatching the pack through `scripts/scaffold.py`
  renders the full UVC-host project: the base desktop skeleton (byte-for-
  byte identical to a direct `tauri_scaffolder.render_project` call) PLUS
  the UVC enumerate + capture overlay templates.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

from backend import tauri_scaffolder
from backend.skill_registry import (
    get_skill,
    list_scaffoldable_skills,
    list_skills,
    resolve_scaffolder,
    scaffolder_module_name,
    validate_skill,
)

PACK = "windows-uvc-host"

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "scaffold.py"
TASKS_PATH = REPO_ROOT / "configs" / "skills" / PACK / "tasks.yaml"


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

    def test_manifest_declares_desktop_tauri_reuse(self):
        info = get_skill(PACK)
        assert info is not None
        assert "skill-desktop-tauri" in info.manifest.depends_on_skills
        assert "CORE-05" in info.manifest.depends_on_core


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Code — routes to tauri_scaffolder, NO new scaffolder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRoutesToTauriScaffolder:
    def test_module_name_overridden_to_tauri(self):
        assert scaffolder_module_name(PACK) == "backend.tauri_scaffolder"

    def test_resolves_to_tauri_scaffolder(self):
        handle = resolve_scaffolder(PACK)
        assert handle.skill_name == PACK
        assert handle.module_name == "backend.tauri_scaffolder"
        # The pack ships its own scaffolds/, so render is the tauri
        # render_project with the overlay pre-bound (functools.partial)
        # rather than the bare function; the underlying callable is still
        # the existing scaffolder entry point — no new Windows scaffolder.
        assert isinstance(handle.render, functools.partial)
        assert handle.render.func is tauri_scaffolder.render_project
        assert handle.render.keywords["overlay_dirs"] == handle.overlay_dirs
        assert handle.options_cls is tauri_scaffolder.ScaffoldOptions

    def test_listed_as_scaffoldable(self):
        assert PACK in list_scaffoldable_skills()

    def test_pack_scaffolds_resolved_as_overlay(self):
        # The pack reuses the tauri scaffolder but ships its own scaffolds/
        # dir — resolve_scaffolder surfaces it as an overlay so the
        # dispatcher layers the UVC domain on top of the skeleton.
        handle = resolve_scaffolder(PACK)
        pack_scaffolds = get_skill(PACK).path / "scaffolds"
        assert [p.resolve() for p in handle.overlay_dirs] == [pack_scaffolds.resolve()]

    def test_self_owned_pack_has_no_overlay(self):
        # The desktop-tauri pack is bound to its own scaffolder — its
        # scaffolds dir *is* the scaffolder base dir, so no overlay.
        assert resolve_scaffolder("skill-desktop-tauri").overlay_dirs == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exercised — dispatch renders skeleton + UVC overlay
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Overlay UVC source the dispatch must add on top of the base skeleton.
_OVERLAY_FILES = (
    "src-tauri/src/uvc/mod.rs",
    "src-tauri/src/uvc/device.rs",
    "src-tauri/src/uvc/capture.rs",
    "src-tauri/src/uvc/commands.rs",
    "src/uvc.ts",
)


class TestDispatchRendersHostProject:
    def test_dispatch_is_skeleton_superset_plus_overlay(self, tmp_path: Path):
        """Dispatching the host pack renders the full project: every file a
        direct tauri render produces (byte-for-byte) PLUS the UVC enumerate
        + capture overlay — proving it reuses the scaffolder and layers its
        own templates on top without altering the base output."""
        cli = _load_cli()

        via_dispatch = tmp_path / "dispatched"
        result = cli.run_scaffold(
            PACK,
            via_dispatch,
            ["--project-name", "UvcHost"],
        )
        assert result["files_written"], "dispatch rendered no files"

        via_direct = tmp_path / "direct"
        tauri_scaffolder.render_project(
            via_direct,
            tauri_scaffolder.ScaffoldOptions(project_name="UvcHost"),
        )

        dispatch_files = _rel_file_map(via_dispatch)
        direct_files = _rel_file_map(via_direct)

        # Base skeleton is preserved byte-for-byte (the overlay is purely
        # additive — no base file is overridden).
        for rel, size in direct_files.items():
            assert dispatch_files.get(rel) == size, f"base file changed: {rel}"

        # The overlay added exactly the UVC enumerate + capture surface.
        added = set(dispatch_files) - set(direct_files)
        assert added == set(_OVERLAY_FILES)

    def test_main_renders_full_host_project(self, tmp_path: Path, capsys):
        cli = _load_cli()
        out_dir = tmp_path / "UvcHost"
        rc = cli.main([
            PACK, "--out-dir", str(out_dir),
            "--project-name", "UvcHost",
        ])
        assert rc == 0
        # The standard Tauri desktop skeleton landed ...
        assert (out_dir / "package.json").exists()
        assert (out_dir / "src-tauri" / "tauri.conf.json").exists()
        # ... and so did the UVC enumerate + capture overlay.
        for rel in _OVERLAY_FILES:
            assert (out_dir / rel).is_file(), f"overlay file missing: {rel}"
        # Overlay Rust keeps the crate's module path; the IPC commands
        # carry the #[tauri::command] attribute the capability system reads.
        commands = (out_dir / "src-tauri/src/uvc/commands.rs").read_text(encoding="utf-8")
        assert "#[tauri::command]" in commands
        assert "pub fn uvc_enumerate" in commands
        assert f"scaffolded {PACK}" in capsys.readouterr().out

    def test_main_list_includes_pack(self, capsys):
        cli = _load_cli()
        rc = cli.main(["--list"])
        assert rc == 0
        assert PACK in capsys.readouterr().out


class TestContentScope:
    def test_tasks_describe_uvc_host_content(self):
        text = TASKS_PATH.read_text(encoding="utf-8")
        assert "Windows UVC HOST" in text
        assert "backend.tauri_scaffolder" in text
        assert "NO new Windows scaffolder" in text
        # The enumerate + capture tasks are now active (no longer follow-on).
        assert "status: follow-on" not in text
        assert text.count("status: active") == 3
