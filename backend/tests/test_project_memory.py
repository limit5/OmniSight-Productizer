"""Tests for backend.agents.project_memory (Phase 4 — multi-rule walker).

Locks:
  * load_project_memory finds every recognised filename and skips
    missing / empty
  * load_project_memory walks current dir + up to three parents with
    distance-derived weights
  * project_rule_signature changes when watched rule files change
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
    PROJECT_RULE_FILE_MAX_BYTES,
    PROJECT_RULE_TOTAL_MAX_BYTES,
    USER_RULE_FILENAMES,
    MemoryFile,
    load_all_memory,
    load_project_memory,
    load_user_memory,
    project_rule_dirs,
    project_rule_merge_dirs,
    project_rule_signature,
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


def test_load_project_preserves_filename_precedence_order(tmp_path: Path) -> None:
    """Output order matches PROJECT_RULE_FILENAMES, regardless of write order."""
    for fn in ("WARP.md", "AGENTS.md", "CLAUDE.md", "OMNISIGHT.md"):
        (tmp_path / fn).write_text(f"{fn} body\n")
    out = load_project_memory(tmp_path)
    assert [m.convention for m in out] == list(PROJECT_RULE_FILENAMES)
    assert out[-1].convention == "OMNISIGHT.md"


def test_load_project_returns_empty_when_root_missing_files(tmp_path: Path) -> None:
    assert load_project_memory(tmp_path) == []


def test_load_project_custom_filenames(tmp_path: Path) -> None:
    (tmp_path / "MY_RULES.md").write_text("custom\n")
    (tmp_path / "CLAUDE.md").write_text("claude\n")
    out = load_project_memory(tmp_path, filenames=("MY_RULES.md",))
    assert len(out) == 1
    assert out[0].convention == "MY_RULES.md"


def test_project_rule_dirs_current_plus_three_parents(tmp_path: Path) -> None:
    current = tmp_path / "a" / "b" / "c" / "d"
    current.mkdir(parents=True)

    out = project_rule_dirs(current)

    assert [p.name for p, _, _ in out] == ["d", "c", "b", "a"]
    assert [distance for _, distance, _ in out] == [0, 1, 2, 3]
    assert [weight for _, _, weight in out] == [4, 3, 2, 1]


def test_project_rule_merge_dirs_parents_before_current(tmp_path: Path) -> None:
    current = tmp_path / "a" / "b" / "c" / "d"
    current.mkdir(parents=True)

    out = project_rule_merge_dirs(current)

    assert [p.name for p, _, _ in out] == ["a", "b", "c", "d"]
    assert [distance for _, distance, _ in out] == [3, 2, 1, 0]
    assert [weight for _, _, weight in out] == [1, 2, 3, 4]


def test_load_project_walks_parents_in_merge_precedence_order(
    tmp_path: Path,
) -> None:
    current = tmp_path / "a" / "b" / "c" / "d"
    current.mkdir(parents=True)
    (current / "CLAUDE.md").write_text("current\n")
    (current.parent / "CLAUDE.md").write_text("parent-1\n")
    (current.parent.parent / "CLAUDE.md").write_text("parent-2\n")
    (current.parent.parent.parent / "CLAUDE.md").write_text("parent-3\n")
    (tmp_path / "CLAUDE.md").write_text("too-far\n")

    out = load_project_memory(current, filenames=("CLAUDE.md",))

    assert [m.content.strip() for m in out] == [
        "parent-3",
        "parent-2",
        "parent-1",
        "current",
    ]
    assert [m.distance for m in out] == [3, 2, 1, 0]
    assert [m.weight for m in out] == [1, 2, 3, 4]


def test_load_project_project_specific_rules_come_after_generic(
    tmp_path: Path,
) -> None:
    current = tmp_path / "a" / "b"
    current.mkdir(parents=True)
    (current.parent / "OMNISIGHT.md").write_text("parent project specific\n")
    (current / "CLAUDE.md").write_text("current generic\n")
    (current / "OMNISIGHT.md").write_text("current project specific\n")

    out = load_project_memory(current)

    assert [m.content.strip() for m in out] == [
        "parent project specific",
        "current generic",
        "current project specific",
    ]
    assert out[-1].convention == "OMNISIGHT.md"
    assert out[-1].distance == 0


def test_project_rule_signature_tracks_content_changes(tmp_path: Path) -> None:
    rule_file = tmp_path / "CLAUDE.md"
    rule_file.write_text("first\n")
    first = project_rule_signature(tmp_path, filenames=("CLAUDE.md",))

    rule_file.write_text("second rules\n")
    second = project_rule_signature(tmp_path, filenames=("CLAUDE.md",))

    assert first != second


def test_project_rule_signature_tracks_add_and_remove(tmp_path: Path) -> None:
    first = project_rule_signature(tmp_path, filenames=("CLAUDE.md",))

    rule_file = tmp_path / "CLAUDE.md"
    rule_file.write_text("rules\n")
    second = project_rule_signature(tmp_path, filenames=("CLAUDE.md",))

    rule_file.unlink()
    third = project_rule_signature(tmp_path, filenames=("CLAUDE.md",))

    assert first == ()
    assert second != first
    assert third == first


def test_load_project_truncates_each_rule_file_at_five_kib(
    tmp_path: Path,
) -> None:
    rule_file = tmp_path / "CLAUDE.md"
    rule_file.write_text("a" * (PROJECT_RULE_FILE_MAX_BYTES + 20))

    out = load_project_memory(tmp_path, filenames=("CLAUDE.md",))

    assert len(out) == 1
    assert out[0].size_bytes == PROJECT_RULE_FILE_MAX_BYTES + 20
    assert out[0].included_bytes == PROJECT_RULE_FILE_MAX_BYTES
    assert out[0].truncated is True
    assert out[0].truncated_reason == "file"
    assert len(out[0].content.encode("utf-8")) == PROJECT_RULE_FILE_MAX_BYTES


def test_load_project_truncates_merged_rules_at_fifty_kib(
    tmp_path: Path,
) -> None:
    current = tmp_path / "a" / "b" / "c" / "d"
    current.mkdir(parents=True)
    for base in (current, current.parent, current.parent.parent):
        for fn in PROJECT_RULE_FILENAMES:
            (base / fn).write_text("x" * PROJECT_RULE_FILE_MAX_BYTES)

    out = load_project_memory(current)

    assert sum(m.included_bytes for m in out) == PROJECT_RULE_TOTAL_MAX_BYTES
    assert out[-1].truncated is True
    assert out[-1].truncated_reason == "total"
    assert out[-1].included_bytes == 0


def test_load_project_marks_operator_ignored_rule_file(
    tmp_path: Path,
) -> None:
    rule_file = tmp_path / "CLAUDE.md"
    rule_file.write_text("ignore me\n")

    out = load_project_memory(tmp_path, ignored_paths=[rule_file])

    assert len(out) == 1
    assert out[0].ignored is True
    assert out[0].content == ""
    assert out[0].size_bytes == len("ignore me\n")
    assert out[0].included_bytes == 0


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
            distance=0,
            weight=4,
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
    assert "## CLAUDE.md（scope=project, distance=0, weight=4）" in out
    assert "## AGENTS.md（scope=user）" in out
    assert "rule A" in out
    assert "rule X" in out


def test_render_surfaces_truncated_and_ignore_options(tmp_path: Path) -> None:
    files = [
        MemoryFile(
            path=tmp_path / "CLAUDE.md",
            convention="CLAUDE.md",
            scope="project",
            content="a" * PROJECT_RULE_FILE_MAX_BYTES,
            size_bytes=PROJECT_RULE_FILE_MAX_BYTES + 1,
            included_bytes=PROJECT_RULE_FILE_MAX_BYTES,
            truncated=True,
            truncated_reason="file",
        ),
        MemoryFile(
            path=tmp_path / "AGENTS.md",
            convention="AGENTS.md",
            scope="project",
            content="",
            size_bytes=9,
            ignored=True,
        ),
    ]

    out = render_for_prompt(files)

    assert "truncated=true, reason=file" in out
    assert "[truncated; operator may ignore this file]" in out
    assert "ignored=true" in out
    assert "[ignored by operator]" in out


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
