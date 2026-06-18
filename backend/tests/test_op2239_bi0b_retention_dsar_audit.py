"""OP-2239 BI0b -- transcript privacy/retention/DSAR/audit integration.

Exercises the BI0b lifecycle wiring against the real asyncpg pool:

  * DSAR erasure removes meetings + transcript_segments scoped to the
    requesting user's tenant, leaving cross-tenant rows untouched;
  * the portability export envelope carries meetings + transcript_segments
    for the user's tenant;
  * the retention sweep deletes only meetings whose ``retention_until``
    is in the past (fresh rows survive), and the per-tenant call shape
    does not bleed across tenants;
  * the transcript router writes an audit_log row on segment ingest
    (``entity_kind = 'transcript_segment'``) and on explicit meeting
    open (``entity_kind = 'meeting'``).

Skipped cleanly when ``OMNI_TEST_PG_URL`` is unset -- mirrors the BI0
contract suite (``test_router_transcripts_op2238.py``).
"""
from __future__ import annotations

import os
import time
import uuid

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP/DB path depends on asyncpg pool -- requires OMNI_TEST_PG_URL.",
)


# Note: this module mixes sync and async tests, so the global
# ``pytestmark = pytest.mark.asyncio`` shortcut would emit a warning on
# every sync test. The conftest already enables ``asyncio_mode=auto``
# (asyncio plugin config), so async tests run without per-test marks.


# ━━ Pure unit smoke (no PG) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_retention_window_default_is_90_days(monkeypatch) -> None:
    """No env knob set -> 90-day default. Guards against silent
    regression of the documented BI0b retention contract."""
    from backend import transcripts_retention as r

    monkeypatch.delenv("OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S", raising=False)
    assert r._retention_window_s() == float(90 * 24 * 3600)


def test_retention_window_env_override(monkeypatch) -> None:
    """A positive env knob value overrides the default 1-for-1."""
    from backend import transcripts_retention as r

    monkeypatch.setenv("OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S", "3600")
    assert r._retention_window_s() == 3600.0


def test_retention_window_zero_means_no_deadline(monkeypatch) -> None:
    """A zero (or non-positive) env value disables retention -- the
    stamper returns ``None`` so the sweep never fires on those rows.
    Guard against an accidental config typo wiping fresh rows."""
    from backend import transcripts_retention as r

    monkeypatch.setenv("OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S", "0")
    assert r._retention_window_s() == 0.0
    assert r.compute_retention_until(1.0) is None


def test_retention_window_non_numeric_falls_back_to_default(monkeypatch) -> None:
    """A malformed env value warns and falls back to the default rather
    than crashing the router import path."""
    from backend import transcripts_retention as r

    monkeypatch.setenv("OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S", "ten-minutes")
    assert r._retention_window_s() == float(90 * 24 * 3600)


def test_compute_retention_until_stamps_offset(monkeypatch) -> None:
    """The stamper returns ``created_at + window`` so each row carries
    its own expiry independent of later env changes."""
    from backend import transcripts_retention as r

    monkeypatch.setenv("OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S", "600")
    assert r.compute_retention_until(1000.0) == pytest.approx(1600.0)


# ━━ PG-live fixtures ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _new_tenant_id(prefix: str = "t-bi0b") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _new_meeting_id() -> str:
    return f"mtg-{uuid.uuid4().hex[:12]}"


async def _purge_tenant(pool, tenant_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM transcript_segments WHERE tenant_id = $1",
            tenant_id,
        )
        await conn.execute(
            "DELETE FROM meetings WHERE tenant_id = $1",
            tenant_id,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE tenant_id = $1",
            tenant_id,
        )


