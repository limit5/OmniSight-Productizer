"""OP-1816 (skeleton) + OP-1820 (B-1 content) — tests for the Case-3
iOS map-AR pack.

The pack (`configs/skills/ios-map-ar`) ships NO scaffolder of its own: the
dispatcher routes it to the existing `backend.ios_scaffolder` via the
explicit override in `backend/skill_registry.py`. OP-1820 adds ARKit +
MapKit domain templates to the pack's `scaffolds/` dir; because that dir
differs from the iOS scaffolder's own base dir, `resolve_scaffolder`
surfaces it as an overlay and the dispatcher layers it on top of the
borrowed skeleton. These tests pin the acceptance surfaces:

* **Integration** — the pack is discoverable by the registry / dispatcher
  and its `skill.yaml` validates (`skill_manifest`).
* **Code** — `resolve_scaffolder` binds the pack to
  `backend.ios_scaffolder` (no new scaffolder), gated on the manifest
  declaring a `scaffolds` artifact.
* **Exercised** — dispatching the pack through `scripts/scaffold.py`
  renders the full iOS map-AR project: the base skeleton plus the ARKit +
  MapKit overlay templates.
"""

from __future__ import annotations

import importlib.util
import functools
from pathlib import Path

import yaml

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
PACK_DIR = REPO_ROOT / "configs" / "skills" / PACK


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

    def test_tasks_activate_mapkit_and_arkit_layers(self):
        tasks_doc = yaml.safe_load((PACK_DIR / "tasks.yaml").read_text(encoding="utf-8"))
        tasks = {task["id"]: task for task in tasks_doc["tasks"]}

        assert tasks["ios-mapkit-map-view"]["status"] == "active"
        assert tasks["ios-mapkit-map-view"]["depends_on"] == ["ios-map-ar-scaffold-init"]
        assert tasks["ios-mapkit-map-view"]["artifacts"] == ["ios_mapkit"]

        assert tasks["ios-arkit-ar-view"]["status"] == "active"
        assert tasks["ios-arkit-ar-view"]["depends_on"] == ["ios-mapkit-map-view"]
        assert tasks["ios-arkit-ar-view"]["artifacts"] == ["ios_arkit"]


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
        # The pack ships its own scaffolds/, so render is the iOS
        # render_project with the overlay pre-bound (functools.partial)
        # rather than the bare function; the underlying callable is still
        # the existing scaffolder entry point.
        assert isinstance(handle.render, functools.partial)
        assert handle.render.func is ios_scaffolder.render_project
        assert handle.render.keywords["overlay_dirs"] == handle.overlay_dirs
        assert handle.options_cls is ios_scaffolder.ScaffoldOptions

    def test_listed_as_scaffoldable(self):
        assert PACK in list_scaffoldable_skills()

    def test_pack_scaffolds_resolved_as_overlay(self):
        handle = resolve_scaffolder(PACK)
        pack_scaffolds = get_skill(PACK).path / "scaffolds"
        assert [p.resolve() for p in handle.overlay_dirs] == [pack_scaffolds.resolve()]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exercised — dispatch renders skeleton + ARKit + MapKit overlay
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_OVERLAY_FILES = (
    "App/Resources/Info.plist",
    "App/Sources/ContentView.swift",
    "App/Sources/MapAR/ARKitOverlayView.swift",
    "App/Sources/MapAR/MapARHomeView.swift",
    "App/Sources/MapAR/MapKitMapView.swift",
    "App/Sources/MapAR/MapLocationStore.swift",
    "App/Sources/MapAR/PointOfInterest.swift",
    "Modules/MapARCore/Package.swift",
    "Modules/MapARCore/Sources/MapARCore/GeoCoordinate.swift",
    "Modules/MapARCore/Sources/MapARCore/PointOfInterest.swift",
    "Modules/MapARCore/Tests/MapARCoreTests/GeoCoordinateTests.swift",
    "project.yml",
    "Tests/ContentViewTests.swift",
    "Tests/MapLocationStoreTests.swift",
    "UITests/SmokeTests.swift",
)

_OVERLAY_OVERRIDES = {
    "App/Resources/Info.plist",
    "App/Sources/ContentView.swift",
    "project.yml",
    "Tests/ContentViewTests.swift",
    "UITests/SmokeTests.swift",
}


