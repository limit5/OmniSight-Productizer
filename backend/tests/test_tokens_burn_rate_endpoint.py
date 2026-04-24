"""ZZ.B3 #304-3 checkbox 1 — ``GET /runtime/tokens/burn-rate`` tests.

Locks the contract for the burn-rate time-series endpoint that feeds
the dashboard sparkline + daily-budget extrapolation toast:

1. **60-second bucketing** — two ``turn.complete`` events in the same
   minute merge into one bucket; tokens + cost are summed.
2. **Rate normalisation** — ``tokens_per_hour = sum(bucket_tokens) *
   60`` for the fixed 60 s bucket width (checkbox 2 UI renders one
   y-axis regardless of window).
3. **Window filtering** — ``15m`` / ``1h`` / ``24h`` windows exclude
   events older than the requested cutoff via a server-side
   ``NOW() - INTERVAL`` comparison.
4. **Event-type isolation** — only ``turn.complete`` rows contribute;
   ``agent_update`` / ``task_update`` / other persisted events are
   ignored even when their payloads happen to contain cost-shaped
   fields.
5. **NULL-vs-genuine-zero contract** — a ``turn.complete`` payload
   with ``cost_usd: null`` (unknown model, per
   ``_estimate_turn_cost_usd``) aggregates as zero cost but its
   ``tokens_used`` still contribute to tokens-per-hour.
6. **Tenant isolation** — Tenant A's spend doesn't leak to Tenant
   B's sparkline.
7. **Empty** — no rows → empty ``points`` list (not an error).
8. **Bad window** — any value outside the whitelist → HTTP 400.
9. **Ordering** — ``points`` are sorted ascending by timestamp so the
   sparkline can render left-to-right directly.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
``OMNI_TEST_PG_URL`` — same pattern as ``test_db_events.py`` +
``test_runtime_turns_endpoint.py``).
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from backend.db_context import set_tenant_id
from backend.routers.system import get_token_burn_rate


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


def _turn_payload(
    *,
    turn_id: str = "t-1",
    tokens_used: int = 150,
    cost_usd: float | None = 0.01,
) -> str:
    """Mirror the ``emit_turn_complete`` on-the-wire payload shape.

    ``cost_usd=None`` represents the unknown-model NULL-vs-genuine-zero
    contract documented in ``_estimate_turn_cost_usd``; serialised as
    JSON ``null`` so the endpoint's ``COALESCE(...::numeric, 0)`` path
    gets exercised.
    """
    return json.dumps({
        "turn_id": turn_id,
        "model": "claude-opus-4-7",
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 50,
        "tokens_used": tokens_used,
        "latency_ms": 200,
        "cost_usd": cost_usd,
        "messages": [],
        "tool_calls": [],
        "tool_call_count": 0,
        "tool_failure_count": 0,
    })


async def _seed_turn_complete(
    conn,
    *,
    created_at: str,
    tokens_used: int = 150,
    cost_usd: float | None = 0.01,
    turn_id: str = "t-1",
    tenant_id: str = "t-alpha",
) -> None:
    """INSERT a ``turn.complete`` row with a caller-controlled timestamp.

    ``db.insert_event`` relies on the DB default for ``created_at``
    (``to_char(now(), …)``) which would always stamp the row "now" —
    unusable for window tests. We bypass it here and explicitly set
    ``created_at`` in the exact TEXT format the column stores
    (``YYYY-MM-DD HH24:MI:SS``) so the endpoint's
    ``to_timestamp(created_at, …)`` parse round-trips.
    """
    await conn.execute(
        "INSERT INTO event_log (event_type, data_json, created_at, tenant_id) "
        "VALUES ($1, $2, $3, $4)",
        "turn.complete",
        _turn_payload(turn_id=turn_id, tokens_used=tokens_used, cost_usd=cost_usd),
        created_at,
        tenant_id,
    )


async def _now_minus_minutes_text(conn, minutes: int) -> str:
    """Produce a ``YYYY-MM-DD HH24:MI:SS`` text anchored to PG's clock.

    Anchoring on PG's own clock — rather than Python's
    ``datetime.utcnow()`` — sidesteps host/PG timezone drift in the
    test container: the endpoint compares ``to_timestamp(text, …)``
    against ``NOW()``, both in PG's session timezone, so an aged
    timestamp produced by PG is always interpretable the same way.
    """
    row = await conn.fetchrow(
        "SELECT to_char(NOW() - make_interval(mins => $1), "
        "'YYYY-MM-DD HH24:MI:SS') AS ts",
        minutes,
    )
    return row["ts"]


class TestBurnRateBucketing:
    @pytest.mark.asyncio
    async def test_same_minute_events_merge_into_one_bucket(self, pg_test_conn):
        set_tenant_id("t-alpha")
        # Two turns at the same minute → one bucket, tokens + cost summed.
        stamp = await _now_minus_minutes_text(pg_test_conn, 5)
        await _seed_turn_complete(
            pg_test_conn,
            created_at=stamp, tokens_used=100, cost_usd=0.02, turn_id="t-a",
        )
        await _seed_turn_complete(
            pg_test_conn,
            created_at=stamp, tokens_used=50, cost_usd=0.01, turn_id="t-b",
        )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert resp["window"] == "15m"
        assert resp["bucket_seconds"] == 60
        assert len(resp["points"]) == 1
        point = resp["points"][0]
        # 150 tokens in the bucket → 150 * 60 = 9000 tokens/hour.
        assert point["tokens_per_hour"] == 9000
        # 0.03 USD in the bucket → 0.03 * 60 = 1.8 USD/hour.
        assert point["cost_per_hour"] == pytest.approx(1.8, rel=1e-6)

    @pytest.mark.asyncio
    async def test_different_minutes_produce_separate_buckets_sorted(
        self, pg_test_conn,
    ):
        set_tenant_id("t-alpha")
        # Events at 2 distinct minute boundaries within the 15m window.
        older = await _now_minus_minutes_text(pg_test_conn, 10)
        newer = await _now_minus_minutes_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn, created_at=older, tokens_used=100,
            cost_usd=0.005, turn_id="t-old",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=newer, tokens_used=200,
            cost_usd=0.01, turn_id="t-new",
        )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp["points"]) == 2
        # Sorted ascending so the sparkline renders L→R directly.
        assert resp["points"][0]["timestamp"] < resp["points"][1]["timestamp"]
        assert resp["points"][0]["tokens_per_hour"] == 100 * 60
        assert resp["points"][1]["tokens_per_hour"] == 200 * 60


class TestBurnRateWindowFiltering:
    @pytest.mark.asyncio
    async def test_15m_window_excludes_older_rows(self, pg_test_conn):
        set_tenant_id("t-alpha")
        in_window = await _now_minus_minutes_text(pg_test_conn, 10)
        out_of_window = await _now_minus_minutes_text(pg_test_conn, 30)
        await _seed_turn_complete(
            pg_test_conn, created_at=in_window, turn_id="t-in",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=out_of_window, turn_id="t-out",
        )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        # Only the in-window row contributes.
        assert len(resp["points"]) == 1

    @pytest.mark.asyncio
    async def test_1h_window_includes_30m_excluded_from_15m(self, pg_test_conn):
        set_tenant_id("t-alpha")
        stamp = await _now_minus_minutes_text(pg_test_conn, 30)
        await _seed_turn_complete(pg_test_conn, created_at=stamp)

        resp_15m = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        resp_1h = await get_token_burn_rate(window="1h", conn=pg_test_conn)
        assert len(resp_15m["points"]) == 0
        assert len(resp_1h["points"]) == 1

    @pytest.mark.asyncio
    async def test_24h_window_includes_6h_excluded_from_1h(self, pg_test_conn):
        set_tenant_id("t-alpha")
        stamp = await _now_minus_minutes_text(pg_test_conn, 6 * 60)
        await _seed_turn_complete(pg_test_conn, created_at=stamp)

        resp_1h = await get_token_burn_rate(window="1h", conn=pg_test_conn)
        resp_24h = await get_token_burn_rate(window="24h", conn=pg_test_conn)
        assert len(resp_1h["points"]) == 0
        assert len(resp_24h["points"]) == 1

    @pytest.mark.asyncio
    async def test_unsupported_window_raises_400(self, pg_test_conn):
        set_tenant_id("t-alpha")
        with pytest.raises(HTTPException) as exc:
            await get_token_burn_rate(window="7d", conn=pg_test_conn)
        assert exc.value.status_code == 400
        # Error message lists the whitelist so a misconfigured client
        # can self-correct without digging through openapi.json.
        assert "15m" in str(exc.value.detail)
        assert "1h" in str(exc.value.detail)
        assert "24h" in str(exc.value.detail)


class TestBurnRateEventTypeIsolation:
    @pytest.mark.asyncio
    async def test_non_turn_complete_events_are_ignored(self, pg_test_conn):
        """Drift guard: if a future event type grows a ``tokens_used``
        field, it must NOT leak into burn-rate aggregates. Only rows
        with ``event_type = 'turn.complete'`` contribute."""
        set_tenant_id("t-alpha")
        stamp = await _now_minus_minutes_text(pg_test_conn, 5)

        # Seed a real turn.complete AND several red-herring persisted
        # events with the same payload shape — only the first should
        # show up in the aggregate.
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=100,
            cost_usd=0.005, turn_id="t-real",
        )
        for etype in ("agent_update", "task_update", "simulation", "invoke"):
            await pg_test_conn.execute(
                "INSERT INTO event_log "
                "(event_type, data_json, created_at, tenant_id) "
                "VALUES ($1, $2, $3, $4)",
                etype,
                # Deliberately shaped like a turn.complete payload to
                # prove the filter is by event_type, not by payload shape.
                _turn_payload(turn_id=f"t-{etype}", tokens_used=9_999),
                stamp,
                "t-alpha",
            )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp["points"]) == 1
        # Only the real turn.complete's 100 tokens → 6000/hour.
        assert resp["points"][0]["tokens_per_hour"] == 6000


class TestBurnRateNullCostContract:
    @pytest.mark.asyncio
    async def test_null_cost_row_contributes_tokens_but_zero_cost(self, pg_test_conn):
        """NULL-vs-genuine-zero contract: an unknown-model turn emits
        ``cost_usd: null`` (see ``_estimate_turn_cost_usd``). The
        aggregate must treat that as 0 cost — NOT drop the whole
        bucket — because ``tokens_used`` is still authoritative."""
        set_tenant_id("t-alpha")
        stamp = await _now_minus_minutes_text(pg_test_conn, 5)
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=100,
            cost_usd=None, turn_id="t-unknown",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=50,
            cost_usd=0.01, turn_id="t-known",
        )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp["points"]) == 1
        # Both turns' tokens count (150) → 9000/hour.
        assert resp["points"][0]["tokens_per_hour"] == 9000
        # Only the known-model cost contributes: 0.01 * 60 = 0.6.
        assert resp["points"][0]["cost_per_hour"] == pytest.approx(0.6, rel=1e-6)


class TestBurnRateTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_a_does_not_see_tenant_b_spend(self, pg_test_conn):
        set_tenant_id("t-alpha")
        stamp = await _now_minus_minutes_text(pg_test_conn, 5)
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=100,
            tenant_id="t-alpha", turn_id="t-alpha-1",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=5_000,
            tenant_id="t-beta", turn_id="t-beta-1",
        )

        # Read as Tenant A — should only see its own 100 tokens.
        resp_alpha = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp_alpha["points"]) == 1
        assert resp_alpha["points"][0]["tokens_per_hour"] == 6000

        # Switch to Tenant B — should only see its own 5000 tokens.
        set_tenant_id("t-beta")
        resp_beta = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp_beta["points"]) == 1
        assert resp_beta["points"][0]["tokens_per_hour"] == 5_000 * 60


class TestBurnRateEmpty:
    @pytest.mark.asyncio
    async def test_empty_event_log_returns_empty_points(self, pg_test_conn):
        set_tenant_id("t-alpha")
        resp = await get_token_burn_rate(window="1h", conn=pg_test_conn)
        assert resp["window"] == "1h"
        assert resp["bucket_seconds"] == 60
        assert resp["points"] == []
