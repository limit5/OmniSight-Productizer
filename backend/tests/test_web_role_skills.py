"""W3 #277 — Web role skills contract tests.

Pins the six W3 role definitions (frontend-react / frontend-vue /
frontend-svelte / a11y / seo / perf) against the W2 simulate-track
gates they are supposed to uphold. If a role drops a quality gate
reference or a role-specific tool whitelist becomes ``[all]`` (i.e.
regresses into an unbounded role), the test fails — this file is the
"living spec" that keeps the prompt layer and the gate layer in sync.
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
from backend.web_simulator import (
    LIGHTHOUSE_MIN_A11Y,
    LIGHTHOUSE_MIN_PERF,
    LIGHTHOUSE_MIN_SEO,
)


WEB_CATEGORY = "web"
EXPECTED_ROLE_IDS = (
    "frontend-react",
    "frontend-vue",
    "frontend-svelte",
    "a11y",
    "seo",
    "perf",
)


class TestWebRoleEnumeration:
    """All six W3 role IDs must be discoverable via list_available_roles()."""

    def test_web_category_has_six_roles(self):
        roles = list_available_roles()
        web_roles = [r for r in roles if r["category"] == WEB_CATEGORY]
        assert len(web_roles) == 6, (
            f"Expected 6 web roles, got {len(web_roles)}: "
            f"{[r['role_id'] for r in web_roles]}"
        )

    def test_expected_role_ids_present(self):
        roles = list_available_roles()
        web_ids = {r["role_id"] for r in roles if r["category"] == WEB_CATEGORY}
        assert web_ids == set(EXPECTED_ROLE_IDS), (
            f"Missing or extra: expected={set(EXPECTED_ROLE_IDS)}, got={web_ids}"
        )


@pytest.mark.parametrize("role_id", EXPECTED_ROLE_IDS)
class TestWebRoleFrontmatterContract:
    """Each role's YAML frontmatter exposes the fields list_available_roles()
    and the downstream prompt builder depend on."""

    def _meta(self, role_id: str) -> dict:
        path = _ROLES_DIR / WEB_CATEGORY / f"{role_id}.skill.md"
        assert path.is_file(), f"Missing skill file: {path}"
        return _parse_frontmatter(path)

    def test_required_fields(self, role_id: str):
        meta = self._meta(role_id)
        for key in ("role_id", "category", "label", "label_en", "keywords", "tools", "description"):
            assert meta.get(key), f"web/{role_id} missing frontmatter field: {key}"

    def test_role_id_matches_filename(self, role_id: str):
        meta = self._meta(role_id)
        assert meta["role_id"] == role_id
        assert meta["category"] == WEB_CATEGORY

    def test_keywords_non_empty(self, role_id: str):
        kws = get_role_keywords(WEB_CATEGORY, role_id)
        assert len(kws) >= 5, f"web/{role_id} needs ≥5 keywords for matching, got {kws}"

    def test_tool_whitelist_is_specific_not_all(self, role_id: str):
        """W3 spec: role-specific tool whitelist. `[all]` is forbidden —
        the point is to constrain each role to its domain tools."""
        meta = self._meta(role_id)
        tools = meta.get("tools", [])
        assert tools, f"web/{role_id} has empty tools list"
        assert tools != ["all"], (
            f"web/{role_id} uses 'tools: [all]' — W3 requires role-specific whitelist"
        )
        # Sanity: no role needs more than 20 tools
        assert len(tools) <= 20, f"web/{role_id} tool list too broad: {len(tools)}"

    def test_content_has_quality_standards_section(self, role_id: str):
        body = load_role_skill(WEB_CATEGORY, role_id)
        assert body, f"web/{role_id} body empty"
        assert "品質標準" in body, f"web/{role_id} missing 品質標準 section"
        assert "核心職責" in body, f"web/{role_id} missing 核心職責 section"


class TestFrontendRolesReferenceSimulateTrack:
    """Frontend roles must cite the W2 Lighthouse thresholds so that
    LLM-generated code is gated against the same numbers simulate.sh
    runs. If W2 moves a threshold, these references must track it."""

    @pytest.mark.parametrize("role_id", ["frontend-react", "frontend-vue", "frontend-svelte"])
    def test_frontend_role_mentions_lighthouse_gates(self, role_id: str):
        body = load_role_skill(WEB_CATEGORY, role_id)
        assert f"≥ {LIGHTHOUSE_MIN_PERF}" in body, (
            f"web/{role_id} should reference LIGHTHOUSE_MIN_PERF ({LIGHTHOUSE_MIN_PERF})"
        )
        assert f"≥ {LIGHTHOUSE_MIN_A11Y}" in body, (
            f"web/{role_id} should reference LIGHTHOUSE_MIN_A11Y ({LIGHTHOUSE_MIN_A11Y})"
        )
        assert f"≥ {LIGHTHOUSE_MIN_SEO}" in body, (
            f"web/{role_id} should reference LIGHTHOUSE_MIN_SEO ({LIGHTHOUSE_MIN_SEO})"
        )

    @pytest.mark.parametrize("role_id", ["frontend-react", "frontend-vue", "frontend-svelte"])
    def test_frontend_role_mentions_bundle_budget(self, role_id: str):
        body = load_role_skill(WEB_CATEGORY, role_id)
        # W1 profile budgets: 500 KiB / 5 MiB / 1 MiB / 50 MiB
        assert "500 KiB" in body or "bundle_size_budget" in body, (
            f"web/{role_id} should reference W1 bundle budgets"
        )

    @pytest.mark.parametrize("role_id", ["frontend-react", "frontend-vue", "frontend-svelte"])
    def test_frontend_role_mentions_simulate_sh(self, role_id: str):
        body = load_role_skill(WEB_CATEGORY, role_id)
        assert "simulate.sh" in body, (
            f"web/{role_id} should tell the agent how to run the W2 gate"
        )


class TestA11yRoleCoversWcag22AA:
    """WCAG 2.2 AA spec requires a11y role to cover the 2.2-specific
    success criteria (2.4.11 / 2.5.7 / 2.5.8 / 3.3.8) added on top of 2.1."""

    def test_mentions_wcag_2_2(self):
        body = load_role_skill(WEB_CATEGORY, "a11y")
        assert "WCAG 2.2" in body

    def test_covers_wcag_2_2_new_criteria(self):
        body = load_role_skill(WEB_CATEGORY, "a11y")
        # Critical 2.2 additions that weren't in 2.1
        for criterion in ("2.4.11", "2.5.7", "2.5.8", "3.3.8"):
            assert criterion in body, (
                f"a11y role missing WCAG 2.2 criterion {criterion}"
            )

    def test_references_lighthouse_a11y_threshold(self):
        body = load_role_skill(WEB_CATEGORY, "a11y")
        assert f"≥ {LIGHTHOUSE_MIN_A11Y}" in body


class TestSeoRoleCoversW2SeoLint:
    """SEO role must describe the same five tags W2 run_seo_lint() checks
    so that the agent knows what the static linter enforces."""

    def test_covers_required_tags(self):
        body = load_role_skill(WEB_CATEGORY, "seo")
        # W2 run_seo_lint checks: title / description / viewport / canonical / og
        assert "<title>" in body
        assert "description" in body.lower()
        assert "viewport" in body.lower()
        assert "canonical" in body.lower()
        assert "og:" in body.lower() or "open graph" in body.lower()

    def test_references_lighthouse_seo_threshold(self):
        body = load_role_skill(WEB_CATEGORY, "seo")
        assert f"≥ {LIGHTHOUSE_MIN_SEO}" in body


class TestPerfRoleCoversCoreWebVitals:
    """Perf role must define all three Core Web Vitals + thresholds.
    INP (not FID) is the 2024+ interactive metric — pinning this keeps
    the role from drifting back to deprecated metrics."""

    def test_covers_lcp_inp_cls(self):
        body = load_role_skill(WEB_CATEGORY, "perf")
        for metric in ("LCP", "INP", "CLS"):
            assert metric in body, f"perf role missing CWV metric {metric}"

    def test_uses_inp_not_fid_as_primary(self):
        """FID was replaced by INP on 2024-03-12. New code should target INP."""
        body = load_role_skill(WEB_CATEGORY, "perf")
        assert "INP" in body
        # FID can appear in context (e.g. "replaced FID") but INP must be emphasized
        assert body.count("INP") >= body.count("FID"), (
            "perf role should emphasize INP over deprecated FID"
        )

    def test_references_cwv_thresholds(self):
        body = load_role_skill(WEB_CATEGORY, "perf")
        # Google's Good thresholds: LCP 2.5s / INP 200ms / CLS 0.1
        assert "2.5" in body, "perf role missing LCP 2.5s threshold"
        assert "200" in body, "perf role missing INP 200ms threshold"
        assert "0.1" in body, "perf role missing CLS 0.1 threshold"

    def test_references_w1_bundle_budgets(self):
        body = load_role_skill(WEB_CATEGORY, "perf")
        for budget in ("500 KiB", "5 MiB", "1 MiB", "50 MiB"):
            assert budget in body, f"perf role missing W1 budget: {budget}"


class TestRoleKeywordsRouteSensibly:
    """Keywords must route intent — "react component" should pick react,
    not vue. Light smoke test that the keyword sets are disjoint on the
    framework name itself."""

    def test_framework_keywords_disjoint(self):
        react_kws = set(get_role_keywords(WEB_CATEGORY, "frontend-react"))
        vue_kws = set(get_role_keywords(WEB_CATEGORY, "frontend-vue"))
        svelte_kws = set(get_role_keywords(WEB_CATEGORY, "frontend-svelte"))
        assert "react" in react_kws and "react" not in vue_kws and "react" not in svelte_kws
        assert "vue" in vue_kws and "vue" not in react_kws and "vue" not in svelte_kws
        assert "svelte" in svelte_kws and "svelte" not in react_kws and "svelte" not in vue_kws
