"""BP.Q.7 -- Vector RAG contract tests.

Coverage axes:
  * chunking strategy correctness for markdown, code, TODO/SKILL, and fallback text
  * retrieval relevance on a canned corpus
  * tenant isolation at query/delete/list boundaries
  * pgvector/qdrant/chroma adapter payload and result contracts
  * KnowledgeRetrieval handler integration
  * optional live pgvector smoke via ``OMNISIGHT_RAG_PGVECTOR_TEST_DSN``

Module-global state audit: these tests introduce only immutable canned data and
per-test fake stores/embedders. No mutable process-global state is shared across
workers; monkeypatch mutations are pytest-scoped.
"""

from __future__ import annotations

import math
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.agents import rag
from backend.agents import rag_indexer as idx
from backend.agents import runner_handlers


def _legacy_doc(**kw: Any) -> rag.VectorDocument:
    base = {
        "chunk_id": "c-1",
        "tenant_id": "t-acme",
        "source_path": "docs/guide.md",
        "chunk_text": "install pgvector",
        "embedding": [0.1, 0.2, 0.3],
        "metadata": {"line_start": 10, "kind": "markdown"},
    }
    base.update(kw)
    return rag.VectorDocument(**base)


class _FakeHttpClient:
    calls: list[dict[str, Any]] = []
    responses: list[httpx.Response] = []

    def __init__(self, *, timeout: float):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.responses.pop(0)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": "PUT", "url": url, **kwargs})
        return self.responses.pop(0)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeHttpClient.calls = []
    _FakeHttpClient.responses = []
    monkeypatch.setattr(rag.httpx, "AsyncClient", _FakeHttpClient)
    return _FakeHttpClient


@dataclass
class FakeNode:
    start_point: tuple[int, int]
    end_point: tuple[int, int]
    type: str
    is_named: bool = True


class FakeParser:
    def __init__(self, nodes: list[FakeNode], *, fail: bool = False):
        self.nodes = nodes
        self.fail = fail

    def parse(self, raw: bytes) -> Any:
        if self.fail:
            raise RuntimeError("parser failed")
        assert raw

        class Tree:
            root_node = type("Root", (), {"children": self.nodes})()

        return Tree()


class TopicEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        lower = text.lower()
        return [
            float(any(word in lower for word in ("pgvector", "embedding", "rag"))),
            float(any(word in lower for word in ("oauth", "token", "login"))),
            float(any(word in lower for word in ("sensor", "mipi", "camera"))),
        ]


class MemoryVectorStore:
    def __init__(self):
        self.documents: dict[str, rag.VectorDocument] = {}
        self.queries: list[rag.VectorQuery] = []
        self.deletes: list[dict[str, Any]] = []
        self.lists: list[dict[str, Any]] = []

    async def upsert(self, documents: list[rag.VectorDocument]) -> None:
        tenant_ids = {doc.tenant_id for doc in documents}
        if len(tenant_ids) != 1:
            raise ValueError("documents must belong to exactly one tenant")
        for doc in documents:
            self.documents[doc.chunk_id] = doc

    async def query(self, query: rag.VectorQuery) -> list[rag.VectorHit]:
        self.queries.append(query)
        hits: list[rag.VectorHit] = []
        for doc in self.documents.values():
            if doc.tenant_id != query.tenant_id:
                continue
            if query.source_path and doc.source_path != query.source_path:
                continue
            if any(doc.metadata.get(k) != v for k, v in query.metadata_filter.items()):
                continue
            score = _cosine(query.embedding, doc.embedding)
            hits.append(
                rag.VectorHit(
                    chunk_id=doc.chunk_id,
                    tenant_id=doc.tenant_id,
                    source_path=doc.source_path,
                    chunk_text=doc.chunk_text,
                    score=score,
                    metadata=dict(doc.metadata),
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: query.limit]

    async def delete(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        source_path: str | None = None,
    ) -> int:
        self.deletes.append(
            {"tenant_id": tenant_id, "chunk_ids": chunk_ids, "source_path": source_path}
        )
        deleted = 0
        for chunk_id, doc in list(self.documents.items()):
            if doc.tenant_id != tenant_id:
                continue
            if chunk_ids and chunk_id not in chunk_ids:
                continue
            if source_path and doc.source_path != source_path:
                continue
            del self.documents[chunk_id]
            deleted += 1
        return deleted

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        source_path: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[rag.VectorDocument]:
        self.lists.append(
            {
                "tenant_id": tenant_id,
                "source_path": source_path,
                "limit": limit,
                "offset": offset,
            }
        )
        docs = [
            doc
            for doc in self.documents.values()
            if doc.tenant_id == tenant_id
            and (source_path is None or doc.source_path == source_path)
        ]
        docs.sort(key=lambda doc: (doc.source_path, doc.chunk_id))
        return docs[offset : offset + limit]


class FakePgConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = rows or []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.executemany_calls: list[tuple[str, list[tuple[Any, ...]]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *values: Any) -> str:
        self.execute_calls.append((sql, values))
        if sql.startswith("DELETE"):
            return "DELETE 2"
        return "SELECT 1"

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.executemany_calls.append((sql, rows))

    async def fetch(self, sql: str, *values: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, values))
        return self.rows


class FakeCloseable:
    def __init__(self):
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _doc(
    chunk_id: str,
    tenant_id: str,
    source_path: str,
    text: str,
    embedding: list[float],
    **metadata: Any,
) -> rag.VectorDocument:
    return rag.VectorDocument(
        chunk_id=chunk_id,
        tenant_id=tenant_id,
        source_path=source_path,
        chunk_text=text,
        embedding=embedding,
        metadata=metadata,
    )


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("TODO.md", ("todo", "markdown")),
        ("docs/rag-setup.md", ("doc", "markdown")),
        ("docs/runbook.txt", ("doc", "text")),
        ("skills/camera/SKILL.md", ("skill", "markdown")),
        ("backend/agents/rag.py", ("code", "python")),
        ("frontend/src/App.tsx", ("code", "tsx")),
        ("backend/query.sql", ("code", "sql")),
        ("src/main.go", ("code", "go")),
        ("README.md", (None, None)),
        ("docs/image.png", (None, None)),
    ],
)
def test_classify_source_path_contract(raw: str, expected: tuple[str | None, str | None]):
    assert idx.classify_source_path(raw) == expected


