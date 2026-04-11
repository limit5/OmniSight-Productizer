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
