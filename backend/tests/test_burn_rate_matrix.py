"""ZZ.B3 #304-3 checkbox 4 — Burn-rate cross-axis integration matrix.

Per-axis tests already cover the individual surfaces:

  * **aggregation bucketing** (backend contract) →
    ``backend/tests/test_tokens_burn_rate_endpoint.py``  (10 cases)
  * **window 切換** (UI contract) →
    ``test/components/token-usage-stats-burn-rate.test.tsx``  (4 cases)
  * **外推預警條件** (pure math + render) →
    ``test/components/burn-rate-freeze-toast-center.test.tsx``  (15 cases)

What none of those exercise is the **handoff between axes** — the
place regressions tend to hide:

  1. The same real ``turn.complete`` stream seeded into ``event_log``
     should produce a bucket aggregate whose ``cost_per_hour`` — once
     fed through the frontend's linear-extrapolation rule — triggers
     (or doesn't trigger) a freeze-ETA warning deterministically.
     Per-axis tests fabricate points in isolation; the cross-axis
     contract is that what axis 1 emits is exactly what axis 3 consumes.
  2. Switching the window keyword (``15m`` → ``1h`` → ``24h``) must not
     alter the ``cost_per_hour`` of the buckets that survive the
     filter — window 切換 widens the time horizon but doesn't scale the
     rate. Axis 3 hard-codes ``"1h"`` today, but this contract keeps a
     future checkbox that lets operators pick the extrapolation window
     from silently over-counting.
  3. NULL-vs-genuine-zero: a mixed stream of known-model + unknown-model
     turns must produce a ``cost_per_hour`` that reflects only the
     known-model cost (unknowns contribute tokens but zero cost), and
     the freeze-ETA decision must be made off that aggregate — not off
     the raw JSON ``null`` (which would misclassify as "zero burn,
     don't toast").

The freeze-ETA rule under test mirrors the frontend pure helper
``computeFreezeEta`` from
``components/omnisight/burn-rate-freeze-toast-center.tsx`` verbatim:

    trigger ⇔ budget > 0 and not frozen and remaining > 0
              and cost_per_hour > 0
              and cost_per_hour × 24 > budget

ETA label (``"HH:MM"``) is not asserted — local-TZ dependent and
already locked in the frontend test. We lock the **trigger decision**
plus the **numeric inputs** (``remaining``, ``projected_daily``) that
drive the decision.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
``OMNI_TEST_PG_URL`` — same pattern as sibling test files).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from backend.db_context import set_tenant_id
from backend.routers.system import (
    _BURN_RATE_BUCKET_SECONDS,
    _BURN_RATE_WINDOWS,
    get_token_burn_rate,
)


# ---------- Python mirror of the frontend ``computeFreezeEta`` helper.

@dataclass(frozen=True)
class FreezeEta:
    remaining: float
    cost_per_hour: float
    projected_daily: float
    # etaMs / etaLabel intentionally omitted — TZ-dependent and locked
    # in the frontend pure-function tests. Here we only lock the
    # trigger decision + numeric inputs.


def compute_freeze_eta(
    *,
    budget: float,
    usage: float,
    frozen: bool,
    points: list[dict],
) -> FreezeEta | None:
    """Verbatim port of ``computeFreezeEta`` from
    ``burn-rate-freeze-toast-center.tsx``. Return ``None`` for any of
    the silence conditions (matches the 5 frontend guards 1:1), else a
    FreezeEta describing the trigger inputs.
    """
    if budget is None:
        return None
    if frozen:
        return None
    if budget <= 0:
        return None
    remaining = budget - usage
    if remaining <= 0:
        return None
    if not points:
        return None
    latest = points[-1]
    cost_per_hour = latest.get("cost_per_hour") or 0
    if not isinstance(cost_per_hour, (int, float)) or cost_per_hour <= 0:
        return None
    projected_daily = cost_per_hour * 24
    if projected_daily <= budget:
        return None
    return FreezeEta(
        remaining=float(remaining),
        cost_per_hour=float(cost_per_hour),
        projected_daily=float(projected_daily),
    )


# ---------- Fixtures & helpers (parallel to test_tokens_burn_rate_endpoint.py).


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


def _turn_payload(
    *,
    turn_id: str,
    tokens_used: int,
    cost_usd: float | None,
    model: str = "claude-opus-4-7",
) -> str:
    return json.dumps({
        "turn_id": turn_id,
        "model": model,
        "provider": "anthropic",
        "input_tokens": tokens_used // 2,
        "output_tokens": tokens_used - (tokens_used // 2),
        "tokens_used": tokens_used,
        "latency_ms": 200,
        "cost_usd": cost_usd,
        "messages": [],
        "tool_calls": [],
        "tool_call_count": 0,
        "tool_failure_count": 0,
    })


async def _seed(
    conn,
    *,
    created_at: str,
    tokens_used: int,
    cost_usd: float | None,
    turn_id: str,
    tenant_id: str = "t-alpha",
    model: str = "claude-opus-4-7",
) -> None:
    await conn.execute(
        "INSERT INTO event_log (event_type, data_json, created_at, tenant_id) "
        "VALUES ($1, $2, $3, $4)",
        "turn.complete",
        _turn_payload(
            turn_id=turn_id,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            model=model,
        ),
        created_at,
        tenant_id,
    )


async def _minutes_ago(conn, minutes: int) -> str:
    row = await conn.fetchrow(
        "SELECT to_char(NOW() - make_interval(mins => $1), "
        "'YYYY-MM-DD HH24:MI:SS') AS ts",
        minutes,
    )
    return row["ts"]


# ---------- Axis 1 ↔ Axis 3 handoff: bucket output → extrapolation input.


class TestBucketingFeedsExtrapolation:
    """The ``cost_per_hour`` coming out of ``get_token_burn_rate`` must
    be the exact input the freeze-ETA rule consumes. Drift here breaks
    the real warning UX even when per-axis tests stay green."""

    @pytest.mark.asyncio
    async def test_sustained_over_budget_burn_triggers_warning(self, pg_test_conn):
        set_tenant_id("t-alpha")
        # 0.1 USD per minute sustained for 5 consecutive minutes within
        # the 15 m window. Each bucket independently gets ``0.1 * 60 =
        # 6 USD/hour`` — and ``6 × 24 = 144 > 10 USD`` daily budget so
        # the extrapolation rule fires.
        for i in range(5):
            stamp = await _minutes_ago(pg_test_conn, i + 1)
            await _seed(
                pg_test_conn,
                created_at=stamp,
                tokens_used=200,
                cost_usd=0.1,
                turn_id=f"t-burn-{i}",
            )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        # One bucket per minute, 5 in total, chronologically ascending
        # (axis 1 contract).
        assert len(resp["points"]) == 5
        for p in resp["points"]:
            assert p["cost_per_hour"] == pytest.approx(6.0, rel=1e-6)

        # Feed the latest bucket into the axis-3 rule: at $6/hr with an
        # $8 remaining budget, freeze is projected in ~1.33 h — trigger.
        eta = compute_freeze_eta(
            budget=10.0,
            usage=2.0,
            frozen=False,
            points=resp["points"],
        )
        assert eta is not None
        assert eta.cost_per_hour == pytest.approx(6.0, rel=1e-6)
        assert eta.remaining == pytest.approx(8.0, rel=1e-6)
        assert eta.projected_daily == pytest.approx(144.0, rel=1e-6)

    @pytest.mark.asyncio
    async def test_sustainable_burn_produces_points_but_no_warning(self, pg_test_conn):
        """Per-axis tests guarantee the sparkline renders; this case
        proves the rule actively *refuses* to warn on a benign rate —
        so a noisy bucket aggregate can't push a false-positive toast."""
        set_tenant_id("t-alpha")
        # 0.005 USD per minute × 5 min → $0.3/hour per bucket.
        # 0.3 × 24 = $7.2 < $10 budget → sustainable.
        for i in range(5):
            stamp = await _minutes_ago(pg_test_conn, i + 1)
            await _seed(
                pg_test_conn,
                created_at=stamp,
                tokens_used=50,
                cost_usd=0.005,
                turn_id=f"t-safe-{i}",
            )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp["points"]) == 5
        for p in resp["points"]:
            assert p["cost_per_hour"] == pytest.approx(0.3, rel=1e-6)

        assert compute_freeze_eta(
            budget=10.0,
            usage=0.0,
            frozen=False,
            points=resp["points"],
        ) is None

    @pytest.mark.asyncio
    async def test_same_minute_merge_yields_same_trigger_as_separate_emits(self, pg_test_conn):
        """Axis-1 bucketing: two same-minute turns merge; axis-3: the
        merged ``cost_per_hour`` must match the trigger condition for
        the summed rate, not double-counted and not halved. Proves the
        bucketing arithmetic ↔ extrapolation arithmetic agree."""
        set_tenant_id("t-alpha")
        stamp = await _minutes_ago(pg_test_conn, 3)
        # Two turns in the same minute, 0.08 + 0.04 = 0.12 USD.
        # Bucket cost_per_hour = 0.12 * 60 = 7.2 USD/hr.
        # 7.2 × 24 = 172.8 > 10 USD → trigger.
        await _seed(
            pg_test_conn, created_at=stamp, tokens_used=100,
            cost_usd=0.08, turn_id="t-same-a",
        )
        await _seed(
            pg_test_conn, created_at=stamp, tokens_used=100,
            cost_usd=0.04, turn_id="t-same-b",
        )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp["points"]) == 1
        assert resp["points"][0]["cost_per_hour"] == pytest.approx(7.2, rel=1e-6)

        eta = compute_freeze_eta(
            budget=10.0, usage=1.0, frozen=False, points=resp["points"],
        )
        assert eta is not None
        assert eta.cost_per_hour == pytest.approx(7.2, rel=1e-6)
        # remaining 9 USD / 7.2 per hour = 1.25 h to freeze.
        assert eta.projected_daily == pytest.approx(172.8, rel=1e-6)


