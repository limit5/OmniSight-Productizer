"""Tests for backend/prompt_loader.py — model rules, role skills, prompt assembly."""

from backend.prompt_loader import (
    _fuzzy_match_model_file,
    _strip_frontmatter,
    load_model_rules,
    load_role_skill,
    build_system_prompt,
    list_available_roles,
    list_available_models,
)


class TestFuzzyMatchModel:

    def test_claude_sonnet_version(self):
        match = _fuzzy_match_model_file("claude-sonnet-4-20250514")
        assert match is not None
        assert match.stem == "claude-sonnet"

    def test_claude_opus_version(self):
        match = _fuzzy_match_model_file("claude-opus-4-20250514")
        assert match is not None
        assert match.stem == "claude-opus"

    def test_gpt_variants(self):
        for model in ["gpt-4o", "gpt-5.4", "gpt-5.3", "gpt-5.2"]:
            match = _fuzzy_match_model_file(model)
            assert match is not None
            assert match.stem == "gpt", f"Expected gpt for {model}, got {match.stem}"

    def test_gemini_variants(self):
        for model in ["gemini-1.5-pro", "gemini-3.1-pro", "gemini-3.1-thinking"]:
            match = _fuzzy_match_model_file(model)
            assert match is not None
            assert match.stem == "gemini", f"Expected gemini for {model}, got {match.stem}"

    def test_grok_variants(self):
        match = _fuzzy_match_model_file("grok-3-mini")
        assert match is not None
        assert match.stem == "grok"

    def test_unknown_model_fallback(self):
        match = _fuzzy_match_model_file("totally-unknown-model-xyz")
        assert match is not None
        assert match.stem == "_default"


class TestStripFrontmatter:

    def test_removes_frontmatter(self):
        text = "---\nkey: value\n---\n\n# Content"
        assert _strip_frontmatter(text) == "# Content"

    def test_no_frontmatter(self):
        text = "# Just content"
        assert _strip_frontmatter(text) == "# Just content"


class TestLoadModelRules:

    def test_load_claude_sonnet(self):
        content = load_model_rules("claude-sonnet-4-20250514")
        assert "Claude Sonnet" in content
        assert "Tool Calling" in content

    def test_load_default_for_unknown(self):
        content = load_model_rules("unknown-model")
        assert "Default" in content or "General" in content

    def test_empty_model_name_uses_default(self):
        content = load_model_rules("")
        assert content  # should load _default.md


class TestLoadRoleSkill:

    def test_load_bsp_skill(self):
        content = load_role_skill("firmware", "bsp")
        assert "BSP" in content
        assert "kernel" in content.lower()

    def test_load_sdet_skill(self):
        content = load_role_skill("validator", "sdet")
        assert "SDET" in content or "Test" in content

    def test_missing_role_returns_empty(self):
        content = load_role_skill("firmware", "nonexistent-role")
        assert content == ""


class TestBuildSystemPrompt:

    def test_with_model_and_role(self):
        prompt = build_system_prompt(
            model_name="claude-sonnet-4",
            agent_type="firmware",
            sub_type="bsp",
        )
        assert "Model Behavior Rules" in prompt
        assert "BSP" in prompt

    def test_with_handoff_context(self):
        prompt = build_system_prompt(
            model_name="claude-sonnet-4",
            agent_type="firmware",
            sub_type="bsp",
            handoff_context="Previous agent compiled the driver successfully.",
        )
        assert "Previous Task Handoff" in prompt
        assert "compiled the driver" in prompt

    def test_fallback_when_no_skill_file(self):
        prompt = build_system_prompt(
            model_name="",
            agent_type="firmware",
            sub_type="",
        )
        # Should fall back to built-in prompt
        assert "Firmware Agent" in prompt

    def test_general_agent_fallback(self):
        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
        )
        assert "AI agent" in prompt.lower() or "general" in prompt.lower()

    def test_handoff_truncation(self):
        long_handoff = "x" * 10000
        prompt = build_system_prompt(
            model_name="",
            agent_type="firmware",
            sub_type="bsp",
            handoff_context=long_handoff,
        )
        assert "[handoff truncated]" in prompt


