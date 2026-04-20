"""Phase-3-Runtime-v2 SP-3.11 — contract tests for ported decision_rules
db.py functions.

Replaces the SQLite-backed ``test_decision_rules_replace_load`` in
``test_db.py`` AND preserves the RLS coverage previously in
``tests/test_rls.py::TestDecisionRulesRLS``.

Coverage:
  * Two functions: load_decision_rules / replace_decision_rules.
  * **Atomic replace (load-bearing)**: second replace deletes all
    old rows from the current tenant's slice and inserts the new
    set; no partial state is ever observable by a concurrent reader.
  * **Tenant-scoped replace (load-bearing safety)**: replace called
    in Tenant A's context MUST NOT delete Tenant B's rules.
  * JSON field round-trip: ``auto_in_modes`` is a list; ``enabled``
    is stored as INTEGER 0/1 and coerced back to bool.
  * Empty-state contract: load on a fresh tenant returns ``[]``.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db
from backend.db_context import set_tenant_id


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


TENANT_A = "t-alpha"
TENANT_B = "t-beta"


def _rule(**overrides) -> dict:
    base = {
        "id": "r-test",
        "kind_pattern": "test/*",
        "severity": "routine",
        "auto_in_modes": [],
        "default_option_id": None,
        "priority": 100,
        "enabled": True,
        "note": "",
    }
    base.update(overrides)
    return base


# ─── Basic CRUD + JSON/bool round-trip ──────────────────────────


class TestDecisionRulesBasics:
    @pytest.mark.asyncio
    async def test_load_empty(self, pg_test_conn) -> None:
        assert await db.load_decision_rules(pg_test_conn) == []

    @pytest.mark.asyncio
    async def test_replace_then_load(self, pg_test_conn) -> None:
        await db.replace_decision_rules(pg_test_conn, [
            _rule(
                id="r1",
                kind_pattern="git_push/*",
                severity="destructive",
                auto_in_modes=["full_auto"],
                default_option_id="abort",
                priority=10,
                enabled=True,
                note="prod safety",
            ),
            _rule(
                id="r2",
                kind_pattern="stuck/*",
                severity="risky",
                auto_in_modes=["supervised", "full_auto"],
                default_option_id="switch_model",
                priority=100,
                enabled=False,
                note="",
            ),
        ])
        rules = await db.load_decision_rules(pg_test_conn)
        assert len(rules) == 2
        assert {r["id"] for r in rules} == {"r1", "r2"}
        r1 = next(r for r in rules if r["id"] == "r1")
        # JSON round-trip: auto_in_modes stored as TEXT, loaded as list.
        assert r1["auto_in_modes"] == ["full_auto"]
        # Bool round-trip: enabled stored as INTEGER 1, loaded as True.
        assert r1["enabled"] is True
        r2 = next(r for r in rules if r["id"] == "r2")
        assert r2["enabled"] is False

    @pytest.mark.asyncio
    async def test_replace_with_empty_list_wipes(
        self, pg_test_conn,
    ) -> None:
        await db.replace_decision_rules(pg_test_conn, [_rule(id="r-a")])
        assert len(await db.load_decision_rules(pg_test_conn)) == 1
        # Replace with empty list deletes everything in the tenant's slice.
        await db.replace_decision_rules(pg_test_conn, [])
        assert await db.load_decision_rules(pg_test_conn) == []

    @pytest.mark.asyncio
    async def test_note_truncated_at_240_chars(
        self, pg_test_conn,
    ) -> None:
        # Defence-in-depth truncation — stops a malicious or
        # misbehaving editor from bloating the rules table.
        long_note = "x" * 1000
        await db.replace_decision_rules(pg_test_conn, [
            _rule(id="r-long", note=long_note),
        ])
        rules = await db.load_decision_rules(pg_test_conn)
        assert len(rules[0]["note"]) == 240


# ─── Atomic replace contract ────────────────────────────────────


class TestDecisionRulesAtomicReplace:
    @pytest.mark.asyncio
    async def test_second_replace_deletes_old_rules(
        self, pg_test_conn,
    ) -> None:
        # Canonical "atomic swap" semantics — the second call's
        # DELETE must land before its INSERT-all, and both must
        # commit as a single unit.
        await db.replace_decision_rules(pg_test_conn, [
            _rule(id="r-old-1"),
            _rule(id="r-old-2"),
        ])
        await db.replace_decision_rules(pg_test_conn, [
            _rule(id="r-new"),
        ])
        rules = await db.load_decision_rules(pg_test_conn)
        assert {r["id"] for r in rules} == {"r-new"}

    @pytest.mark.asyncio
    async def test_replace_rolls_back_on_insert_failure(
        self, pg_test_conn,
    ) -> None:
        # LOAD-BEARING atomicity: if a mid-loop INSERT fails, the
        # earlier DELETE must roll back too — otherwise the tenant
        # ends up with no rules at all.
        #
        # We force the second INSERT to fail by omitting the
        # required ``kind_pattern`` field (NOT NULL).
        await db.replace_decision_rules(pg_test_conn, [
            _rule(id="r-pre-1"),
            _rule(id="r-pre-2"),
        ])
        import asyncpg
        bad_rules = [
            _rule(id="r-new-ok"),
            {"id": "r-new-bad"},  # missing kind_pattern → KeyError
        ]
        with pytest.raises((asyncpg.PostgresError, KeyError)):
            await db.replace_decision_rules(pg_test_conn, bad_rules)

        # After the failed replace, the pre-existing rules must still
        # be there — the DELETE inside the failed tx rolled back.
        # (pg_test_conn's outer savepoint makes this a sub-savepoint
        # verification; asyncpg nested transactions use PG SAVEPOINT.)
        rules = await db.load_decision_rules(pg_test_conn)
        assert {r["id"] for r in rules} == {"r-pre-1", "r-pre-2"}


# ─── Tenant isolation — preserves TestDecisionRulesRLS coverage ─


class TestDecisionRulesTenantIsolation:
    @pytest.mark.asyncio
    async def test_load_scoped_to_current_tenant(
        self, pg_test_conn,
    ) -> None:
        set_tenant_id(TENANT_A)
        await db.replace_decision_rules(pg_test_conn, [_rule(id="r-alpha")])
        set_tenant_id(TENANT_B)
        await db.replace_decision_rules(pg_test_conn, [_rule(id="r-beta")])

        set_tenant_id(TENANT_A)
        rows = await db.load_decision_rules(pg_test_conn)
        assert {r["id"] for r in rows} == {"r-alpha"}

    @pytest.mark.asyncio
    async def test_replace_scoped_to_current_tenant(
        self, pg_test_conn,
    ) -> None:
        # LOAD-BEARING: replace in Tenant A must NOT touch Tenant B's
        # rules. Regression guard — the pre-port code used
        # tenant_where on the DELETE, but the port must preserve that.
        set_tenant_id(TENANT_B)
        await db.replace_decision_rules(pg_test_conn, [
            _rule(id="r-beta-1"),
            _rule(id="r-beta-2"),
        ])

        set_tenant_id(TENANT_A)
        await db.replace_decision_rules(pg_test_conn, [
            _rule(id="r-alpha-1"),
        ])

        # Tenant A sees only its own rule.
        set_tenant_id(TENANT_A)
        rows_a = await db.load_decision_rules(pg_test_conn)
        assert {r["id"] for r in rows_a} == {"r-alpha-1"}

        # Tenant B's rules still intact — the replace did NOT touch them.
        set_tenant_id(TENANT_B)
        rows_b = await db.load_decision_rules(pg_test_conn)
        assert {r["id"] for r in rows_b} == {"r-beta-1", "r-beta-2"}

    @pytest.mark.asyncio
    async def test_insert_auto_fills_tenant_from_context(
        self, pg_test_conn,
    ) -> None:
        # Anti-forge: a caller who stuffs their own tenant_id into
        # the rule dict cannot actually influence what lands in the
        # DB — ``tenant_insert_value()`` wins.
        set_tenant_id(TENANT_A)
        await db.replace_decision_rules(pg_test_conn, [
            _rule(id="r-forge", tenant_id=TENANT_B),
        ])
        row = await pg_test_conn.fetchrow(
            "SELECT tenant_id FROM decision_rules WHERE id = $1",
            "r-forge",
        )
        assert row["tenant_id"] == TENANT_A