# ---------- Axis 2 (window 切換) cross-window consistency.


class TestWindowSwitchingPreservesRate:
    """The cost_per_hour of a bucket that survives the window filter
    must be identical across ``15m`` / ``1h`` / ``24h`` — switching
    the window widens the time horizon but never rescales the rate
    (the rate is per-bucket, the window is how many buckets you get
    back). If this ever drifted, the axis-3 toast would read a
    different rate depending on which window the frontend polled."""

    @pytest.mark.asyncio
    async def test_same_bucket_reports_same_rate_across_windows(self, pg_test_conn):
        set_tenant_id("t-alpha")
        # One bucket 10 minutes old — visible in all three windows.
        stamp = await _minutes_ago(pg_test_conn, 10)
        await _seed(
            pg_test_conn,
            created_at=stamp,
            tokens_used=300,
            cost_usd=0.1,
            turn_id="t-shared",
        )

        rates_by_window: dict[str, float] = {}
        for window in ("15m", "1h", "24h"):
            resp = await get_token_burn_rate(window=window, conn=pg_test_conn)
            assert len(resp["points"]) == 1
            assert resp["bucket_seconds"] == _BURN_RATE_BUCKET_SECONDS
            rates_by_window[window] = resp["points"][0]["cost_per_hour"]

        # All three windows see the same bucket with the same rate:
        # 0.1 * 60 = 6 USD/hr.
        assert rates_by_window["15m"] == pytest.approx(6.0, rel=1e-6)
        assert rates_by_window["1h"] == pytest.approx(rates_by_window["15m"], rel=1e-6)
        assert rates_by_window["24h"] == pytest.approx(rates_by_window["15m"], rel=1e-6)

        # And feeding any of them into the freeze-ETA rule yields the
        # same trigger decision (so the toast's behaviour is independent
        # of which window the sparkline happens to be polling).
        for window, rate in rates_by_window.items():
            eta = compute_freeze_eta(
                budget=10.0,
                usage=2.0,
                frozen=False,
                points=[{"cost_per_hour": rate}],
            )
            assert eta is not None, f"window={window} lost trigger"
            assert eta.cost_per_hour == pytest.approx(6.0, rel=1e-6)

    @pytest.mark.asyncio
    async def test_older_bucket_visible_in_wider_windows_only(self, pg_test_conn):
        """Lock that window switching adds older points but keeps the
        newer ones at their original rate. The axis-2 UI lets the
        operator widen to 24 h to see history; the freeze-ETA rule
        still uses the *latest* bucket — which must not change just
        because older data became visible."""
        set_tenant_id("t-alpha")
        recent = await _minutes_ago(pg_test_conn, 2)
        old = await _minutes_ago(pg_test_conn, 6 * 60)  # 6 hours ago
        await _seed(
            pg_test_conn, created_at=recent, tokens_used=100,
            cost_usd=0.05, turn_id="t-recent",
        )
        await _seed(
            pg_test_conn, created_at=old, tokens_used=9_999,
            cost_usd=5.0, turn_id="t-old",
        )

        resp_15m = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        resp_1h = await get_token_burn_rate(window="1h", conn=pg_test_conn)
        resp_24h = await get_token_burn_rate(window="24h", conn=pg_test_conn)

        # 15 m / 1 h see only the recent bucket; 24 h sees both.
        assert len(resp_15m["points"]) == 1
        assert len(resp_1h["points"]) == 1
        assert len(resp_24h["points"]) == 2

        # Latest bucket's rate is stable across all three windows
        # (ascending sort guarantees ``points[-1]`` is the freshest).
        latest_rate_15m = resp_15m["points"][-1]["cost_per_hour"]
        latest_rate_1h = resp_1h["points"][-1]["cost_per_hour"]
        latest_rate_24h = resp_24h["points"][-1]["cost_per_hour"]
        assert latest_rate_15m == pytest.approx(3.0, rel=1e-6)
        assert latest_rate_1h == pytest.approx(latest_rate_15m, rel=1e-6)
        assert latest_rate_24h == pytest.approx(latest_rate_15m, rel=1e-6)

        # Freeze-ETA reads the latest bucket → decision is window-
        # independent for the same stream.
        for resp in (resp_15m, resp_1h, resp_24h):
            eta = compute_freeze_eta(
                budget=100.0,
                usage=0.0,
                frozen=False,
                points=resp["points"],
            )
            # 3.0 × 24 = 72 ≤ 100 → sustainable, no trigger.
            assert eta is None

    @pytest.mark.asyncio
    async def test_bad_window_rejects_before_touching_db(self, pg_test_conn):
        """Window whitelist is the frontend's drift guard (``lib/api.ts``
        ``TokenBurnRateWindow`` type alias must match). Anything off
        the list must 400 — so a malformed poll can't cost-leak via
        fall-through to a default window."""
        set_tenant_id("t-alpha")
        for bad in ("", "7d", "0", "1m", "60"):
            with pytest.raises(HTTPException) as exc:
                await get_token_burn_rate(window=bad, conn=pg_test_conn)
            assert exc.value.status_code == 400
        # Whitelist is exactly the three values the frontend sends.
        assert set(_BURN_RATE_WINDOWS.keys()) == {"15m", "1h", "24h"}


