"""Phase-3-Runtime-v2 SP-3.5 — contract tests for ported token_usage
db.py functions.

Replaces the SQLite-backed ``test_token_usage_upsert_list`` in
``test_db.py``. The functions now require an asyncpg.Connection as
first argument, so tests run against the pg_test_conn fixture.

Coverage:
  * Three functions: list_token_usage / upsert_token_usage /
    clear_token_usage.
  * UPSERT ON CONFLICT(model) semantics — second upsert replaces
    first, count stays at 1.
  * Type coercion through positional parameter binding (int/float
    columns get python ints/floats).
  * clear_token_usage wipe behaviour.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db


def _usage_fixture(**overrides) -> dict:
    base = {
        "model": "claude-opus-4-6",
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "cost": 0.012,
        "request_count": 1,
        "avg_latency": 1,
        "last_used": "12:34:56",
    }
    base.update(overrides)
    return base


class TestTokenUsageCrud:
    @pytest.mark.asyncio
    async def test_empty_list(self, pg_test_conn) -> None:
        assert await db.list_token_usage(pg_test_conn) == []

    @pytest.mark.asyncio
    async def test_upsert_then_list(self, pg_test_conn) -> None:
        await db.upsert_token_usage(pg_test_conn, _usage_fixture(
            model="claude-opus-4-6", total_tokens=150,
        ))
        rows = await db.list_token_usage(pg_test_conn)
        assert len(rows) == 1
        assert rows[0]["model"] == "claude-opus-4-6"
        assert rows[0]["total_tokens"] == 150
        assert rows[0]["cost"] == pytest.approx(0.012)

    @pytest.mark.asyncio
    async def test_upsert_replaces_on_conflict(self, pg_test_conn) -> None:
        # PK is (model) — second upsert with same model wins.
        await db.upsert_token_usage(pg_test_conn, _usage_fixture(
            model="gpt-4o", total_tokens=150, cost=0.012,
        ))
        await db.upsert_token_usage(pg_test_conn, _usage_fixture(
            model="gpt-4o", total_tokens=300, cost=0.024,
            input_tokens=200, output_tokens=100, request_count=2,
        ))
        rows = await db.list_token_usage(pg_test_conn)
        assert len(rows) == 1
        assert rows[0]["total_tokens"] == 300
        assert rows[0]["cost"] == pytest.approx(0.024)
        assert rows[0]["request_count"] == 2

    @pytest.mark.asyncio
    async def test_multiple_models_coexist(self, pg_test_conn) -> None:
        # Different models → distinct rows; list returns all.
        for model in ("opus", "sonnet", "haiku"):
            await db.upsert_token_usage(pg_test_conn, _usage_fixture(
                model=f"claude-{model}-4",
            ))
        rows = await db.list_token_usage(pg_test_conn)
        assert len(rows) == 3
        assert {r["model"] for r in rows} == {
            "claude-opus-4", "claude-sonnet-4", "claude-haiku-4",
        }


class TestTokenUsageClear:
    @pytest.mark.asyncio
    async def test_clear_wipes_all_rows(self, pg_test_conn) -> None:
        for i in range(3):
            await db.upsert_token_usage(pg_test_conn, _usage_fixture(
                model=f"model-{i}",
            ))
        assert len(await db.list_token_usage(pg_test_conn)) == 3
        await db.clear_token_usage(pg_test_conn)
        assert await db.list_token_usage(pg_test_conn) == []

    @pytest.mark.asyncio
    async def test_clear_on_empty_is_noop(self, pg_test_conn) -> None:
        # Matches the ``reset_token_usage`` handler contract: the
        # reset endpoint may be called when no rows exist; the delete
        # must not raise.
        await db.clear_token_usage(pg_test_conn)
        assert await db.list_token_usage(pg_test_conn) == []


class TestTokenUsageTypeCoercion:
    @pytest.mark.asyncio
    async def test_dict_with_missing_keys_uses_zero_defaults(
        self, pg_test_conn,
    ) -> None:
        # The port coerces missing keys via ``data.get(key, default)``
        # — this matches the pre-port compat behaviour (named params
        # with default None would have failed the NOT NULL columns).
        # Regression guard: if a future refactor drops the .get()
        # fallback and callers send a partial dict, NOT NULL violation
        # surfaces immediately instead of silently mis-persisting.
        await db.upsert_token_usage(pg_test_conn, {
            "model": "partial-model",
            # everything else missing
        })
        rows = await db.list_token_usage(pg_test_conn)
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 0
        assert rows[0]["output_tokens"] == 0
        assert rows[0]["total_tokens"] == 0
        assert rows[0]["cost"] == pytest.approx(0.0)
        assert rows[0]["request_count"] == 0
        assert rows[0]["avg_latency"] == 0
        assert rows[0]["last_used"] == ""
        # ZZ.A1 (#303-1): cache_* fields default to NULL on partial
        # dicts → preserves the pre-ZZ data semantics.
        assert rows[0]["cache_read_tokens"] is None
        assert rows[0]["cache_create_tokens"] is None
        assert rows[0]["cache_hit_ratio"] is None

    @pytest.mark.asyncio
    async def test_cache_columns_round_trip(self, pg_test_conn) -> None:
        # ZZ.A1 (#303-1): when SharedTokenUsage hands us ZZ-era payload
        # with cache_read_tokens / cache_create_tokens / cache_hit_ratio,
        # they round-trip through upsert_token_usage → list_token_usage
        # unchanged. Numeric ints stay ints, ratio stays a float.
        await db.upsert_token_usage(pg_test_conn, _usage_fixture(
            model="zz-cache-model",
            cache_read_tokens=800,
            cache_create_tokens=120,
            cache_hit_ratio=0.8,
        ))
        rows = await db.list_token_usage(pg_test_conn)
        row = next(r for r in rows if r["model"] == "zz-cache-model")
        assert row["cache_read_tokens"] == 800
        assert row["cache_create_tokens"] == 120
        assert row["cache_hit_ratio"] == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_cache_columns_null_preserved(self, pg_test_conn) -> None:
        # ZZ.A1 (#303-1): a pre-ZZ caller that never populates the
        # cache_* keys must land NULL (not 0) in PG so the dashboard
        # can distinguish "no data" from "genuine zero hits".
        await db.upsert_token_usage(pg_test_conn, _usage_fixture(
            model="pre-zz-model",
            cache_read_tokens=None,
            cache_create_tokens=None,
            cache_hit_ratio=None,
        ))
        rows = await db.list_token_usage(pg_test_conn)
        row = next(r for r in rows if r["model"] == "pre-zz-model")
        assert row["cache_read_tokens"] is None
        assert row["cache_create_tokens"] is None
        assert row["cache_hit_ratio"] is None

    @pytest.mark.asyncio
    async def test_numeric_types_round_trip(self, pg_test_conn) -> None:
        # Port uses explicit int()/float() casts on caller dict values
        # because track_tokens may hand us numpy ints or Decimals
        # depending on the LLM backend. Binding without cast makes
        # asyncpg raise a type mismatch. Regression guard for that
        # defence-in-depth cast.
        await db.upsert_token_usage(pg_test_conn, {
            "model": "cast-test",
            "input_tokens": "250",  # string → int
            "output_tokens": 100.0,  # float → int
            "total_tokens": 350,
            "cost": "0.5",  # string → float
            "request_count": 1,
            "avg_latency": 1,
            "last_used": "00:00:00",
        })
        rows = await db.list_token_usage(pg_test_conn)
        assert rows[0]["input_tokens"] == 250
        assert rows[0]["output_tokens"] == 100
        assert rows[0]["cost"] == pytest.approx(0.5)
