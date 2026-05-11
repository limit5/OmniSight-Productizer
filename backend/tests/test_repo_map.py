"""OP-840 repo-map preamble tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from backend.agents import repo_map
from backend.agents.repo_map import (
    CACHE_CORRUPTION,
    DEFAULT_TOKEN_BUDGET,
    NO_FILES_TOUCHED_TOKEN_BUDGET,
    RepoMapEntry,
    build_graph,
    build_repo_map_system_prefix,
    load_or_build_graph,
    rank_repo_files,
    render_repo_map_prefix,
    token_budget_for_ticket,
)
from backend.agents.treesitter_parser import (
    PARSE_ERROR_PER_FILE,
    TREE_SITTER_GRAMMAR_MISSING_LANG,
    TreeSitterImportParser,
)
from backend.prompt_loader import build_system_prompt


class FakeNode:
    def __init__(
        self,
        node_type: str,
        start_byte: int = 0,
        end_byte: int = 0,
        *,
        children: list["FakeNode"] | None = None,
        has_error: bool = False,
    ):
        self.type = node_type
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.children = children or []
        self.has_error = has_error


class FakeTree:
    def __init__(self, root_node: FakeNode):
        self.root_node = root_node


class FakeParser:
    def __init__(self, *, error_marker: str = "BROKEN"):
        self.calls = 0
        self.error_marker = error_marker

    def parse(self, raw: bytes) -> FakeTree:
        self.calls += 1
        text = raw.decode()
        if self.error_marker in text:
            return FakeTree(FakeNode("module", has_error=True))
        children: list[FakeNode] = []
        offset = 0
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                children.append(FakeNode("import_statement", offset, offset + len(line)))
            elif stripped.startswith("export "):
                children.append(FakeNode("export_statement", offset, offset + len(line)))
            elif "import(" in stripped:
                children.append(FakeNode("call_expression", offset, offset + len(line)))
            offset += len(line)
        return FakeTree(FakeNode("module", children=children))


class OomParser:
    def parse_file(self, repo_root: Path, source_path: Path):
        raise MemoryError("too large")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _parser(fake: FakeParser | None = None) -> TreeSitterImportParser:
    fake = fake or FakeParser()
    return TreeSitterImportParser(
        {"python": fake, "typescript": fake, "tsx": fake}
    )


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)


def _commit(path: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", message], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_happy_path_builds_symbol_graph_from_python_and_typescript(tmp_path: Path):
    _write(tmp_path / "backend" / "a.py", "import backend.b\n")
    _write(tmp_path / "backend" / "b.py", "VALUE = 1\n")
    _write(tmp_path / "ui" / "app.ts", "import './view'\n")
    _write(tmp_path / "ui" / "view.tsx", "export const View = () => null\n")

    graph = build_graph(tmp_path, head_sha="head", parser=_parser())

    assert set(graph.nodes) == {
        "backend/a.py",
        "backend/b.py",
        "ui/app.ts",
        "ui/view.tsx",
    }
    assert graph.edges["backend/a.py"] == ("backend/b.py",)
    assert graph.edges["ui/app.ts"] == ("ui/view.tsx",)


def test_missing_language_grammar_skips_that_language(tmp_path: Path):
    _write(tmp_path / "backend" / "a.py", "import backend.b\n")

    graph = build_graph(
        tmp_path,
        head_sha="head",
        parser=TreeSitterImportParser({"python": None}),
    )

    assert graph.nodes == ()
    assert [gap.code for gap in graph.logs] == [TREE_SITTER_GRAMMAR_MISSING_LANG]
    assert graph.logs[0].language == "python"


def test_per_file_parse_error_skips_only_bad_file(tmp_path: Path):
    _write(tmp_path / "backend" / "good.py", "import backend.target\n")
    _write(tmp_path / "backend" / "bad.py", "BROKEN\n")
    _write(tmp_path / "backend" / "target.py", "VALUE = 1\n")

    graph = build_graph(tmp_path, head_sha="head", parser=_parser())

    assert "backend/bad.py" not in graph.nodes
    assert "backend/good.py" in graph.nodes
    assert [gap.code for gap in graph.logs] == [PARSE_ERROR_PER_FILE]


def test_oom_falls_back_to_top_100_recent_files(tmp_path: Path):
    for idx in range(105):
        _write(tmp_path / f"f{idx:03}.py", f"VALUE = {idx}\n")

    graph = build_graph(tmp_path, head_sha="head", parser=OomParser())

    assert graph.fallback == repo_map.REPO_MAP_OOM
    assert len(graph.nodes) == 100
    assert graph.logs[0].code == repo_map.REPO_MAP_OOM


def test_cache_hit_uses_head_sha_cache_without_reparsing(tmp_path: Path):
    _git_repo(tmp_path)
    _write(tmp_path / "backend" / "a.py", "VALUE = 1\n")
    _commit(tmp_path, "initial")
    fake = FakeParser()

    first = load_or_build_graph(tmp_path, cache_dir=Path(".cache-test"), parser=_parser(fake))
    second = load_or_build_graph(
        tmp_path,
        cache_dir=Path(".cache-test"),
        parser=TreeSitterImportParser({"python": None}),
    )

    assert second == first
    assert fake.calls == 1


def test_cache_miss_rebuilds_and_cache_corruption_is_logged(tmp_path: Path, caplog):
    _git_repo(tmp_path)
    _write(tmp_path / "backend" / "a.py", "VALUE = 1\n")
    head = _commit(tmp_path, "initial")
    cache_dir = tmp_path / ".cache-test"
    cache_dir.mkdir()
    (cache_dir / f"{head}.json").write_text("{not json")
    fake = FakeParser()

    graph = load_or_build_graph(tmp_path, cache_dir=Path(".cache-test"), parser=_parser(fake))

    assert graph.head_sha == head
    assert fake.calls == 1
    assert any(record.code == CACHE_CORRUPTION for record in caplog.records)


def test_cache_invalidates_when_head_sha_changes(tmp_path: Path):
    _git_repo(tmp_path)
    _write(tmp_path / "backend" / "a.py", "VALUE = 1\n")
    first_head = _commit(tmp_path, "initial")
    load_or_build_graph(tmp_path, cache_dir=Path(".cache-test"), parser=_parser(FakeParser()))
    _write(tmp_path / "backend" / "b.py", "VALUE = 2\n")
    second_head = _commit(tmp_path, "second")
    fake = FakeParser()

    graph = load_or_build_graph(tmp_path, cache_dir=Path(".cache-test"), parser=_parser(fake))

    assert first_head != second_head
    assert graph.head_sha == second_head
    assert fake.calls == 2


def test_seeded_pagerank_budget_and_system_prompt_prefix(tmp_path: Path):
    _write(tmp_path / "backend" / "a.py", "import backend.b\n")
    _write(tmp_path / "backend" / "b.py", "import backend.c\n")
    _write(tmp_path / "backend" / "c.py", "VALUE = 1\n")

    graph = build_graph(tmp_path, head_sha="head", parser=_parser())
    ranked = rank_repo_files(
        graph,
        ticket_text="AC mentions backend/b.py and Files Touched\n* backend/a.py",
    )
    rendered = render_repo_map_prefix(
        [
            RepoMapEntry("backend/a.py", 0.9, 1),
            RepoMapEntry("backend/b.py", 0.8, 1),
            RepoMapEntry("backend/c.py", 0.7, 0),
        ],
        token_budget=38,
    )
    prompt = build_system_prompt(
        agent_type="general",
        repo_map_preamble="# Repo Map Context\n\n- `backend/a.py`",
    )

    assert ranked[0].path == "backend/b.py"
    assert "`backend/a.py`" in rendered
    assert "`backend/c.py`" not in rendered
    assert token_budget_for_ticket("No hints here") == NO_FILES_TOUCHED_TOKEN_BUDGET
    assert token_budget_for_ticket("Files Touched\n* backend/a.py") == DEFAULT_TOKEN_BUDGET
    assert prompt.startswith("# Repo Map Context")


def test_build_repo_map_system_prefix_degrades_when_binary_missing(tmp_path: Path):
    _write(tmp_path / "backend" / "a.py", "VALUE = 1\n")

    prefix = build_repo_map_system_prefix(tmp_path, ticket_text="backend/a.py")

    assert prefix == "" or prefix.startswith("# Repo Map Context")
