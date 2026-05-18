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
  * Real-world smoke: load this repo's bundled skills and verify at least
    one frontmatter skill + one legacy skill round-trip
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.agents.skills_loader import (
    SKILLS_LOADER_ENABLED_ENV,
    Skill,
    SkillRegistry,
    load_default_scopes,
    make_skill_handler,
    parse_skill_file,
    render_catalog_for_prompt,
    watch_project_scopes,
)


SAFE_SKILL_NAME = st.from_regex(
    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,40}",
    fullmatch=True,
)
SAFE_DESCRIPTION = st.from_regex(
    r"[A-Za-z0-9][A-Za-z0-9 _./:-]{0,120}",
    fullmatch=True,
)
SAFE_BODY = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters="\x00\r",
    ),
    max_size=4096,
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


@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    name=SAFE_SKILL_NAME,
    description=SAFE_DESCRIPTION,
    keywords=st.lists(SAFE_SKILL_NAME, max_size=8),
    body=SAFE_BODY,
)
def test_parse_frontmatter_property_preserves_metadata_and_is_idempotent(
    tmp_path: Path,
    name: str,
    description: str,
    keywords: list[str],
    body: str,
) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"keywords: [{', '.join(keywords)}]\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )

    first = parse_skill_file(f, "project")
    second = parse_skill_file(f, "project")

    assert first == second
    assert isinstance(first, Skill)
    assert first.name == name
    assert first.description == description.strip()
    assert first.keywords == tuple(keywords)
    assert first.body == body.lstrip("\n")
    assert first.source_path == f
    assert first.scope == "project"


@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    whitespace=st.text(
        alphabet=st.sampled_from([" ", "\t", "\n", "\r"]),
        max_size=4096,
    )
)
def test_parse_empty_or_whitespace_property_returns_none(
    tmp_path: Path,
    whitespace: str,
) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(whitespace, encoding="utf-8")

    assert parse_skill_file(f, "project") is None


# ─── Registry shadowing ─────────────────────────────────────────


def _skill(name: str, scope: str, body: str = "x") -> Skill:
    return Skill(name=name, description=f"{name} desc", body=body, scope=scope)


def _write_skill_file(
    path: Path,
    *,
    name: str,
    description: str,
    body: str = "body\n",
) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )


def test_registry_add_first_wins() -> None:
    reg = SkillRegistry()
    assert reg.add(_skill("a", "project", body="proj-body"))
    # Lower-priority same name is shadowed
    assert not reg.add(_skill("a", "bundled", body="bundle-body"))
    assert reg.get("a").body == "proj-body"
    assert reg.get("a").scope == "project"


def test_registry_higher_provider_rank_overrides_lower(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="backend.agents.skills_loader")
    reg = SkillRegistry()
    low = _skill("a", "bundled", body="low-body")
    high = _skill("a", "project", body="high-body")
    assert reg.add(low, provider_rank=100)
    assert reg.add(high, provider_rank=300)
    assert reg.get("a").body == "high-body"
    assert reg.provider_rank("a") == 300
    assert "overrides" in caplog.text
    assert "rank=300" in caplog.text


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


@settings(max_examples=75, deadline=None)
@given(
    entries=st.lists(
        st.tuples(SAFE_SKILL_NAME, st.integers(min_value=0, max_value=500), SAFE_BODY),
        min_size=1,
        max_size=30,
    )
)
def test_registry_property_highest_rank_wins_and_reads_are_idempotent(
    entries: list[tuple[str, int, str]],
) -> None:
    reg = SkillRegistry()
    winners: dict[str, tuple[int, str]] = {}

    for name, provider_rank, body in entries:
        accepted = reg.add(
            Skill(
                name=name,
                description=f"{name} desc",
                body=body,
                scope="project",
            ),
            provider_rank=provider_rank,
        )
        previous = winners.get(name)
        should_accept = previous is None or provider_rank > previous[0]
        assert accepted is should_accept
        if should_accept:
            winners[name] = (provider_rank, body)

    assert reg.names() == sorted(winners)
    assert [skill.name for skill in reg.list_all()] == reg.names()
    assert len(reg) == len(winners)

    for name, (provider_rank, body) in winners.items():
        assert reg.has(name)
        assert reg.provider_rank(name) == provider_rank
        skill = reg.get(name)
        assert skill is not None
        assert reg.get(name) == skill
        assert skill.body == body


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


