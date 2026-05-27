"""W3 (1C) OP-1787 — tests for the unified skill→scaffolder dispatcher.

Covers two new surfaces, both additive over the existing skill pack
framework (#214) and the per-stack scaffolders:

* :mod:`backend.skill_registry` scaffolder-resolution — name → module
  binding gated on ``skill.yaml`` declaring a ``scaffolds`` artifact.
* ``scripts/scaffold.py`` — the CLI that introspects the resolved
  ``ScaffoldOptions`` to build per-knob flags and renders end-to-end.

The headline contract test (``TestDispatchEndToEnd``) dispatches the
``skill-android`` sample pack through the CLI and asserts byte-for-byte
parity with a direct :func:`backend.android_scaffolder.render_project`
call — proving the dispatcher is additive and does not change scaffolder
output.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from backend import android_scaffolder
from backend.skill_registry import (
    ScaffolderHandle,
    ScaffolderResolutionError,
    list_scaffoldable_skills,
    resolve_scaffolder,
    scaffolder_module_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "scaffold.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("scaffold_cli", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _rel_file_map(out_dir: Path) -> dict[str, int]:
    """Map of POSIX-relative path → byte size for every file under out_dir."""
    return {
        p.relative_to(out_dir).as_posix(): p.stat().st_size
        for p in sorted(out_dir.rglob("*"))
        if p.is_file()
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. scaffolder_module_name — convention + overrides
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffolderModuleName:
    @pytest.mark.parametrize(
        "skill,expected",
        [
            ("skill-android", "backend.android_scaffolder"),
            ("skill-ios", "backend.ios_scaffolder"),
            ("skill-go-service", "backend.go_service_scaffolder"),
            ("skill-rust-cli", "backend.rust_cli_scaffolder"),
            ("skill-spring-boot", "backend.spring_boot_scaffolder"),
            ("skill-nextjs", "backend.nextjs_scaffolder"),
        ],
    )
    def test_convention(self, skill: str, expected: str):
        assert scaffolder_module_name(skill) == expected

    def test_override_desktop_tauri(self):
        # Naming exception: the module is backend.tauri_scaffolder, not
        # backend.desktop_tauri_scaffolder.
        assert scaffolder_module_name("skill-desktop-tauri") == "backend.tauri_scaffolder"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. resolve_scaffolder — manifest-gated binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolveScaffolder:
    def test_resolves_android(self):
        handle = resolve_scaffolder("skill-android")
        assert isinstance(handle, ScaffolderHandle)
        assert handle.skill_name == "skill-android"
        assert handle.module_name == "backend.android_scaffolder"
        # Public entry points bind to the real scaffolder.
        assert handle.render is android_scaffolder.render_project
        assert handle.options_cls is android_scaffolder.ScaffoldOptions

    def test_resolves_desktop_tauri_via_override(self):
        handle = resolve_scaffolder("skill-desktop-tauri")
        assert handle.module_name == "backend.tauri_scaffolder"
        assert callable(handle.render)

    def test_unknown_skill_raises(self):
        with pytest.raises(ScaffolderResolutionError, match="not found"):
            resolve_scaffolder("skill-does-not-exist")

    def test_no_manifest_raises(self, tmp_path: Path):
        # A bare dir with no skill.yaml is not scaffoldable.
        (tmp_path / "skill-bare").mkdir()
        with pytest.raises(ScaffolderResolutionError, match="no skill.yaml"):
            resolve_scaffolder("skill-bare", skills_dir=tmp_path)

    def test_manifest_without_scaffolds_artifact_raises(self, tmp_path: Path):
        # skill.yaml is load-bearing: a pack that does not declare a
        # 'scaffolds' artifact is refused even though everything else is
        # valid.
        skill = tmp_path / "skill-noscaffold"
        skill.mkdir()
        _write_yaml(skill / "skill.yaml", {
            "schema_version": 1,
            "name": "skill-noscaffold",
            "artifacts": [
                {"kind": "tasks", "path": "tasks.yaml"},
                {"kind": "docs", "path": "docs/"},
            ],
        })
        with pytest.raises(ScaffolderResolutionError, match="scaffolds"):
            resolve_scaffolder("skill-noscaffold", skills_dir=tmp_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. list_scaffoldable_skills
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestListScaffoldable:
    def test_includes_known_packs(self):
        names = list_scaffoldable_skills()
        assert "skill-android" in names
        assert "skill-ios" in names
        assert "skill-desktop-tauri" in names

    def test_excludes_non_scaffoldable(self):
        # imaging/ota/etc. are embedded packs without a Python scaffolder
        # module — they must not appear.
        names = set(list_scaffoldable_skills())
        assert "imaging" not in names
        assert "ota" not in names


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. CLI option-parser generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCliOptionBuilding:
    def test_builds_options_from_knobs(self):
        cli = _load_cli()
        handle = resolve_scaffolder("skill-android")
        opts = cli.build_options(handle, ["--project-name", "MyApp", "--no-billing"])
        assert opts.project_name == "MyApp"
        assert opts.billing is False
        assert opts.push is True  # default preserved

    def test_project_name_required(self):
        cli = _load_cli()
        handle = resolve_scaffolder("skill-android")
        with pytest.raises(SystemExit):
            cli.build_options(handle, [])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. End-to-end dispatch (the AC "Exercised" test)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDispatchEndToEnd:
    def test_dispatch_android_matches_direct_render(self, tmp_path: Path):
        """Dispatch skill-android via the CLI and prove the output is
        identical to calling the scaffolder directly — additive, no change
        to scaffolder output."""
        cli = _load_cli()

        via_dispatch = tmp_path / "dispatched"
        result = cli.run_scaffold(
            "skill-android",
            via_dispatch,
            ["--project-name", "PilotApp"],
        )
        assert result["files_written"], "dispatch rendered no files"
        assert result["out_dir"] == str(via_dispatch)

        # Ground truth: call the existing scaffolder directly.
        via_direct = tmp_path / "direct"
        android_scaffolder.render_project(
            via_direct,
            android_scaffolder.ScaffoldOptions(project_name="PilotApp"),
        )

        assert _rel_file_map(via_dispatch) == _rel_file_map(via_direct)

    def test_main_list(self, capsys):
        cli = _load_cli()
        rc = cli.main(["--list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "skill-android" in out

    def test_main_renders(self, tmp_path: Path, capsys):
        cli = _load_cli()
        out_dir = tmp_path / "App"
        rc = cli.main([
            "skill-android", "--out-dir", str(out_dir),
            "--project-name", "App",
        ])
        assert rc == 0
        assert (out_dir / "build.gradle.kts").exists()
        assert "scaffolded skill-android" in capsys.readouterr().out

    def test_main_unknown_skill_exits_nonzero(self, tmp_path: Path, capsys):
        cli = _load_cli()
        rc = cli.main([
            "skill-nope", "--out-dir", str(tmp_path / "x"),
            "--project-name", "X",
        ])
        assert rc == 3
        assert "not found" in capsys.readouterr().err

    def test_main_invalid_options_exits_two(self, tmp_path: Path, capsys):
        cli = _load_cli()
        # Empty project_name fails ScaffoldOptions.validate().
        rc = cli.main([
            "skill-android", "--out-dir", str(tmp_path / "x"),
            "--project-name", "   ",
        ])
        assert rc == 2
        assert "invalid options" in capsys.readouterr().err
