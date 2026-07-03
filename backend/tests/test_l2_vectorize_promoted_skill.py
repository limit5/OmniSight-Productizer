"""BP.M / ADR-0008 L2 — vectorize_promoted_skill feeds the dim memory.

Uses a fake embedder + a capturing store (no external embedder, no DB) to prove
the promote→L2 wire builds the right DistilledSkillMemoryEntry and upserts it
with the producer-scope agent_id + source_skill_draft_id back-reference, so a
later retrieve_distilled_skills can scope on it.
"""
from __future__ import annotations

import asyncio

from backend.agents import rag
from backend.agents import skill_memory as sm


class _FakeEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 1.0] for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 1.0]


class _CapturingStore:
    def __init__(self) -> None:
        self.docs: list[rag.VectorDocument] = []

    async def upsert(self, documents: list[rag.VectorDocument]) -> None:
        self.docs.extend(documents)


def test_vectorize_promoted_skill_builds_scoped_entry(monkeypatch) -> None:
    store = _CapturingStore()
    monkeypatch.setattr(sm, "pgvector_skill_memory_store", lambda pool, table=sm.DEFAULT_PGVECTOR_TABLE: store)

    written = asyncio.run(
        sm.vectorize_promoted_skill(
            object(),  # pool unused (store is faked)
            tenant_id="t-default",
            skill_id="auto-architect-guild-smoke",
            summary="Draft body: cross-compile with the platform toolchain.",
            source_skill_draft_id="ads-abc123",
            embedder=_FakeEmbedder(),
        )
    )

    assert written == 1
    assert len(store.docs) == 1
    doc = store.docs[0]
    assert doc.tenant_id == "t-default"
    meta = doc.metadata
    assert meta["kind"] == sm.DISTILLED_SKILL_RAG_KIND
    # producer-scope default so retrieval can tag guild-level distilled skills
    assert meta["agent_id"] == sm.DISTILLED_SKILL_PRODUCER_SCOPE == "architect_guild"
    assert meta["skill_id"] == "auto-architect-guild-smoke"
    assert meta["source_skill_draft_id"] == "ads-abc123"
    assert "cross-compile" in doc.chunk_text


def test_vectorize_promoted_skill_default_embedder_is_env(monkeypatch) -> None:
    # When no embedder is passed it resolves the shared env factory; a build
    # failure (no provider configured) propagates so the caller can degrade.
    store = _CapturingStore()
    monkeypatch.setattr(sm, "pgvector_skill_memory_store", lambda pool, table=sm.DEFAULT_PGVECTOR_TABLE: store)
    import backend.agents.rag_indexer as ri

    def _boom() -> object:
        raise RuntimeError("no embedder configured")

    monkeypatch.setattr(ri, "build_embedder_from_env", _boom)
    try:
        asyncio.run(
            sm.vectorize_promoted_skill(
                object(), tenant_id="t", skill_id="s", summary="x",
            )
        )
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "a missing embedder must propagate for the caller to catch"
