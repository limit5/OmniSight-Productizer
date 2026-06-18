"""OP-2238 -- BI0 transcript-ingest API payload-correctness contract.

Exercises ``backend/routers/transcripts.py`` end-to-end against a real
asyncpg pool (``pg_test_pool`` from conftest). Skipped cleanly when
``OMNI_TEST_PG_URL`` is unset -- mirrors the pattern used by
``test_catalog_api.py`` so CI environments without a test PG still
collect this module.

Covers every scenario the ticket's "Exercised" line calls out:

  * duplicate (same seq, same final-state)             -> deduped
  * out-of-order arrival                               -> read ordered
  * partial-then-final on same seq                     -> superseded
  * final-then-partial on same seq                     -> rejected
  * reconnect-replay of the same batch                 -> idempotent
  * cross-tenant meeting_id                            -> 404 (not 403)
  * malformed body                                     -> 422
  * Idempotency-Key whole-batch retry                  -> same result

Plus a module-level smoke that the router + models import without PG,
so a fresh checkout never hits a collection-time ImportError.
"""
from __future__ import annotations

import os
import secrets
import uuid

import pytest


# ━━ PG availability gate ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool -- requires OMNI_TEST_PG_URL.",
)


# ━━ Smoke test (no PG) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_op2238_module_imports() -> None:
    """The router + Pydantic models import cleanly; no missing names."""
    from backend.routers import transcripts as r
    from backend.models import (
        MeetingEnvelope,
        OpenMeetingRequest,
        TranscriptIngestRejection,
        TranscriptIngestRequest,
        TranscriptIngestResult,
        TranscriptSegmentIn,
        TranscriptSegmentOut,
    )

    assert hasattr(r, "router")
    assert hasattr(r, "ingest_segments")
    assert hasattr(r, "open_meeting")
    assert hasattr(r, "list_segments")
    assert hasattr(r, "get_meeting_envelope")

    # Pydantic round-trip: rejection record reason vocabulary is stable.
    rej = TranscriptIngestRejection(segment_seq=5, reason="final_superseded")
    assert rej.model_dump()["reason"] == "final_superseded"

    body = TranscriptIngestRequest(
        session_id="s",
        segments=[
            TranscriptSegmentIn(
                segment_seq=0, text="hi", is_final=False, source="asr",
            )
        ],
    )
    assert body.segments[0].segment_seq == 0
    assert MeetingEnvelope(id="mtg-x", tenant_id="t-x").segment_count == 0
    assert OpenMeetingRequest(title="optional").title == "optional"
    assert TranscriptIngestResult().accepted == 0
    assert TranscriptSegmentOut(
        id="seg-x", meeting_id="mtg-x", session_id="s",
        segment_seq=0, text="hi", is_final=False, source="asr",
    ).text == "hi"


def test_router_registered_in_main() -> None:
    """The transcripts router is wired into backend/main.py.

    Grep the source rather than importing the live app -- a fresh
    checkout without the full backend dep tree (psycopg2 etc.) still
    catches a missed include_router line."""
    from pathlib import Path

    main_src = (
        Path(__file__).resolve().parents[1] / "main.py"
    ).read_text()
    assert "from backend.routers import transcripts" in main_src
    assert "_transcripts_router.router" in main_src


# ━━ PG-live fixtures ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def tx_client(pg_test_pool, monkeypatch):
    """AsyncClient against the FastAPI app, with pool lifecycle owned by
    pg_test_pool. Mirrors the bs24_client pattern from test_catalog_api.
    """
    from backend.main import app
    from backend import bootstrap as _boot
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    _boot._gate_cache_reset()


def _override_user(role: str, tenant_id: str):
    """Inject a fake authenticated user with given role + tenant."""
    from backend import auth as _au

    fake = _au.User(
        id=f"u-bi0-{role}-{secrets.token_hex(4)}",
        email=f"{role}@bi0.test",
        name=f"BI0 {role}",
        role=role,
        enabled=True,
        tenant_id=tenant_id,
    )

    async def _fake() -> _au.User:
        return fake

    return _fake


