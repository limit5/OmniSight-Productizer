"""Tests for backend.agents.skills_loader (Phase 2 — runner ↔ skill packs).

Locks:
  * frontmatter parsing: name / description / keywords / body separation
  * legacy header-only parsing: title → name_hint, first prose → desc
  * directory-name fallback when neither frontmatter nor legacy header set name
  * 3-scope shadowing: project > home > bundled
  * SkillRegistry add/get/has/names/list_all/__len__
  * Skill tool handler returns body; unknown name raises; empty name raises;
    args prepended to body
  * render_catalog_for_prompt: empty registry → empty string; truncation
  * Real-world smoke: load this repo's bundled configs/skills/ and verify
    at least one frontmatter skill + one legacy skill round-trip
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.skills_loader import (
    Skill,
    SkillRegistry,
    load_default_scopes,
    make_skill_handler,
    parse_skill_file,
    render_catalog_for_prompt,
)


# ─── Parser ──────────────────────────────────────────────────────


def test_parse_frontmatter_extracts_metadata(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(
        "---\n"
        "name: my-skill\n"
        "description: Does a useful thing.\n"
        "keywords: [alpha, beta, gamma]\n"
        "---\n"
        "# Body\n"
        "actual content here.\n"
    )
    sk = parse_skill_file(f, scope="project")
    assert sk is not None
    assert sk.name == "my-skill"
    assert sk.description == "Does a useful thing."
    assert sk.keywords == ("alpha", "beta", "gamma")
    assert sk.body.startswith("# Body")
    assert "actual content" in sk.body
    assert sk.scope == "project"


def test_parse_frontmatter_quoted_values(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(
        '---\n'
        'name: "quoted-name"\n'
        "description: 'single-quoted desc'\n"
        "---\n"
        "body\n"
    )
    sk = parse_skill_file(f, "home")
    assert sk is not None
    assert sk.name == "quoted-name"
    assert sk.description == "single-quoted desc"


def test_parse_legacy_header_format(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(
        "# SKILL-NEXTJS — W6 #280 (pilot)\n\n"
        "First web-vertical skill pack.\n"
        "More body.\n"
    )
    sk = parse_skill_file(f, "bundled")
    assert sk is not None
    assert sk.name == "skill-nextjs"
    assert "First web-vertical skill pack" in sk.description


def test_parse_falls_back_to_dir_name(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-thing"
    skill_dir.mkdir()
    f = skill_dir / "SKILL.md"
    f.write_text("just some markdown without a heading\n")
    sk = parse_skill_file(f, "bundled")
    assert sk is not None
    assert sk.name == "my-thing"


def test_parse_empty_file_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("   \n")
    assert parse_skill_file(f, "project") is None


def test_parse_keywords_csv_string(tmp_path: Path) -> None:
    """Some loose authors write `keywords: a, b, c` without brackets."""
    f = tmp_path / "SKILL.md"
    f.write_text(
        "---\n"
        "name: x\n"
        "description: y\n"
        "keywords: a, b, c\n"
        "---\n"
        "body\n"
    )
    sk = parse_skill_file(f, "home")
    assert sk is not None
    assert sk.keywords == ("a", "b", "c")


# ─── Registry shadowing ─────────────────────────────────────────


def _skill(name: str, scope: str, body: str = "x") -> Skill:
    return Skill(name=name, description=f"{name} desc", body=body, scope=scope)


def test_registry_add_first_wins() -> None:
    reg = SkillRegistry()
    assert reg.add(_skill("a", "project", body="proj-body"))
    # Lower-priority same name is shadowed
    assert not reg.add(_skill("a", "bundled", body="bundle-body"))
    assert reg.get("a").body == "proj-body"
    assert reg.get("a").scope == "project"


def test_registry_basic_ops() -> None:
    reg = SkillRegistry()
    reg.add(_skill("z", "bundled"))
    reg.add(_skill("a", "bundled"))
    reg.add(_skill("m", "bundled"))
    assert reg.has("a") and reg.has("m") and reg.has("z")
    assert not reg.has("missing")
    assert reg.names() == ["a", "m", "z"]
    assert len(reg) == 3
    listed = reg.list_all()
    assert [s.name for s in listed] == ["a", "m", "z"]


# ─── Scope walking ──────────────────────────────────────────────


def test_load_default_scopes_project_shadows_bundled(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "fakehome"
    (project / ".claude" / "skills" / "shared").mkdir(parents=True)
    (project / ".claude" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: from-project\n---\nbody-proj\n"
    )
    (project / "configs" / "skills" / "shared").mkdir(parents=True)
    (project / "configs" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: from-bundled\n---\nbody-bundle\n"
    )
    home.mkdir()
    reg = load_default_scopes(project, home=home)
    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "project"
    assert sk.body == "body-proj\n"
    assert "from-project" in sk.description


def test_load_default_scopes_home_between_project_and_bundled(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    (home / ".claude" / "skills" / "homed").mkdir(parents=True)
    (home / ".claude" / "skills" / "homed" / "SKILL.md").write_text(
        "---\nname: homed\ndescription: home-only\n---\nb\n"
    )
    (project / "configs" / "skills" / "homed").mkdir(parents=True)
    (project / "configs" / "skills" / "homed" / "SKILL.md").write_text(
        "---\nname: homed\ndescription: bundled-only\n---\nb\n"
    )
    reg = load_default_scopes(project, home=home)
    assert reg.get("homed").scope == "home"


def test_load_default_scopes_skips_readme(tmp_path: Path) -> None:
    project = tmp_path / "p"
    bundled = project / "configs" / "skills"
    bundled.mkdir(parents=True)
    (bundled / "README.md").write_text(
        "# Skills directory\nthis is just a readme\n"
    )
    (bundled / "real-skill" / "").mkdir()
    (bundled / "real-skill" / "SKILL.md").write_text(
        "---\nname: real-skill\ndescription: x\n---\nb\n"
    )
    reg = load_default_scopes(project, home=tmp_path / "nohome")
    assert reg.has("real-skill")
    assert not reg.has("README")
    assert not reg.has("readme")


# ─── Tool handler ───────────────────────────────────────────────


def test_skill_handler_returns_body() -> None:
    reg = SkillRegistry()
    reg.add(Skill(name="ping", description="d", body="ping-body", scope="bundled"))
    h = make_skill_handler(reg)
    assert h({"skill": "ping"}) == "ping-body"


def test_skill_handler_prepends_args_when_given() -> None:
    reg = SkillRegistry()
    reg.add(Skill(name="ping", description="d", body="body", scope="bundled"))
    h = make_skill_handler(reg)
    out = h({"skill": "ping", "args": "--verbose foo"})
    assert "invoked with args: --verbose foo" in out
    assert out.endswith("body")


def test_skill_handler_unknown_name_raises_keyerror() -> None:
    reg = SkillRegistry()
    reg.add(Skill(name="known", description="d", body="b", scope="bundled"))
    h = make_skill_handler(reg)
    with pytest.raises(KeyError, match="Unknown skill"):
        h({"skill": "missing"})


def test_skill_handler_empty_name_raises_valueerror() -> None:
    reg = SkillRegistry()
    h = make_skill_handler(reg)
    with pytest.raises(ValueError, match="non-empty"):
        h({"skill": ""})


# ─── Catalog rendering ─────────────────────────────────────────


def test_render_catalog_empty_registry_returns_empty() -> None:
    assert render_catalog_for_prompt(SkillRegistry()) == ""


def test_render_catalog_lists_all_under_max() -> None:
    reg = SkillRegistry()
    reg.add(Skill(name="alpha", description="A short", body="b", scope="bundled"))
    reg.add(Skill(name="beta", description="B short",
                  keywords=("k1", "k2"), body="b", scope="bundled"))
    out = render_catalog_for_prompt(reg)
    assert "**alpha**" in out
    assert "**beta**" in out
    assert "k1, k2" in out


def test_render_catalog_truncates_over_max() -> None:
    reg = SkillRegistry()
    for i in range(10):
        reg.add(
            Skill(name=f"s{i:02}", description=f"d{i}", body="b", scope="bundled")
        )
    out = render_catalog_for_prompt(reg, max_entries=3)
    assert "**s00**" in out
    assert "**s02**" in out
    assert "**s09**" not in out
    assert "還有 7 個未列出" in out


# ─── Real-world smoke against this repo ────────────────────────


def test_real_repo_loads_bundled_skills() -> None:
    """Load the live ``configs/skills/`` and assert sanity."""
    project_root = Path(__file__).resolve().parents[2]
    reg = load_default_scopes(
        project_root, home=Path("/__nonexistent_for_test__")
    )
    # 30+ bundled skills currently shipped
    assert len(reg) >= 20, f"expected ≥20 bundled skills, got {len(reg)}"
    # Frontmatter-style skill round-trips
    mcp_builder = reg.get("mcp-builder")
    assert mcp_builder is not None
    assert "MCP" in mcp_builder.description.upper()
    assert mcp_builder.scope == "bundled"
    # Legacy-format skill round-trips (skill-nextjs uses # SKILL-NEXTJS header)
    nextjs = reg.get("skill-nextjs")
    assert nextjs is not None
    assert nextjs.scope == "bundled"
