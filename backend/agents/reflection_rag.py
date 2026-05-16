"""RPG.W6.1 -- vector RAG for past reflection success/fail summaries.

Layer 3 reflection RAG stores compact summaries of prior runner outcomes as
tenant-scoped vector documents.  The module deliberately reuses the BP.Q
``VectorStore`` / ``EmbeddingProvider`` contracts so pgvector remains the
storage implementation and this layer does not own schema changes.

Module-global state audit
-------------------------
Only immutable constants, dataclasses, and pure helper functions live at module
scope.  Vector state is persisted through the supplied ``VectorStore`` and
embedding state stays on the caller-provided ``EmbeddingProvider`` instance, so
workers do not share mutable process-local truth.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from backend.agents.rag import (
    DEFAULT_PGVECTOR_TABLE,
    EmbeddingProvider,
    PgvectorStore,
    VectorDocument,
    VectorHit,
    VectorQuery,
    VectorStore,
)


REFLECTION_RAG_KIND = "reflection_summary"
REFLECTION_OUTCOME_SUCCESS = "success"
REFLECTION_OUTCOME_FAILURE = "failure"
VALID_REFLECTION_OUTCOMES = frozenset(
    {REFLECTION_OUTCOME_SUCCESS, REFLECTION_OUTCOME_FAILURE}
)
DEFAULT_REFLECTION_TOP_K = 5
DEFAULT_REFLECTION_INJECTION_MAX_BYTES = 2048
REFLECTION_SOURCE_PREFIX = "reflection://"

ReflectionOutcome = Literal["success", "failure"]


@dataclass(frozen=True)
class ReflectionSummary:
    """Compact past outcome suitable for vector retrieval."""

    tenant_id: str
    ticket_key: str
    outcome: ReflectionOutcome
    summary: str
    failure_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required("tenant_id", self.tenant_id)
        _required("ticket_key", self.ticket_key)
        _required("summary", self.summary)
        if self.outcome not in VALID_REFLECTION_OUTCOMES:
            raise ValueError(
                f"outcome must be one of {sorted(VALID_REFLECTION_OUTCOMES)}"
            )

    @property
    def source_path(self) -> str:
        return f"{REFLECTION_SOURCE_PREFIX}{self.ticket_key.strip()}/{self.outcome}"

    def text_for_embedding(self) -> str:
        bits = [
            f"ticket: {self.ticket_key.strip()}",
            f"outcome: {self.outcome}",
        ]
        failure_type = self.failure_type.strip()
        if failure_type:
            bits.append(f"failure_type: {failure_type}")
        bits.append(f"summary: {self.summary.strip()}")
        return "\n".join(bits)

    def to_metadata(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata.update(
            {
                "kind": REFLECTION_RAG_KIND,
                "ticket_key": self.ticket_key.strip(),
                "outcome": self.outcome,
            }
        )
        failure_type = self.failure_type.strip()
        if failure_type:
            metadata["failure_type"] = failure_type
        return metadata


@dataclass(frozen=True)
class ReflectionSearchHit:
    """One retrieved prior reflection summary."""

    ticket_key: str
    outcome: str
    summary: str
    score: float
    failure_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: VectorHit | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_key": self.ticket_key,
            "outcome": self.outcome,
            "summary": self.summary,
            "failure_type": self.failure_type,
            "score": self.score,
            "metadata": dict(self.metadata),
        }


def pgvector_reflection_store(
    conn_or_pool: Any,
    *,
    table: str = DEFAULT_PGVECTOR_TABLE,
) -> PgvectorStore:
    """Return the pgvector-backed store used by reflection RAG."""

    return PgvectorStore(conn_or_pool, table=table)


async def vectorize_reflection_summaries(
    summaries: Iterable[ReflectionSummary],
    *,
    embedder: EmbeddingProvider,
    store: VectorStore,
) -> int:
    """Embed and upsert prior success/fail summaries.

    Returns the number of summaries written.  Empty input is a no-op so callers
    can pass optional historical batches without special casing.
    """

    batch = list(summaries)
    if not batch:
        return 0
    tenant_ids = {item.tenant_id.strip() for item in batch}
    if len(tenant_ids) != 1:
        raise ValueError("summaries must belong to exactly one tenant")

    texts = [item.text_for_embedding() for item in batch]
    embeddings = await embedder.embed_texts(texts)
    if len(embeddings) != len(batch):
        raise ValueError("embedder returned the wrong number of embeddings")

    documents = [
        VectorDocument(
            chunk_id=_chunk_id(summary),
            tenant_id=summary.tenant_id,
            source_path=summary.source_path,
            chunk_text=summary.summary.strip(),
            embedding=embedding,
            metadata=summary.to_metadata(),
        )
        for summary, embedding in zip(batch, embeddings)
    ]
    await store.upsert(documents)
    return len(documents)


async def retrieve_reflection_summaries(
    *,
    tenant_id: str,
    query_text: str,
    embedder: EmbeddingProvider,
    store: VectorStore,
    top_k: int = DEFAULT_REFLECTION_TOP_K,
    outcome: ReflectionOutcome | None = None,
    failure_type: str | None = None,
) -> tuple[ReflectionSearchHit, ...]:
    """Retrieve tenant-scoped prior reflection summaries by semantic match."""

    tenant_id = _required("tenant_id", tenant_id)
    query_text = _required("query_text", query_text)
    if top_k < 1:
        raise ValueError("top_k must be positive")
    metadata_filter: dict[str, Any] = {"kind": REFLECTION_RAG_KIND}
    if outcome is not None:
        if outcome not in VALID_REFLECTION_OUTCOMES:
            raise ValueError(
                f"outcome must be one of {sorted(VALID_REFLECTION_OUTCOMES)}"
            )
        metadata_filter["outcome"] = outcome
    if failure_type:
        metadata_filter["failure_type"] = failure_type.strip()

    embedding = await embedder.embed_query(query_text)
    hits = await store.query(
        VectorQuery(
            tenant_id=tenant_id,
            embedding=embedding,
            limit=top_k,
            metadata_filter=metadata_filter,
        )
    )
    return tuple(_search_hit_from_vector(hit) for hit in hits)


def render_reflection_context(
    hits: Iterable[ReflectionSearchHit],
    *,
    max_bytes: int = DEFAULT_REFLECTION_INJECTION_MAX_BYTES,
) -> str:
    """Render retrieved reflections as a bounded task prompt block.

    Returns an empty string when no hits are supplied.  The final UTF-8 encoded
    block is capped to ``max_bytes`` so reflection RAG cannot bloat each task's
    injected context.
    """

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")

    batch = list(hits)
    if not batch:
        return ""

    lines = [
        "# Reflection RAG context",
        "",
        "Relevant prior success/failure summaries:",
    ]
    for hit in batch:
        prefix = f"- {hit.ticket_key} outcome={hit.outcome}"
        if hit.failure_type:
            prefix += f" failure_type={hit.failure_type}"
        lines.append(f"{prefix} score={hit.score:.3f}")
        lines.append(f"  summary: {hit.summary.strip()}")
    lines.extend(
        [
            "",
            "Use these as prior examples only; verify the current task with its "
            "own tests before signing off.",
        ]
    )
    return _truncate_utf8("\n".join(lines), max_bytes)


def _chunk_id(summary: ReflectionSummary) -> str:
    digest = hashlib.sha256(summary.text_for_embedding().encode("utf-8")).hexdigest()
    return (
        f"reflection:{summary.tenant_id.strip()}:"
        f"{summary.ticket_key.strip()}:{summary.outcome}:{digest[:16]}"
    )


def _search_hit_from_vector(hit: VectorHit) -> ReflectionSearchHit:
    metadata = dict(hit.metadata)
    return ReflectionSearchHit(
        ticket_key=str(metadata.get("ticket_key", "")),
        outcome=str(metadata.get("outcome", "")),
        summary=hit.chunk_text,
        failure_type=str(metadata.get("failure_type", "")),
        score=hit.score,
        metadata=metadata,
        raw=hit,
    )


def _required(name: str, value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{name} is required")
    return clean


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore").rstrip()


__all__ = [
    "DEFAULT_REFLECTION_INJECTION_MAX_BYTES",
    "DEFAULT_REFLECTION_TOP_K",
    "REFLECTION_OUTCOME_FAILURE",
    "REFLECTION_OUTCOME_SUCCESS",
    "REFLECTION_RAG_KIND",
    "REFLECTION_SOURCE_PREFIX",
    "ReflectionOutcome",
    "ReflectionSearchHit",
    "ReflectionSummary",
    "VALID_REFLECTION_OUTCOMES",
    "pgvector_reflection_store",
    "render_reflection_context",
    "retrieve_reflection_summaries",
    "vectorize_reflection_summaries",
]
