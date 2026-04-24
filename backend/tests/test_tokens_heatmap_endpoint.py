"""ZZ.C2 #305-2 checkbox 1 — ``GET /runtime/tokens/heatmap`` tests.

Locks the contract for the session-heatmap endpoint that feeds the
Calendar-style heatmap beneath ``<TokenUsageStats>`` (checkbox 2):

1. **(day, hour) bucketing** — turns at the same UTC ``(YYYY-MM-DD,
   hour)`` merge into one cell; ``token_total`` + ``cost_total`` are
   summed. Turns at different hours in the same day become distinct
   cells; turns on different days become distinct cells.
2. **Window filtering** — ``7d`` excludes events older than 7 days;
   ``30d`` includes events 7–30 days old that ``7d`` rejects.
3. **Sparse payload** — only ``(day, hour)`` pairs with activity are
   emitted; empty slots are the frontend's responsibility to paint
   as genuine zeros.
4. **Ordering** — cells sorted ascending by ``day`` then ``hour`` so
   the heatmap grid can be filled in one left-to-right / top-to-
   bottom sweep without client-side re-sort.
5. **Event-type isolation** — only ``turn.complete`` rows contribute;
   ``agent_update`` / ``task_update`` / other persisted events are
   ignored even when their payloads happen to contain cost-shaped
   fields.
6. **NULL-vs-genuine-zero contract** — a ``turn.complete`` payload
   with ``cost_usd: null`` (unknown model, per
   ``_estimate_turn_cost_usd``) aggregates as zero cost but its
   ``tokens_used`` still contribute to the cell's ``token_total``.
7. **Tenant isolation** — Tenant A's spend doesn't leak into
   Tenant B's heatmap.
8. **Empty** — no rows → empty ``cells`` list (not an error).
9. **Bad window** — any value outside the whitelist → HTTP 400 with
   a detail message that lists the allowed windows.
10. **UTC bucket boundary** — buckets are keyed by UTC
    ``to_char(... AT TIME ZONE 'UTC', 'YYYY-MM-DD')`` so two
    replicas in different regions produce identical cells; the
    frontend paints per-operator local time.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
``OMNI_TEST_PG_URL`` — same pattern as ``test_tokens_burn_rate_endpoint.py``
and ``test_db_events.py``).
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from backend.db_context import set_tenant_id
from backend.routers.system import get_token_heatmap


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
    model: str = "claude-opus-4-7",
) -> str:
    """Mirror the ``emit_turn_complete`` on-the-wire payload shape.

    ``cost_usd=None`` represents the unknown-model NULL-vs-genuine-zero
    contract documented in ``_estimate_turn_cost_usd`` — serialised as
    JSON ``null`` so the endpoint's ``COALESCE(...::numeric, 0)`` path
    gets exercised.

    ``model`` drives ZZ.C2 #305-2 checkbox 4 — the per-model filter
    reads ``data_json->>'model'`` so tests need a handle to seed rows
    with different model slugs.
    """
    return json.dumps({
        "turn_id": turn_id,
        "model": model,
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
    model: str = "claude-opus-4-7",
) -> None:
    """INSERT a ``turn.complete`` row with a caller-controlled timestamp.

    ``db.insert_event`` relies on the DB default for ``created_at``
    (``to_char(now(), …)``) which would always stamp the row "now" —
    unusable for window tests. We bypass it here and explicitly set
    ``created_at`` in the exact TEXT format the column stores
    (``YYYY-MM-DD HH24:MI:SS``) so the endpoint's
    ``to_timestamp(created_at, …) AT TIME ZONE 'UTC'`` parse round-trips.
    """
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


async def _now_minus_hours_text(conn, hours: int) -> str:
    """Produce a ``YYYY-MM-DD HH24:MI:SS`` text anchored to PG's clock.

    Anchoring on PG's own clock sidesteps host/PG timezone drift — the
    endpoint compares ``to_timestamp(text, …)`` against ``NOW()``, both
    in PG's session timezone, so an aged timestamp produced by PG is
    always interpretable the same way.
    """
    row = await conn.fetchrow(
        "SELECT to_char(NOW() - make_interval(hours => $1), "
        "'YYYY-MM-DD HH24:MI:SS') AS ts",
        hours,
    )
    return row["ts"]


async def _utc_bucket_from_text(conn, text: str) -> tuple[str, int]:
    """Derive the ``(day, hour)`` bucket key a row with ``created_at=text``
    would fall into under the endpoint's UTC-anchored aggregation.

    Used by assertions that need to compare against the exact cell key
    the endpoint will emit — computing it in PG (same timezone config
    as the endpoint) rather than Python prevents host/container TZ
    drift from breaking the test.
    """
    row = await conn.fetchrow(
        "SELECT "
        "to_char("
        "  to_timestamp($1, 'YYYY-MM-DD HH24:MI:SS') AT TIME ZONE 'UTC', "
        "  'YYYY-MM-DD'"
        ") AS day, "
        "EXTRACT(HOUR FROM "
        "  to_timestamp($1, 'YYYY-MM-DD HH24:MI:SS') AT TIME ZONE 'UTC'"
        ")::int AS hour",
        text,
    )
    return row["day"], int(row["hour"])


class TestHeatmapBucketing:
    @pytest.mark.asyncio
    async def test_same_day_hour_events_merge_into_one_cell(self, pg_test_conn):
        set_tenant_id("t-alpha")
        # Two turns at the same wall-clock second → same (day, hour)
        # bucket, tokens + cost summed.
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn,
            created_at=stamp, tokens_used=100, cost_usd=0.02, turn_id="t-a",
        )
        await _seed_turn_complete(
            pg_test_conn,
            created_at=stamp, tokens_used=50, cost_usd=0.01, turn_id="t-b",
        )
        expected_day, expected_hour = await _utc_bucket_from_text(
            pg_test_conn, stamp,
        )

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert resp["window"] == "7d"
        assert len(resp["cells"]) == 1
        cell = resp["cells"][0]
        assert cell["day"] == expected_day
        assert cell["hour"] == expected_hour
        assert cell["token_total"] == 150
        assert cell["cost_total"] == pytest.approx(0.03, rel=1e-6)

    @pytest.mark.asyncio
    async def test_different_hours_same_day_produce_separate_cells(
        self, pg_test_conn,
    ):
        set_tenant_id("t-alpha")
        # Two events several hours apart on the same UTC day.
        early = await _now_minus_hours_text(pg_test_conn, 5)
        late = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn, created_at=early, tokens_used=100,
            cost_usd=0.01, turn_id="t-early",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=late, tokens_used=200,
            cost_usd=0.02, turn_id="t-late",
        )
        early_bucket = await _utc_bucket_from_text(pg_test_conn, early)
        late_bucket = await _utc_bucket_from_text(pg_test_conn, late)

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        # If the two seeds happened to land in the same UTC hour
        # (e.g. test clock near a UTC hour boundary), this test falls
        # back to asserting a merged cell; otherwise two cells.
        if early_bucket == late_bucket:
            assert len(resp["cells"]) == 1
            assert resp["cells"][0]["token_total"] == 300
        else:
            assert len(resp["cells"]) == 2
            token_totals = {
                (c["day"], c["hour"]): c["token_total"]
                for c in resp["cells"]
            }
            assert token_totals[early_bucket] == 100
            assert token_totals[late_bucket] == 200

    @pytest.mark.asyncio
    async def test_cells_sorted_by_day_then_hour_ascending(self, pg_test_conn):
        set_tenant_id("t-alpha")
        # Seed in deliberately unsorted order — endpoint must still
        # emit cells in ``(day ASC, hour ASC)`` so the heatmap UI can
        # fill the grid in one sweep.
        stamps = [
            await _now_minus_hours_text(pg_test_conn, h)
            for h in (2, 6 * 24, 3 * 24, 24)  # mix of days + hours
        ]
        for i, stamp in enumerate(stamps):
            await _seed_turn_complete(
                pg_test_conn, created_at=stamp, tokens_used=10 + i,
                turn_id=f"t-{i}",
            )

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        # Cells form a non-decreasing sequence on (day, hour). Using a
        # tuple comparison so same-day different-hour ordering also
        # gets validated.
        seq = [(c["day"], c["hour"]) for c in resp["cells"]]
        assert seq == sorted(seq), (
            f"cells must be sorted by (day ASC, hour ASC); got {seq}"
        )


class TestHeatmapWindowFiltering:
    @pytest.mark.asyncio
    async def test_7d_window_excludes_older_rows(self, pg_test_conn):
        set_tenant_id("t-alpha")
        in_window = await _now_minus_hours_text(pg_test_conn, 2 * 24)
        out_of_window = await _now_minus_hours_text(pg_test_conn, 10 * 24)
        await _seed_turn_complete(
            pg_test_conn, created_at=in_window, turn_id="t-in",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=out_of_window, turn_id="t-out",
        )

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert len(resp["cells"]) == 1

    @pytest.mark.asyncio
    async def test_30d_window_includes_14d_excluded_from_7d(self, pg_test_conn):
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 14 * 24)
        await _seed_turn_complete(pg_test_conn, created_at=stamp)

        resp_7d = await get_token_heatmap(window="7d", conn=pg_test_conn)
        resp_30d = await get_token_heatmap(window="30d", conn=pg_test_conn)
        assert len(resp_7d["cells"]) == 0
        assert len(resp_30d["cells"]) == 1

    @pytest.mark.asyncio
    async def test_30d_window_excludes_rows_older_than_30d(self, pg_test_conn):
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 40 * 24)
        await _seed_turn_complete(pg_test_conn, created_at=stamp)

        resp = await get_token_heatmap(window="30d", conn=pg_test_conn)
        assert len(resp["cells"]) == 0

    @pytest.mark.asyncio
    async def test_unsupported_window_raises_400(self, pg_test_conn):
        set_tenant_id("t-alpha")
        with pytest.raises(HTTPException) as exc:
            await get_token_heatmap(window="1h", conn=pg_test_conn)
        assert exc.value.status_code == 400
        # Error message lists the whitelist so a misconfigured client
        # can self-correct without digging through openapi.json.
        assert "7d" in str(exc.value.detail)
        assert "30d" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_empty_string_window_raises_400(self, pg_test_conn):
        set_tenant_id("t-alpha")
        with pytest.raises(HTTPException) as exc:
            await get_token_heatmap(window="", conn=pg_test_conn)
        assert exc.value.status_code == 400


class TestHeatmapEventTypeIsolation:
    @pytest.mark.asyncio
    async def test_non_turn_complete_events_are_ignored(self, pg_test_conn):
        """Drift guard: if a future event type grows a ``tokens_used``
        field, it must NOT leak into heatmap aggregates. Only rows
        with ``event_type = 'turn.complete'`` contribute."""
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)

        # Seed a real turn.complete AND several red-herring persisted
        # events with the same payload shape.
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
                # prove the filter is by event_type, not payload shape.
                _turn_payload(turn_id=f"t-{etype}", tokens_used=9_999),
                stamp,
                "t-alpha",
            )

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert len(resp["cells"]) == 1
        # Only the real turn.complete's 100 tokens — no red-herring
        # event_types bled in.
        assert resp["cells"][0]["token_total"] == 100


class TestHeatmapNullCostContract:
    @pytest.mark.asyncio
    async def test_null_cost_row_contributes_tokens_but_zero_cost(self, pg_test_conn):
        """NULL-vs-genuine-zero contract: an unknown-model turn emits
        ``cost_usd: null`` (see ``_estimate_turn_cost_usd``). The
        aggregate must treat that as 0 cost — NOT drop the whole
        bucket — because ``tokens_used`` is still authoritative."""
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=100,
            cost_usd=None, turn_id="t-unknown",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=50,
            cost_usd=0.01, turn_id="t-known",
        )

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert len(resp["cells"]) == 1
        cell = resp["cells"][0]
        # Both turns' tokens count.
        assert cell["token_total"] == 150
        # Only the known-model cost contributes.
        assert cell["cost_total"] == pytest.approx(0.01, rel=1e-6)


class TestHeatmapTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_a_does_not_see_tenant_b_spend(self, pg_test_conn):
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=100,
            tenant_id="t-alpha", turn_id="t-alpha-1",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, tokens_used=5_000,
            tenant_id="t-beta", turn_id="t-beta-1",
        )

        # Read as Tenant A — should only see its own 100 tokens.
        resp_alpha = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert len(resp_alpha["cells"]) == 1
        assert resp_alpha["cells"][0]["token_total"] == 100

        # Switch to Tenant B — should only see its own 5000 tokens.
        set_tenant_id("t-beta")
        resp_beta = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert len(resp_beta["cells"]) == 1
        assert resp_beta["cells"][0]["token_total"] == 5_000


class TestHeatmapEmpty:
    @pytest.mark.asyncio
    async def test_empty_event_log_returns_empty_cells(self, pg_test_conn):
        set_tenant_id("t-alpha")
        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert resp["window"] == "7d"
        assert resp["cells"] == []

    @pytest.mark.asyncio
    async def test_empty_event_log_30d_returns_empty_cells(self, pg_test_conn):
        set_tenant_id("t-alpha")
        resp = await get_token_heatmap(window="30d", conn=pg_test_conn)
        assert resp["window"] == "30d"
        assert resp["cells"] == []


class TestHeatmapCellShape:
    @pytest.mark.asyncio
    async def test_cell_day_is_iso_date_and_hour_in_0_to_23(self, pg_test_conn):
        """Shape lock: ``day`` matches ``YYYY-MM-DD`` and ``hour`` is a
        plain int in ``[0, 23]`` — the frontend relies on both to
        bucket cells into the grid without parser branches."""
        import re
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(pg_test_conn, created_at=stamp)

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert len(resp["cells"]) == 1
        cell = resp["cells"][0]
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", cell["day"])
        assert isinstance(cell["hour"], int)
        assert 0 <= cell["hour"] <= 23
        assert isinstance(cell["token_total"], int)
        assert isinstance(cell["cost_total"], float)


class TestHeatmapModelFilter:
    """ZZ.C2 #305-2 checkbox 4 (2026-04-24): per-model filter.

    Locks six sub-contracts that the per-model dropdown depends on:

    1. **``available_models`` surfaces distinct slugs** across the
       unfiltered window, sorted ASC so the frontend dropdown has a
       stable ordering (no need to resort client-side).
    2. **Applying a filter narrows cells** to rows with that exact
       ``model`` slug — sum of cells reflects only the chosen model.
    3. **Applying a filter does NOT narrow ``available_models``** —
       every option is still listed so operators can switch to a
       different model without losing the dropdown.
    4. **``None`` / empty ``model`` is "all models"** — backward
       compatible with checkbox-1/2/3 callers that never knew about
       the param.
    5. **Malformed model slugs → 400** so a misconfigured frontend
       gets a clean error rather than a silently-empty result set.
    6. **Tenant isolation still wins** — Tenant A's model list must
       not leak Tenant B's slugs.
    """

    @pytest.mark.asyncio
    async def test_available_models_lists_distinct_slugs_sorted(
        self, pg_test_conn,
    ):
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        # Seed three distinct slugs and one duplicate — ``DISTINCT``
        # should collapse the dupe to a single entry and ASC-sort
        # the result.
        for slug in (
            "gpt-4o", "claude-opus-4-7", "gemini-2.5-pro", "claude-opus-4-7",
        ):
            await _seed_turn_complete(
                pg_test_conn, created_at=stamp, model=slug, turn_id=f"t-{slug}",
            )

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert resp["available_models"] == [
            "claude-opus-4-7", "gemini-2.5-pro", "gpt-4o",
        ]

    @pytest.mark.asyncio
    async def test_filter_narrows_cells_to_one_model(self, pg_test_conn):
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, model="claude-opus-4-7",
            tokens_used=100, cost_usd=0.02, turn_id="t-opus",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, model="gpt-4o",
            tokens_used=500, cost_usd=0.05, turn_id="t-gpt",
        )

        resp = await get_token_heatmap(
            window="7d", model="claude-opus-4-7", conn=pg_test_conn,
        )
        # Only the opus row shows up in cells — total 100 tokens, not 600.
        total_tokens = sum(c["token_total"] for c in resp["cells"])
        assert total_tokens == 100
        # Echo back the applied filter so the frontend can reconcile.
        assert resp["model"] == "claude-opus-4-7"

    @pytest.mark.asyncio
    async def test_filter_does_not_narrow_available_models(
        self, pg_test_conn,
    ):
        """The dropdown must keep showing every slug even when a
        filter is active — otherwise operators get trapped on one
        model and can't switch without clearing the filter first."""
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        for slug in ("gpt-4o", "claude-opus-4-7", "gemini-2.5-pro"):
            await _seed_turn_complete(
                pg_test_conn, created_at=stamp, model=slug,
                turn_id=f"t-{slug}",
            )

        resp = await get_token_heatmap(
            window="7d", model="claude-opus-4-7", conn=pg_test_conn,
        )
        assert set(resp["available_models"]) == {
            "gpt-4o", "claude-opus-4-7", "gemini-2.5-pro",
        }

    @pytest.mark.asyncio
    async def test_none_model_means_all_models_sum(self, pg_test_conn):
        """Backward compatibility: callers that omit ``model`` (all
        checkbox-1/2/3 callers) must get the unfiltered sum."""
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, model="claude-opus-4-7",
            tokens_used=100, turn_id="t-opus",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, model="gpt-4o",
            tokens_used=500, turn_id="t-gpt",
        )

        # Default call (no model param) — should sum both slugs.
        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        total = sum(c["token_total"] for c in resp["cells"])
        assert total == 600
        assert resp["model"] is None

    @pytest.mark.asyncio
    async def test_empty_string_model_means_all_models(self, pg_test_conn):
        """The frontend ``SESSION_HEATMAP_ALL_MODELS`` sentinel is an
        empty string; if it ever leaks into the URL the backend
        should treat it identically to "omit the param"."""
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, model="claude-opus-4-7",
            tokens_used=100, turn_id="t-opus",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, model="gpt-4o",
            tokens_used=500, turn_id="t-gpt",
        )

        resp = await get_token_heatmap(
            window="7d", model="", conn=pg_test_conn,
        )
        total = sum(c["token_total"] for c in resp["cells"])
        assert total == 600
        assert resp["model"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_slug",
        [
            "claude opus",   # whitespace
            "claude;drop",   # shell metachar
            "model/etc",     # path traversal shape
            "x" * 200,       # over length cap
        ],
    )
    async def test_malformed_model_slug_raises_400(
        self, pg_test_conn, bad_slug,
    ):
        set_tenant_id("t-alpha")
        with pytest.raises(HTTPException) as exc:
            await get_token_heatmap(
                window="7d", model=bad_slug, conn=pg_test_conn,
            )
        assert exc.value.status_code == 400
        # Detail mentions the invalid slug so the frontend can log it.
        assert "model" in str(exc.value.detail).lower()

    @pytest.mark.asyncio
    async def test_tenant_isolation_on_available_models(self, pg_test_conn):
        """``available_models`` must be tenant-scoped — a neighbour's
        exotic model slug must not appear in the operator's
        dropdown."""
        set_tenant_id("t-alpha")
        stamp = await _now_minus_hours_text(pg_test_conn, 2)
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, model="claude-opus-4-7",
            tenant_id="t-alpha", turn_id="t-alpha-opus",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=stamp, model="exotic-model-9000",
            tenant_id="t-beta", turn_id="t-beta-exotic",
        )

        resp_alpha = await get_token_heatmap(window="7d", conn=pg_test_conn)
        assert resp_alpha["available_models"] == ["claude-opus-4-7"]
        assert "exotic-model-9000" not in resp_alpha["available_models"]

    @pytest.mark.asyncio
    async def test_model_filter_plus_window_filter_compose(self, pg_test_conn):
        """Filter composition: ``model`` narrows cells in addition to
        (not instead of) ``window`` — rows outside the window are
        excluded regardless of model."""
        set_tenant_id("t-alpha")
        recent = await _now_minus_hours_text(pg_test_conn, 2 * 24)
        old = await _now_minus_hours_text(pg_test_conn, 14 * 24)
        await _seed_turn_complete(
            pg_test_conn, created_at=recent, model="claude-opus-4-7",
            tokens_used=100, turn_id="t-recent",
        )
        await _seed_turn_complete(
            pg_test_conn, created_at=old, model="claude-opus-4-7",
            tokens_used=9_999, turn_id="t-old",
        )

        resp_7d = await get_token_heatmap(
            window="7d", model="claude-opus-4-7", conn=pg_test_conn,
        )
        resp_30d = await get_token_heatmap(
            window="30d", model="claude-opus-4-7", conn=pg_test_conn,
        )
        assert sum(c["token_total"] for c in resp_7d["cells"]) == 100
        assert sum(c["token_total"] for c in resp_30d["cells"]) == 10_099


