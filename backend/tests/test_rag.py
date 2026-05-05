"""BP.Q.1 -- Vector RAG adapter contract tests."""

from __future__ import annotations

import sys
import types
from typing import Any

import httpx
import pytest

from backend.agents import rag


def _doc(**kw: Any) -> rag.VectorDocument:
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


class TestVectorModels:

    def test_vector_document_normalizes_and_serializes(self):
        doc = _doc(chunk_id=" c-1 ", tenant_id=" t-acme ")

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
            _doc(embedding=[])


class _FakeConn:
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


class TestPgvectorStore:

    @pytest.mark.asyncio
    async def test_upsert_writes_pgvector_literal_and_metadata_json(self):
        conn = _FakeConn()
        store = rag.PgvectorStore(conn)

        await store.upsert([_doc()])

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
        conn = _FakeConn()
        store = rag.PgvectorStore(conn)

        with pytest.raises(ValueError, match="exactly one tenant"):
            await store.upsert([
                _doc(chunk_id="c-a", tenant_id="t-acme"),
                _doc(chunk_id="c-b", tenant_id="t-beta"),
            ])

        assert conn.execute_calls == []
        assert conn.executemany_calls == []

    @pytest.mark.asyncio
    async def test_query_always_filters_by_tenant(self):
        conn = _FakeConn()
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
        store = rag.PgvectorStore(_FakeConn())

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
        conn = _FakeConn()
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
        class FailingConn(_FakeConn):
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


class TestQdrantStore:

    @pytest.mark.asyncio
    async def test_upsert_sends_points_with_tenant_payload(self, fake_http):
        fake_http.responses.append(httpx.Response(200, json={"result": {}}))
        store = rag.QdrantStore(collection="knowledge", api_key="q-key")

        await store.upsert([_doc()])

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


class TestChromaStore:

    @pytest.mark.asyncio
    async def test_upsert_sends_chroma_batch(self, fake_http):
        fake_http.responses.append(httpx.Response(200, json={}))
        store = rag.ChromaStore(collection="knowledge")

        await store.upsert([_doc()])

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


class TestEmbeddingAdapters:

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
