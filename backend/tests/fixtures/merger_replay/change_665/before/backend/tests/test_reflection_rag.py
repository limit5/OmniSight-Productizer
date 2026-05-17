# op-698-allow-conflict-marker: intentional replay fixture conflict markers
"""RPG.W6.1 -- reflection RAG unit tests (OP-141).

Mirrors the BP.Q vector-store tests with in-memory fakes: the contract under
test is that past success/fail summaries become tenant-scoped pgvector-ready
documents and retrieval stays filtered to reflection-summary metadata.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents import reflection_rag as rr
from backend.agents import rag


class FakeEmbedder:
    def __init__(self) -> None:
        self.text_batches: list[list[str]] = []
        self.queries: list[str] = []
        self.next_text_embeddings: list[list[float]] | None = None

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.text_batches.append(texts)
        if self.next_text_embeddings is not None:
            return self.next_text_embeddings
        return [[float(index + 1), float(len(text))] for index, text in enumerate(texts)]

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, float(len(text))]


class FakeStore:
    def __init__(self, hits: list[rag.VectorHit] | None = None) -> None:
        self.documents: list[rag.VectorDocument] = []
        self.queries: list[rag.VectorQuery] = []
        self.hits = hits or []

    async def upsert(self, documents: list[rag.VectorDocument]) -> None:
        self.documents.extend(documents)

    async def query(self, query: rag.VectorQuery) -> list[rag.VectorHit]:
        self.queries.append(query)
        return self.hits[: query.limit]

    async def delete(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str] | None = None,
        source_path: str | None = None,
    ) -> int:
        raise AssertionError("delete is not used by reflection_rag tests")

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        source_path: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[rag.VectorDocument]:
        raise AssertionError("list_by_tenant is not used by reflection_rag tests")


def _summary(**kwargs: Any) -> rr.ReflectionSummary:
    base = {
        "tenant_id": "t-acme",
        "ticket_key": "OP-141",
        "outcome": rr.REFLECTION_OUTCOME_FAILURE,
        "summary": "pytest failed until pgvector metadata filter was added",
        "failure_type": "test",
        "metadata": {"component": "RPG"},
    }
    base.update(kwargs)
    return rr.ReflectionSummary(**base)


def test_reflection_summary_normalizes_embedding_text_and_metadata():
    summary = _summary()

    assert summary.source_path == "reflection://OP-141/failure"
    assert summary.text_for_embedding() == (
        "ticket: OP-141\n"
        "outcome: failure\n"
        "failure_type: test\n"
        "summary: pytest failed until pgvector metadata filter was added"
    )
    assert summary.to_metadata() == {
        "component": "RPG",
        "kind": rr.REFLECTION_RAG_KIND,
        "ticket_key": "OP-141",
        "outcome": "failure",
        "failure_type": "test",
    }


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"tenant_id": " "}, "tenant_id is required"),
        ({"ticket_key": ""}, "ticket_key is required"),
        ({"summary": ""}, "summary is required"),
        ({"outcome": "blocked"}, "outcome must be one of"),
    ],
)
def test_reflection_summary_validates_required_fields(
    kwargs: dict[str, Any],
    match: str,
):
    with pytest.raises(ValueError, match=match):
        _summary(**kwargs)


@pytest.mark.asyncio
async def test_vectorize_reflection_summaries_embeds_and_upserts_documents():
    embedder = FakeEmbedder()
    store = FakeStore()
    success = _summary(
        ticket_key="OP-140",
        outcome=rr.REFLECTION_OUTCOME_SUCCESS,
        summary="lint and tests passed after reusing the existing adapter",
        failure_type="",
    )
    failure = _summary()

    written = await rr.vectorize_reflection_summaries(
        [success, failure],
        embedder=embedder,
        store=store,
    )

    assert written == 2
    assert len(embedder.text_batches) == 1
    assert "outcome: success" in embedder.text_batches[0][0]
    assert "outcome: failure" in embedder.text_batches[0][1]
    assert [doc.tenant_id for doc in store.documents] == ["t-acme", "t-acme"]
    assert store.documents[0].chunk_id.startswith(
        "reflection:t-acme:OP-140:success:"
    )
    assert store.documents[0].source_path == "reflection://OP-140/success"
    assert store.documents[0].chunk_text == (
        "lint and tests passed after reusing the existing adapter"
    )
    assert store.documents[0].metadata == {
        "kind": rr.REFLECTION_RAG_KIND,
        "ticket_key": "OP-140",
        "outcome": "success",
        "component": "RPG",
    }
    assert store.documents[1].metadata["failure_type"] == "test"


@pytest.mark.asyncio
async def test_vectorize_empty_batch_is_noop():
    embedder = FakeEmbedder()
    store = FakeStore()

    assert (
        await rr.vectorize_reflection_summaries([], embedder=embedder, store=store)
    ) == 0
    assert embedder.text_batches == []
    assert store.documents == []


@pytest.mark.asyncio
async def test_vectorize_rejects_mixed_tenant_batch():
    with pytest.raises(ValueError, match="exactly one tenant"):
        await rr.vectorize_reflection_summaries(
            [_summary(), _summary(tenant_id="t-other")],
            embedder=FakeEmbedder(),
            store=FakeStore(),
        )


@pytest.mark.asyncio
async def test_vectorize_rejects_embedding_count_mismatch():
    embedder = FakeEmbedder()
    embedder.next_text_embeddings = [[1.0]]

    with pytest.raises(ValueError, match="wrong number of embeddings"):
        await rr.vectorize_reflection_summaries(
            [_summary(ticket_key="OP-1"), _summary(ticket_key="OP-2")],
            embedder=embedder,
            store=FakeStore(),
        )


@pytest.mark.asyncio
async def test_retrieve_reflection_summaries_filters_kind_outcome_and_failure_type():
    store = FakeStore(
        [
            rag.VectorHit(
                chunk_id="c1",
                tenant_id="t-acme",
                source_path="reflection://OP-100/failure",
                chunk_text="rerun pytest after changing metadata filters",
                score=0.91,
                metadata={
                    "kind": rr.REFLECTION_RAG_KIND,
                    "ticket_key": "OP-100",
                    "outcome": "failure",
                    "failure_type": "test",
                },
            )
        ]
    )
    embedder = FakeEmbedder()

    hits = await rr.retrieve_reflection_summaries(
        tenant_id="t-acme",
        query_text="pytest metadata filter regression",
        embedder=embedder,
        store=store,
        top_k=3,
        outcome=rr.REFLECTION_OUTCOME_FAILURE,
        failure_type="test",
    )

    assert embedder.queries == ["pytest metadata filter regression"]
    query = store.queries[0]
    assert query.tenant_id == "t-acme"
    assert query.limit == 3
    assert query.metadata_filter == {
        "kind": rr.REFLECTION_RAG_KIND,
        "outcome": "failure",
        "failure_type": "test",
    }
    assert hits[0].to_dict() == {
        "ticket_key": "OP-100",
        "outcome": "failure",
        "summary": "rerun pytest after changing metadata filters",
        "failure_type": "test",
        "score": 0.91,
        "metadata": {
            "kind": rr.REFLECTION_RAG_KIND,
            "ticket_key": "OP-100",
            "outcome": "failure",
            "failure_type": "test",
        },
    }


@pytest.mark.asyncio
async def test_retrieve_reflection_summaries_defaults_to_top_five():
    store = FakeStore()
    embedder = FakeEmbedder()

    await rr.retrieve_reflection_summaries(
        tenant_id="t-acme",
        query_text="pytest metadata filter regression",
        embedder=embedder,
        store=store,
    )

    assert store.queries[0].limit == rr.DEFAULT_REFLECTION_TOP_K == 5


@pytest.mark.asyncio
async def test_retrieve_reflection_summaries_validates_query_inputs():
    with pytest.raises(ValueError, match="tenant_id is required"):
        await rr.retrieve_reflection_summaries(
            tenant_id=" ",
            query_text="x",
            embedder=FakeEmbedder(),
            store=FakeStore(),
        )
    with pytest.raises(ValueError, match="query_text is required"):
        await rr.retrieve_reflection_summaries(
            tenant_id="t-acme",
            query_text=" ",
            embedder=FakeEmbedder(),
            store=FakeStore(),
        )
    with pytest.raises(ValueError, match="top_k must be positive"):
        await rr.retrieve_reflection_summaries(
            tenant_id="t-acme",
            query_text="x",
            embedder=FakeEmbedder(),
            store=FakeStore(),
            top_k=0,
        )
    with pytest.raises(ValueError, match="outcome must be one of"):
        await rr.retrieve_reflection_summaries(
            tenant_id="t-acme",
            query_text="x",
            embedder=FakeEmbedder(),
            store=FakeStore(),
            outcome="blocked",  # type: ignore[arg-type]
        )


def test_pgvector_reflection_store_reuses_existing_pgvector_adapter():
    conn = object()
    store = rr.pgvector_reflection_store(conn)

    assert isinstance(store, rag.PgvectorStore)
    assert store._db is conn


<<<<<<< /tmp/tmpb1n45_tz/ours
def test_render_reflection_context_caps_injection_to_two_kibibytes():
    hits = [
        rr.ReflectionSearchHit(
            ticket_key=f"OP-{index}",
            outcome=rr.REFLECTION_OUTCOME_FAILURE,
            failure_type="test",
            summary=("pytest failed on a long assertion " * 20) + "tail",
            score=0.9 - (index / 100),
        )
        for index in range(10)
    ]

    block = rr.render_reflection_context(hits)

    assert "Reflection RAG context" in block
    assert len(block.encode("utf-8")) <= rr.DEFAULT_REFLECTION_INJECTION_MAX_BYTES
    assert block.encode("utf-8").decode("utf-8") == block


def test_render_reflection_context_empty_hits_returns_empty():
    assert rr.render_reflection_context([]) == ""


def test_render_reflection_context_validates_max_bytes():
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        rr.render_reflection_context([], max_bytes=0)
=======
def test_render_reflection_lesson_block_formats_hits_within_prompt_budget():
    hit = rr.ReflectionSearchHit(
        ticket_key="OP-100",
        outcome=rr.REFLECTION_OUTCOME_FAILURE,
        summary="rerun pytest after changing metadata filters",
        failure_type="test",
        score=0.91234,
    )

    block = rr.render_reflection_lesson_block((hit,), max_chars=2000)

    assert block.startswith(rr.REFLECTION_LESSON_PROMPT_HEADER)
    assert "OP-100 | outcome=failure | score=0.912 | failure_type=test" in block
    assert "Lesson: rerun pytest after changing metadata filters" in block
    assert len(block) <= 2000


def test_render_reflection_lesson_block_truncates_to_adr_budget():
    hit = rr.ReflectionSearchHit(
        ticket_key="OP-101",
        outcome=rr.REFLECTION_OUTCOME_SUCCESS,
        summary="x" * (rr.DEFAULT_REFLECTION_PROMPT_BUDGET * 2),
        score=0.5,
    )

    block = rr.render_reflection_lesson_block((hit,))

    assert len(block) <= rr.DEFAULT_REFLECTION_PROMPT_BUDGET
    assert block.endswith("...[truncated]")


def test_inject_reflection_lesson_block_appends_when_present():
    prompt = "base prompt\n"
    block = f"{rr.REFLECTION_LESSON_PROMPT_HEADER}\nlesson"

    injected = rr.inject_reflection_lesson_block(prompt, block)

    assert injected == f"base prompt\n\n{block}"
    assert rr.inject_reflection_lesson_block(prompt, "") == prompt


@pytest.mark.asyncio
async def test_build_reflection_lesson_injection_queries_top_k_and_renders_block():
    store = FakeStore(
        [
            rag.VectorHit(
                chunk_id="c1",
                tenant_id="t-acme",
                source_path="reflection://OP-100/failure",
                chunk_text="metadata filters fixed the reflection retrieval test",
                score=0.9,
                metadata={
                    "kind": rr.REFLECTION_RAG_KIND,
                    "ticket_key": "OP-100",
                    "outcome": rr.REFLECTION_OUTCOME_FAILURE,
                },
            ),
            rag.VectorHit(
                chunk_id="c2",
                tenant_id="t-acme",
                source_path="reflection://OP-99/success",
                chunk_text="reuse existing vector-store contracts",
                score=0.8,
                metadata={
                    "kind": rr.REFLECTION_RAG_KIND,
                    "ticket_key": "OP-99",
                    "outcome": rr.REFLECTION_OUTCOME_SUCCESS,
                },
            ),
        ]
    )
    embedder = FakeEmbedder()

    block = await rr.build_reflection_lesson_injection(
        tenant_id="t-acme",
        ticket_key="OP-142",
        ticket_summary="Pre-task hook",
        ticket_description="query top-K relevant past lessons",
        embedder=embedder,
        store=store,
        top_k=2,
    )

    assert "OP-100" in block
    assert "OP-99" in block
    assert embedder.queries == [
        "ticket: OP-142\n"
        "summary: Pre-task hook\n"
        "description: query top-K relevant past lessons"
    ]
    query = store.queries[0]
    assert query.limit == 2
    assert query.metadata_filter == {"kind": rr.REFLECTION_RAG_KIND}


@pytest.mark.asyncio
async def test_build_reflection_lesson_injection_degrades_when_retrieval_fails():
    class BrokenStore(FakeStore):
        async def query(self, query: rag.VectorQuery) -> list[rag.VectorHit]:
            raise RuntimeError("pgvector offline")

    block = await rr.build_reflection_lesson_injection(
        tenant_id="t-acme",
        ticket_key="OP-142",
        ticket_summary="Pre-task hook",
        ticket_description="query top-K relevant past lessons",
        embedder=FakeEmbedder(),
        store=BrokenStore(),
    )

    assert block == ""
>>>>>>> /tmp/tmpb1n45_tz/theirs