def test_source_file_for_path_rejects_test_assets_and_binary(tmp_path):
    _write(tmp_path / "test_assets" / "fixture.py", "print('fixture')\n")
    (tmp_path / "docs" / "blob.txt").parent.mkdir(parents=True)
    (tmp_path / "docs" / "blob.txt").write_bytes(b"abc\0def")

    assert idx.source_file_for_path(tmp_path, "test_assets/fixture.py") is None
    assert idx.source_file_for_path(tmp_path, "docs/blob.txt") is None


def test_source_file_for_path_rejects_escape_path(tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")

    assert idx.source_file_for_path(tmp_path, "../outside.md") is None


def test_markdown_chunking_splits_at_headers_and_preserves_citations(tmp_path):
    source_path = tmp_path / "docs" / "guide.md"
    _write(source_path, "intro\n# Install\npgvector\n## Query\nsemantic search\n")
    source = idx.SourceFile(
        path="docs/guide.md",
        absolute_path=source_path,
        kind="doc",
        language="markdown",
    )

    chunks = idx.chunk_source_file(source)

    assert [chunk.line_start for chunk in chunks] == [1, 2, 4]
    assert [chunk.line_end for chunk in chunks] == [1, 3, 5]
    assert [chunk.metadata["header"] for chunk in chunks] == ["", "Install", "Query"]
    assert all(chunk.metadata["chunk_strategy"] == "markdown-header" for chunk in chunks)


@pytest.mark.parametrize(
    "kind,path",
    [
        ("todo", "TODO.md"),
        ("skill", "configs/skills/demo/SKILL.md"),
    ],
)
def test_special_markdown_sources_use_header_chunking(tmp_path, kind: str, path: str):
    source_path = tmp_path / path
    _write(source_path, "# One\nbody\n# Two\nbody\n")
    source = idx.SourceFile(path=path, absolute_path=source_path, kind=kind, language="markdown")

    chunks = idx.chunk_source_file(source)

    assert [chunk.metadata["kind"] for chunk in chunks] == [kind, kind]
    assert [chunk.metadata["chunk_strategy"] for chunk in chunks] == [
        "markdown-header",
        "markdown-header",
    ]


def test_empty_source_file_returns_no_chunks(tmp_path):
    source_path = tmp_path / "docs" / "empty.md"
    _write(source_path, "\n\n")
    source = idx.SourceFile(
        path="docs/empty.md",
        absolute_path=source_path,
        kind="doc",
        language="markdown",
    )

    assert idx.chunk_source_file(source) == []


def test_code_chunking_uses_top_level_tree_sitter_nodes(tmp_path):
    source_path = tmp_path / "backend" / "sample.py"
    _write(source_path, "import os\n\n\ndef run():\n    return os.getcwd()\n")
    source = idx.SourceFile(
        path="backend/sample.py",
        absolute_path=source_path,
        kind="code",
        language="python",
    )
    parser = FakeParser(
        [
            FakeNode((0, 0), (0, 9), "import_statement"),
            FakeNode((3, 0), (4, 23), "function_definition"),
        ]
    )

    chunks = idx.chunk_source_file(source, parser_factory=lambda language: parser)

    assert [chunk.line_start for chunk in chunks] == [1, 4]
    assert [chunk.line_end for chunk in chunks] == [1, 5]
    assert [chunk.metadata["node_type"] for chunk in chunks] == [
        "import_statement",
        "function_definition",
    ]


def test_code_chunking_skips_unnamed_nodes(tmp_path):
    chunks = idx.chunk_code_with_tree_sitter(
        "backend/sample.py",
        "x = 1\n\ndef f():\n    return x\n",
        language="python",
        parser_factory=lambda language: FakeParser(
            [
                FakeNode((0, 0), (0, 5), "assignment", is_named=False),
                FakeNode((2, 0), (3, 12), "function_definition"),
            ]
        ),
    )

    assert len(chunks) == 1
    assert chunks[0].metadata["node_type"] == "function_definition"


def test_code_chunking_falls_back_to_line_window_when_parser_missing(tmp_path):
    source_path = tmp_path / "backend" / "sample.py"
    _write(source_path, "print('a')\nprint('b')\n")
    source = idx.SourceFile(
        path="backend/sample.py",
        absolute_path=source_path,
        kind="code",
        language="python",
    )

    chunks = idx.chunk_source_file(source, parser_factory=lambda language: None)

    assert len(chunks) == 1
    assert chunks[0].metadata["chunk_strategy"] == "line-window"
    assert chunks[0].line_start == 1
    assert chunks[0].line_end == 2


def test_code_chunking_falls_back_when_parser_raises():
    source = idx.SourceFile(
        path="backend/sample.py",
        absolute_path=Path("backend/tests/test_rag.py"),
        kind="code",
        language="python",
    )

    chunks = idx.chunk_source_file(
        source,
        parser_factory=lambda language: FakeParser([], fail=True),
    )

    assert chunks[0].metadata["chunk_strategy"] == "line-window"


def test_line_window_chunking_uses_stable_ranges():
    chunks = idx.chunk_by_line_window(
        "docs/long.txt",
        "\n".join(f"line {i}" for i in range(1, 8)),
        source_kind="doc",
        max_lines=3,
    )

    assert [(chunk.line_start, chunk.line_end) for chunk in chunks] == [
        (1, 3),
        (4, 6),
        (7, 7),
    ]
    assert all(chunk.metadata["chunk_strategy"] == "line-window" for chunk in chunks)


def test_chunk_id_changes_when_body_changes():
    left = idx.SourceChunk("docs/a.md", "body", 1, 1)
    right = idx.SourceChunk("docs/a.md", "changed", 1, 1)

    assert left.chunk_id != right.chunk_id


@pytest.fixture
async def canned_store() -> MemoryVectorStore:
    store = MemoryVectorStore()
    await store.upsert(
        [
            _doc(
                "acme-rag",
                "t-acme",
                "docs/rag.md",
                "pgvector embeddings support tenant-scoped RAG retrieval",
                [1.0, 0.0, 0.0],
                line_start=1,
                line_end=3,
                kind="doc",
            ),
            _doc(
                "acme-oauth",
                "t-acme",
                "docs/auth.md",
                "OAuth token exchange and login callback handling",
                [0.0, 1.0, 0.0],
                line_start=4,
                line_end=8,
                kind="doc",
            ),
            _doc(
                "acme-sensor",
                "t-acme",
                "hd/sensor.md",
                "camera sensor MIPI timing and register map",
                [0.0, 0.0, 1.0],
                line_start=9,
                line_end=11,
                kind="hardware",
            ),
        ]
    )
    await store.upsert(
        [
            _doc(
                "other-rag",
                "t-other",
                "docs/rag.md",
                "other tenant pgvector deployment notes",
                [1.0, 0.0, 0.0],
                line_start=1,
                line_end=2,
                kind="doc",
            ),
        ]
    )
    return store


@pytest.mark.asyncio
async def test_retrieval_returns_most_relevant_canned_chunk(canned_store: MemoryVectorStore):
    query = rag.VectorQuery(tenant_id="t-acme", embedding=[1.0, 0.0, 0.0], limit=2)

    hits = await canned_store.query(query)

    assert hits[0].chunk_id == "acme-rag"
    assert hits[0].score == pytest.approx(1.0)
    assert all(hit.tenant_id == "t-acme" for hit in hits)


@pytest.mark.asyncio
async def test_retrieval_respects_top_k(canned_store: MemoryVectorStore):
    hits = await canned_store.query(
        rag.VectorQuery(tenant_id="t-acme", embedding=[1.0, 1.0, 1.0], limit=2)
    )

    assert len(hits) == 2


@pytest.mark.asyncio
async def test_retrieval_filters_by_source_path(canned_store: MemoryVectorStore):
    hits = await canned_store.query(
        rag.VectorQuery(
            tenant_id="t-acme",
            embedding=[1.0, 1.0, 1.0],
            limit=5,
            source_path="docs/auth.md",
        )
    )

    assert [hit.chunk_id for hit in hits] == ["acme-oauth"]


@pytest.mark.asyncio
async def test_retrieval_filters_by_metadata(canned_store: MemoryVectorStore):
    hits = await canned_store.query(
        rag.VectorQuery(
            tenant_id="t-acme",
            embedding=[1.0, 1.0, 1.0],
            limit=5,
            metadata_filter={"kind": "hardware"},
        )
    )

    assert [hit.chunk_id for hit in hits] == ["acme-sensor"]


@pytest.mark.asyncio
async def test_tenant_isolation_query_excludes_other_tenant(canned_store: MemoryVectorStore):
    hits = await canned_store.query(
        rag.VectorQuery(tenant_id="t-acme", embedding=[1.0, 0.0, 0.0], limit=10)
    )

    assert "other-rag" not in {hit.chunk_id for hit in hits}


@pytest.mark.asyncio
async def test_tenant_isolation_list_by_tenant_excludes_other_tenant(
    canned_store: MemoryVectorStore,
):
    docs = await canned_store.list_by_tenant("t-other")

    assert [doc.chunk_id for doc in docs] == ["other-rag"]


@pytest.mark.asyncio
async def test_tenant_isolation_delete_removes_only_selected_tenant(
    canned_store: MemoryVectorStore,
):
    deleted = await canned_store.delete(tenant_id="t-other", source_path="docs/rag.md")

    assert deleted == 1
    assert "other-rag" not in canned_store.documents
    assert "acme-rag" in canned_store.documents


@pytest.mark.asyncio
async def test_vector_store_rejects_mixed_tenant_upsert():
    store = MemoryVectorStore()

    with pytest.raises(ValueError, match="exactly one tenant"):
        await store.upsert(
            [
                _doc("a", "t-a", "a.md", "a", [1.0]),
                _doc("b", "t-b", "b.md", "b", [1.0]),
            ]
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: rag.VectorDocument("", "t", "p", "text", [1.0]),
        lambda: rag.VectorDocument("c", "", "p", "text", [1.0]),
        lambda: rag.VectorDocument("c", "t", "", "text", [1.0]),
        lambda: rag.VectorDocument("c", "t", "p", "", [1.0]),
        lambda: rag.VectorDocument("c", "t", "p", "text", []),
        lambda: rag.VectorQuery("", [1.0]),
        lambda: rag.VectorQuery("t", []),
        lambda: rag.VectorQuery("t", [1.0], limit=0),
    ],
)
def test_vector_dataclasses_validate_required_fields(factory):
    with pytest.raises(ValueError):
        factory()