def test_load_default_scopes_three_scope_precedence_matrix(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "fakehome"
    _write_skill_file(
        project / "omnisight" / "agents" / "skills" / "shared" / "SKILL.md",
        name="shared",
        description="bundled shared",
        body="bundled-body\n",
    )
    _write_skill_file(
        home / ".claude" / "skills" / "shared" / "SKILL.md",
        name="shared",
        description="home shared",
        body="home-body\n",
    )
    _write_skill_file(
        project / ".claude" / "skills" / "shared" / "SKILL.md",
        name="shared",
        description="project shared",
        body="project-body\n",
    )
    _write_skill_file(
        project / "configs" / "skills" / "bundled-only" / "SKILL.md",
        name="bundled-only",
        description="bundled only",
    )
    _write_skill_file(
        home / ".omnisight" / "skills" / "home-only" / "SKILL.md",
        name="home-only",
        description="home only",
    )
    _write_skill_file(
        home / ".warp" / "skills" / "home-warp-only" / "SKILL.md",
        name="home-warp-only",
        description="home warp only",
    )
    _write_skill_file(
        project / ".omnisight" / "skills" / "project-only" / "SKILL.md",
        name="project-only",
        description="project only",
    )
    _write_skill_file(
        project / ".warp" / "skills" / "project-warp-only" / "SKILL.md",
        name="project-warp-only",
        description="project warp only",
    )

    reg = load_default_scopes(project, home=home)

    shared = reg.get("shared")
    assert shared is not None
    assert shared.scope == "project"
    assert shared.body == "project-body\n"
    assert reg.provider_rank("shared") == 310
    assert reg.get("home-only").scope == "home"
    assert reg.provider_rank("home-only") == 220
    assert reg.get("home-warp-only").scope == "home"
    assert reg.provider_rank("home-warp-only") == 215
    assert reg.get("bundled-only").scope == "bundled"
    assert reg.provider_rank("bundled-only") == 110
    assert reg.get("project-only").scope == "project"
    assert reg.provider_rank("project-only") == 320
    assert reg.get("project-warp-only").scope == "project"
    assert reg.provider_rank("project-warp-only") == 315


def test_load_default_scopes_project_omnisight_shadows_bundled(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "fakehome"
    (project / ".omnisight" / "skills" / "shared").mkdir(parents=True)
    (project / ".omnisight" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: from-project-omnisight\n---\nbody-proj\n"
    )
    (project / "omnisight" / "agents" / "skills" / "shared").mkdir(parents=True)
    (
        project
        / "omnisight"
        / "agents"
        / "skills"
        / "shared"
        / "SKILL.md"
    ).write_text(
        "---\nname: shared\ndescription: from-bundled\n---\nbody-bundle\n"
    )
    home.mkdir()
    reg = load_default_scopes(project, home=home)
    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "project"
    assert sk.body == "body-proj\n"
    assert "from-project-omnisight" in sk.description


def test_load_default_scopes_omnisight_provider_shadows_claude_with_warn(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="backend.agents.skills_loader")
    project = tmp_path / "proj"
    home = tmp_path / "fakehome"
    (project / ".claude" / "skills" / "shared").mkdir(parents=True)
    (project / ".claude" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: from-claude\n---\nbody-claude\n"
    )
    (project / ".omnisight" / "skills" / "shared").mkdir(parents=True)
    (project / ".omnisight" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: from-omnisight\n---\nbody-omni\n"
    )
    home.mkdir()
    reg = load_default_scopes(project, home=home)
    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "project"
    assert sk.body == "body-omni\n"
    assert reg.provider_rank("shared") == 320
    assert "overrides" in caplog.text
    assert ".omnisight" in caplog.text
    assert ".claude" in caplog.text


def test_load_default_scopes_warp_provider_shadows_claude_with_warn(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="backend.agents.skills_loader")
    project = tmp_path / "proj"
    home = tmp_path / "fakehome"
    (project / ".claude" / "skills" / "shared").mkdir(parents=True)
    (project / ".claude" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: from-claude\n---\nbody-claude\n"
    )
    (project / ".warp" / "skills" / "shared").mkdir(parents=True)
    (project / ".warp" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: from-warp\n---\nbody-warp\n"
    )
    home.mkdir()
    reg = load_default_scopes(project, home=home)
    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "project"
    assert sk.body == "body-warp\n"
    assert reg.provider_rank("shared") == 315
    assert "overrides" in caplog.text
    assert ".warp" in caplog.text
    assert ".claude" in caplog.text


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


def test_load_default_scopes_project_shadows_home_with_warn(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="backend.agents.skills_loader")
    project = tmp_path / "p"
    home = tmp_path / "h"
    (home / ".omnisight" / "skills" / "shared").mkdir(parents=True)
    (home / ".omnisight" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: home\n---\nhome-body\n"
    )
    (project / ".claude" / "skills" / "shared").mkdir(parents=True)
    (project / ".claude" / "skills" / "shared" / "SKILL.md").write_text(
        "---\nname: shared\ndescription: project\n---\nproject-body\n"
    )
    reg = load_default_scopes(project, home=home)
    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "project"
    assert sk.body == "project-body\n"
    assert reg.provider_rank("shared") == 310
    assert "shadowed by" in caplog.text
    assert "rank=220" in caplog.text
    assert "rank=310" in caplog.text


def test_load_default_scopes_same_name_across_all_providers_warns(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="backend.agents.skills_loader")
    project = tmp_path / "p"
    home = tmp_path / "h"
    for skill_file, description in (
        (
            project / ".claude" / "skills" / "shared" / "SKILL.md",
            "project claude",
        ),
        (
            project / ".warp" / "skills" / "shared" / "SKILL.md",
            "project warp",
        ),
        (
            project / ".omnisight" / "skills" / "shared" / "SKILL.md",
            "project omnisight",
        ),
        (
            home / ".claude" / "skills" / "shared" / "SKILL.md",
            "home claude",
        ),
        (
            home / ".warp" / "skills" / "shared" / "SKILL.md",
            "home warp",
        ),
        (
            home / ".omnisight" / "skills" / "shared" / "SKILL.md",
            "home omnisight",
        ),
        (
            project / "configs" / "skills" / "shared" / "SKILL.md",
            "legacy bundled",
        ),
        (
            project / "omnisight" / "agents" / "skills" / "shared" / "SKILL.md",
            "canonical bundled",
        ),
    ):
        _write_skill_file(
            skill_file,
            name="shared",
            description=description,
            body=f"{description}\n",
        )

    reg = load_default_scopes(project, home=home)

    sk = reg.get("shared")
    assert sk is not None
    assert sk.description == "project omnisight"
    assert sk.body == "project omnisight\n"
    assert sk.scope == "project"
    assert reg.provider_rank("shared") == 320
    assert "overrides" in caplog.text
    assert "shadowed by" in caplog.text
    assert "rank=320" in caplog.text
    assert "rank=315" in caplog.text
    assert "rank=215" in caplog.text
    assert "rank=210" in caplog.text
    assert "rank=120" in caplog.text
    assert "rank=110" in caplog.text


def test_load_default_scopes_home_omnisight_between_project_and_bundled(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    (home / ".omnisight" / "skills" / "homed").mkdir(parents=True)
    (home / ".omnisight" / "skills" / "homed" / "SKILL.md").write_text(
        "---\nname: homed\ndescription: home-omnisight\n---\nb\n"
    )
    (project / "omnisight" / "agents" / "skills" / "homed").mkdir(
        parents=True
    )
    (
        project
        / "omnisight"
        / "agents"
        / "skills"
        / "homed"
        / "SKILL.md"
    ).write_text(
        "---\nname: homed\ndescription: bundled-only\n---\nb\n"
    )
    reg = load_default_scopes(project, home=home)
    sk = reg.get("homed")
    assert sk is not None
    assert sk.scope == "home"
    assert sk.description == "home-omnisight"


def test_load_default_scopes_home_warp_between_project_and_bundled(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    (home / ".warp" / "skills" / "homed").mkdir(parents=True)
    (home / ".warp" / "skills" / "homed" / "SKILL.md").write_text(
        "---\nname: homed\ndescription: home-warp\n---\nb\n"
    )
    (project / "omnisight" / "agents" / "skills" / "homed").mkdir(
        parents=True
    )
    (
        project
        / "omnisight"
        / "agents"
        / "skills"
        / "homed"
        / "SKILL.md"
    ).write_text(
        "---\nname: homed\ndescription: bundled-only\n---\nb\n"
    )
    reg = load_default_scopes(project, home=home)
    sk = reg.get("homed")
    assert sk is not None
    assert sk.scope == "home"
    assert sk.description == "home-warp"
    assert reg.provider_rank("homed") == 215


def test_load_default_scopes_bundled_uses_omnisight_agents_skills(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    bundled = project / "omnisight" / "agents" / "skills" / "bundled"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text(
        "---\nname: bundled\ndescription: canonical bundled\n---\nb\n"
    )
    reg = load_default_scopes(project, home=tmp_path / "nohome")
    sk = reg.get("bundled")
    assert sk is not None
    assert sk.scope == "bundled"
    assert sk.source_path == bundled / "SKILL.md"


def test_load_default_scopes_skips_readme(tmp_path: Path) -> None:
    project = tmp_path / "p"
    bundled = project / "omnisight" / "agents" / "skills"
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


def test_load_default_scopes_disabled_uses_hardcoded_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    project_skill = project / ".omnisight" / "skills" / "custom" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    project_skill.write_text(
        "---\nname: custom\ndescription: project custom\n---\nproject-body\n"
    )
    monkeypatch.setenv(SKILLS_LOADER_ENABLED_ENV, "false")

    reg = load_default_scopes(project, home=home)

    assert len(reg) == 28
    assert not reg.has("custom")
    sk = reg.get("SKILL_HD_PARSE")
    assert sk is not None
    assert sk.scope == "hardcoded"
    assert sk.source_path is None
    assert sk.description.startswith("[HD.1]")


def test_watch_project_scopes_reloads_modified_project_skill(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    skill_file = project / ".claude" / "skills" / "watched" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: watched\ndescription: v1\n---\nbody-v1\n"
    )

    reg = watch_project_scopes(project, home=home)
    assert reg.get("watched").body == "body-v1\n"

    skill_file.write_text(
        "---\nname: watched\ndescription: v2\n---\nbody-v2\n"
    )
    assert reg.get("watched").body == "body-v2\n"


def test_watch_project_scopes_reloads_added_project_override(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    bundled = project / "configs" / "skills" / "shared" / "SKILL.md"
    bundled.parent.mkdir(parents=True)
    bundled.write_text(
        "---\nname: shared\ndescription: bundled\n---\nbundled-body\n"
    )

    reg = watch_project_scopes(project, home=home)
    assert reg.get("shared").scope == "bundled"

    project_skill = project / ".omnisight" / "skills" / "shared" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    project_skill.write_text(
        "---\nname: shared\ndescription: project\n---\nproject-body\n"
    )
    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "project"
    assert sk.body == "project-body\n"


def test_watch_project_scopes_reloads_added_warp_project_override(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    bundled = project / "configs" / "skills" / "shared" / "SKILL.md"
    bundled.parent.mkdir(parents=True)
    bundled.write_text(
        "---\nname: shared\ndescription: bundled\n---\nbundled-body\n"
    )

    reg = watch_project_scopes(project, home=home)
    assert reg.get("shared").scope == "bundled"

    project_skill = project / ".warp" / "skills" / "shared" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    project_skill.write_text(
        "---\nname: shared\ndescription: project warp\n---\nwarp-body\n"
    )
    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "project"
    assert sk.body == "warp-body\n"
    assert reg.provider_rank("shared") == 315


def test_watch_project_scopes_reloads_deleted_project_override(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    bundled = project / "configs" / "skills" / "shared" / "SKILL.md"
    project_skill = project / ".claude" / "skills" / "shared" / "SKILL.md"
    bundled.parent.mkdir(parents=True)
    project_skill.parent.mkdir(parents=True)
    bundled.write_text(
        "---\nname: shared\ndescription: bundled\n---\nbundled-body\n"
    )
    project_skill.write_text(
        "---\nname: shared\ndescription: project\n---\nproject-body\n"
    )

    reg = watch_project_scopes(project, home=home)
    assert reg.get("shared").scope == "project"

    project_skill.unlink()
    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "bundled"
    assert sk.body == "bundled-body\n"


def test_watch_project_scopes_manual_reload_updates_registry(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    reg = watch_project_scopes(project, home=tmp_path / "h")
    assert not reg.has("manual")

    _write_skill_file(
        project / ".omnisight" / "skills" / "manual" / "SKILL.md",
        name="manual",
        description="manual v1",
        body="body-v1\n",
    )
    reg.reload()

    sk = reg.get("manual")
    assert sk is not None
    assert sk.description == "manual v1"
    assert sk.body == "body-v1\n"


def test_skill_handler_with_watched_registry_reloads_body(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    skill_file = project / ".claude" / "skills" / "watched" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: watched\ndescription: v1\n---\nbody-v1\n"
    )
    reg = watch_project_scopes(project, home=tmp_path / "h")
    handler = make_skill_handler(reg)
    assert handler({"skill": "watched"}) == "body-v1\n"

    skill_file.write_text(
        "---\nname: watched\ndescription: v2\n---\nbody-v2\n"
    )
    assert handler({"skill": "watched"}) == "body-v2\n"


def test_watched_registry_integration_updates_handler_and_catalog(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    _write_skill_file(
        project / "omnisight" / "agents" / "skills" / "shared" / "SKILL.md",
        name="shared",
        description="bundled shared",
        body="bundled-body\n",
    )
    reg = watch_project_scopes(project, home=home)
    handler = make_skill_handler(reg)

    assert reg.get("shared").scope == "bundled"
    assert handler({"skill": "shared"}) == "bundled-body\n"

    _write_skill_file(
        project / ".omnisight" / "skills" / "shared" / "SKILL.md",
        name="shared",
        description="project shared",
        body="project-body\n",
    )

    sk = reg.get("shared")
    assert sk is not None
    assert sk.scope == "project"
    assert reg.provider_rank("shared") == 320
    assert handler({"skill": "shared", "args": "--dry-run"}).endswith(
        "project-body\n"
    )
    catalog = render_catalog_for_prompt(reg)
    assert "**shared**" in catalog
    assert "project shared" in catalog
    assert "bundled shared" not in catalog


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


@settings(max_examples=75, deadline=None)
@given(name=SAFE_SKILL_NAME, body=SAFE_BODY, args=st.one_of(st.none(), SAFE_BODY))
def test_skill_handler_property_returns_body_and_preserves_args_contract(
    name: str,
    body: str,
    args: str | None,
) -> None:
    reg = SkillRegistry()
    reg.add(Skill(name=name, description="d", body=body, scope="bundled"))
    handler = make_skill_handler(reg)
    payload: dict[str, object] = {"skill": f"  {name}  "}
    if args is not None:
        payload["args"] = args

    out = handler(payload)

    assert isinstance(out, str)
    if args is not None and args.strip():
        assert out.startswith(f"_(invoked with args: {args.strip()})_")
        assert out.endswith(body)
    else:
        assert out == body


@pytest.mark.parametrize(
    ("payload", "exc_type", "match"),
    [
        ({}, ValueError, "non-empty"),
        ({"skill": ""}, ValueError, "non-empty"),
        ({"skill": None}, KeyError, "Unknown skill"),
    ],
)
def test_skill_handler_edge_payloads_raise_contract_errors(
    payload: dict[str, object],
    exc_type: type[Exception],
    match: str,
) -> None:
    handler = make_skill_handler(SkillRegistry())

    with pytest.raises(exc_type, match=match):
        handler(payload)


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


@settings(max_examples=75, deadline=None)
@given(
    names=st.lists(SAFE_SKILL_NAME, unique=True, max_size=100),
    max_entries=st.integers(min_value=0, max_value=120),
)
def test_render_catalog_property_is_deterministic_and_respects_max_entries(
    names: list[str],
    max_entries: int,
) -> None:
    reg = SkillRegistry()
    for name in names:
        reg.add(Skill(name=name, description=f"{name} desc", body="b"))

    out = render_catalog_for_prompt(reg, max_entries=max_entries)

    assert out == render_catalog_for_prompt(reg, max_entries=max_entries)
    if not names:
        assert out == ""
        return
    assert f"共 {len(names)} 個 skill" in out
    assert out.count("- **") == min(len(names), max_entries)
    if len(names) > max_entries:
        assert f"還有 {len(names) - max_entries} 個未列出" in out
    else:
        assert "個未列出" not in out


# ─── Real-world smoke against this repo ────────────────────────


def test_real_repo_loads_bundled_skills() -> None:
    """Load the live bundled skills and assert sanity."""
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
