"""RPG.W5.1 -- BP.M dim memory scoped by ``(agent_id, skill_id)``.

ADR-0008 §"Memory hierarchy" pins Layer 2 (distilled skills) on
*BP.M dim memory tagged with ``(agent_id, skill_id)``*. The
``auto_distilled_skills`` review queue from BP.M.1 already holds the
markdown body; this module wraps each distilled summary as a tenant-scoped
:class:`backend.agents.rag.VectorDocument` so the existing pgvector
``embedding_chunks`` storage can serve scoped top-K retrieval keyed by
``agent_id``, ``skill_id``, or both.

The contract under test is intentionally narrow:

* ``vectorize_distilled_skills`` -- embed and upsert one tenant's
  distilled-skill batch with ``kind`` / ``agent_id`` / ``skill_id``
  metadata that the pgvector ``metadata @>`` filter can pin against.
* ``retrieve_distilled_skills`` -- embed the query and apply an
  ``agent_id`` and/or ``skill_id`` metadata filter so callers can
  retrieve one agent's skill library, every agent's take on a single
  skill, or the intersection. ``kind`` is always pinned so other vector
  payloads in the same tenant table (BP.Q content RAG, reflection
  summaries from W6) cannot leak into a skill retrieval.

The L1 stat-sheet retrieval (``character_card`` row lookup) is the
indexed-PG path and lives outside this module; this module owns Layer 2
only. W5.3 owns the latency budget tests; W5.2 owns the trigger that
auto-distils a ≤200-token summary on lessons_learned write.

Module-global state audit (SOP Step 1)
--------------------------------------
Only immutable constants, dataclasses, and pure helpers live at module
scope. Vector state is persisted through the supplied
:class:`backend.agents.rag.VectorStore`; embedding state stays on the
caller-provided :class:`backend.agents.rag.EmbeddingProvider`. The
module does not introduce a process-local cache, so uvicorn workers do
not share mutable truth here.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
``vectorize_distilled_skills`` writes to pgvector via the existing
:class:`backend.agents.rag.PgvectorStore.upsert` path inside the
caller's transaction. ``retrieve_distilled_skills`` reads through the
same store; readers observe the row only after the upsert commit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.agents.rag import (
    DEFAULT_PGVECTOR_TABLE,
    EmbeddingProvider,
    PgvectorStore,
    VectorDocument,
    VectorHit,
    VectorQuery,
    VectorStore,
)


DISTILLED_SKILL_RAG_KIND = "distilled_skill_summary"
DEFAULT_DISTILLED_SKILL_TOP_K = 5
DISTILLED_SKILL_SOURCE_PREFIX = "distilled-skill://"


@dataclass(frozen=True)
class DistilledSkillMemoryEntry:
    """One BP.M dim-memory entry suitable for tenant-scoped vector storage.

    ``agent_id`` and ``skill_id`` are persisted into ``metadata`` so the
    pgvector ``metadata @>`` filter can scope retrieval. ``summary`` is
    the ≤200-token distilled body (W5.2 owns enforcing the budget); the
    field is the only retrievable payload returned in
    :class:`DistilledSkillSearchHit`.

    ``source_skill_draft_id`` is the originating
    ``auto_distilled_skills.id`` (BP.M.1) when the summary was distilled
    from a review-queue row. It is optional so callers can also tag
    skills that were promoted directly from another source (W5.2 calls
    this path from ``lessons_learned`` writes).
    """

    tenant_id: str
    agent_id: str
    skill_id: str
    summary: str
    source_skill_draft_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _required("tenant_id", self.tenant_id))
        object.__setattr__(self, "agent_id", _required("agent_id", self.agent_id))
        object.__setattr__(self, "skill_id", _required("skill_id", self.skill_id))
        object.__setattr__(self, "summary", _required("summary", self.summary))
        if self.source_skill_draft_id is not None:
            cleaned = self.source_skill_draft_id.strip() or None
            object.__setattr__(self, "source_skill_draft_id", cleaned)

    @property
    def source_path(self) -> str:
        return (
            f"{DISTILLED_SKILL_SOURCE_PREFIX}"
            f"{self.agent_id}/{self.skill_id}"
        )

    def text_for_embedding(self) -> str:
        bits = [
            f"agent_id: {self.agent_id}",
            f"skill_id: {self.skill_id}",
            f"summary: {self.summary.strip()}",
        ]
        return "\n".join(bits)

    def to_metadata(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata.update(
            {
                "kind": DISTILLED_SKILL_RAG_KIND,
                "agent_id": self.agent_id,
                "skill_id": self.skill_id,
            }
        )
        if self.source_skill_draft_id:
            metadata["source_skill_draft_id"] = self.source_skill_draft_id
        return metadata


@dataclass(frozen=True)
class DistilledSkillSearchHit:
    """One retrieved BP.M dim-memory entry."""

    agent_id: str
    skill_id: str
    summary: str
    score: float
    source_skill_draft_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: VectorHit | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "skill_id": self.skill_id,
            "summary": self.summary,
            "source_skill_draft_id": self.source_skill_draft_id,
            "score": self.score,
            "metadata": dict(self.metadata),
        }


def pgvector_skill_memory_store(
    conn_or_pool: Any,
    *,
    table: str = DEFAULT_PGVECTOR_TABLE,
) -> PgvectorStore:
    """Return the pgvector-backed store used by BP.M dim memory.

    Reuses the BP.Q ``embedding_chunks`` table -- this module does not
    introduce a new schema. Per-row scoping is enforced by ``tenant_id``
    + ``kind`` metadata filter so other vector payloads in the same
    table cannot leak across kinds.
    """

    return PgvectorStore(conn_or_pool, table=table)


async def vectorize_distilled_skills(
    entries: Iterable[DistilledSkillMemoryEntry],
    *,
    embedder: EmbeddingProvider,
    store: VectorStore,
) -> int:
    """Embed and upsert a tenant-scoped batch of distilled skills.

    Returns the number of summaries written. Empty input is a no-op so
    callers can pass optional batches without special casing. Mixed
    tenant batches are rejected to keep the pgvector tenant-scope
    invariant from W6 (and the BP.Q RLS policy upstream).
    """

    batch = list(entries)
    if not batch:
        return 0
    tenant_ids = {item.tenant_id for item in batch}
    if len(tenant_ids) != 1:
        raise ValueError("entries must belong to exactly one tenant")

    texts = [item.text_for_embedding() for item in batch]
    embeddings = await embedder.embed_texts(texts)
    if len(embeddings) != len(batch):
        raise ValueError("embedder returned the wrong number of embeddings")

    documents = [
        VectorDocument(
            chunk_id=_chunk_id(entry),
            tenant_id=entry.tenant_id,
            source_path=entry.source_path,
            chunk_text=entry.summary.strip(),
            embedding=embedding,
            metadata=entry.to_metadata(),
        )
        for entry, embedding in zip(batch, embeddings)
    ]
    await store.upsert(documents)
    return len(documents)


async def retrieve_distilled_skills(
    *,
    tenant_id: str,
    query_text: str,
    embedder: EmbeddingProvider,
    store: VectorStore,
    agent_id: str | None = None,
    skill_id: str | None = None,
    top_k: int = DEFAULT_DISTILLED_SKILL_TOP_K,
) -> tuple[DistilledSkillSearchHit, ...]:
    """Retrieve tenant-scoped distilled skills, scoped by id metadata.

    ``agent_id`` and ``skill_id`` are optional and additive: omitting
    both returns every distilled skill in the tenant; supplying either
    restricts retrieval to that scope; supplying both restricts to the
    intersection. ``kind`` is always pinned so other RAG payloads in the
    shared ``embedding_chunks`` table cannot leak into a skill query.
    """

    tenant_id = _required("tenant_id", tenant_id)
    query_text = _required("query_text", query_text)
    if top_k < 1:
        raise ValueError("top_k must be positive")
    metadata_filter: dict[str, Any] = {"kind": DISTILLED_SKILL_RAG_KIND}
    if agent_id is not None:
        metadata_filter["agent_id"] = _required("agent_id", agent_id)
    if skill_id is not None:
        metadata_filter["skill_id"] = _required("skill_id", skill_id)

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


def _chunk_id(entry: DistilledSkillMemoryEntry) -> str:
    if entry.source_skill_draft_id:
        identity = entry.source_skill_draft_id
    else:
        identity = hashlib.sha256(
            entry.summary.strip().encode("utf-8"),
        ).hexdigest()[:16]
    return (
        f"distilled-skill:{entry.tenant_id}:"
        f"{entry.agent_id}:{entry.skill_id}:{identity}"
    )


def _search_hit_from_vector(hit: VectorHit) -> DistilledSkillSearchHit:
    metadata = dict(hit.metadata)
    return DistilledSkillSearchHit(
        agent_id=str(metadata.get("agent_id", "")),
        skill_id=str(metadata.get("skill_id", "")),
        summary=hit.chunk_text,
        score=hit.score,
        source_skill_draft_id=str(metadata.get("source_skill_draft_id", "")),
        metadata=metadata,
        raw=hit,
    )


def _required(name: str, value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{name} is required")
    return clean


__all__ = [
    "DEFAULT_DISTILLED_SKILL_TOP_K",
    "DISTILLED_SKILL_RAG_KIND",
    "DISTILLED_SKILL_SOURCE_PREFIX",
    "DistilledSkillMemoryEntry",
    "DistilledSkillSearchHit",
    "pgvector_skill_memory_store",
    "retrieve_distilled_skills",
    "vectorize_distilled_skills",
]