def test_payload_from_document_preserves_contract_fields():
    doc = _doc("c", "t-acme", "docs/rag.md", "text", [1.0], line_start=1)

    payload = rag._payload_from_document(doc)

    assert payload == {
        "tenant_id": "t-acme",
        "source_path": "docs/rag.md",
        "chunk_text": "text",
        "metadata": {"line_start": 1},
    }


def test_qdrant_filter_includes_tenant_source_metadata_and_ids():
    out = rag._qdrant_filter_for_tenant(
        "t-acme",
        "docs/rag.md",
        metadata_filter={"kind": "doc"},
        chunk_ids=["a", "b"],
    )

    assert {"key": "tenant_id", "match": {"value": "t-acme"}} in out["must"]
    assert {"key": "source_path", "match": {"value": "docs/rag.md"}} in out["must"]
    assert {"key": "metadata.kind", "match": {"value": "doc"}} in out["must"]
    assert {"has_id": ["a", "b"]} in out["must"]


def test_chroma_where_single_clause_is_plain_object():
    assert rag._chroma_where("t-acme") == {"tenant_id": "t-acme"}


def test_chroma_where_multiple_clauses_uses_and():
    assert rag._chroma_where("t-acme", "docs/rag.md", {"kind": "doc"}) == {
        "$and": [
            {"tenant_id": "t-acme"},
            {"source_path": "docs/rag.md"},
            {"metadata.kind": "doc"},
        ]
    }


