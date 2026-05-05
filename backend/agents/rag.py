"""BP.Q.1 -- Vector RAG storage and embedding adapter contracts.

This module mirrors the hosted-search adapter shape in ``backend.search``:
small provider-neutral dataclasses, Protocol/ABC contracts, and provider
adapters that lazy-touch external systems only when called. BP.Q.4 owns the
``embedding_chunks`` migration/RLS policy; this file keeps the runtime
contract tenant-scoped so later handlers cannot query across tenants by
accident.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable constants, dataclasses, functions, and adapter
classes only. It does not read/write mutable module-level caches or singletons;
store state lives in PG/Qdrant/Chroma and local embedding model state lives on
the adapter instance, so uvicorn workers do not share process-local truth.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import httpx


DEFAULT_PGVECTOR_TABLE = "embedding_chunks"
DEFAULT_QDRANT_API_BASE = "http://localhost:6333"
DEFAULT_CHROMA_API_BASE = "http://localhost:8000"


class RagError(Exception):
    """Base error for vector RAG adapters."""


class VectorStoreError(RagError):
    """Raised when a vector store rejects or fails an operation."""

    def __init__(self, message: str, *, provider: str = "", status: int = 0):
        super().__init__(message)
        self.provider = provider
        self.status = status


class EmbeddingError(RagError):
    """Raised when an embedding provider rejects or fails a request."""

    def __init__(self, message: str, *, provider: str = "", status: int = 0):
        super().__init__(message)
        self.provider = provider
        self.status = status


@dataclass
class VectorDocument:
    """Provider-neutral chunk to persist in a vector store."""

    chunk_id: str
    tenant_id: str
    source_path: str
    chunk_text: str
    embedding: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.chunk_id = self.chunk_id.strip()
        self.tenant_id = self.tenant_id.strip()
        self.source_path = self.source_path.strip()
        if not self.chunk_id:
            raise ValueError("chunk_id is required")
        if not self.tenant_id:
            raise ValueError("tenant_id is required")
        if not self.source_path:
            raise ValueError("source_path is required")
        if not self.chunk_text:
            raise ValueError("chunk_text is required")
        self.embedding = _coerce_embedding(self.embedding)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "tenant_id": self.tenant_id,
            "source_path": self.source_path,
            "chunk_text": self.chunk_text,
            "embedding": list(self.embedding),
            "metadata": dict(self.metadata),
        }


@dataclass
class VectorQuery:
    """Provider-neutral vector query scoped to one tenant."""

    tenant_id: str
    embedding: list[float]
    limit: int = 10
    source_path: str | None = None
    metadata_filter: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tenant_id = self.tenant_id.strip()
        if not self.tenant_id:
            raise ValueError("tenant_id is required")
        self.embedding = _coerce_embedding(self.embedding)
        if self.limit < 1:
            raise ValueError("limit must be positive")
        if self.source_path is not None:
            self.source_path = self.source_path.strip() or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "embedding": list(self.embedding),
            "limit": self.limit,
            "source_path": self.source_path,
            "metadata_filter": dict(self.metadata_filter),
        }


@dataclass
class VectorHit:
    """One tenant-scoped search hit returned by a vector store."""

    chunk_id: str
    tenant_id: str
    source_path: str
    chunk_text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "tenant_id": self.tenant_id,
            "source_path": self.source_path,
            "chunk_text": self.chunk_text,
            "score": self.score,
            "metadata": dict(self.metadata),
        }


class VectorStore(Protocol):
    """Tenant-scoped vector storage surface for BP.Q retrieval."""

    async def upsert(self, documents: list[VectorDocument]) -> None: ...
    async def query(self, query: VectorQuery) -> list[VectorHit]: ...
    async def delete(
        self, *, tenant_id: str, chunk_ids: list[str] | None = None,
        source_path: str | None = None
    ) -> int: ...
    async def list_by_tenant(
        self, tenant_id: str, *, source_path: str | None = None,
        limit: int = 100, offset: int = 0
    ) -> list[VectorDocument]: ...


class EmbeddingProvider(Protocol):
    """Provider-neutral embedding surface used by indexers and tools."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