# ━━ Retention sweep ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_sweep_deletes_only_expired_meetings(pg_test_pool):
    """A meeting whose ``retention_until`` is in the past is removed
    along with its segments; a fresh meeting (or one with NULL
    retention_until) is left alone."""
    from backend import transcripts_retention as retention

    tid = _new_tenant_id()
    expired_mid = _new_meeting_id()
    fresh_mid = _new_meeting_id()
    nullret_mid = _new_meeting_id()
    now = time.time()

    async with pg_test_pool.acquire() as conn:
        # expired -- retention_until 1s in the past
        await conn.execute(
            "INSERT INTO meetings (id, tenant_id, status, retention_until, "
            "created_at, updated_at) VALUES ($1, $2, 'open', $3, $4, $4)",
            expired_mid, tid, now - 1.0, now - 600.0,
        )
        # fresh -- retention_until 1h in the future
        await conn.execute(
            "INSERT INTO meetings (id, tenant_id, status, retention_until, "
            "created_at, updated_at) VALUES ($1, $2, 'open', $3, $4, $4)",
            fresh_mid, tid, now + 3600.0, now,
        )
        # explicit NULL retention -- the "no deadline" sentinel must
        # NEVER be swept.
        await conn.execute(
            "INSERT INTO meetings (id, tenant_id, status, retention_until, "
            "created_at, updated_at) VALUES ($1, $2, 'open', NULL, $3, $3)",
            nullret_mid, tid, now,
        )
        # Two segments under the expired meeting, one under the fresh.
        await conn.execute(
            "INSERT INTO transcript_segments "
            "(id, tenant_id, meeting_id, session_id, segment_seq, "
            " text, is_final, source, created_at, updated_at) VALUES "
            "($1, $2, $3, 'sess-x', 0, 'expired-1', TRUE, 'asr', $4, $4), "
            "($5, $2, $3, 'sess-x', 1, 'expired-2', TRUE, 'asr', $4, $4), "
            "($6, $2, $7, 'sess-y', 0, 'fresh', TRUE, 'asr', $4, $4)",
            f"seg-x0-{uuid.uuid4().hex[:6]}", tid, expired_mid, now,
            f"seg-x1-{uuid.uuid4().hex[:6]}",
            f"seg-y0-{uuid.uuid4().hex[:6]}", fresh_mid,
        )

    try:
        async with pg_test_pool.acquire() as conn:
            counts = await retention.sweep_expired_meetings(
                conn, tenant_id=tid, now=now,
            )
        assert counts["meetings"] == 1
        assert counts["transcript_segments"] == 2

        async with pg_test_pool.acquire() as conn:
            remaining_mtg = [
                r["id"] for r in await conn.fetch(
                    "SELECT id FROM meetings WHERE tenant_id = $1 "
                    "ORDER BY id",
                    tid,
                )
            ]
            remaining_seg = [
                r["meeting_id"] for r in await conn.fetch(
                    "SELECT meeting_id FROM transcript_segments "
                    "WHERE tenant_id = $1 ORDER BY meeting_id",
                    tid,
                )
            ]
        assert expired_mid not in remaining_mtg
        assert fresh_mid in remaining_mtg
        assert nullret_mid in remaining_mtg
        assert expired_mid not in remaining_seg
        assert remaining_seg == [fresh_mid]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_sweep_does_not_cross_tenant_boundary(pg_test_pool):
    """A tenant-scoped sweep MUST NOT delete another tenant's expired
    meetings -- the AC cross-tenant invariant."""
    from backend import transcripts_retention as retention

    tid_a, tid_b = _new_tenant_id("t-bi0b-a"), _new_tenant_id("t-bi0b-b")
    mid_a, mid_b = _new_meeting_id(), _new_meeting_id()
    now = time.time()

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO meetings (id, tenant_id, status, retention_until, "
            "created_at, updated_at) VALUES "
            "($1, $2, 'open', $3, $4, $4), "
            "($5, $6, 'open', $3, $4, $4)",
            mid_a, tid_a, now - 1.0, now - 600.0,
            mid_b, tid_b,
        )

    try:
        async with pg_test_pool.acquire() as conn:
            counts = await retention.sweep_expired_meetings(
                conn, tenant_id=tid_a, now=now,
            )
        assert counts["meetings"] == 1

        async with pg_test_pool.acquire() as conn:
            survivors_a = [
                r["id"] for r in await conn.fetch(
                    "SELECT id FROM meetings WHERE tenant_id = $1",
                    tid_a,
                )
            ]
            survivors_b = [
                r["id"] for r in await conn.fetch(
                    "SELECT id FROM meetings WHERE tenant_id = $1",
                    tid_b,
                )
            ]
        assert survivors_a == []
        assert survivors_b == [mid_b]
    finally:
        await _purge_tenant(pg_test_pool, tid_a)
        await _purge_tenant(pg_test_pool, tid_b)


