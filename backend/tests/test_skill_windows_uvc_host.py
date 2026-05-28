"""OP-1815 (Track B / B-1) - tests for the Windows UVC HOST skeleton pack.

The pack (`configs/skills/windows-uvc-host`) ships NO scaffolder of its
own: the dispatcher routes it to the existing `backend.tauri_scaffolder`
via the explicit override in `backend/skill_registry.py`. These tests pin
the acceptance surfaces:

* **Code** - registry/dispatcher discovery, manifest validation, and
  routing to `backend.tauri_scaffolder` (no new Windows scaffolder).
* **Exercised** - dispatching through `scripts/scaffold.py` renders the
  same desktop skeleton as a direct `tauri_scaffolder.render_project`.
* **Scope** - the pack documents skeleton-only UVC host work; capture
  domain code remains follow-on.
"""

from __future__ import annotations

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


class TestRoutesToTauriScaffolder:
    def test_module_name_overridden_to_tauri(self):
        assert scaffolder_module_name(PACK) == "backend.tauri_scaffolder"

    def test_resolves_to_tauri_scaffolder(self):
        handle = resolve_scaffolder(PACK)
        assert handle.skill_name == PACK
        assert handle.module_name == "backend.tauri_scaffolder"
        assert handle.render is tauri_scaffolder.render_project
        assert handle.options_cls is tauri_scaffolder.ScaffoldOptions

    def test_listed_as_scaffoldable(self):
        assert PACK in list_scaffoldable_skills()


class TestDispatchRendersDesktopSkeleton:
    def test_dispatch_matches_direct_tauri_render(self, tmp_path: Path):
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

        assert _rel_file_map(via_dispatch) == _rel_file_map(via_direct)

    def test_main_renders_desktop_project(self, tmp_path: Path, capsys):
        cli = _load_cli()
        out_dir = tmp_path / "UvcHost"
        rc = cli.main([
            PACK, "--out-dir", str(out_dir),
            "--project-name", "UvcHost",
        ])
        assert rc == 0
        assert (out_dir / "package.json").exists()
        assert (out_dir / "src-tauri" / "tauri.conf.json").exists()
        assert f"scaffolded {PACK}" in capsys.readouterr().out

    def test_main_list_includes_pack(self, capsys):
        cli = _load_cli()
        rc = cli.main(["--list"])
        assert rc == 0
        assert PACK in capsys.readouterr().out


class TestSkeletonScope:
    def test_tasks_describe_windows_uvc_host_skeleton(self):
        text = TASKS_PATH.read_text(encoding="utf-8")
        assert "Windows UVC HOST" in text
        assert "backend.tauri_scaffolder" in text
        assert "NO new Windows scaffolder" in text
        assert "status: follow-on" in text
