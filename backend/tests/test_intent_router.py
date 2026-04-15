"""Phase 68-C — HTTP surface for intent_parser.

Exercises /parse and /clarify through the real FastAPI stack. LLM
is disabled (`use_llm: false`) so the heuristic parser alone drives
these — fast, deterministic, zero provider dependency.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_parse_returns_structured_spec(client):
    r = await client.post(
        "/api/v1/intent/parse",
        json={"text": "Build a Next.js SSG site on x86_64", "use_llm": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["framework"]["value"] == "nextjs"
    assert body["runtime_model"]["value"] == "ssg"
    assert body["target_arch"]["value"] == "x86_64"
    assert "conflicts" in body
    assert isinstance(body["conflicts"], list)


@pytest.mark.asyncio
async def test_parse_surfaces_conflict(client):
    r = await client.post(
        "/api/v1/intent/parse",
        json={
            "text": "Build a static Next.js site that reads from a local "
                    "SQLite at request time.",
            "use_llm": False,
        },
    )
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()["conflicts"]]
    assert "static_with_runtime_db" in ids


@pytest.mark.asyncio
async def test_clarify_applies_operator_choice(client):
    r = await client.post(
        "/api/v1/intent/parse",
        json={
            "text": "Build a static Next.js site reads sqlite at request time.",
            "use_llm": False,
        },
    )
    spec = r.json()
    assert any(c["id"] == "static_with_runtime_db" for c in spec["conflicts"])
    r2 = await client.post(
        "/api/v1/intent/clarify",
        json={
            "parsed": spec,
            "conflict_id": "static_with_runtime_db",
            "option_id": "ssr_runtime",
        },
    )
    assert r2.status_code == 200
    updated = r2.json()
    assert updated["runtime_model"]["value"] == "ssr"
    assert updated["runtime_model"]["confidence"] == 1.0
    assert not any(c["id"] == "static_with_runtime_db" for c in updated["conflicts"])


@pytest.mark.asyncio
async def test_clarify_unknown_ids_return_422(client):
    r = await client.post(
        "/api/v1/intent/parse",
        json={"text": "anything", "use_llm": False},
    )
    r2 = await client.post(
        "/api/v1/intent/clarify",
        json={
            "parsed": r.json(),
            "conflict_id": "does-not-exist",
            "option_id": "anything",
        },
    )
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_parse_empty_text_yields_all_unknown(client):
    """Empty input must not crash and must not trigger an LLM call
    (the router's fast path detects this and short-circuits)."""
    r = await client.post(
        "/api/v1/intent/parse",
        json={"text": "", "use_llm": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["framework"]["value"] == "unknown"
    assert body["framework"]["confidence"] == 0.0
