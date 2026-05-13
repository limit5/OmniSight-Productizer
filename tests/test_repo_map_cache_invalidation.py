from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace as NS

from backend.agents import repo_map
from backend.agents.repo_map import load_or_build_graph
from backend.agents.treesitter_parser import TreeSitterImportParser


class FakeParser:
    def __init__(self):
        self.calls = 0

    def parse(self, raw: bytes):
        self.calls += 1
        return NS(root_node=NS(type="module", children=[], has_error=False))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _parser(fake: FakeParser) -> TreeSitterImportParser:
    return TreeSitterImportParser({"python": fake})


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    for key, value in (("user.email", "test@example.invalid"), ("user.name", "test")):
        _git(path, "config", key, value)


def _commit(path: Path, message: str) -> str:
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def test_ttl_eviction_rebuilds_even_when_head_sha_matches(tmp_path: Path, monkeypatch) -> None:
    _git_repo(tmp_path)
    _write(tmp_path / "backend" / "a.py", "VALUE = 1\n")
    head = _commit(tmp_path, "initial")
    cache_dir = Path(".cache-test")
    first = load_or_build_graph(tmp_path, cache_dir=cache_dir, parser=_parser(FakeParser()))
    old_mtime = time.time() - 10
    os.utime(tmp_path / cache_dir / f"{head}.json", (old_mtime, old_mtime))
    monkeypatch.setenv(repo_map.REPO_MAP_TTL_ENV, "1")
    fake = FakeParser()
    second = load_or_build_graph(tmp_path, cache_dir=cache_dir, parser=_parser(fake))
    stats = repo_map.get_cache_stats(tmp_path, cache_dir=cache_dir)
    assert first.head_sha == second.head_sha == head
    assert fake.calls == 1
    assert stats["ttl_sec"] == 1
    assert stats["head_cached"] is True
    assert stats["expired_entries"] == 0


def test_head_keyed_cache_invalidates_under_simulated_sha_change(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "backend" / "a.py", "VALUE = 1\n")
    shas = iter(("sha-one", "sha-two"))
    monkeypatch.setattr(repo_map, "repo_head_sha", lambda repo_root: next(shas))
    cache_dir = Path(".cache-test")
    first = load_or_build_graph(tmp_path, cache_dir=cache_dir, parser=_parser(FakeParser()))
    _write(tmp_path / "backend" / "b.py", "VALUE = 2\n")
    fake = FakeParser()
    monkeypatch.setattr(repo_map, "repo_head_sha", lambda repo_root: "sha-two")
    second = load_or_build_graph(tmp_path, cache_dir=cache_dir, parser=_parser(fake))
    assert first.head_sha == "sha-one"
    assert second.head_sha == "sha-two"
    assert "backend/b.py" in second.nodes
    assert fake.calls == 2


def test_real_git_commit_invalidates_cache_for_next_repo_map_call(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    _write(tmp_path / "backend" / "a.py", "VALUE = 1\n")
    first_head = _commit(tmp_path, "initial")
    cache_dir = Path(".cache-test")
    load_or_build_graph(tmp_path, cache_dir=cache_dir, parser=_parser(FakeParser()))
    _write(tmp_path / "backend" / "b.py", "VALUE = 2\n")
    second_head = _commit(tmp_path, "second")
    fake = FakeParser()
    graph = load_or_build_graph(tmp_path, cache_dir=cache_dir, parser=_parser(fake))
    assert first_head != second_head
    assert graph.head_sha == second_head
    assert "backend/b.py" in graph.nodes
    assert fake.calls == 2