@_requires_pg
async def test_sweep_global_call_deletes_every_tenant(pg_test_pool):
    """The cron-style unconstrained call deletes expired rows for
    every tenant. This is the shape a future scheduled job will use."""
    from backend import transcripts_retention as retention

    tid_a, tid_b = _new_tenant_id("t-bi0b-c"), _new_tenant_id("t-bi0b-d")
    mid_a, mid_b = _new_meeting_id(), _new_meeting_id()
    now = time.time()

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO meetings (id, tenant_id, status, retention_until, "
            "created_at, updated_at) VALUES "
            "($1, $2, 'open', $3, $4, $4), "
            "($5, $6, 'open', $3, $4, $4)",
            mid_a, tid_a, now - 1.0, now - 600.0,
            mid_b, tid_b,
        )

    try:
        async with pg_test_pool.acquire() as conn:
            counts = await retention.sweep_expired_meetings(conn, now=now)
        assert counts["meetings"] >= 2

        async with pg_test_pool.acquire() as conn:
            survivors_a = await conn.fetchval(
                "SELECT count(*) FROM meetings WHERE tenant_id = $1",
                tid_a,
            )
            survivors_b = await conn.fetchval(
                "SELECT count(*) FROM meetings WHERE tenant_id = $1",
                tid_b,
            )
        assert survivors_a == 0
        assert survivors_b == 0
    finally:
        await _purge_tenant(pg_test_pool, tid_a)
        await _purge_tenant(pg_test_pool, tid_b)


# ━━ DSAR erasure ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_dsar_erasure_removes_meetings_and_segments_tenant_scoped(
    pg_test_pool,
):
    """The DSAR erasure path -- exercised at the helper level so the
    test does not depend on the full /privacy/erasure HTTP stack and
    its bootstrap gate -- removes the requesting user's tenant's
    meetings + segments AND leaves another tenant's rows untouched."""
    from backend import auth as _au
    from backend.routers import privacy

    tid_alice = _new_tenant_id("t-bi0b-erase-a")
    tid_bob = _new_tenant_id("t-bi0b-erase-b")
    mid_alice = _new_meeting_id()
    mid_bob = _new_meeting_id()
    now = time.time()

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO meetings (id, tenant_id, status, retention_until, "
            "created_at, updated_at) VALUES "
            "($1, $2, 'open', NULL, $3, $3), "
            "($4, $5, 'open', NULL, $3, $3)",
            mid_alice, tid_alice, now,
            mid_bob, tid_bob,
        )
        await conn.execute(
            "INSERT INTO transcript_segments "
            "(id, tenant_id, meeting_id, session_id, segment_seq, "
            " text, is_final, source, created_at, updated_at) VALUES "
            "($1, $2, $3, 's', 0, 'alice-text', TRUE, 'asr', $4, $4), "
            "($5, $6, $7, 's', 0, 'bob-text', TRUE, 'asr', $4, $4)",
            f"seg-a-{uuid.uuid4().hex[:6]}", tid_alice, mid_alice, now,
            f"seg-b-{uuid.uuid4().hex[:6]}", tid_bob, mid_bob,
        )

    alice = _au.User(
        id=f"u-alice-{uuid.uuid4().hex[:6]}",
        email="alice-bi0b@example.test",
        name="Alice BI0b",
        role="viewer",
        tenant_id=tid_alice,
    )

    try:
        async with pg_test_pool.acquire() as conn:
            async with conn.transaction():
                erased = await privacy._erase_user_data(conn, alice)

        # The per-tenant transcripts erasure must report a non-zero count
        # for Alice's tenant.
        assert erased["meetings"] == 1
        assert erased["transcript_segments"] == 1

        async with pg_test_pool.acquire() as conn:
            alice_mtg = await conn.fetchval(
                "SELECT count(*) FROM meetings WHERE tenant_id = $1",
                tid_alice,
            )
            alice_seg = await conn.fetchval(
                "SELECT count(*) FROM transcript_segments WHERE tenant_id = $1",
                tid_alice,
            )
            bob_mtg = await conn.fetchval(
                "SELECT count(*) FROM meetings WHERE tenant_id = $1",
                tid_bob,
            )
            bob_seg = await conn.fetchval(
                "SELECT count(*) FROM transcript_segments WHERE tenant_id = $1",
                tid_bob,
            )

        assert alice_mtg == 0
        assert alice_seg == 0
        assert bob_mtg == 1
        assert bob_seg == 1
    finally:
        await _purge_tenant(pg_test_pool, tid_alice)
        await _purge_tenant(pg_test_pool, tid_bob)