class TestDispatchRendersMapARProject:
    def test_dispatch_is_skeleton_plus_map_ar_overlay(self, tmp_path: Path):
        """Dispatching the map-AR pack renders the full project: the base
        iOS skeleton plus the ARKit + MapKit overlay. Non-overridden base
        files stay byte-for-byte identical to a direct iOS render."""
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

        dispatch_files = _rel_file_map(via_dispatch)
        direct_files = _rel_file_map(via_direct)

        for rel, size in direct_files.items():
            if rel in _OVERLAY_OVERRIDES:
                continue
            assert dispatch_files.get(rel) == size, f"base file changed: {rel}"

        added = set(dispatch_files) - set(direct_files)
        assert added == set(_OVERLAY_FILES) - _OVERLAY_OVERRIDES

    def test_main_renders_full_map_ar_project(self, tmp_path: Path, capsys):
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
        # ... and so did the ARKit + MapKit overlay.
        for rel in _OVERLAY_FILES:
            assert (out_dir / rel).is_file(), f"overlay file missing: {rel}"
        content = (out_dir / "App/Sources/ContentView.swift").read_text(encoding="utf-8")
        assert "MapARHomeView()" in content
        info = (out_dir / "App/Resources/Info.plist").read_text(encoding="utf-8")
        assert "NSCameraUsageDescription" in info
        assert "NSLocationWhenInUseUsageDescription" in info
        map_ar = (out_dir / "App/Sources/MapAR/MapARHomeView.swift").read_text(
            encoding="utf-8"
        )
        assert "MapKitMapView(store: store)" in map_ar
        assert "ARKitOverlayView(" in map_ar
        assert f"scaffolded {PACK}" in capsys.readouterr().out

    def test_map_ar_core_package_is_rendered(self, tmp_path: Path):
        cli = _load_cli()
        out_dir = tmp_path / "MapAR"
        rc = cli.main([
            PACK, "--out-dir", str(out_dir),
            "--project-name", "MapAR",
        ])
        assert rc == 0

        package = (out_dir / "Modules/MapARCore/Package.swift").read_text(
            encoding="utf-8"
        )
        geo = (
            out_dir / "Modules/MapARCore/Sources/MapARCore/GeoCoordinate.swift"
        ).read_text(encoding="utf-8")
        poi = (
            out_dir / "Modules/MapARCore/Sources/MapARCore/PointOfInterest.swift"
        ).read_text(encoding="utf-8")

        assert 'name: "MapARCore"' in package
        assert '.library(name: "MapARCore", targets: ["MapARCore"])' in package
        assert "public struct GeoCoordinate" in geo
        assert "public struct PointOfInterest" in poi

    def test_map_ar_core_has_no_apple_framework_imports(self, tmp_path: Path):
        cli = _load_cli()
        out_dir = tmp_path / "MapAR"
        rc = cli.main([
            PACK, "--out-dir", str(out_dir),
            "--project-name", "MapAR",
        ])
        assert rc == 0

        apple_frameworks = {
            "ARKit",
            "CoreLocation",
            "MapKit",
            "RealityKit",
            "SwiftUI",
            "UIKit",
        }
        core_dir = out_dir / "Modules/MapARCore"
        for swift_file in core_dir.rglob("*.swift"):
            text = swift_file.read_text(encoding="utf-8")
            for framework in apple_frameworks:
                assert f"import {framework}" not in text, (
                    f"{swift_file.relative_to(out_dir)} imports {framework}"
                )

    def test_project_yml_wires_map_ar_core_package(self, tmp_path: Path):
        cli = _load_cli()
        out_dir = tmp_path / "MapAR"
        rc = cli.main([
            PACK, "--out-dir", str(out_dir),
            "--project-name", "MapAR",
        ])
        assert rc == 0

        project = yaml.safe_load((out_dir / "project.yml").read_text(encoding="utf-8"))
        assert project["packages"]["MapARCore"] == {"path": "Modules/MapARCore"}
        app_deps = project["targets"]["MapAR"]["dependencies"]
        test_deps = project["targets"]["MapARTests"]["dependencies"]
        assert {"package": "MapARCore", "product": "MapARCore"} in app_deps
        assert {"package": "MapARCore", "product": "MapARCore"} in test_deps

    def test_corelocation_conversion_stays_in_app_target(self, tmp_path: Path):
        cli = _load_cli()
        out_dir = tmp_path / "MapAR"
        rc = cli.main([
            PACK, "--out-dir", str(out_dir),
            "--project-name", "MapAR",
        ])
        assert rc == 0

        bridge = (out_dir / "App/Sources/MapAR/PointOfInterest.swift").read_text(
            encoding="utf-8"
        )
        store = (out_dir / "App/Sources/MapAR/MapLocationStore.swift").read_text(
            encoding="utf-8"
        )
        assert "import CoreLocation" in bridge
        assert "extension GeoCoordinate" in bridge
        assert "import MapARCore" in store
        assert "CLLocationManager" in store

    def test_dispatch_storekit_push_off_overrides_stale_base_tests(
        self,
        tmp_path: Path,
    ):
        cli = _load_cli()
        out_dir = tmp_path / "MapAR"
        result = cli.run_scaffold(
            PACK,
            out_dir,
            [
                "--project-name", "MapAR",
                "--no-storekit",
                "--no-push",
            ],
        )

        assert result["files_written"], "dispatch rendered no files"
        assert not (out_dir / "App/Sources/StoreKit/StoreView.swift").exists()
        assert not (out_dir / "App/Sources/Push/AppDelegate.swift").exists()

        content_tests = (out_dir / "Tests/ContentViewTests.swift").read_text(
            encoding="utf-8"
        )
        assert "FeatureCounter" not in content_tests
        assert "MapLocationStore" in content_tests
        assert "@MainActor" in content_tests

        smoke_tests = (out_dir / "UITests/SmokeTests.swift").read_text(
            encoding="utf-8"
        )
        assert "Increment counter" not in smoke_tests
        assert "Open in-app purchase store" not in smoke_tests
        assert "ContentView.mapARRoot" in smoke_tests
        assert "MapARHomeView.selectedPoint" in smoke_tests

    def test_main_list_includes_pack(self, capsys):
        cli = _load_cli()
        rc = cli.main(["--list"])
        assert rc == 0
        assert PACK in capsys.readouterr().out