@pytest.mark.asyncio
async def test_qdrant_upsert_posts_points_payload(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_put_json(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(rag, "_put_json", fake_put_json)
    store = rag.QdrantStore(collection="chunks", api_key="secret", api_base="http://q")

    await store.upsert([_doc("c1", "t-acme", "docs/rag.md", "text", [0.1, 0.2])])

    body = calls[0]["json_body"]
    assert calls[0]["url"] == "http://q/collections/chunks/points"
    assert calls[0]["headers"]["api-key"] == "secret"
    assert body["points"][0]["id"] == "c1"
    assert body["points"][0]["payload"]["tenant_id"] == "t-acme"


@pytest.mark.asyncio
async def test_qdrant_query_posts_tenant_filter_and_maps_hits(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_post_json(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "result": [
                {
                    "id": "c1",
                    "score": 0.75,
                    "payload": {
                        "tenant_id": "t-acme",
                        "source_path": "docs/rag.md",
                        "chunk_text": "text",
                        "metadata": {"line_start": 1},
                    },
                }
            ]
        }

    monkeypatch.setattr(rag, "_post_json", fake_post_json)
    store = rag.QdrantStore(collection="chunks", api_base="http://q")

    hits = await store.query(
        rag.VectorQuery(
            tenant_id="t-acme",
            embedding=[1.0, 0.0],
            limit=3,
            source_path="docs/rag.md",
        )
    )

    assert calls[0]["json_body"]["filter"]["must"][0] == {
        "key": "tenant_id",
        "match": {"value": "t-acme"},
    }
    assert hits[0].chunk_id == "c1"
    assert hits[0].metadata == {"line_start": 1}


@pytest.mark.asyncio
async def test_qdrant_delete_requires_selector():
    store = rag.QdrantStore(collection="chunks")

    with pytest.raises(ValueError, match="chunk_ids or source_path"):
        await store.delete(tenant_id="t-acme")


@pytest.mark.asyncio
async def test_qdrant_list_by_tenant_uses_scroll_payload(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_post_json(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "result": {
                "points": [
                    {
                        "id": "c1",
                        "vector": [0.2],
                        "payload": {
                            "tenant_id": "t-acme",
                            "source_path": "docs/rag.md",
                            "chunk_text": "text",
                            "metadata": {},
                        },
                    }
                ]
            }
        }

    monkeypatch.setattr(rag, "_post_json", fake_post_json)
    store = rag.QdrantStore(collection="chunks", api_base="http://q")

    docs = await store.list_by_tenant("t-acme", source_path="docs/rag.md", limit=7)

    assert calls[0]["url"] == "http://q/collections/chunks/points/scroll"
    assert calls[0]["json_body"]["limit"] == 7
    assert docs[0].chunk_id == "c1"


@pytest.mark.asyncio
async def test_chroma_upsert_posts_collection_payload(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_post_json(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(rag, "_post_json", fake_post_json)
    store = rag.ChromaStore(collection="chunks", api_base="http://chroma")

    await store.upsert([_doc("c1", "t-acme", "docs/rag.md", "text", [0.1])])

    body = calls[0]["json_body"]
    assert calls[0]["url"] == "http://chroma/api/v1/collections/chunks/upsert"
    assert body["ids"] == ["c1"]
    assert body["metadatas"][0]["tenant_id"] == "t-acme"


@pytest.mark.asyncio
async def test_chroma_query_maps_distance_to_similarity(monkeypatch):
    async def fake_post_json(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["json_body"]["where"] == {"tenant_id": "t-acme"}
        return {
            "ids": [["c1"]],
            "documents": [["text"]],
            "metadatas": [[{"tenant_id": "t-acme", "source_path": "docs/rag.md"}]],
            "distances": [[0.25]],
        }

    monkeypatch.setattr(rag, "_post_json", fake_post_json)
    store = rag.ChromaStore(collection="chunks")

    hits = await store.query(rag.VectorQuery("t-acme", [1.0], limit=1))

    assert hits[0].score == pytest.approx(0.75)
    assert hits[0].source_path == "docs/rag.md"


@pytest.mark.asyncio
async def test_chroma_delete_with_ids_keeps_tenant_where(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_post_json(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(rag, "_post_json", fake_post_json)
    store = rag.ChromaStore(collection="chunks")

    deleted = await store.delete(tenant_id="t-acme", chunk_ids=["c1", "c2"])

    assert deleted == 2
    assert calls[0]["json_body"] == {
        "where": {"tenant_id": "t-acme"},
        "ids": ["c1", "c2"],
    }


@pytest.mark.asyncio
async def test_chroma_list_by_tenant_maps_documents(monkeypatch):
    async def fake_post_json(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["json_body"]["offset"] == 2
        return {
            "ids": ["c1"],
            "documents": ["text"],
            "metadatas": [{"tenant_id": "t-acme", "source_path": "docs/rag.md"}],
            "embeddings": [[0.5]],
        }

    monkeypatch.setattr(rag, "_post_json", fake_post_json)
    store = rag.ChromaStore(collection="chunks")

    docs = await store.list_by_tenant("t-acme", limit=5, offset=2)

    assert docs[0].chunk_id == "c1"
    assert docs[0].embedding == [0.5]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: rag.QdrantStore(collection=""),
        lambda: rag.ChromaStore(collection=""),
    ],
)
def test_http_stores_validate_collection(factory):
    with pytest.raises(ValueError, match="collection is required"):
        factory()


@pytest.mark.asyncio
async def test_pgvector_upsert_sets_tenant_scope_and_batches_rows():
    conn = FakePgConn()
    store = rag.PgvectorStore(conn)

    await store.upsert([_doc("c1", "t-acme", "docs/rag.md", "text", [1.0, 2.0])])

    assert conn.execute_calls[0][1] == (rag.PGVECTOR_TENANT_SETTING, "t-acme")
    assert conn.execute_calls[-1][1] == (rag.PGVECTOR_TENANT_SETTING,)
    sql, rows = conn.executemany_calls[0]
    assert "ON CONFLICT (chunk_id) DO UPDATE" in sql
    assert rows[0][:5] == ("c1", "t-acme", "docs/rag.md", "text", "[1.0,2.0]")


@pytest.mark.asyncio
async def test_pgvector_upsert_rejects_empty_batch():
    with pytest.raises(ValueError, match="at least one document"):
        await rag.PgvectorStore(FakePgConn()).upsert([])


@pytest.mark.asyncio
async def test_pgvector_query_includes_tenant_source_metadata_and_limit():
    conn = FakePgConn(
        [
            {
                "chunk_id": "c1",
                "tenant_id": "t-acme",
                "source_path": "docs/rag.md",
                "chunk_text": "text",
                "metadata": {"line_start": 1},
                "score": 0.9,
            }
        ]
    )
    store = rag.PgvectorStore(conn)

    hits = await store.query(
        rag.VectorQuery(
            tenant_id="t-acme",
            embedding=[1.0],
            limit=4,
            source_path="docs/rag.md",
            metadata_filter={"kind": "doc"},
        )
    )

    sql, values = conn.fetch_calls[0]
    assert "tenant_id = $1" in sql
    assert "source_path = $3" in sql
    assert "metadata @> $4::jsonb" in sql
    assert values == ("t-acme", "[1.0]", "docs/rag.md", '{"kind": "doc"}', 4)
    assert hits[0].chunk_id == "c1"


@pytest.mark.asyncio
async def test_pgvector_delete_requires_selector():
    with pytest.raises(ValueError, match="chunk_ids or source_path"):
        await rag.PgvectorStore(FakePgConn()).delete(tenant_id="t-acme")


@pytest.mark.asyncio
async def test_pgvector_delete_returns_status_row_count():
    conn = FakePgConn()
    deleted = await rag.PgvectorStore(conn).delete(
        tenant_id="t-acme", chunk_ids=["c1", "c2"]
    )

    assert deleted == 2
    assert "chunk_id = ANY($2::text[])" in conn.execute_calls[1][0]


@pytest.mark.asyncio
async def test_pgvector_list_by_tenant_validates_pagination():
    store = rag.PgvectorStore(FakePgConn())

    with pytest.raises(ValueError, match="limit must be positive"):
        await store.list_by_tenant("t-acme", limit=0)
    with pytest.raises(ValueError, match="offset must be non-negative"):
        await store.list_by_tenant("t-acme", offset=-1)


@pytest.mark.asyncio
async def test_pgvector_list_by_tenant_maps_rows():
    conn = FakePgConn(
        [
            {
                "chunk_id": "c1",
                "tenant_id": "t-acme",
                "source_path": "docs/rag.md",
                "chunk_text": "text",
                "embedding": [0.1],
                "metadata": '{"line_start": 1}',
            }
        ]
    )

    docs = await rag.PgvectorStore(conn).list_by_tenant("t-acme", limit=10, offset=5)

    sql, values = conn.fetch_calls[0]
    assert "ORDER BY source_path, chunk_id" in sql
    assert values == ("t-acme", 10, 5)
    assert docs[0].metadata == {"line_start": 1}


def test_safe_identifier_rejects_unsafe_table_names():
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        rag.PgvectorStore(FakePgConn(), table="embedding_chunks;drop")


def test_hit_to_knowledge_result_builds_citation_line_range():
    hit = rag.VectorHit(
        chunk_id="c1",
        tenant_id="t-acme",
        source_path="docs/rag.md",
        chunk_text="text",
        score=0.8,
        metadata={"line_start": "2", "line_end": 5},
    )

    result = runner_handlers._hit_to_knowledge_result(hit)

    assert result["citation"] == {
        "path": "docs/rag.md",
        "line_start": 2,
        "line_end": 5,
        "line_range": "L2-L5",
        "similarity_score": 0.8,
    }


def test_knowledge_source_path_normalises_inside_base(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_handlers, "BASE_DIR", tmp_path)
    _write(tmp_path / "docs" / "rag.md", "# RAG\n")

    assert runner_handlers._normalise_knowledge_source_path("docs/rag.md") == (
        "docs/rag.md"
    )


def test_knowledge_source_path_rejects_outside_base(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_handlers, "BASE_DIR", tmp_path)

    with pytest.raises(PermissionError):
        runner_handlers._normalise_knowledge_source_path(tmp_path.parent / "x.md")


@pytest.mark.asyncio
async def test_knowledge_retrieval_handler_queries_store_and_closes(monkeypatch):
    store = MemoryVectorStore()
    await store.upsert(
        [_doc("c1", "t-acme", "docs/rag.md", "pgvector retrieval", [1.0, 0.0, 0.0])]
    )
    closeable = FakeCloseable()
    monkeypatch.setattr(runner_handlers, "_build_embedder_from_env", lambda: TopicEmbedder())

    async def fake_build_store():
        return store, closeable

    monkeypatch.setattr(runner_handlers, "_build_store_from_env", fake_build_store)

    result = await runner_handlers.knowledge_retrieval_handler(
        {"query": "pgvector rag", "tenant_id": "t-acme", "top_k": 1}
    )

    assert result["query"] == "pgvector rag"
    assert result["tenant_id"] == "t-acme"
    assert result["results"][0]["chunk_id"] == "c1"
    assert closeable.closed


@pytest.mark.asyncio
async def test_knowledge_retrieval_handler_passes_source_and_metadata_filters(
    monkeypatch,
):
    store = MemoryVectorStore()
    await store.upsert(
        [
            _doc(
                "c1",
                "t-acme",
                "docs/rag.md",
                "pgvector retrieval",
                [1.0, 0.0, 0.0],
                kind="doc",
            )
        ]
    )
    monkeypatch.setattr(runner_handlers, "BASE_DIR", Path.cwd())
    monkeypatch.setattr(runner_handlers, "_build_embedder_from_env", lambda: TopicEmbedder())

    async def fake_build_store():
        return store, None

    monkeypatch.setattr(runner_handlers, "_build_store_from_env", fake_build_store)

    await runner_handlers.knowledge_retrieval_handler(
        {
            "query": "pgvector",
            "tenant_id": "t-acme",
            "source_path": "docs/rag.md",
            "metadata_filter": {"kind": "doc"},
        }
    )

    query = store.queries[0]
    assert query.source_path == "docs/rag.md"
    assert query.metadata_filter == {"kind": "doc"}


@pytest.mark.parametrize(
    "payload,match",
    [
        ({}, "query is required"),
        ({"query": "rag", "top_k": -1}, "top_k must be between 1 and 20"),
        ({"query": "rag", "top_k": 21}, "top_k must be between 1 and 20"),
        ({"query": "rag", "metadata_filter": "kind=doc"}, "metadata_filter"),
        ({"query": "rag", "tenant_id": " "}, "tenant_id is required"),
    ],
)
@pytest.mark.asyncio
async def test_knowledge_retrieval_handler_validates_payload(payload, match):
    with pytest.raises(ValueError, match=match):
        await runner_handlers.knowledge_retrieval_handler(payload)


@pytest.mark.asyncio
async def test_knowledge_retrieval_handler_uses_env_tenant(monkeypatch):
    store = MemoryVectorStore()
    monkeypatch.setenv("OMNISIGHT_RAG_TENANT_ID", "t-env")
    monkeypatch.setattr(runner_handlers, "_build_embedder_from_env", lambda: TopicEmbedder())

    async def fake_build_store():
        return store, None

    monkeypatch.setattr(runner_handlers, "_build_store_from_env", fake_build_store)

    result = await runner_handlers.knowledge_retrieval_handler({"query": "pgvector"})

    assert result["tenant_id"] == "t-env"
    assert store.queries[0].tenant_id == "t-env"


@pytest.mark.asyncio
async def test_knowledge_retrieval_handler_closes_store_after_query_error(monkeypatch):
    class RaisingStore(MemoryVectorStore):
        async def query(self, query: rag.VectorQuery) -> list[rag.VectorHit]:
            raise RuntimeError("store down")

    closeable = FakeCloseable()
    monkeypatch.setattr(runner_handlers, "_build_embedder_from_env", lambda: TopicEmbedder())

    async def fake_build_store():
        return RaisingStore(), closeable

    monkeypatch.setattr(runner_handlers, "_build_store_from_env", fake_build_store)

    with pytest.raises(RuntimeError, match="store down"):
        await runner_handlers.knowledge_retrieval_handler(
            {"query": "pgvector", "tenant_id": "t-acme"}
        )
    assert closeable.closed


class TestBPQ1VectorModels:

    def test_vector_document_normalizes_and_serializes(self):
        doc = _legacy_doc(chunk_id=" c-1 ", tenant_id=" t-acme ")

        assert doc.chunk_id == "c-1"
        assert doc.tenant_id == "t-acme"
        assert doc.to_dict()["embedding"] == [0.1, 0.2, 0.3]

    def test_vector_query_requires_tenant_and_limit(self):
        with pytest.raises(ValueError, match="tenant_id"):
            rag.VectorQuery(tenant_id=" ", embedding=[1.0])
        with pytest.raises(ValueError, match="limit"):
            rag.VectorQuery(tenant_id="t-acme", embedding=[1.0], limit=0)

    def test_vector_document_rejects_empty_embedding(self):
        with pytest.raises(ValueError, match="embedding"):
            _legacy_doc(embedding=[])


class _LegacyFakeConn:
    def __init__(self):
        self.executemany_calls: list[tuple[str, list[tuple[Any, ...]]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_rows: list[dict[str, Any]] = []
        self.execute_status = "DELETE 2"

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.executemany_calls.append((sql, rows))

    async def fetch(self, sql: str, *values: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, values))
        return self.fetch_rows

    async def execute(self, sql: str, *values: Any) -> str:
        self.execute_calls.append((sql, values))
        return self.execute_status


class TestBPQ1PgvectorStore:

    @pytest.mark.asyncio
    async def test_upsert_writes_pgvector_literal_and_metadata_json(self):
        conn = _LegacyFakeConn()
        store = rag.PgvectorStore(conn)

        await store.upsert([_legacy_doc()])

        sql, rows = conn.executemany_calls[0]
        assert conn.execute_calls[0] == (
            "SELECT set_config($1, $2, false)",
            (rag.PGVECTOR_TENANT_SETTING, "t-acme"),
        )
        assert conn.execute_calls[-1] == (
            "SELECT set_config($1, '', false)",
            (rag.PGVECTOR_TENANT_SETTING,),
        )
        assert "INSERT INTO embedding_chunks" in sql
        assert rows[0][0:5] == (
            "c-1",
            "t-acme",
            "docs/guide.md",
            "install pgvector",
            "[0.1,0.2,0.3]",
        )
        assert '"line_start": 10' in rows[0][5]

    @pytest.mark.asyncio
    async def test_upsert_rejects_cross_tenant_batch_before_pg_write(self):
        conn = _LegacyFakeConn()
        store = rag.PgvectorStore(conn)

        with pytest.raises(ValueError, match="exactly one tenant"):
            await store.upsert([
                _legacy_doc(chunk_id="c-a", tenant_id="t-acme"),
                _legacy_doc(chunk_id="c-b", tenant_id="t-beta"),
            ])

        assert conn.execute_calls == []
        assert conn.executemany_calls == []

    @pytest.mark.asyncio
    async def test_query_always_filters_by_tenant(self):
        conn = _LegacyFakeConn()
        conn.fetch_rows = [
            {
                "chunk_id": "c-1",
                "tenant_id": "t-acme",
                "source_path": "docs/guide.md",
                "chunk_text": "install pgvector",
                "metadata": {"line_start": 10},
                "score": 0.82,
            }
        ]
        store = rag.PgvectorStore(conn)

        hits = await store.query(
            rag.VectorQuery(
                tenant_id="t-acme",
                embedding=[0.1, 0.2, 0.3],
                source_path="docs/guide.md",
                metadata_filter={"kind": "markdown"},
            )
        )

        sql, values = conn.fetch_calls[0]
        assert conn.execute_calls[0] == (
            "SELECT set_config($1, $2, false)",
            (rag.PGVECTOR_TENANT_SETTING, "t-acme"),
        )
        assert conn.execute_calls[-1] == (
            "SELECT set_config($1, '', false)",
            (rag.PGVECTOR_TENANT_SETTING,),
        )
        assert "tenant_id = $1" in sql
        assert "metadata @>" in sql
        assert values[0] == "t-acme"
        assert hits[0].score == 0.82

    @pytest.mark.asyncio
    async def test_delete_requires_selector_and_returns_row_count(self):
        store = rag.PgvectorStore(_LegacyFakeConn())

        with pytest.raises(ValueError, match="chunk_ids or source_path"):
            await store.delete(tenant_id="t-acme")

        assert await store.delete(tenant_id="t-acme", chunk_ids=["c-1"]) == 2
        conn = store._db
        assert conn.execute_calls[0] == (
            "SELECT set_config($1, $2, false)",
            (rag.PGVECTOR_TENANT_SETTING, "t-acme"),
        )
        assert "DELETE FROM embedding_chunks WHERE tenant_id = $1" in (
            conn.execute_calls[1][0]
        )
        assert conn.execute_calls[-1] == (
            "SELECT set_config($1, '', false)",
            (rag.PGVECTOR_TENANT_SETTING,),
        )

    @pytest.mark.asyncio
    async def test_list_by_tenant_returns_documents(self):
        conn = _LegacyFakeConn()
        conn.fetch_rows = [
            {
                "chunk_id": "c-1",
                "tenant_id": "t-acme",
                "source_path": "docs/guide.md",
                "chunk_text": "install pgvector",
                "embedding": [0.1, 0.2, 0.3],
                "metadata": {"kind": "markdown"},
            }
        ]
        store = rag.PgvectorStore(conn)

        docs = await store.list_by_tenant("t-acme", source_path="docs/guide.md")

        sql, values = conn.fetch_calls[0]
        assert conn.execute_calls[0] == (
            "SELECT set_config($1, $2, false)",
            (rag.PGVECTOR_TENANT_SETTING, "t-acme"),
        )
        assert conn.execute_calls[-1] == (
            "SELECT set_config($1, '', false)",
            (rag.PGVECTOR_TENANT_SETTING,),
        )
        assert "tenant_id = $1" in sql
        assert values[:2] == ("t-acme", "docs/guide.md")
        assert docs[0].chunk_id == "c-1"

    @pytest.mark.asyncio
    async def test_pg_tenant_scope_resets_after_query_error(self):
        class FailingConn(_LegacyFakeConn):
            async def fetch(self, sql: str, *values: Any) -> list[dict[str, Any]]:
                self.fetch_calls.append((sql, values))
                raise RuntimeError("boom")

        conn = FailingConn()
        store = rag.PgvectorStore(conn)

        with pytest.raises(RuntimeError, match="boom"):
            await store.query(rag.VectorQuery(tenant_id="t-acme", embedding=[1.0]))

        assert conn.execute_calls == [
            (
                "SELECT set_config($1, $2, false)",
                (rag.PGVECTOR_TENANT_SETTING, "t-acme"),
            ),
            (
                "SELECT set_config($1, '', false)",
                (rag.PGVECTOR_TENANT_SETTING,),
            ),
        ]


class TestBPQ1QdrantStore:

    @pytest.mark.asyncio
    async def test_upsert_sends_points_with_tenant_payload(self, fake_http):
        fake_http.responses.append(httpx.Response(200, json={"result": {}}))
        store = rag.QdrantStore(collection="knowledge", api_key="q-key")

        await store.upsert([_legacy_doc()])

        call = fake_http.calls[0]
        assert call["method"] == "PUT"
        assert call["headers"]["api-key"] == "q-key"
        point = call["json"]["points"][0]
        assert point["id"] == "c-1"
        assert point["payload"]["tenant_id"] == "t-acme"

    @pytest.mark.asyncio
    async def test_query_includes_tenant_filter(self, fake_http):
        fake_http.responses.append(
            httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": "c-1",
                            "score": 0.9,
                            "payload": {
                                "tenant_id": "t-acme",
                                "source_path": "docs/guide.md",
                                "chunk_text": "install pgvector",
                                "metadata": {"kind": "markdown"},
                            },
                        }
                    ]
                },
            )
        )
        store = rag.QdrantStore(collection="knowledge")

        hits = await store.query(rag.VectorQuery(tenant_id="t-acme", embedding=[1.0]))

        body = fake_http.calls[0]["json"]
        assert body["filter"]["must"][0] == {
            "key": "tenant_id",
            "match": {"value": "t-acme"},
        }
        assert hits[0].tenant_id == "t-acme"

    @pytest.mark.asyncio
    async def test_delete_chunk_ids_remain_tenant_scoped(self, fake_http):
        fake_http.responses.append(
            httpx.Response(200, json={"result": {"operation_id": 123}})
        )
        store = rag.QdrantStore(collection="knowledge")

        deleted = await store.delete(tenant_id="t-acme", chunk_ids=["c-1"])

        assert deleted == 1
        must = fake_http.calls[0]["json"]["filter"]["must"]
        assert {"key": "tenant_id", "match": {"value": "t-acme"}} in must
        assert {"has_id": ["c-1"]} in must


class TestBPQ1ChromaStore:

    @pytest.mark.asyncio
    async def test_upsert_sends_chroma_batch(self, fake_http):
        fake_http.responses.append(httpx.Response(200, json={}))
        store = rag.ChromaStore(collection="knowledge")

        await store.upsert([_legacy_doc()])

        body = fake_http.calls[0]["json"]
        assert body["ids"] == ["c-1"]
        assert body["metadatas"][0]["tenant_id"] == "t-acme"

    @pytest.mark.asyncio
    async def test_query_uses_metadata_tenant_filter(self, fake_http):
        fake_http.responses.append(
            httpx.Response(
                200,
                json={
                    "ids": [["c-1"]],
                    "documents": [["install pgvector"]],
                    "metadatas": [[{"tenant_id": "t-acme", "source_path": "docs/guide.md"}]],
                    "distances": [[0.2]],
                },
            )
        )
        store = rag.ChromaStore(collection="knowledge")

        hits = await store.query(rag.VectorQuery(tenant_id="t-acme", embedding=[1.0]))

        assert fake_http.calls[0]["json"]["where"] == {"tenant_id": "t-acme"}
        assert hits[0].score == pytest.approx(0.8)


class TestBPQ1EmbeddingAdapters:

    @pytest.mark.asyncio
    async def test_openai_embedding_sorts_provider_rows(self, fake_http):
        fake_http.responses.append(
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.3, 0.4]},
                        {"index": 0, "embedding": [0.1, 0.2]},
                    ]
                },
            )
        )
        embedder = rag.OpenAIEmbedding(api_key="sk-test")

        vectors = await embedder.embed_texts(["a", "b"])

        assert vectors == [[0.1, 0.2], [0.3, 0.4]]
        assert fake_http.calls[0]["headers"]["Authorization"] == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_anthropic_embedding_uses_explicit_endpoint(self, fake_http):
        fake_http.responses.append(
            httpx.Response(200, json={"embeddings": [[0.1], [0.2]]})
        )
        embedder = rag.AnthropicEmbedding(api_key="sk-ant", model="embed")

        vectors = await embedder.embed_texts(["a", "b"])

        assert vectors == [[0.1], [0.2]]
        assert fake_http.calls[0]["url"].endswith("/embeddings")
        assert fake_http.calls[0]["headers"]["x-api-key"] == "sk-ant"

    @pytest.mark.asyncio
    async def test_google_embedding_uses_batch_embed_content(self, fake_http):
        fake_http.responses.append(
            httpx.Response(200, json={"embeddings": [{"values": [0.1, 0.2]}]})
        )
        embedder = rag.GoogleEmbedding(api_key="g-key", model="text-embedding-004")

        vectors = await embedder.embed_texts(["hello"])

        assert vectors == [[0.1, 0.2]]
        assert fake_http.calls[0]["params"] == {"key": "g-key"}
        assert fake_http.calls[0]["json"]["requests"][0]["content"]["parts"] == [
            {"text": "hello"}
        ]

    @pytest.mark.asyncio
    async def test_local_sentence_transformer_lazy_import(self, monkeypatch):
        module = types.ModuleType("sentence_transformers")

        class FakeSentenceTransformer:
            def __init__(self, model: str, **kwargs: Any):
                self.model = model
                self.kwargs = kwargs

            def encode(self, texts: list[str], convert_to_numpy: bool):
                assert convert_to_numpy is False
                return [[float(len(text))] for text in texts]

        module.SentenceTransformer = FakeSentenceTransformer
        monkeypatch.setitem(sys.modules, "sentence_transformers", module)
        embedder = rag.LocalSentenceTransformerEmbedding(model="local", device="cpu")

        vectors = await embedder.embed_texts(["aa", "bbbb"])

        assert vectors == [[2.0], [4.0]]


