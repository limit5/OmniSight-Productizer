"""X2 #298 — Software role skills contract tests.

Pins the nine X2 software role definitions against the X1 software
simulate-track gates (``backend.software_simulator.COVERAGE_THRESHOLDS``)
they are supposed to uphold. If a role drops a coverage citation or its
tool whitelist regresses to ``[all]`` (i.e. an unbounded role), the test
fails — this file is the living spec keeping the prompt layer (role
skills under ``configs/roles/software/``) and the gate layer
(``software_simulator.py``) in sync.

Companion to ``test_web_role_skills.py`` (W3 #277) and
``test_*role*`` mobile tests (P9 #294).
"""

from __future__ import annotations

import pytest

from backend.prompt_loader import (
    _parse_frontmatter,
    _ROLES_DIR,
    get_role_keywords,
    list_available_roles,
    load_role_skill,
)
from backend.software_simulator import COVERAGE_THRESHOLDS


SOFTWARE_CATEGORY = "software"

# Nine X2 role IDs added by #298. Existing software roles
# (algorithm / middleware / ai-deploy / ux-design) are NOT covered here
# — they predate X2 and have their own contract.
EXPECTED_X2_ROLE_IDS = (
    "backend-python",
    "backend-go",
    "backend-rust",
    "backend-node",
    "backend-java",
    "cli-tooling",
    "desktop-electron",
    "desktop-tauri",
    "desktop-qt",
)

# Map role_id → X1 simulate-track language whose coverage threshold the
# role MUST cite. cli-tooling / desktop-electron / desktop-tauri /
# desktop-qt are multi-language; they cite at least the principal
# language's threshold (verified separately).
ROLE_TO_PRIMARY_LANG = {
    "backend-python": "python",
    "backend-go": "go",
    "backend-rust": "rust",
    "backend-node": "node",
    "backend-java": "java",
    "desktop-electron": "node",     # Node-driven; renderer + main both Node
    "desktop-tauri": "rust",        # Rust backend is the hard gate
}


class TestX2RoleEnumeration:
    """All nine X2 role IDs must be discoverable via list_available_roles()."""

    def test_software_category_includes_x2_roles(self):
        roles = list_available_roles()
        sw_ids = {r["role_id"] for r in roles if r["category"] == SOFTWARE_CATEGORY}
        missing = set(EXPECTED_X2_ROLE_IDS) - sw_ids
        assert not missing, f"Missing X2 software role files: {sorted(missing)}"

    def test_no_duplicate_software_role_ids(self):
        roles = list_available_roles()
        sw_ids = [r["role_id"] for r in roles if r["category"] == SOFTWARE_CATEGORY]
        assert len(sw_ids) == len(set(sw_ids)), (
            f"Duplicate software role_id detected: {sw_ids}"
        )


@pytest.mark.parametrize("role_id", EXPECTED_X2_ROLE_IDS)
class TestX2RoleFrontmatterContract:
    """Each role's YAML frontmatter exposes the fields list_available_roles()
    and the prompt builder depend on."""

    def _meta(self, role_id: str) -> dict:
        path = _ROLES_DIR / SOFTWARE_CATEGORY / f"{role_id}.skill.md"
        assert path.is_file(), f"Missing skill file: {path}"
        return _parse_frontmatter(path)

    def test_required_fields(self, role_id: str):
        meta = self._meta(role_id)
        for key in ("role_id", "category", "label", "label_en", "keywords", "tools", "description"):
            assert meta.get(key), f"software/{role_id} missing frontmatter field: {key}"

    def test_role_id_matches_filename(self, role_id: str):
        meta = self._meta(role_id)
        assert meta["role_id"] == role_id
        assert meta["category"] == SOFTWARE_CATEGORY

    def test_keywords_non_empty(self, role_id: str):
        kws = get_role_keywords(SOFTWARE_CATEGORY, role_id)
        assert len(kws) >= 5, (
            f"software/{role_id} needs ≥5 keywords for matching, got {kws}"
        )

    def test_tool_whitelist_is_specific_not_all(self, role_id: str):
        """X2 spec mirrors W3: role-specific tool whitelist. `[all]` is
        forbidden — the point is to constrain each role to its domain
        tools (security / least-privilege)."""
        meta = self._meta(role_id)
        tools = meta.get("tools", [])
        assert tools, f"software/{role_id} has empty tools list"
        assert tools != ["all"], (
            f"software/{role_id} uses 'tools: [all]' — X2 requires role-specific whitelist"
        )
        assert len(tools) <= 20, f"software/{role_id} tool list too broad: {len(tools)}"

    def test_content_has_required_sections(self, role_id: str):
        body = load_role_skill(SOFTWARE_CATEGORY, role_id)
        assert body, f"software/{role_id} body empty"
        assert "核心職責" in body, f"software/{role_id} missing 核心職責 section"
        assert "品質標準" in body, f"software/{role_id} missing 品質標準 section"
        assert "Anti-patterns" in body, f"software/{role_id} missing Anti-patterns section"