# ---------- Axis 3 NULL-vs-genuine-zero + aggregation agreement.


class TestExtrapolationEdgeCases:
    @pytest.mark.asyncio
    async def test_null_cost_turns_do_not_suppress_warning_when_known_costs_trigger(
        self, pg_test_conn,
    ):
        """A stream of unknown-model turns (cost_usd=None) + one
        known-model turn that pushes the cost above budget: axis 1
        must report the known-only cost; axis 3 must still trigger.
        Silent misclassification here would look identical to
        'sustainable' on the UI."""
        set_tenant_id("t-alpha")
        stamp = await _minutes_ago(pg_test_conn, 5)
        # 3 unknown-model turns (cost null) + 1 known-model turn.
        for i in range(3):
            await _seed(
                pg_test_conn, created_at=stamp, tokens_used=500,
                cost_usd=None, turn_id=f"t-unknown-{i}",
                model=f"custom-unknown-{i}",
            )
        await _seed(
            pg_test_conn, created_at=stamp, tokens_used=200,
            cost_usd=0.08, turn_id="t-known",
        )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp["points"]) == 1
        # Known-model cost only: 0.08 * 60 = 4.8 USD/hr.
        assert resp["points"][0]["cost_per_hour"] == pytest.approx(4.8, rel=1e-6)
        # But tokens include every turn: 3×500 + 200 = 1700 → 102 000/hr.
        assert resp["points"][0]["tokens_per_hour"] == 1_700 * 60

        # 4.8 × 24 = 115.2 > 10 → trigger.
        eta = compute_freeze_eta(
            budget=10.0, usage=2.0, frozen=False, points=resp["points"],
        )
        assert eta is not None
        assert eta.cost_per_hour == pytest.approx(4.8, rel=1e-6)

    @pytest.mark.asyncio
    async def test_all_null_cost_stream_yields_silence_not_noise(self, pg_test_conn):
        """All-unknown-model stream → bucket ``cost_per_hour`` is 0
        (COALESCE to zero in SQL). Axis 3 must treat that as "no
        recent turns" silence — NOT as "zero rate, sustainable"
        trigger path (which would still be None here, but for the
        wrong reason; this test pins the right reason)."""
        set_tenant_id("t-alpha")
        stamp = await _minutes_ago(pg_test_conn, 5)
        for i in range(4):
            await _seed(
                pg_test_conn, created_at=stamp, tokens_used=1_000,
                cost_usd=None, turn_id=f"t-allnull-{i}",
                model=f"custom-{i}",
            )

        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp["points"]) == 1
        assert resp["points"][0]["cost_per_hour"] == pytest.approx(0.0, abs=1e-9)
        assert resp["points"][0]["tokens_per_hour"] == 4_000 * 60

        # cost_per_hour ≤ 0 hits the "no burn" silence guard in
        # computeFreezeEta — deterministic None.
        assert compute_freeze_eta(
            budget=10.0, usage=0.0, frozen=False, points=resp["points"],
        ) is None

    @pytest.mark.asyncio
    async def test_frozen_budget_silences_warning_even_with_huge_burn(
        self, pg_test_conn,
    ):
        """Once backend flips frozen=true the warning is moot — axis 3
        must go silent. Regression guard: an operator already past the
        gate shouldn't see a 'you will freeze at HH:MM' toast."""
        set_tenant_id("t-alpha")
        stamp = await _minutes_ago(pg_test_conn, 2)
        await _seed(
            pg_test_conn, created_at=stamp, tokens_used=5_000,
            cost_usd=2.0, turn_id="t-crazy",
        )
        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert resp["points"][0]["cost_per_hour"] == pytest.approx(120.0, rel=1e-6)

        # 120 × 24 = 2880 >> any budget — but frozen silences.
        assert compute_freeze_eta(
            budget=10.0, usage=9.5, frozen=True, points=resp["points"],
        ) is None
        # And silence still holds when remaining has already fallen
        # below zero without the frozen flag (race between frontend
        # poll and backend freeze flip).
        assert compute_freeze_eta(
            budget=10.0, usage=11.0, frozen=False, points=resp["points"],
        ) is None

    @pytest.mark.asyncio
    async def test_unlimited_budget_never_warns(self, pg_test_conn):
        """budget ≤ 0 means "no daily cap configured" — extrapolation
        is meaningless. Locked separately from frozen because operators
        on unmetered tiers should never see the toast regardless of
        burn."""
        set_tenant_id("t-alpha")
        stamp = await _minutes_ago(pg_test_conn, 2)
        await _seed(
            pg_test_conn, created_at=stamp, tokens_used=5_000,
            cost_usd=2.0, turn_id="t-big",
        )
        resp = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert resp["points"][0]["cost_per_hour"] == pytest.approx(120.0, rel=1e-6)

        assert compute_freeze_eta(
            budget=0.0, usage=0.0, frozen=False, points=resp["points"],
        ) is None
        assert compute_freeze_eta(
            budget=-1.0, usage=0.0, frozen=False, points=resp["points"],
        ) is None