@pytest.mark.asyncio
async def test_pgvector_live_smoke_when_configured():
    dsn = os.environ.get("OMNISIGHT_RAG_PGVECTOR_TEST_DSN")
    if not dsn:
        pytest.skip("OMNISIGHT_RAG_PGVECTOR_TEST_DSN not configured")
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(dsn)
    table = "tmp_bp_q7_embedding_chunks"
    try:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"DROP TABLE IF EXISTS {table}")
            await conn.execute(
                f"""
                CREATE TEMP TABLE {table} (
                    chunk_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding vector(3) NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
                )
                """
            )
        except Exception as exc:  # pragma: no cover - live environment gate
            pytest.skip(f"pgvector unavailable: {exc}")
        store = rag.PgvectorStore(conn, table=table)
        await store.upsert(
            [
                _doc(
                    "live-rag",
                    "t-live",
                    "docs/rag.md",
                    "pgvector live retrieval",
                    [1.0, 0.0, 0.0],
                    line_start=1,
                    line_end=1,
                )
            ]
        )

        hits = await store.query(rag.VectorQuery("t-live", [1.0, 0.0, 0.0], limit=1))

        assert hits[0].chunk_id == "live-rag"
        assert hits[0].score == pytest.approx(1.0)
    finally:
        await conn.close()