def _new_tenant_id() -> str:
    return f"t-bi0-{uuid.uuid4().hex[:10]}"


def _new_meeting_id() -> str:
    return f"mtg-{uuid.uuid4().hex[:12]}"


async def _purge(pool, tenant_id: str, meeting_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM transcript_segments "
            "WHERE tenant_id = $1 AND meeting_id = $2",
            tenant_id, meeting_id,
        )
        await conn.execute(
            "DELETE FROM meetings WHERE tenant_id = $1 AND id = $2",
            tenant_id, meeting_id,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE tenant_id = $1 "
            "AND entity_kind IN ('meeting', 'transcript_segment')",
            tenant_id,
        )


def _override(app, role: str, tenant_id: str):
    """Install + return a teardown callable for an auth override."""
    from backend import auth as _au

    fn = _override_user(role, tenant_id)
    app.dependency_overrides[_au.current_user] = fn

    def _teardown() -> None:
        app.dependency_overrides.pop(_au.current_user, None)

    return _teardown


# ━━ Contract tests ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_ingest_duplicate_segment_is_deduped(tx_client, pg_test_pool):
    """Re-sending the same finalised seg in the same session: deduped."""
    from backend.main import app

    tid, mid = _new_tenant_id(), _new_meeting_id()
    teardown = _override(app, "operator", tid)
    try:
        body = {
            "session_id": "sess-1",
            "segments": [
                {"segment_seq": 0, "text": "hello", "is_final": True,
                 "source": "asr"},
            ],
        }
        r1 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments", json=body,
        )
        assert r1.status_code == 201, r1.text
        assert r1.json()["accepted"] == 1
        r2 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments", json=body,
        )
        assert r2.status_code == 201, r2.text
        result = r2.json()
        assert result["accepted"] == 0
        assert result["deduped"] == 1
        assert result["rejected"] == []
    finally:
        teardown()
        await _purge(pg_test_pool, tid, mid)


@_requires_pg
async def test_out_of_order_arrivals_read_in_seq_order(tx_client, pg_test_pool):
    """Segments posted seq=2 then seq=0 then seq=1; GET returns in
    ascending segment_seq order."""
    from backend.main import app

    tid, mid = _new_tenant_id(), _new_meeting_id()
    teardown = _override(app, "operator", tid)
    try:
        for seq, text in ((2, "third"), (0, "first"), (1, "second")):
            r = await tx_client.post(
                f"/api/v1/meetings/{mid}/segments",
                json={
                    "session_id": "sess-1",
                    "segments": [{
                        "segment_seq": seq, "text": text,
                        "is_final": True, "source": "asr",
                    }],
                },
            )
            assert r.status_code == 201, r.text
        r = await tx_client.get(f"/api/v1/meetings/{mid}/segments")
        assert r.status_code == 200
        seqs = [s["segment_seq"] for s in r.json()]
        assert seqs == [0, 1, 2]
        texts = [s["text"] for s in r.json()]
        assert texts == ["first", "second", "third"]
    finally:
        teardown()
        await _purge(pg_test_pool, tid, mid)


@_requires_pg
async def test_partial_then_final_supersedes(tx_client, pg_test_pool):
    """Partial seg at seq=0 then final at seq=0: superseded counter
    increments, GET shows the final text."""
    from backend.main import app

    tid, mid = _new_tenant_id(), _new_meeting_id()
    teardown = _override(app, "operator", tid)
    try:
        r1 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={
                "session_id": "sess-1",
                "segments": [{
                    "segment_seq": 0, "text": "partial",
                    "is_final": False, "source": "asr",
                }],
            },
        )
        assert r1.status_code == 201, r1.text
        r2 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={
                "session_id": "sess-1",
                "segments": [{
                    "segment_seq": 0, "text": "FINAL TEXT",
                    "is_final": True, "source": "asr",
                }],
            },
        )
        assert r2.status_code == 201, r2.text
        result = r2.json()
        assert result["superseded"] == 1
        assert result["accepted"] == 0
        r = await tx_client.get(f"/api/v1/meetings/{mid}/segments")
        segs = r.json()
        assert len(segs) == 1
        assert segs[0]["text"] == "FINAL TEXT"
        assert segs[0]["is_final"] is True
    finally:
        teardown()
        await _purge(pg_test_pool, tid, mid)