# ---------- Cross-axis full-lifecycle scenario.


class TestCrossAxisLifecycle:
    @pytest.mark.asyncio
    async def test_burn_escalation_transitions_from_sustainable_to_warning(
        self, pg_test_conn,
    ):
        """End-to-end scenario: an operator's burn starts sustainable
        (axis 3 silent), then escalates past the budget threshold
        (axis 3 triggers). We drive the transition via seeded
        ``event_log`` rows and show the axis-1 bucket output flips
        the axis-3 rule deterministically. This is the contract that
        makes the toast feel meaningful in real use."""
        set_tenant_id("t-alpha")

        # Phase 1: 5 minutes of low burn (0.01 USD/min → $0.6/hr).
        # $0.6 × 24 = $14.4 < $20 budget → sustainable.
        for i in range(5):
            stamp = await _minutes_ago(pg_test_conn, 12 - i)
            await _seed(
                pg_test_conn, created_at=stamp, tokens_used=100,
                cost_usd=0.01, turn_id=f"t-phase1-{i}",
            )
        resp_phase1 = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp_phase1["points"]) == 5
        for p in resp_phase1["points"]:
            assert p["cost_per_hour"] == pytest.approx(0.6, rel=1e-6)
        assert compute_freeze_eta(
            budget=20.0, usage=1.0, frozen=False, points=resp_phase1["points"],
        ) is None

        # Phase 2: burn spikes to 0.5 USD/min for 3 minutes.
        # $30/hr × 24 = $720 >> $20 budget → trigger.
        for i in range(3):
            stamp = await _minutes_ago(pg_test_conn, 3 - i)
            await _seed(
                pg_test_conn, created_at=stamp, tokens_used=1_000,
                cost_usd=0.5, turn_id=f"t-phase2-{i}",
            )

        resp_phase2 = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        # 5 low buckets + 3 high buckets = 8 total (all unique minutes).
        assert len(resp_phase2["points"]) == 8
        # Latest bucket's rate is the spike ($30/hr), not the average.
        latest = resp_phase2["points"][-1]
        assert latest["cost_per_hour"] == pytest.approx(30.0, rel=1e-6)

        # Rule fires on the escalated rate.
        eta = compute_freeze_eta(
            budget=20.0, usage=1.0, frozen=False, points=resp_phase2["points"],
        )
        assert eta is not None
        assert eta.cost_per_hour == pytest.approx(30.0, rel=1e-6)
        assert eta.remaining == pytest.approx(19.0, rel=1e-6)
        # remaining 19 / 30 per hour ≈ 0.633 h to freeze.
        assert eta.projected_daily == pytest.approx(720.0, rel=1e-6)

        # And the 1 h / 24 h windows agree on the escalation (no
        # rate-rescaling as the window widens).
        for window in ("1h", "24h"):
            resp = await get_token_burn_rate(window=window, conn=pg_test_conn)
            assert resp["points"][-1]["cost_per_hour"] == pytest.approx(30.0, rel=1e-6)
            eta_w = compute_freeze_eta(
                budget=20.0, usage=1.0, frozen=False, points=resp["points"],
            )
            assert eta_w is not None
            assert eta_w.cost_per_hour == pytest.approx(30.0, rel=1e-6)

    @pytest.mark.asyncio
    async def test_tenant_isolation_across_axes(self, pg_test_conn):
        """Tenant A running hot must never trigger tenant B's freeze
        warning. Per-axis tenant isolation is in the endpoint test;
        this seals the cross-axis leak that matters most — a false-
        positive toast for a quiet tenant."""
        set_tenant_id("t-alpha")
        stamp = await _minutes_ago(pg_test_conn, 3)
        # Tenant A: aggressive burn.
        await _seed(
            pg_test_conn, created_at=stamp, tokens_used=5_000,
            cost_usd=2.0, turn_id="t-a-hot",
            tenant_id="t-alpha",
        )
        # Tenant B: nothing at all.

        # Tenant A sees the hot bucket and the rule fires.
        resp_a = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert len(resp_a["points"]) == 1
        assert compute_freeze_eta(
            budget=10.0, usage=0.0, frozen=False, points=resp_a["points"],
        ) is not None

        # Tenant B sees an empty series, so the rule is silent — not
        # because the rate is low but because there's no rate at all.
        set_tenant_id("t-beta")
        resp_b = await get_token_burn_rate(window="15m", conn=pg_test_conn)
        assert resp_b["points"] == []
        assert compute_freeze_eta(
            budget=10.0, usage=0.0, frozen=False, points=resp_b["points"],
        ) is None
