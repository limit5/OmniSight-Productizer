"""RPG.W5.3 -- latency-budget tests for Layer 1 and Layer 2 retrieval."""

from __future__ import annotations

from time import perf_counter
from typing import Any

import pytest

from backend.agents import rag
from backend.agents import skill_memory as sm
from backend.agents.character_card import (
    CharacterCardCreate,
    InMemoryCharacterCardStore,
)


LAYER_1_RETRIEVAL_BUDGET_SECONDS = 0.050
LAYER_2_RETRIEVAL_BUDGET_SECONDS = 0.300


class FakeEmbedder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[float(index + 1), float(len(text))] for index, text in enumerate(texts)]

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, float(len(text))]


class FilteringStore:
    def __init__(self, hits: list[rag.VectorHit]) -> None:
        self.hits = hits
        self.queries: list[rag.VectorQuery] = []

    async def upsert(self, documents: list[rag.VectorDocument]) -> None:
        raise AssertionError("upsert is not used by latency retrieval tests")

    async def query(self, query: rag.VectorQuery) -> list[rag.VectorHit]:
        self.queries.append(query)
        matching = [
            hit
            for hit in self.hits
            if hit.tenant_id == query.tenant_id
            and _metadata_contains(hit.metadata, query.metadata_filter)
        ]
        return matching[: query.limit]

    async def delete(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        source_path: str | None = None,
    ) -> int:
        raise AssertionError("delete is not used by latency retrieval tests")

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        source_path: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[rag.VectorDocument]:
        raise AssertionError("list_by_tenant is not used by latency retrieval tests")


def _metadata_contains(
    metadata: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    return all(metadata.get(key) == value for key, value in expected.items())


@pytest.mark.asyncio
async def test_layer_1_character_card_retrieval_stays_under_50ms():
    """ADR-0008 Layer 1 is a tiny stat-sheet row keyed by ``agent_id``."""

    store = InMemoryCharacterCardStore()
    for index in range(500):
        await store.create_card(
            CharacterCardCreate(
                agent_id=f"api-anthropic-{index:03d}",
                agent_class="api-anthropic",
                level=(index % 5) + 1,
                xp=index,
            )
        )

    start = perf_counter()
    card = await store.get_card("api-anthropic-499")
    elapsed = perf_counter() - start

    assert card is not None
    assert card.agent_id == "api-anthropic-499"
    assert elapsed < LAYER_1_RETRIEVAL_BUDGET_SECONDS


@pytest.mark.asyncio
async def test_layer_2_distilled_skill_retrieval_stays_under_300ms():
    """RPG.W5 Layer 2 retrieval is scoped by tenant, kind, agent, and skill."""

    store = FilteringStore(
        [
            rag.VectorHit(
                chunk_id=f"distilled-skill:t-acme:agent-{index % 8}:skill-{index % 6}:{index}",
                tenant_id="t-acme",
                source_path=f"distilled-skill://agent-{index % 8}/skill-{index % 6}",
                chunk_text=f"summary {index} for metadata-filtered skill retrieval",
                score=1.0 - (index / 10_000),
                metadata={
                    "kind": sm.DISTILLED_SKILL_RAG_KIND,
                    "agent_id": f"agent-{index % 8}",
                    "skill_id": f"skill-{index % 6}",
                },
            )
            for index in range(1_200)
        ]
    )
    embedder = FakeEmbedder()

    start = perf_counter()
    hits = await sm.retrieve_distilled_skills(
        tenant_id="t-acme",
        query_text="metadata-filtered skill retrieval",
        embedder=embedder,
        store=store,
        agent_id="agent-7",
        skill_id="skill-1",
        top_k=5,
    )
    elapsed = perf_counter() - start

    assert embedder.queries == ["metadata-filtered skill retrieval"]
    assert store.queries[0].metadata_filter == {
        "kind": sm.DISTILLED_SKILL_RAG_KIND,
        "agent_id": "agent-7",
        "skill_id": "skill-1",
    }
    assert len(hits) == 5
    assert {hit.agent_id for hit in hits} == {"agent-7"}
    assert {hit.skill_id for hit in hits} == {"skill-1"}
    assert elapsed < LAYER_2_RETRIEVAL_BUDGET_SECONDS
