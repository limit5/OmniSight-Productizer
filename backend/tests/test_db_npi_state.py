"""Phase-3-Runtime-v2 SP-3.7 — contract tests for ported npi_state
db.py functions.

Replaces the SQLite-backed ``test_npi_state_roundtrip`` in
``test_db.py``. The functions require asyncpg.Connection now.

Coverage:
  * Two functions: get_npi_state / save_npi_state.
  * Single-row table (id='current') — ON CONFLICT DO UPDATE contract.
  * JSON round-trip fidelity for nested phase/milestone structures —
    the routers/system.py handlers rely on modify-in-place + save.
  * Empty-state return: ``get_npi_state`` on a fresh DB returns
    ``{}`` (not None, not error) — the router handlers branch on
    this to decide whether to seed from npi_lifecycle.json.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db


class TestNpiStateRoundTrip:
    @pytest.mark.asyncio
    async def test_empty_returns_empty_dict(self, pg_test_conn) -> None:
        # pg_test_conn truncates npi_state inside its outer tx, so
        # the initial read always sees zero rows. The contract is
        # {} (not None, not KeyError) — routers depend on this for
        # the "first load from config file" branch.
        assert await db.get_npi_state(pg_test_conn) == {}

    @pytest.mark.asyncio
    async def test_save_then_get(self, pg_test_conn) -> None:
        await db.save_npi_state(pg_test_conn, {
            "phase": "MVP", "progress": 0.42,
        })
        got = await db.get_npi_state(pg_test_conn)
        assert got == {"phase": "MVP", "progress": 0.42}

    @pytest.mark.asyncio
    async def test_save_overwrites_existing(self, pg_test_conn) -> None:
        # Single-row semantics: second save replaces, no new rows.
        await db.save_npi_state(pg_test_conn, {"phase": "MVP"})
        await db.save_npi_state(pg_test_conn, {"phase": "GA"})
        got = await db.get_npi_state(pg_test_conn)
        assert got == {"phase": "GA"}

    @pytest.mark.asyncio
    async def test_single_row_invariant(self, pg_test_conn) -> None:
        # The ON CONFLICT (id) target enforces at most one row.
        # Regression guard: if someone replaces the INSERT with an
        # unconstrained append, the table would grow unbounded.
        for phase in ("A", "B", "C"):
            await db.save_npi_state(pg_test_conn, {"phase": phase})
        count = await pg_test_conn.fetchval(
            "SELECT COUNT(*) FROM npi_state"
        )
        assert count == 1


class TestNpiStateJsonFidelity:
    @pytest.mark.asyncio
    async def test_nested_phases_round_trip(self, pg_test_conn) -> None:
        # The real payload the routers/system.py handlers persist is
        # a nested structure: phases with milestones, each with
        # status + due_date. Lock the round-trip shape so a JSON
        # codec change can't silently corrupt it.
        payload = {
            "business_model": "odm",
            "current_phase_id": "phase-2",
            "phases": [
                {
                    "id": "phase-1",
                    "status": "completed",
                    "target_date": "2026-04-01",
                    "milestones": [
                        {"id": "m-1", "status": "completed", "due_date": "2026-03-15"},
                    ],
                },
                {
                    "id": "phase-2",
                    "status": "active",
                    "milestones": [
                        {"id": "m-2", "status": "in_progress"},
                        {"id": "m-3", "status": "pending"},
                    ],
                },
            ],
        }
        await db.save_npi_state(pg_test_conn, payload)
        got = await db.get_npi_state(pg_test_conn)
        assert got == payload

    @pytest.mark.asyncio
    async def test_empty_phases_list_preserved(self, pg_test_conn) -> None:
        # The default-initialised state has ``phases: []`` — must
        # survive round-trip as an empty list, not collapse to
        # missing-key or None.
        await db.save_npi_state(pg_test_conn, {
            "business_model": "odm",
            "phases": [],
            "current_phase_id": None,
        })
        got = await db.get_npi_state(pg_test_conn)
        assert got["phases"] == []
        assert got["current_phase_id"] is None