@_requires_pg
async def test_portability_export_includes_meetings_and_segments(
    pg_test_pool,
):
    """The portability fetch helper picks up the requesting user's
    tenant's meetings + segments, scoped strictly to that tenant."""
    from backend import auth as _au
    from backend.routers import privacy

    tid_a, tid_b = _new_tenant_id("t-bi0b-port-a"), _new_tenant_id("t-bi0b-port-b")
    mid_a, mid_b = _new_meeting_id(), _new_meeting_id()
    now = time.time()

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO meetings (id, tenant_id, title, status, "
            "retention_until, created_at, updated_at) VALUES "
            "($1, $2, 'Alice Meet', 'open', NULL, $3, $3), "
            "($4, $5, 'Bob Meet', 'open', NULL, $3, $3)",
            mid_a, tid_a, now,
            mid_b, tid_b,
        )
        await conn.execute(
            "INSERT INTO transcript_segments "
            "(id, tenant_id, meeting_id, session_id, segment_seq, "
            " text, is_final, source, created_at, updated_at) VALUES "
            "($1, $2, $3, 's', 0, 'alice line', TRUE, 'asr', $4, $4), "
            "($5, $6, $7, 's', 0, 'bob line', TRUE, 'asr', $4, $4)",
            f"seg-a-{uuid.uuid4().hex[:6]}", tid_a, mid_a, now,
            f"seg-b-{uuid.uuid4().hex[:6]}", tid_b, mid_b,
        )

    alice = _au.User(
        id=f"u-alice-{uuid.uuid4().hex[:6]}",
        email="alice-port@example.test",
        name="Alice Port",
        role="viewer",
        tenant_id=tid_a,
    )

    try:
        async with pg_test_pool.acquire() as conn:
            data = await privacy._fetch_all_user_data(conn, alice)

        assert "meetings" in data
        assert "transcript_segments" in data
        meeting_ids = [m["id"] for m in data["meetings"]]
        segment_meetings = {s["meeting_id"] for s in data["transcript_segments"]}
        assert mid_a in meeting_ids
        assert mid_b not in meeting_ids
        assert segment_meetings == {mid_a}
        # Texts must round-trip into the export (portability spec).
        assert any(s["text"] == "alice line" for s in data["transcript_segments"])

        export = privacy._build_portability_export(data, alice, now)
        assert export["data"]["meetings"][0]["id"] == mid_a
        assert export["data"]["transcript_segments"][0]["meeting_id"] == mid_a
    finally:
        await _purge_tenant(pg_test_pool, tid_a)
        await _purge_tenant(pg_test_pool, tid_b)


