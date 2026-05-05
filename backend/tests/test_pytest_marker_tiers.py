"""BP.L.1 regression tests for pytest marker tier aggregation."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

from tests.conftest import _BP_L_MARKER_TIERS, _bp_l_marker_for_path


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_bp_l_marker_tier_file_sets_are_disjoint() -> None:
    seen: dict[str, str] = {}
    overlaps: list[tuple[str, str, str]] = []
    for marker_name, filenames in _BP_L_MARKER_TIERS.items():
        for filename in filenames:
            previous = seen.setdefault(filename, marker_name)
            if previous != marker_name:
                overlaps.append((filename, previous, marker_name))

    assert overlaps == []


def test_bp_l_marker_for_path_assigns_representative_files() -> None:
    assert _bp_l_marker_for_path("backend/tests/test_auth.py") == "critical"
    assert _bp_l_marker_for_path("backend/tests/test_skill_framework.py") == "guild_loadout"
    assert _bp_l_marker_for_path("backend/tests/test_compliance_harness.py") == "compliance"
    assert _bp_l_marker_for_path("backend/tests/test_nodes.py") is None


def test_bp_l_markers_registered_in_pytest_ini() -> None:
    parser = ConfigParser()
    parser.read(_REPO_ROOT / "backend" / "pytest.ini")
    marker_lines = parser.get("pytest", "markers").splitlines()
    registered = {line.split(":", 1)[0].strip() for line in marker_lines if ":" in line}

    assert {"critical", "guild_loadout", "compliance"} <= registered