class BaseEmbedding(ABC):
    """Common validation wrapper for embedding providers."""

    provider: ClassVar[str] = ""

    def __init__(self, *, model: str, timeout: float = 30.0):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        self.model = model
        self.timeout = timeout

    async def embed_query(self, text: str) -> list[float]:
        embeddings = await self.embed_texts([text])
        return embeddings[0]

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed one or more texts."""

    def _validate_texts(self, texts: list[str]) -> list[str]:
        clean = [text for text in (t.strip() for t in texts) if text]
        if not clean:
            raise ValueError("at least one text is required")
        return clean


class OpenAIEmbedding(BaseEmbedding):
    """OpenAI embeddings adapter using the public HTTP API."""

    provider = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "text-embedding-3-small",
        api_base: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
    ):
        super().__init__(model=model, timeout=timeout)
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        clean = self._validate_texts(texts)
        data = await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json_body={"model": self.model, "input": clean},
            timeout=self.timeout,
            error_cls=EmbeddingError,
        )
        rows = sorted(data.get("data", []), key=lambda row: row.get("index", 0))
        return [_coerce_embedding(row.get("embedding", [])) for row in rows]


class AnthropicEmbedding(BaseEmbedding):
    """Anthropic embeddings adapter.

    Anthropic deployments may front this behind a compatible embeddings
    endpoint. The adapter keeps that endpoint explicit instead of assuming it
    exists in every Anthropic account.
    """

    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str = "https://api.anthropic.com/v1",
        timeout: float = 30.0,
        anthropic_version: str = "2023-06-01",
    ):
        super().__init__(model=model, timeout=timeout)
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._anthropic_version = anthropic_version

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        clean = self._validate_texts(texts)
        data = await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/embeddings",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._anthropic_version,
            },
            json_body={"model": self.model, "input": clean},
            timeout=self.timeout,
            error_cls=EmbeddingError,
        )
        rows = data.get("data", data.get("embeddings", []))
        return [_coerce_embedding(_embedding_from_row(row)) for row in rows]


class GoogleEmbedding(BaseEmbedding):
    """Google Gemini embeddings adapter using ``:embedContent`` HTTP API."""

    provider = "google"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "text-embedding-004",
        api_base: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 30.0,
    ):
        super().__init__(model=model, timeout=timeout)
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        clean = self._validate_texts(texts)
        url = f"{self._api_base}/models/{self.model}:batchEmbedContents"
        requests = [
            {"model": f"models/{self.model}", "content": {"parts": [{"text": text}]}}
            for text in clean
        ]
        data = await _post_json(
            provider=self.provider,
            url=url,
            headers={},
            json_body={"requests": requests},
            timeout=self.timeout,
            error_cls=EmbeddingError,
            params={"key": self._api_key},
        )
        return [
            _coerce_embedding(row.get("values", []))
            for row in data.get("embeddings", [])
        ]


class LocalSentenceTransformerEmbedding(BaseEmbedding):
    """Air-gap embedding adapter backed by ``sentence-transformers``."""

    provider = "local-sentence-transformer"

    def __init__(
        self,
        *,
        model: str = "sentence-transformers/all-MiniLM-L6-v2",
        timeout: float = 30.0,
        device: str | None = None,
    ):
        super().__init__(model=model, timeout=timeout)
        self._device = device
        self._model: Any | None = None

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - optional dep path
                raise RuntimeError(
                    "sentence-transformers is required for "
                    "LocalSentenceTransformerEmbedding"
                ) from exc
            kwargs = {"device": self._device} if self._device else {}
            self._model = SentenceTransformer(self.model, **kwargs)
        return self._model

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        clean = self._validate_texts(texts)
        model = self._load_model()
        vectors = await asyncio.get_running_loop().run_in_executor(
            None, lambda: model.encode(clean, convert_to_numpy=False)
        )
        return [_coerce_embedding(vec) for vec in vectors]


class PgvectorStore:
    """PG/pgvector store for the BP.Q ``embedding_chunks`` table."""

    provider = "pgvector"

    def __init__(self, conn_or_pool: Any, *, table: str = DEFAULT_PGVECTOR_TABLE):
        self._db = conn_or_pool
        self._table = _safe_identifier(table)

    async def upsert(self, documents: list[VectorDocument]) -> None:
        if not documents:
            raise ValueError("at least one document is required")
        sql = f"""
            INSERT INTO {self._table}
                (chunk_id, tenant_id, source_path, chunk_text, embedding, metadata)
            VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
            ON CONFLICT (chunk_id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                source_path = EXCLUDED.source_path,
                chunk_text = EXCLUDED.chunk_text,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata
        """
        rows = [
            (
                doc.chunk_id,
                doc.tenant_id,
                doc.source_path,
                doc.chunk_text,
                _pgvector_literal(doc.embedding),
                json.dumps(doc.metadata, sort_keys=True),
            )
            for doc in documents
        ]
        async with _acquire(self._db) as conn:
            await conn.executemany(sql, rows)

    async def query(self, query: VectorQuery) -> list[VectorHit]:
        clauses = ["tenant_id = $1"]
        values: list[Any] = [query.tenant_id, _pgvector_literal(query.embedding)]
        if query.source_path:
            values.append(query.source_path)
            clauses.append(f"source_path = ${len(values)}")
        if query.metadata_filter:
            values.append(json.dumps(query.metadata_filter, sort_keys=True))
            clauses.append(f"metadata @> ${len(values)}::jsonb")
        values.append(query.limit)
        sql = f"""
            SELECT
                chunk_id, tenant_id, source_path, chunk_text, metadata,
                1 - (embedding <=> $2::vector) AS score
            FROM {self._table}
            WHERE {' AND '.join(clauses)}
            ORDER BY embedding <=> $2::vector
            LIMIT ${len(values)}
        """
        async with _acquire(self._db) as conn:
            rows = await conn.fetch(sql, *values)
        return [_hit_from_pg_row(row) for row in rows]

    async def delete(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        source_path: str | None = None,
    ) -> int:
        tenant_id = _required("tenant_id", tenant_id)
        ids = [cid.strip() for cid in (chunk_ids or []) if cid.strip()]
        if not ids and not source_path:
            raise ValueError("chunk_ids or source_path is required")
        clauses = ["tenant_id = $1"]
        values: list[Any] = [tenant_id]
        if ids:
            values.append(ids)
            clauses.append(f"chunk_id = ANY(${len(values)}::text[])")
        if source_path:
            values.append(source_path.strip())
            clauses.append(f"source_path = ${len(values)}")
        sql = f"DELETE FROM {self._table} WHERE {' AND '.join(clauses)}"
        async with _acquire(self._db) as conn:
            status = await conn.execute(sql, *values)
        return _rows_from_status(status)

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        source_path: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[VectorDocument]:
        tenant_id = _required("tenant_id", tenant_id)
        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        values: list[Any] = [tenant_id]
        clauses = ["tenant_id = $1"]
        if source_path:
            values.append(source_path.strip())
            clauses.append(f"source_path = ${len(values)}")
        values.extend([limit, offset])
        sql = f"""
            SELECT chunk_id, tenant_id, source_path, chunk_text, embedding, metadata
            FROM {self._table}
            WHERE {' AND '.join(clauses)}
            ORDER BY source_path, chunk_id
            LIMIT ${len(values) - 1} OFFSET ${len(values)}
        """
        async with _acquire(self._db) as conn:
            rows = await conn.fetch(sql, *values)
        return [_document_from_pg_row(row) for row in rows]


class QdrantStore:
    """Qdrant HTTP adapter using payload tenant filters."""

    provider = "qdrant"

    def __init__(
        self,
        *,
        collection: str,
        api_key: str | None = None,
        api_base: str = DEFAULT_QDRANT_API_BASE,
        timeout: float = 30.0,
    ):
        self.collection = collection.strip()
        if not self.collection:
            raise ValueError("collection is required")
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        return headers

    async def upsert(self, documents: list[VectorDocument]) -> None:
        if not documents:
            raise ValueError("at least one document is required")
        points = [
            {
                "id": doc.chunk_id,
                "vector": doc.embedding,
                "payload": _payload_from_document(doc),
            }
            for doc in documents
        ]
        await _put_json(
            provider=self.provider,
            url=f"{self._api_base}/collections/{self.collection}/points",
            headers=self._headers(),
            json_body={"points": points},
            timeout=self._timeout,
            error_cls=VectorStoreError,
        )

    async def query(self, query: VectorQuery) -> list[VectorHit]:
        body: dict[str, Any] = {
            "vector": query.embedding,
            "limit": query.limit,
            "with_payload": True,
            "filter": _qdrant_filter(query),
        }
        data = await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/collections/{self.collection}/points/search",
            headers=self._headers(),
            json_body=body,
            timeout=self._timeout,
            error_cls=VectorStoreError,
        )
        return [_hit_from_qdrant(row) for row in data.get("result", [])]

    async def delete(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        source_path: str | None = None,
    ) -> int:
        tenant_id = _required("tenant_id", tenant_id)
        ids = [cid.strip() for cid in (chunk_ids or []) if cid.strip()]
        if not ids and not source_path:
            raise ValueError("chunk_ids or source_path is required")
        selector: dict[str, Any]
        if ids:
            selector = {
                "filter": _qdrant_filter_for_tenant(
                    tenant_id, source_path, chunk_ids=ids
                )
            }
        else:
            selector = {"filter": _qdrant_filter_for_tenant(tenant_id, source_path)}
        data = await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/collections/{self.collection}/points/delete",
            headers=self._headers(),
            json_body=selector,
            timeout=self._timeout,
            error_cls=VectorStoreError,
        )
        return int(data.get("result", {}).get("operation_id") is not None)

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        source_path: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[VectorDocument]:
        del offset  # Qdrant scroll offset is provider token; BP.Q.1 keeps int API.
        tenant_id = _required("tenant_id", tenant_id)
        if limit < 1:
            raise ValueError("limit must be positive")
        data = await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/collections/{self.collection}/points/scroll",
            headers=self._headers(),
            json_body={
                "limit": limit,
                "with_payload": True,
                "with_vector": True,
                "filter": _qdrant_filter_for_tenant(tenant_id, source_path),
            },
            timeout=self._timeout,
            error_cls=VectorStoreError,
        )
        return [_document_from_qdrant(row) for row in data.get("result", {}).get("points", [])]


class ChromaStore:
    """Chroma HTTP adapter using metadata tenant filters."""

    provider = "chroma"

    def __init__(
        self,
        *,
        collection: str,
        api_base: str = DEFAULT_CHROMA_API_BASE,
        timeout: float = 30.0,
    ):
        self.collection = collection.strip()
        if not self.collection:
            raise ValueError("collection is required")
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    async def upsert(self, documents: list[VectorDocument]) -> None:
        if not documents:
            raise ValueError("at least one document is required")
        await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/api/v1/collections/{self.collection}/upsert",
            headers={"Content-Type": "application/json"},
            json_body={
                "ids": [doc.chunk_id for doc in documents],
                "embeddings": [doc.embedding for doc in documents],
                "documents": [doc.chunk_text for doc in documents],
                "metadatas": [_payload_from_document(doc) for doc in documents],
            },
            timeout=self._timeout,
            error_cls=VectorStoreError,
        )

    async def query(self, query: VectorQuery) -> list[VectorHit]:
        data = await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/api/v1/collections/{self.collection}/query",
            headers={"Content-Type": "application/json"},
            json_body={
                "query_embeddings": [query.embedding],
                "n_results": query.limit,
                "where": _chroma_where(query.tenant_id, query.source_path, query.metadata_filter),
                "include": ["documents", "metadatas", "distances"],
            },
            timeout=self._timeout,
            error_cls=VectorStoreError,
        )
        return _hits_from_chroma(data, query.tenant_id)

    async def delete(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        source_path: str | None = None,
    ) -> int:
        tenant_id = _required("tenant_id", tenant_id)
        ids = [cid.strip() for cid in (chunk_ids or []) if cid.strip()]
        if not ids and not source_path:
            raise ValueError("chunk_ids or source_path is required")
        body: dict[str, Any] = {"where": _chroma_where(tenant_id, source_path)}
        if ids:
            body["ids"] = ids
        await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/api/v1/collections/{self.collection}/delete",
            headers={"Content-Type": "application/json"},
            json_body=body,
            timeout=self._timeout,
            error_cls=VectorStoreError,
        )
        return len(ids)

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        source_path: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[VectorDocument]:
        tenant_id = _required("tenant_id", tenant_id)
        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        data = await _post_json(
            provider=self.provider,
            url=f"{self._api_base}/api/v1/collections/{self.collection}/get",
            headers={"Content-Type": "application/json"},
            json_body={
                "where": _chroma_where(tenant_id, source_path),
                "limit": limit,
                "offset": offset,
                "include": ["documents", "metadatas", "embeddings"],
            },
            timeout=self._timeout,
            error_cls=VectorStoreError,
        )
        return _documents_from_chroma(data, tenant_id)


def _coerce_embedding(raw: Any) -> list[float]:
    values = list(raw)
    if not values:
        raise ValueError("embedding is required")
    return [float(value) for value in values]


def _required(name: str, value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{name} is required")
    return clean


def _safe_identifier(value: str) -> str:
    clean = value.strip()
    if not clean or not all(ch.isalnum() or ch == "_" for ch in clean):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return clean


def _pgvector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in embedding) + "]"


def _rows_from_status(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


class _ConnectionContext:
    def __init__(self, db: Any):
        self._db = db
        self._ctx: Any | None = None
        self._conn: Any | None = None

    async def __aenter__(self) -> Any:
        acquire = getattr(self._db, "acquire", None)
        if acquire is None:
            return self._db
        self._ctx = acquire()
        self._conn = await self._ctx.__aenter__()
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._ctx is not None:
            await self._ctx.__aexit__(exc_type, exc, tb)


def _acquire(db: Any) -> _ConnectionContext:
    return _ConnectionContext(db)


async def _post_json(
    *,
    provider: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: float,
    error_cls: type[EmbeddingError] | type[VectorStoreError],
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=json_body, params=params)
    return _json_or_error(resp, provider=provider, error_cls=error_cls)


async def _put_json(
    *,
    provider: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: float,
    error_cls: type[EmbeddingError] | type[VectorStoreError],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.put(url, headers=headers, json=json_body)
    return _json_or_error(resp, provider=provider, error_cls=error_cls)


def _json_or_error(
    resp: httpx.Response,
    *,
    provider: str,
    error_cls: type[EmbeddingError] | type[VectorStoreError],
) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise error_cls(
            f"{provider} request failed with status {resp.status_code}",
            provider=provider,
            status=resp.status_code,
        )
    return resp.json() if resp.content else {}


def _embedding_from_row(row: Any) -> Any:
    if isinstance(row, dict):
        return row.get("embedding", row.get("values", row))
    return row


def _payload_from_document(doc: VectorDocument) -> dict[str, Any]:
    return {
        "tenant_id": doc.tenant_id,
        "source_path": doc.source_path,
        "chunk_text": doc.chunk_text,
        "metadata": dict(doc.metadata),
    }


def _qdrant_filter(query: VectorQuery) -> dict[str, Any]:
    return _qdrant_filter_for_tenant(
        query.tenant_id, query.source_path, query.metadata_filter
    )


def _qdrant_filter_for_tenant(
    tenant_id: str,
    source_path: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
    chunk_ids: list[str] | None = None,
) -> dict[str, Any]:
    must = [{"key": "tenant_id", "match": {"value": tenant_id}}]
    if chunk_ids:
        must.append({"has_id": chunk_ids})
    if source_path:
        must.append({"key": "source_path", "match": {"value": source_path}})
    for key, value in (metadata_filter or {}).items():
        must.append({"key": f"metadata.{key}", "match": {"value": value}})
    return {"must": must}


def _chroma_where(
    tenant_id: str,
    source_path: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = [{"tenant_id": tenant_id}]
    if source_path:
        clauses.append({"source_path": source_path})
    for key, value in (metadata_filter or {}).items():
        clauses.append({f"metadata.{key}": value})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def _hit_from_pg_row(row: Any) -> VectorHit:
    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return VectorHit(
        chunk_id=row["chunk_id"],
        tenant_id=row["tenant_id"],
        source_path=row["source_path"],
        chunk_text=row["chunk_text"],
        score=float(row["score"]),
        metadata=dict(metadata),
        raw=dict(row),
    )


def _document_from_pg_row(row: Any) -> VectorDocument:
    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return VectorDocument(
        chunk_id=row["chunk_id"],
        tenant_id=row["tenant_id"],
        source_path=row["source_path"],
        chunk_text=row["chunk_text"],
        embedding=_coerce_embedding(row["embedding"]),
        metadata=dict(metadata),
    )


def _hit_from_qdrant(row: dict[str, Any]) -> VectorHit:
    payload = row.get("payload", {})
    return VectorHit(
        chunk_id=str(row.get("id", "")),
        tenant_id=str(payload.get("tenant_id", "")),
        source_path=str(payload.get("source_path", "")),
        chunk_text=str(payload.get("chunk_text", "")),
        score=float(row.get("score", 0.0)),
        metadata=dict(payload.get("metadata") or {}),
        raw=row,
    )


def _document_from_qdrant(row: dict[str, Any]) -> VectorDocument:
    payload = row.get("payload", {})
    return VectorDocument(
        chunk_id=str(row.get("id", "")),
        tenant_id=str(payload.get("tenant_id", "")),
        source_path=str(payload.get("source_path", "")),
        chunk_text=str(payload.get("chunk_text", "")),
        embedding=_coerce_embedding(row.get("vector", [])),
        metadata=dict(payload.get("metadata") or {}),
    )


def _hits_from_chroma(data: dict[str, Any], tenant_id: str) -> list[VectorHit]:
    ids = (data.get("ids") or [[]])[0]
    documents = (data.get("documents") or [[]])[0]
    metadatas = (data.get("metadatas") or [[]])[0]
    distances = (data.get("distances") or [[]])[0]
    hits = []
    for idx, chunk_id in enumerate(ids):
        metadata = dict(metadatas[idx] or {})
        distance = float(distances[idx]) if idx < len(distances) else 0.0
        hits.append(
            VectorHit(
                chunk_id=str(chunk_id),
                tenant_id=str(metadata.get("tenant_id", tenant_id)),
                source_path=str(metadata.get("source_path", "")),
                chunk_text=str(documents[idx] if idx < len(documents) else ""),
                score=1 - distance,
                metadata=dict(metadata.get("metadata") or {}),
                raw=metadata,
            )
        )
    return hits


def _documents_from_chroma(data: dict[str, Any], tenant_id: str) -> list[VectorDocument]:
    ids = data.get("ids", [])
    documents = data.get("documents", [])
    metadatas = data.get("metadatas", [])
    embeddings = data.get("embeddings", [])
    out = []
    for idx, chunk_id in enumerate(ids):
        metadata = dict(metadatas[idx] or {})
        out.append(
            VectorDocument(
                chunk_id=str(chunk_id),
                tenant_id=str(metadata.get("tenant_id", tenant_id)),
                source_path=str(metadata.get("source_path", "")),
                chunk_text=str(documents[idx] if idx < len(documents) else ""),
                embedding=_coerce_embedding(embeddings[idx]),
                metadata=dict(metadata.get("metadata") or {}),
            )
        )
    return out


__all__ = [
    "AnthropicEmbedding",
    "ChromaStore",
    "DEFAULT_CHROMA_API_BASE",
    "DEFAULT_PGVECTOR_TABLE",
    "DEFAULT_QDRANT_API_BASE",
    "EmbeddingError",
    "EmbeddingProvider",
    "GoogleEmbedding",
    "LocalSentenceTransformerEmbedding",
    "OpenAIEmbedding",
    "PgvectorStore",
    "QdrantStore",
    "RagError",
    "VectorDocument",
    "VectorHit",
    "VectorQuery",
    "VectorStore",
    "VectorStoreError",
]
