"""BP.Q.2 -- workspace RAG indexer tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.agents import rag
from backend.agents import rag_indexer as idx
from backend.hooks import post_merge_rag_index as hook


class FakeEmbedder:
    def __init__(self):
        self.text_batches: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.text_batches.append(texts)
        return [[float(len(text)), 1.0] for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 1.0]


class FakeStore:
    def __init__(self):
        self.documents: list[rag.VectorDocument] = []
        self.deletes: list[tuple[str, str | None]] = []
        self.existing: list[rag.VectorDocument] = []

    async def upsert(self, documents: list[rag.VectorDocument]) -> None:
        self.documents.extend(documents)

    async def query(self, query: rag.VectorQuery) -> list[rag.VectorHit]:
        return []

    async def delete(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        source_path: str | None = None,
    ) -> int:
        assert chunk_ids is None
        self.deletes.append((tenant_id, source_path))
        return 1

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        source_path: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[rag.VectorDocument]:
        assert tenant_id == "t-acme"
        return self.existing[offset : offset + limit]


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_markdown_header_chunks_preserve_line_ranges(tmp_path):
    source_path = tmp_path / "docs" / "guide.md"
    _write(source_path, "# One\nbody\n## Two\nmore\n")
    source = idx.SourceFile(
        path="docs/guide.md",
        absolute_path=source_path,
        kind="doc",
        language="markdown",
    )

    chunks = idx.chunk_source_file(source)

    assert [chunk.line_start for chunk in chunks] == [1, 3]
    assert [chunk.line_end for chunk in chunks] == [2, 4]
    assert chunks[0].metadata["chunk_strategy"] == "markdown-header"
    assert chunks[1].metadata["header"] == "Two"


@dataclass
class FakeNode:
    start_point: tuple[int, int]
    end_point: tuple[int, int]
    type: str
    is_named: bool = True


class FakeParser:
    def parse(self, raw: bytes) -> Any:
        assert raw

        class Tree:
            root_node = type(
                "Root",
                (),
                {
                    "children": [
                        FakeNode((0, 0), (1, 0), "import_statement"),
                        FakeNode((3, 0), (5, 0), "function_definition"),
                    ]
                },
            )()

        return Tree()


def test_code_chunks_use_tree_sitter_parser_when_available(tmp_path):
    source_path = tmp_path / "backend" / "sample.py"
    _write(source_path, "import os\n\n\n" "def f():\n    return 1\n\n")
    source = idx.SourceFile(
        path="backend/sample.py",
        absolute_path=source_path,
        kind="code",
        language="python",
    )

    chunks = idx.chunk_source_file(source, parser_factory=lambda language: FakeParser())

    assert [chunk.metadata["chunk_strategy"] for chunk in chunks] == [
        "tree-sitter",
        "tree-sitter",
    ]
    assert chunks[0].line_start == 1
    assert chunks[1].metadata["node_type"] == "function_definition"


def test_discover_source_files_filters_git_tree(tmp_path, monkeypatch):
    _write(tmp_path / "docs" / "guide.md", "# Guide\n")
    _write(tmp_path / "backend" / "app.py", "print('x')\n")
    _write(tmp_path / "configs" / "skills" / "demo" / "SKILL.md", "# Skill\n")
    _write(tmp_path / "TODO.md", "- [ ] thing\n")
    _write(tmp_path / "test_assets" / "fixture.py", "print('fixture')\n")
    monkeypatch.setattr(
        idx,
        "git_tracked_files",
        lambda root: [
            "docs/guide.md",
            "backend/app.py",
            "configs/skills/demo/SKILL.md",
            "TODO.md",
            "test_assets/fixture.py",
        ],
    )

    sources = idx.discover_source_files(tmp_path)

    assert [source.path for source in sources] == [
        "TODO.md",
        "backend/app.py",
        "configs/skills/demo/SKILL.md",
        "docs/guide.md",
    ]


def test_post_merge_hook_fast_path_matches_indexable_paths():
    assert hook._merge_touches_indexable_path(["docs/guide.md"])
    assert hook._merge_touches_indexable_path(["configs/skills/demo/SKILL.md"])
    assert hook._merge_touches_indexable_path(["TODO.md"])
    assert hook._merge_touches_indexable_path(["backend/agents/rag_indexer.py"])
    assert not hook._merge_touches_indexable_path(["package-lock.json"])


@pytest.mark.asyncio
async def test_index_workspace_embeds_and_persists_chunks(tmp_path, monkeypatch):
    _write(tmp_path / "docs" / "guide.md", "# Guide\ninstall pgvector\n")
    monkeypatch.setattr(idx, "git_tracked_files", lambda root: ["docs/guide.md"])
    store = FakeStore()
    embedder = FakeEmbedder()
    indexer = idx.WorkspaceRagIndexer(
        repo_root=tmp_path,
        tenant_id="t-acme",
        embedder=embedder,
        store=store,
        batch_size=2,
    )

    result = await indexer.index_workspace(prune_missing=False)

    assert result.indexed_files == 1
    assert result.chunks == 1
    assert store.deletes == [("t-acme", "docs/guide.md")]
    assert store.documents[0].tenant_id == "t-acme"
    assert store.documents[0].metadata["line_start"] == 1
    assert embedder.text_batches == [["# Guide\ninstall pgvector"]]


@pytest.mark.asyncio
async def test_persisted_chunk_id_is_tenant_scoped(tmp_path, monkeypatch):
    _write(tmp_path / "docs" / "guide.md", "# Guide\ninstall pgvector\n")
    monkeypatch.setattr(idx, "git_tracked_files", lambda root: ["docs/guide.md"])
    left_store = FakeStore()
    right_store = FakeStore()

    await idx.WorkspaceRagIndexer(
        repo_root=tmp_path,
        tenant_id="t-acme",
        embedder=FakeEmbedder(),
        store=left_store,
    ).index_workspace(prune_missing=False)
    await idx.WorkspaceRagIndexer(
        repo_root=tmp_path,
        tenant_id="t-other",
        embedder=FakeEmbedder(),
        store=right_store,
    ).index_workspace(prune_missing=False)

    assert left_store.documents[0].chunk_id != right_store.documents[0].chunk_id


@pytest.mark.asyncio
async def test_incremental_indexes_changed_and_deletes_removed(tmp_path):
    _write(tmp_path / "docs" / "guide.md", "# Guide\n")
    store = FakeStore()
    indexer = idx.WorkspaceRagIndexer(
        repo_root=tmp_path,
        tenant_id="t-acme",
        embedder=FakeEmbedder(),
        store=store,
    )

    result = await indexer.index_changed_paths(
        ["docs/guide.md", "docs/removed.md", "README.md"]
    )

    assert result.indexed_files == 1
    assert result.deleted_files == 2
    assert ("t-acme", "docs/removed.md") in store.deletes
    assert ("t-acme", "README.md") in store.deletes


@pytest.mark.asyncio
async def test_initial_bulk_prunes_stale_sources(tmp_path, monkeypatch):
    _write(tmp_path / "docs" / "guide.md", "# Guide\n")
    monkeypatch.setattr(idx, "git_tracked_files", lambda root: ["docs/guide.md"])
    store = FakeStore()
    store.existing = [
        rag.VectorDocument(
            chunk_id="old",
            tenant_id="t-acme",
            source_path="docs/old.md",
            chunk_text="old",
            embedding=[1.0],
        )
    ]
    indexer = idx.WorkspaceRagIndexer(
        repo_root=tmp_path,
        tenant_id="t-acme",
        embedder=FakeEmbedder(),
        store=store,
    )

    result = await indexer.index_workspace(prune_missing=True)

    assert result.deleted_files == 1
    assert ("t-acme", "docs/old.md") in store.deletes