class TestX2RolesCiteX1CoverageThresholds:
    """X2 roles must cite the X1 simulate-track coverage thresholds so
    that LLM-generated code is gated against the same numbers
    simulate.sh runs. If X1 moves a threshold, these tests force the
    role files to track it."""

    @pytest.mark.parametrize("role_id,language", sorted(ROLE_TO_PRIMARY_LANG.items()))
    def test_role_cites_primary_language_threshold(self, role_id: str, language: str):
        body = load_role_skill(SOFTWARE_CATEGORY, role_id)
        threshold = int(COVERAGE_THRESHOLDS[language])
        # Roles cite the threshold as e.g. "≥ 80%" or "Coverage ≥ 80%"
        assert f"{threshold}%" in body, (
            f"software/{role_id} should reference X1 {language} coverage "
            f"threshold ({threshold}%) from COVERAGE_THRESHOLDS"
        )

    def test_cli_tooling_cites_all_supported_thresholds(self):
        """cli-tooling is multi-language (Go/Rust/Node/Python/Java) so
        it must enumerate every per-language threshold so the LLM can
        pick the right one for the host language."""
        body = load_role_skill(SOFTWARE_CATEGORY, "cli-tooling")
        for lang, threshold in COVERAGE_THRESHOLDS.items():
            if lang == "csharp":
                continue  # cli-tooling skill scope: Go/Rust/Node/Python/Java only
            assert f"{int(threshold)}%" in body, (
                f"cli-tooling missing {lang} threshold ({int(threshold)}%) — "
                f"required for multi-language CLI scope"
            )


class TestX2RolesReferenceSimulateTrack:
    """Every X2 role must tell the agent how to invoke the X1
    simulate-track gate. If the role doesn't mention `simulate.sh`
    `--type=software`, the LLM has no path to validate its output."""

    @pytest.mark.parametrize("role_id", EXPECTED_X2_ROLE_IDS)
    def test_role_mentions_simulate_sh(self, role_id: str):
        body = load_role_skill(SOFTWARE_CATEGORY, role_id)
        assert "simulate.sh" in body, (
            f"software/{role_id} should tell the agent how to run the X1 gate"
        )
        assert "--type=software" in body, (
            f"software/{role_id} should reference --type=software"
        )


class TestX2RolesReferenceX0Profiles:
    """Every X2 role must reference the X0 platform profiles so the
    LLM picks the right `--module=` value when invoking simulate.sh."""

    @pytest.mark.parametrize("role_id", EXPECTED_X2_ROLE_IDS)
    def test_role_mentions_x0_profile(self, role_id: str):
        body = load_role_skill(SOFTWARE_CATEGORY, role_id)
        # At minimum, the Linux x86_64 profile (X1 dogfood baseline)
        assert "linux-x86_64-native" in body, (
            f"software/{role_id} should reference X0 baseline profile linux-x86_64-native"
        )


class TestRoleKeywordsRouteSensibly:
    """Keywords must route intent — "fastapi project" should pick
    backend-python, not backend-go. Light smoke test that keyword sets
    are disjoint on the framework name itself."""

    def test_backend_language_keywords_disjoint(self):
        py_kws = set(get_role_keywords(SOFTWARE_CATEGORY, "backend-python"))
        go_kws = set(get_role_keywords(SOFTWARE_CATEGORY, "backend-go"))
        rust_kws = set(get_role_keywords(SOFTWARE_CATEGORY, "backend-rust"))
        node_kws = set(get_role_keywords(SOFTWARE_CATEGORY, "backend-node"))
        java_kws = set(get_role_keywords(SOFTWARE_CATEGORY, "backend-java"))

        assert "fastapi" in py_kws
        assert "fastapi" not in go_kws and "fastapi" not in rust_kws
        assert "gin" in go_kws and "gin" not in py_kws
        assert "axum" in rust_kws and "axum" not in node_kws
        assert "express" in node_kws and "express" not in py_kws
        assert "spring" in java_kws and "spring" not in node_kws

    def test_desktop_framework_keywords_disjoint(self):
        electron_kws = set(get_role_keywords(SOFTWARE_CATEGORY, "desktop-electron"))
        tauri_kws = set(get_role_keywords(SOFTWARE_CATEGORY, "desktop-tauri"))
        qt_kws = set(get_role_keywords(SOFTWARE_CATEGORY, "desktop-qt"))

        assert "electron" in electron_kws and "electron" not in tauri_kws
        assert "tauri" in tauri_kws and "tauri" not in electron_kws
        assert "qt" in qt_kws and "qt" not in electron_kws