@_requires_pg
async def test_final_then_partial_rejected(tx_client, pg_test_pool):
    """A partial arriving after the seq is already final is rejected with
    ``final_superseded`` -- we never downgrade."""
    from backend.main import app

    tid, mid = _new_tenant_id(), _new_meeting_id()
    teardown = _override(app, "operator", tid)
    try:
        r1 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={
                "session_id": "sess-1",
                "segments": [{
                    "segment_seq": 0, "text": "FINAL",
                    "is_final": True, "source": "asr",
                }],
            },
        )
        assert r1.status_code == 201, r1.text
        r2 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={
                "session_id": "sess-1",
                "segments": [{
                    "segment_seq": 0, "text": "later partial",
                    "is_final": False, "source": "asr",
                }],
            },
        )
        assert r2.status_code == 201, r2.text
        result = r2.json()
        assert result["accepted"] == 0
        assert result["superseded"] == 0
        assert len(result["rejected"]) == 1
        rej = result["rejected"][0]
        assert rej["segment_seq"] == 0
        assert rej["reason"] == "final_superseded"
        # GET still reflects the stored final.
        r = await tx_client.get(f"/api/v1/meetings/{mid}/segments")
        segs = r.json()
        assert len(segs) == 1
        assert segs[0]["text"] == "FINAL"
    finally:
        teardown()
        await _purge(pg_test_pool, tid, mid)


@_requires_pg
async def test_reconnect_replay_is_idempotent(tx_client, pg_test_pool):
    """Replaying the entire batch of segments (same session_id, same
    segment_seqs) yields zero accepted + same total stored rows."""
    from backend.main import app

    tid, mid = _new_tenant_id(), _new_meeting_id()
    teardown = _override(app, "operator", tid)
    try:
        body = {
            "session_id": "sess-replay",
            "segments": [
                {"segment_seq": 0, "text": "a", "is_final": True,
                 "source": "asr"},
                {"segment_seq": 1, "text": "b", "is_final": True,
                 "source": "asr"},
                {"segment_seq": 2, "text": "c", "is_final": True,
                 "source": "asr"},
            ],
        }
        r1 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments", json=body,
        )
        assert r1.json()["accepted"] == 3
        # full replay -- e.g. after reconnect
        r2 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments", json=body,
        )
        assert r2.status_code == 201
        result = r2.json()
        assert result["accepted"] == 0
        assert result["deduped"] == 3
        # Read-back: exactly 3 rows total, no duplicates.
        r = await tx_client.get(f"/api/v1/meetings/{mid}/segments")
        assert len(r.json()) == 3
    finally:
        teardown()
        await _purge(pg_test_pool, tid, mid)


@_requires_pg
async def test_cross_tenant_meeting_id_returns_404(tx_client, pg_test_pool):
    """A meeting opened by tenant A is invisible to tenant B -- the GET
    must surface 404, NOT 403 (existence must not leak across tenants)."""
    from backend.main import app

    tid_a, tid_b, mid = _new_tenant_id(), _new_tenant_id(), _new_meeting_id()
    teardown_a = _override(app, "operator", tid_a)
    try:
        r = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={
                "session_id": "sess-1",
                "segments": [{
                    "segment_seq": 0, "text": "secret",
                    "is_final": True, "source": "asr",
                }],
            },
        )
        assert r.status_code == 201, r.text
    finally:
        teardown_a()
    # Now switch to tenant B and try to read.
    teardown_b = _override(app, "operator", tid_b)
    try:
        r = await tx_client.get(f"/api/v1/meetings/{mid}")
        assert r.status_code == 404
        r = await tx_client.get(f"/api/v1/meetings/{mid}/segments")
        assert r.status_code == 404
        # A write from tenant B against an A-owned meeting also 404s.
        r = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={
                "session_id": "sess-b",
                "segments": [{
                    "segment_seq": 0, "text": "hijack",
                    "is_final": True, "source": "asr",
                }],
            },
        )
        assert r.status_code == 404
    finally:
        teardown_b()
        await _purge(pg_test_pool, tid_a, mid)
        await _purge(pg_test_pool, tid_b, mid)