class TestListAvailable:

    def test_list_roles_not_empty(self):
        roles = list_available_roles()
        assert len(roles) >= 10
        categories = {r["category"] for r in roles}
        assert "firmware" in categories
        assert "software" in categories
        assert "validator" in categories
        assert "reporter" in categories

    def test_role_has_required_fields(self):
        roles = list_available_roles()
        for role in roles:
            assert "role_id" in role
            assert "category" in role
            assert "label" in role

    def test_list_models_not_empty(self):
        models = list_available_models()
        assert len(models) >= 7
        families = {m["family"] for m in models}
        assert "claude" in families
        assert "gpt" in families
        assert "gemini" in families


class TestMobileRoleSkills:
    """P4 #289 — Mobile role skills (ios-swift / android-kotlin / flutter-dart /
    react-native / kmp / mobile-a11y) must be discoverable under category=mobile
    and carry the right keywords/content to route agent work to them."""

    _EXPECTED = {
        "ios-swift": ["SwiftUI", "Combine", "XCUITest"],
        "android-kotlin": ["Jetpack Compose", "Coroutines", "Espresso"],
        "flutter-dart": ["Flutter", "Dart", "Riverpod"],
        "react-native": ["React Native", "TurboModule", "Hermes"],
        "kmp": ["Kotlin Multiplatform", "expect", "xcframework"],
        "mobile-a11y": ["VoiceOver", "TalkBack", "Dynamic Type"],
    }

    def test_all_six_mobile_skills_present(self):
        roles = list_available_roles()
        mobile_ids = {r["role_id"] for r in roles if r["category"] == "mobile"}
        assert set(self._EXPECTED.keys()).issubset(mobile_ids), (
            f"missing mobile role skills: {set(self._EXPECTED.keys()) - mobile_ids}"
        )

    def test_each_mobile_skill_loads_with_signature_content(self):
        for role_id, markers in self._EXPECTED.items():
            content = load_role_skill("mobile", role_id)
            assert content, f"load_role_skill('mobile', '{role_id}') returned empty"
            for marker in markers:
                assert marker in content, (
                    f"expected '{marker}' in mobile/{role_id} skill content"
                )

    def test_mobile_skills_expose_metadata(self):
        roles = {
            r["role_id"]: r
            for r in list_available_roles()
            if r["category"] == "mobile"
        }
        for role_id in self._EXPECTED:
            meta = roles[role_id]
            assert meta["label"], f"mobile/{role_id} missing label"
            assert meta["description"], f"mobile/{role_id} missing description"
            assert meta["keywords"], f"mobile/{role_id} missing keywords"

    def test_mobile_a11y_covers_both_platforms(self):
        content = load_role_skill("mobile", "mobile-a11y")
        assert "VoiceOver" in content and "TalkBack" in content, (
            "mobile-a11y must cover both iOS VoiceOver and Android TalkBack"
        )

    def test_kmp_references_dual_platform_profiles(self):
        content = load_role_skill("mobile", "kmp")
        assert "ios-arm64" in content or "iOS" in content
        assert "android-arm64" in content or "Android" in content


