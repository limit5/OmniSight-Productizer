"""Tests for backend.agents.project_memory (Phase 4 — multi-rule walker).

Locks:
  * load_project_memory finds every recognised filename and skips
    missing / empty
  * load_user_memory reads from ~/.claude/ with home override
  * load_all_memory yields user-level first, then project-level
  * render_for_prompt: empty list → empty string; populated list emits
    a header + per-file subheadings
  * Custom filenames argument override
  * MemoryFile carries path, convention, scope, content
  * Real-world smoke against this repo (CLAUDE.md present, others absent)
"""

from __future__ import annotations

from pathlib import Path


from backend.agents.project_memory import (
    PROJECT_RULE_FILENAMES,
    USER_RULE_FILENAMES,
    MemoryFile,
    load_all_memory,
    load_project_memory,
    load_user_memory,
    render_for_prompt,
)


# ─── load_project_memory ─────────────────────────────────────────


def test_load_project_finds_existing_rule_files(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("claude rules\n")
    (tmp_path / "AGENTS.md").write_text("agents rules\n")
    out = load_project_memory(tmp_path)
    conventions = [m.convention for m in out]
    assert "CLAUDE.md" in conventions
    assert "AGENTS.md" in conventions
    assert all(m.scope == "project" for m in out)


def test_load_project_skips_missing(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("rules\n")
    # No other files
    out = load_project_memory(tmp_path)
    assert len(out) == 1
    assert out[0].convention == "CLAUDE.md"


def test_load_project_skips_empty(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("real content\n")
    (tmp_path / "AGENTS.md").write_text("   \n  \n")
    out = load_project_memory(tmp_path)
    conventions = [m.convention for m in out]
    assert "CLAUDE.md" in conventions
    assert "AGENTS.md" not in conventions


def test_load_project_preserves_canonical_order(tmp_path: Path) -> None:
    """Output order matches PROJECT_RULE_FILENAMES, regardless of write order."""
    for fn in ("WARP.md", "AGENTS.md", "CLAUDE.md", "OMNISIGHT.md"):
        (tmp_path / fn).write_text(f"{fn} body\n")
    out = load_project_memory(tmp_path)
    assert [m.convention for m in out] == list(PROJECT_RULE_FILENAMES)


def test_load_project_returns_empty_when_root_missing_files(tmp_path: Path) -> None:
    assert load_project_memory(tmp_path) == []


def test_load_project_custom_filenames(tmp_path: Path) -> None:
    (tmp_path / "MY_RULES.md").write_text("custom\n")
    (tmp_path / "CLAUDE.md").write_text("claude\n")
    out = load_project_memory(tmp_path, filenames=("MY_RULES.md",))
    assert len(out) == 1
    assert out[0].convention == "MY_RULES.md"


# ─── load_user_memory ────────────────────────────────────────────


def test_load_user_uses_home_override(tmp_path: Path) -> None:
    home = tmp_path / "fakehome"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("user rules\n")
    out = load_user_memory(home=home)
    assert len(out) == 1
    assert out[0].convention == "CLAUDE.md"
    assert out[0].scope == "user"


def test_load_user_no_dot_claude_returns_empty(tmp_path: Path) -> None:
    out = load_user_memory(home=tmp_path)
    assert out == []


def test_load_user_skips_missing_files(tmp_path: Path) -> None:
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / "AGENTS.md").write_text("agents user rules\n")
    out = load_user_memory(home=home)
    assert len(out) == 1
    assert out[0].convention == "AGENTS.md"


# ─── load_all_memory ─────────────────────────────────────────────


def test_load_all_user_before_project(tmp_path: Path) -> None:
    project = tmp_path / "p"
    home = tmp_path / "h"
    project.mkdir()
    (project / "CLAUDE.md").write_text("project body\n")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("user body\n")

    out = load_all_memory(project, home=home)
    assert len(out) == 2
    # User comes first
    assert out[0].scope == "user"
    assert out[0].content.strip() == "user body"
    assert out[1].scope == "project"
    assert out[1].content.strip() == "project body"


def test_load_all_only_project_when_no_user(tmp_path: Path) -> None:
    project = tmp_path
    (project / "CLAUDE.md").write_text("rules\n")
    out = load_all_memory(project, home=tmp_path / "no-such-home")
    assert len(out) == 1
    assert out[0].scope == "project"


def test_load_all_only_user_when_no_project(tmp_path: Path) -> None:
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("user-only\n")
    project = tmp_path / "p"
    project.mkdir()
    out = load_all_memory(project, home=home)
    assert len(out) == 1
    assert out[0].scope == "user"


# ─── render_for_prompt ───────────────────────────────────────────


def test_render_empty_list_returns_empty_string() -> None:
    assert render_for_prompt([]) == ""


def test_render_includes_header_and_subheadings(tmp_path: Path) -> None:
    files = [
        MemoryFile(
            path=tmp_path / "CLAUDE.md",
            convention="CLAUDE.md",
            scope="project",
            content="rule A\nrule B",
        ),
        MemoryFile(
            path=tmp_path / "AGENTS.md",
            convention="AGENTS.md",
            scope="user",
            content="rule X",
        ),
    ]
    out = render_for_prompt(files)
    assert "L1 不可違反" in out  # default header
    assert "## CLAUDE.md（scope=project）" in out
    assert "## AGENTS.md（scope=user）" in out
    assert "rule A" in out
    assert "rule X" in out


def test_render_custom_header() -> None:
    files = [
        MemoryFile(
            path=Path("/x"),
            convention="CLAUDE.md",
            scope="project",
            content="body",
        )
    ]
    out = render_for_prompt(files, header="# Custom Header")
    assert out.startswith("# Custom Header")


# ─── Real-repo smoke ────────────────────────────────────────────


def test_real_repo_loads_existing_claude_md() -> None:
    project_root = Path(__file__).resolve().parents[2]
    out = load_project_memory(project_root)
    conventions = [m.convention for m in out]
    assert "CLAUDE.md" in conventions
    claude = next(m for m in out if m.convention == "CLAUDE.md")
    assert "OmniSight" in claude.content or "Co-Authored-By" in claude.content


def test_real_repo_returns_only_existing_files() -> None:
    """The repo currently has CLAUDE.md but not AGENTS.md / OMNISIGHT.md / WARP.md."""
    project_root = Path(__file__).resolve().parents[2]
    out = load_project_memory(project_root)
    conventions = {m.convention for m in out}
    # Whatever's there must be from the canonical set
    assert conventions.issubset(set(PROJECT_RULE_FILENAMES))
    # CLAUDE.md must be present
    assert "CLAUDE.md" in conventions


# ─── Public API surface contract ────────────────────────────────


def test_user_rule_filenames_is_subset_of_project() -> None:
    """User-scope conventions are a subset of project-scope (no surprise files)."""
    assert set(USER_RULE_FILENAMES).issubset(set(PROJECT_RULE_FILENAMES))
