"""Tests for Anthropic-format task skill loading (Phase 18)."""

import os
from pathlib import Path



class TestTaskSkillLoading:

    def test_list_available_task_skills(self):
        from backend.prompt_loader import list_available_task_skills
        skills = list_available_task_skills()
        assert len(skills) >= 4
        names = [s["name"] for s in skills]
        assert "webapp-testing" in names
        assert "pdf-generation" in names
        assert "xlsx-generation" in names
        assert "mcp-builder" in names

    def test_list_available_task_skills_invalidates_on_skill_mtime(
        self, monkeypatch, tmp_path
    ):
        from backend import prompt_loader

        skill_dir = tmp_path / "first"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "---\n"
            "name: first\n"
            "description: First skill\n"
            "keywords: [first]\n"
            "---\n"
            "# First\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(prompt_loader, "_SKILLS_DIR", tmp_path)
        prompt_loader.reload_task_skills_for_tests()

        first = prompt_loader.list_available_task_skills()
        skill_file.write_text(
            "---\n"
            "name: second\n"
            "description: Second skill\n"
            "keywords: [second]\n"
            "---\n"
            "# Second\n",
            encoding="utf-8",
        )
        stat = skill_file.stat()
        os.utime(skill_file, (stat.st_atime + 2.0, stat.st_mtime + 2.0))
        later = prompt_loader.list_available_task_skills()

        assert [skill["name"] for skill in first] == ["first"]
        assert [skill["name"] for skill in later] == ["second"]

    def test_load_task_skill_exists(self):
        from backend.prompt_loader import load_task_skill
        content = load_task_skill("webapp-testing")
        assert content
        assert "Playwright" in content or "playwright" in content

    def test_load_task_skill_nonexistent(self):
        from backend.prompt_loader import load_task_skill
        content = load_task_skill("nonexistent-skill")
        assert content == ""

    def test_load_task_skill_empty(self):
        from backend.prompt_loader import load_task_skill
        content = load_task_skill("")
        assert content == ""

    def test_match_task_skill_webapp(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("Test the web application UI") == "webapp-testing"

    def test_match_task_skill_pdf(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("Generate a PDF compliance report") == "pdf-generation"

    def test_match_task_skill_xlsx(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("Create Excel spreadsheet for test data") == "xlsx-generation"

    def test_match_task_skill_mcp(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("Build an MCP server for external API") == "mcp-builder"

    def test_match_task_skill_no_match(self):
        from backend.prompt_loader import match_task_skill
        result = match_task_skill("Write a firmware driver for IMX335")
        assert result == ""

    def test_match_task_skill_empty(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("") == ""


class TestBuildSystemPromptWithTaskSkill:

    def test_task_skill_injected(self):
        from backend.prompt_loader import build_system_prompt
        prompt = build_system_prompt(
            agent_type="validator",
            task_skill_context="# Web Testing\nUse Playwright to test.",
        )
        assert "Task Skill" in prompt
        assert "Playwright" in prompt

    def test_task_skill_empty_no_section(self):
        from backend.prompt_loader import build_system_prompt
        prompt = build_system_prompt(agent_type="firmware")
        assert "Task Skill" not in prompt

    def test_task_skill_truncated(self):
        from backend.prompt_loader import build_system_prompt
        long_skill = "x" * 10000
        prompt = build_system_prompt(
            agent_type="general",
            task_skill_context=long_skill,
        )
        assert "[task skill truncated]" in prompt


class TestRoleSkillDescriptions:

    def test_all_roles_have_description(self):
        import backend.prompt_loader as pl
        pl._roles_cache = None  # Clear cache to pick up new descriptions
        roles = pl.list_available_roles()
        for role in roles:
            assert "description" in role, f"Role {role.get('role_id')} missing description"
            assert role["description"], f"Role {role.get('role_id')} has empty description"


class TestSkillFilesExist:

    def test_skill_directories_structure(self):
        skills_dir = Path(__file__).resolve().parent.parent.parent / "configs" / "skills"
        assert skills_dir.is_dir()
        for name in ["webapp-testing", "pdf-generation", "xlsx-generation", "mcp-builder"]:
            skill_file = skills_dir / name / "SKILL.md"
            assert skill_file.exists(), f"Missing: {skill_file}"
            content = skill_file.read_text()
            assert "---" in content  # has frontmatter
            assert "name:" in content
            assert "description:" in content
            assert "keywords:" in content
