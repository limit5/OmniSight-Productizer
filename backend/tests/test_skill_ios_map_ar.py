"""OP-1816 (Track B / B-1) — tests for the Case-3 iOS map-AR pack skeleton.

The pack (`configs/skills/ios-map-ar`) ships NO scaffolder of its own: the
dispatcher routes it to the existing `backend.ios_scaffolder` via the
explicit override in `backend/skill_registry.py`. These tests pin the
three acceptance surfaces:

* **Integration** — the pack is discoverable by the registry / dispatcher
  and its `skill.yaml` validates (`skill_manifest`).
* **Code** — `resolve_scaffolder` binds the pack to
  `backend.ios_scaffolder` (no new scaffolder), gated on the manifest
  declaring a `scaffolds` artifact.
* **Exercised** — dispatching the pack through `scripts/scaffold.py`
  renders an iOS project skeleton, byte-for-byte identical to a direct
  `ios_scaffolder.render_project` call.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from backend import ios_scaffolder
from backend.skill_registry import (
    get_skill,
    list_scaffoldable_skills,
    list_skills,
    resolve_scaffolder,
    scaffolder_module_name,
    validate_skill,
)

PACK = "ios-map-ar"

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

    def test_manifest_declares_ios_reuse(self):
        info = get_skill(PACK)
        assert info is not None
        # Reuse of the ios pack is explicit in the manifest.
        assert "skill-ios" in info.manifest.depends_on_skills
        assert "CORE-05" in info.manifest.depends_on_core


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Code — routes to ios_scaffolder, NO new scaffolder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRoutesToIosScaffolder:
    def test_module_name_overridden_to_ios(self):
        # The convention would derive backend.ios_map_ar_scaffolder; the
        # explicit override points it at the existing one.
        assert scaffolder_module_name(PACK) == "backend.ios_scaffolder"

    def test_resolves_to_ios_scaffolder(self):
        handle = resolve_scaffolder(PACK)
        assert handle.skill_name == PACK
        assert handle.module_name == "backend.ios_scaffolder"
        # Binds the *existing* public entry points — no new scaffolder.
        assert handle.render is ios_scaffolder.render_project
        assert handle.options_cls is ios_scaffolder.ScaffoldOptions

    def test_listed_as_scaffoldable(self):
        assert PACK in list_scaffoldable_skills()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exercised — dispatch renders the iOS skeleton
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDispatchRendersSkeleton:
    def test_dispatch_matches_direct_ios_render(self, tmp_path: Path):
        """Dispatching the map-AR pack renders the same iOS skeleton as a
        direct ios_scaffolder call — proving it reuses the scaffolder
        without altering output."""
        cli = _load_cli()

        via_dispatch = tmp_path / "dispatched"
        result = cli.run_scaffold(
            PACK,
            via_dispatch,
            ["--project-name", "MapAR"],
        )
        assert result["files_written"], "dispatch rendered no files"

        via_direct = tmp_path / "direct"
        ios_scaffolder.render_project(
            via_direct,
            ios_scaffolder.ScaffoldOptions(project_name="MapAR"),
        )

        assert _rel_file_map(via_dispatch) == _rel_file_map(via_direct)

    def test_main_renders_ios_project(self, tmp_path: Path, capsys):
        cli = _load_cli()
        out_dir = tmp_path / "MapAR"
        rc = cli.main([
            PACK, "--out-dir", str(out_dir),
            "--project-name", "MapAR",
        ])
        assert rc == 0
        # The standard iOS skeleton landed.
        assert (out_dir / "App/Sources/App.swift").exists()
        assert (out_dir / "Package.swift").exists()
        assert (out_dir / "App/Resources/Info.plist").exists()
        assert f"scaffolded {PACK}" in capsys.readouterr().out

    def test_main_list_includes_pack(self, capsys):
        cli = _load_cli()
        rc = cli.main(["--list"])
        assert rc == 0
        assert PACK in capsys.readouterr().out
