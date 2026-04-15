"""Phase 68-D — decision memory flow.

Verify:
  * Recording a clarification choice creates an episodic_memory row
    with the right signature + solution payload.
  * Lookup finds the row on a subsequent parse of a similar prompt.
  * annotate_conflicts_with_priors attaches `prior_choice` when the
    history is there, omits it when it isn't.
  * Signatures scope per-conflict — picking ssr for conflict A does
    not leak into a future conflict B lookup.
"""

from __future__ import annotations

import json

import pytest

from backend import intent_memory as _imem


@pytest.mark.asyncio
async def test_record_creates_episodic_row(client):
    """The `client` fixture initialises the DB; tap it for its
    side-effects then go direct."""
    mid = await _imem.record_clarification_choice(
        raw_text="Next.js static site with local SQLite runtime query",
        conflict_id="static_with_runtime_db",
        option_id="ssr_runtime",
        operator_email="op@example.com",
    )
    assert mid is not None
    from backend import db
    row = await db.get_episodic_memory(mid)
    assert row is not None
    assert row["error_signature"].startswith("spec-conflict:static_with_runtime_db:")
    payload = json.loads(row["solution"])
    assert payload["conflict_id"] == "static_with_runtime_db"
    assert payload["option_id"] == "ssr_runtime"
    assert payload["operator"] == "op@example.com"


@pytest.mark.asyncio
async def test_lookup_finds_prior_choice(client):
    raw = "Next.js static site with local SQLite runtime query"
    await _imem.record_clarification_choice(
        raw_text=raw, conflict_id="static_with_runtime_db",
        option_id="ssr_runtime",
    )
    prior = await _imem.lookup_prior_choice(
        raw_text=raw, conflict_id="static_with_runtime_db",
    )
    assert prior is not None
    assert prior.option_id == "ssr_runtime"
    assert prior.quality >= 0.5


@pytest.mark.asyncio
async def test_lookup_scoped_per_conflict(client):
    """A prior choice for conflict A must NOT surface when looking
    up conflict B — the signature prefix keeps them separated."""
    raw = "Shared prompt prefix for scoping test"
    await _imem.record_clarification_choice(
        raw_text=raw, conflict_id="static_with_runtime_db",
        option_id="ssr_runtime",
    )
    prior = await _imem.lookup_prior_choice(
        raw_text=raw, conflict_id="embedded_to_cloud_mismatch",
    )
    assert prior is None


@pytest.mark.asyncio
async def test_lookup_returns_none_when_no_history(client):
    prior = await _imem.lookup_prior_choice(
        raw_text="fresh prompt no history ever",
        conflict_id="static_with_runtime_db",
    )
    assert prior is None


@pytest.mark.asyncio
async def test_annotate_conflicts_attaches_prior_choice(client):
    raw = "Next.js SSG with runtime DB query"
    await _imem.record_clarification_choice(
        raw_text=raw, conflict_id="static_with_runtime_db",
        option_id="isr_hybrid",
    )
    conflicts = [{
        "id": "static_with_runtime_db",
        "message": "x",
        "fields": [],
        "options": [
            {"id": "ssg_build_time", "label": "A"},
            {"id": "ssr_runtime", "label": "B"},
            {"id": "isr_hybrid", "label": "C"},
        ],
        "severity": "routine",
    }]
    out = await _imem.annotate_conflicts_with_priors(raw, conflicts)
    assert out[0].get("prior_choice")
    assert out[0]["prior_choice"]["option_id"] == "isr_hybrid"


@pytest.mark.asyncio
async def test_clarify_endpoint_records_to_l3(client):
    """End-to-end: POST /intent/clarify writes a memory row that a
    subsequent /intent/parse surfaces as prior_choice."""
    raw = "static Next.js site reads sqlite at request time"
    # Round 1 — parse, see conflict, clarify.
    r = await client.post(
        "/api/v1/intent/parse",
        json={"text": raw, "use_llm": False},
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

    # Round 2 — parse the same prompt again, expect the prior hint.
    r3 = await client.post(
        "/api/v1/intent/parse",
        json={"text": raw, "use_llm": False},
    )
    # Second parse — runtime_model still ssg in the heuristic, so
    # the conflict fires again, but now its prior_choice field is set.
    spec3 = r3.json()
    conflict = next(
        (c for c in spec3["conflicts"] if c["id"] == "static_with_runtime_db"),
        None,
    )
    assert conflict is not None
    assert conflict.get("prior_choice")
    assert conflict["prior_choice"]["option_id"] == "ssr_runtime"