# ━━ Audit coverage on ingest + open ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_audit_row_written_on_segment_ingest(pg_test_pool, monkeypatch):
    """``POST /meetings/{id}/segments`` writes an audit_log row with
    ``entity_kind='transcript_segment'`` -- the AC for audit coverage."""
    from httpx import ASGITransport, AsyncClient

    from backend import auth as _au
    from backend import bootstrap as _boot
    from backend.main import app

    tid, mid = _new_tenant_id("t-bi0b-audit-seg"), _new_meeting_id()
    fake = _au.User(
        id=f"u-audit-{uuid.uuid4().hex[:6]}",
        email="audit@example.test",
        name="Audit User",
        role="operator",
        enabled=True,
        tenant_id=tid,
    )

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    async def _fake_user():
        return fake

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()
    app.dependency_overrides[_au.current_user] = _fake_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                f"/api/v1/meetings/{mid}/segments",
                json={
                    "session_id": "s-audit",
                    "segments": [{
                        "segment_seq": 0, "text": "hello audit",
                        "is_final": True, "source": "asr",
                    }],
                },
            )
            assert r.status_code == 201, r.text

        async with pg_test_pool.acquire() as conn:
            seg_row = await conn.fetchrow(
                "SELECT id, action, entity_kind "
                "FROM audit_log "
                "WHERE tenant_id = $1 AND entity_kind = 'transcript_segment' "
                "ORDER BY id DESC LIMIT 1",
                tid,
            )
            mtg_row = await conn.fetchrow(
                "SELECT id, action, entity_kind "
                "FROM audit_log "
                "WHERE tenant_id = $1 AND entity_kind = 'meeting' "
                "ORDER BY id DESC LIMIT 1",
                tid,
            )

        assert seg_row is not None, "transcript_segment audit row missing"
        assert seg_row["entity_kind"] == "transcript_segment"
        assert seg_row["action"] == "transcript_segment.insert"
        # The auto-create path on first-segment arrival fires a meeting
        # audit row too -- ticket calls out audit coverage on BOTH
        # entity_kinds, so we assert both.
        assert mtg_row is not None, "meeting audit row missing"
        assert mtg_row["entity_kind"] == "meeting"
        assert mtg_row["action"] == "meeting.create"
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        _boot._gate_cache_reset()
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_audit_row_written_on_explicit_meeting_open(pg_test_pool, monkeypatch):
    """``POST /meetings`` (explicit open, non-auto path) writes an
    audit_log row with ``entity_kind='meeting'``."""
    from httpx import ASGITransport, AsyncClient

    from backend import auth as _au
    from backend import bootstrap as _boot
    from backend.main import app

    tid, mid = _new_tenant_id("t-bi0b-audit-open"), _new_meeting_id()
    fake = _au.User(
        id=f"u-audit-{uuid.uuid4().hex[:6]}",
        email="audit2@example.test",
        name="Audit User",
        role="operator",
        enabled=True,
        tenant_id=tid,
    )

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    async def _fake_user():
        return fake

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()
    app.dependency_overrides[_au.current_user] = _fake_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/v1/meetings",
                json={"id": mid, "title": "On the record"},
            )
            assert r.status_code == 201, r.text

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, action, entity_kind, entity_id "
                "FROM audit_log "
                "WHERE tenant_id = $1 AND entity_kind = 'meeting' "
                "ORDER BY id DESC LIMIT 1",
                tid,
            )

        assert row is not None, "meeting audit row missing"
        assert row["entity_kind"] == "meeting"
        assert row["action"] == "meeting.create"
        assert row["entity_id"] == mid
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        _boot._gate_cache_reset()
        await _purge_tenant(pg_test_pool, tid)


# ━━ Router-side retention stamping ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_open_meeting_stamps_retention_until(pg_test_pool, monkeypatch):
    """An explicit ``POST /meetings`` writes a row with
    ``retention_until`` set to ``created_at + window`` so the sweep can
    find it without a separate stamping pass."""
    from httpx import ASGITransport, AsyncClient

    from backend import auth as _au
    from backend import bootstrap as _boot
    from backend.main import app

    monkeypatch.setenv("OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S", "3600")

    tid, mid = _new_tenant_id("t-bi0b-stamp"), _new_meeting_id()
    fake = _au.User(
        id=f"u-stamp-{uuid.uuid4().hex[:6]}",
        email="stamp@example.test",
        name="Stamp User",
        role="operator",
        enabled=True,
        tenant_id=tid,
    )

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    async def _fake_user():
        return fake

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()
    app.dependency_overrides[_au.current_user] = _fake_user

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/v1/meetings", json={"id": mid})
            assert r.status_code == 201, r.text

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT created_at, retention_until "
                "FROM meetings WHERE id = $1 AND tenant_id = $2",
                mid, tid,
            )
        assert row is not None
        assert row["retention_until"] is not None
        # within a window-tolerance of created_at + 3600
        delta = float(row["retention_until"]) - float(row["created_at"])
        assert delta == pytest.approx(3600.0, abs=2.0)
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        _boot._gate_cache_reset()
        await _purge_tenant(pg_test_pool, tid)