@_requires_pg
async def test_malformed_body_returns_422(tx_client, pg_test_pool):
    """Pydantic validation: missing required ``session_id`` -> 422."""
    from backend.main import app

    tid, mid = _new_tenant_id(), _new_meeting_id()
    teardown = _override(app, "operator", tid)
    try:
        r = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={"segments": []},  # missing session_id
        )
        assert r.status_code == 422
        # Negative segment_seq -> 422.
        r = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={
                "session_id": "s",
                "segments": [{
                    "segment_seq": -1, "text": "x",
                    "is_final": True, "source": "asr",
                }],
            },
        )
        assert r.status_code == 422
    finally:
        teardown()
        await _purge(pg_test_pool, tid, mid)


@_requires_pg
async def test_idempotency_key_returns_cached_response(tx_client, pg_test_pool):
    """The Idempotency-Key header makes whole-batch retries return the
    cached response. Server-side state must NOT increment a second time."""
    from backend.main import app

    tid, mid = _new_tenant_id(), _new_meeting_id()
    teardown = _override(app, "operator", tid)
    key = f"idem-{uuid.uuid4().hex}"
    try:
        body = {
            "session_id": "sess-idem",
            "segments": [
                {"segment_seq": 0, "text": "x", "is_final": True,
                 "source": "asr"},
            ],
        }
        r1 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments", json=body,
            headers={"Idempotency-Key": key},
        )
        assert r1.status_code == 201
        first = r1.json()
        assert first["accepted"] == 1
        # Same body + same key: cached response, no second insert/dedup
        # round-trip (deduped stays 0 in the cached body).
        r2 = await tx_client.post(
            f"/api/v1/meetings/{mid}/segments", json=body,
            headers={"Idempotency-Key": key},
        )
        assert r2.status_code == 201
        assert r2.json() == first
        # Storage still has exactly one row.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM transcript_segments "
                "WHERE tenant_id = $1 AND meeting_id = $2",
                tid, mid,
            )
            assert int(row["n"]) == 1
    finally:
        teardown()
        await _purge(pg_test_pool, tid, mid)


@_requires_pg
async def test_envelope_aggregates_counts_and_languages(tx_client, pg_test_pool):
    """GET /meetings/{id} returns segment_count, final_count, languages
    list, first/last ts."""
    from backend.main import app

    tid, mid = _new_tenant_id(), _new_meeting_id()
    teardown = _override(app, "operator", tid)
    try:
        await tx_client.post(
            f"/api/v1/meetings/{mid}/segments",
            json={
                "session_id": "s",
                "segments": [
                    {"segment_seq": 0, "text": "hi", "is_final": True,
                     "source": "asr", "language": "en",
                     "start_ms": 100, "end_ms": 500},
                    {"segment_seq": 1, "text": "salut", "is_final": False,
                     "source": "asr", "language": "fr",
                     "start_ms": 500, "end_ms": 900},
                ],
            },
        )
        r = await tx_client.get(f"/api/v1/meetings/{mid}")
        assert r.status_code == 200
        env = r.json()
        assert env["segment_count"] == 2
        assert env["final_segment_count"] == 1
        assert sorted(env["languages"]) == ["en", "fr"]
        assert env["first_segment_ts_ms"] == 100
        assert env["last_segment_ts_ms"] == 900
    finally:
        teardown()
        await _purge(pg_test_pool, tid, mid)