class TestSkillLazyLoading:
    """B15 (#350) — two-phase `build_system_prompt`:
      * Phase 1 (lazy mode)  — metadata catalog replaces the full role body.
      * Phase 2              — `build_skill_injection` pulls full bodies on
                               demand via explicit `[LOAD_SKILL: …]` markers
                               or keyword-matching against the CATC
                               ``domain_context`` + user prompt.
    """

    def _env_with_mode(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("OMNISIGHT_SKILL_LOADING", raising=False)
        else:
            monkeypatch.setenv("OMNISIGHT_SKILL_LOADING", value)

    def test_eager_mode_inlines_full_role_skill(self):
        """Default (eager) mode still inlines the BSP body — back-compat."""
        prompt = build_system_prompt(
            agent_type="firmware", sub_type="bsp", mode="eager",
        )
        # Eager mode pulls full body, which contains detailed BSP content.
        assert "Role: bsp" in prompt
        assert "Available Skills (on-demand)" not in prompt

    def test_lazy_mode_emits_catalog_instead_of_full_body(self):
        lazy = build_system_prompt(
            agent_type="firmware", sub_type="bsp", mode="lazy",
        )
        eager = build_system_prompt(
            agent_type="firmware", sub_type="bsp", mode="eager",
        )
        # Lazy mode advertises the catalog marker; eager does not.
        assert "Available Skills (on-demand)" in lazy
        assert "[LOAD_SKILL:" in lazy
        # Both prompts exist; the lazy prompt should still identify the role.
        assert "lazy-loaded skills" in lazy
        assert "Role: bsp" in eager

    def test_lazy_mode_hint_surfaces_relevant_skill_names(self):
        from backend.prompt_loader import build_system_prompt
        lazy = build_system_prompt(
            agent_type="mobile",
            sub_type="android-kotlin",
            mode="lazy",
            domain_context="Android Kotlin Jetpack Compose app",
        )
        assert "Relevant skills for this task" in lazy
        assert "android-kotlin" in lazy

    def test_mode_resolves_from_env_var(self, monkeypatch):
        from backend.prompt_loader import _resolve_skill_loading_mode
        self._env_with_mode(monkeypatch, "lazy")
        assert _resolve_skill_loading_mode(None) == "lazy"
        self._env_with_mode(monkeypatch, "eager")
        assert _resolve_skill_loading_mode(None) == "eager"
        self._env_with_mode(monkeypatch, "garbage")
        assert _resolve_skill_loading_mode(None) == "eager"
        self._env_with_mode(monkeypatch, None)
        assert _resolve_skill_loading_mode(None) == "eager"

    def test_list_all_skills_metadata_finds_role_and_task_skills(self):
        from backend.prompt_loader import list_all_skills_metadata
        skills = list_all_skills_metadata()
        kinds = {s.get("kind") for s in skills}
        assert "role" in kinds, "expected role skills in catalog"
        assert "task" in kinds, "expected task skills in catalog"
        # Spot check: a known role skill is present.
        names = {s.get("name") for s in skills}
        assert "bsp" in names or "android-kotlin" in names

    def test_build_skill_catalog_fits_budget(self):
        from backend.prompt_loader import build_skill_catalog, _MAX_SKILL_CATALOG
        cat = build_skill_catalog()
        assert cat, "catalog should be non-empty"
        assert len(cat) <= _MAX_SKILL_CATALOG + 200  # preamble tolerance
        # Catalog must self-document the load protocol.
        assert "[LOAD_SKILL:" in cat

    def test_match_skills_for_context_scores_android(self):
        from backend.prompt_loader import match_skills_for_context
        matches = match_skills_for_context(
            domain_context="Android Kotlin Jetpack Compose mobile",
            user_prompt="fix login screen layout",
            top_k=3,
        )
        names = [m.get("name") for m in matches]
        # Top-3 must include an android-flavored skill.
        assert any("android" in (n or "") for n in names), (
            f"expected an android skill in top matches, got {names}"
        )

    def test_match_skills_empty_query_returns_nothing(self):
        from backend.prompt_loader import match_skills_for_context
        assert match_skills_for_context("", "") == []

    def test_build_skill_injection_explicit_pulls_role_body(self):
        from backend.prompt_loader import build_skill_injection
        text = build_skill_injection(explicit_skills=["android-kotlin"])
        assert text, "expected non-empty injection for android-kotlin"
        assert "Skill: android-kotlin" in text
        # Signature content from the android-kotlin skill.
        assert "Jetpack Compose" in text or "Kotlin" in text

    def test_build_skill_injection_matches_from_context(self):
        from backend.prompt_loader import build_skill_injection
        text = build_skill_injection(
            domain_context="BSP kernel driver I2C sensor init",
            user_prompt="",
        )
        assert text, "expected non-empty matched injection"
        # One of the matches should be a firmware-family skill.
        assert "## Skill:" in text

    def test_extract_load_skill_requests_parses_markers(self):
        from backend.prompt_loader import extract_load_skill_requests
        text = (
            "I'll load helpers first.\n"
            "[LOAD_SKILL: android-kotlin]\n"
            "Then also [LOAD_SKILL: mobile-a11y] for accessibility.\n"
            "Duplicate: [LOAD_SKILL: android-kotlin] should dedupe.\n"
        )
        assert extract_load_skill_requests(text) == [
            "android-kotlin", "mobile-a11y",
        ]

    def test_extract_load_skill_requests_empty(self):
        from backend.prompt_loader import extract_load_skill_requests
        assert extract_load_skill_requests("") == []
        assert extract_load_skill_requests("no markers here") == []