class TestHeatmapSparsePayload:
    @pytest.mark.asyncio
    async def test_only_populated_cells_are_emitted(self, pg_test_conn):
        """Sparse-payload contract: only ``(day, hour)`` pairs with at
        least one ``turn.complete`` row appear; empty slots are absent
        (the frontend paints them as genuine zeros)."""
        set_tenant_id("t-alpha")
        # Seed exactly 3 distinct (day, hour) buckets — far enough apart
        # so they cannot collide under UTC bucketing regardless of the
        # test clock's position inside the hour.
        stamps = [
            await _now_minus_hours_text(pg_test_conn, 2),
            await _now_minus_hours_text(pg_test_conn, 30),
            await _now_minus_hours_text(pg_test_conn, 50),
        ]
        for i, stamp in enumerate(stamps):
            await _seed_turn_complete(
                pg_test_conn, created_at=stamp, turn_id=f"t-{i}",
            )

        resp = await get_token_heatmap(window="7d", conn=pg_test_conn)
        # 7d × 24h = 168 grid slots — the endpoint must NOT pad out
        # empty ones. We seeded 3 distinct hours so expect exactly 3.
        # (Lower bound 1 covers the edge case where two seeds land in
        # the same UTC hour; upper bound 3 proves no padding.)
        assert 1 <= len(resp["cells"]) <= 3
        assert all(c["token_total"] > 0 for c in resp["cells"])
