"""β-2 (leg-2) — memory promotion eval scheduler: gate, name-parse, live-set
hash, the no-LLM reverted_later terminal, and the PG-gated scan-and-evaluate.

The suite/eval end-to-end (scripted ask_fn through the REAL
run_plan_triage_eval + the authored plan_triage suite) lives in
test_plan_triage_suite.py; here we lock the scheduler contract itself.
"""

from __future__ import annotations

import json

import pytest

from backend.agents import memory_promotion_scheduler as mps


# ── gate + inert-when-disabled ──────────────────────────────────────────────

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_MEMORY_PROMOTION_EVAL", raising=False)
    assert mps.promotion_eval_enabled() is False
    monkeypatch.setenv("OMNISIGHT_MEMORY_PROMOTION_EVAL", "1")
    assert mps.promotion_eval_enabled() is True


@pytest.mark.asyncio
async def test_loop_inert_when_disabled_never_touches_pool(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_MEMORY_PROMOTION_EVAL", raising=False)

    def _boom():
        raise AssertionError("get_pool must NOT be called when disabled")

    assert await mps.run_promotion_eval_loop(get_pool=_boom) == 0


# ── version-name → ticket parse (the ONLY trusted version→ticket link) ──────

def test_ticket_from_version_name_matrix():
    assert mps.ticket_from_version_name("ground-truth:OP-2462") == "OP-2462"
    assert mps.ticket_from_version_name("ground-truth:change-99") is None
    assert mps.ticket_from_version_name("u4j-pilot") is None
    assert mps.ticket_from_version_name("") is None
    # A ticket-shaped SUFFIX must not fool the anchor.
    assert mps.ticket_from_version_name("x ground-truth:OP-1") is None


# ── live-set hash ───────────────────────────────────────────────────────────

class _SnapConn:
    def __init__(self, membership=None) -> None:
        self._membership = membership

    async def fetchrow(self, *_a, **_kw):
        if self._membership is None:
            return None
        return {"membership": self._membership}


@pytest.mark.asyncio
async def test_live_set_hash_empty_and_nonempty():
    from backend.learned_item_publication import compute_live_set_hash

    empty = await mps.current_live_set_hash(_SnapConn(None), scope_key="tenant:t")
    assert empty == compute_live_set_hash([])

    member = {
        "version_id": "v1", "rendered_payload_sha256": "a" * 64,
        "delivery_mode": "retrieved", "publication_event_seq": 1,
    }
    got = await mps.current_live_set_hash(
        _SnapConn(json.dumps([member])), scope_key="tenant:t")
    assert got == compute_live_set_hash([member])
    assert got != empty


# ── reverted_later terminal (no LLM) ────────────────────────────────────────

class _CaptureConn:
    """fetchrow → no snapshot; fetchval → reverted EXISTS; execute captured."""

    def __init__(self, reverted: bool) -> None:
        self._reverted = reverted
        self.executed: list[tuple] = []

    async def fetchrow(self, *_a, **_kw):
        return None

    async def fetchval(self, sql, *_a, **_kw):
        assert "EXISTS" in sql
        return self._reverted

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytest.mark.asyncio
async def test_reverted_version_terminalizes_without_llm(monkeypatch):
    async def _boom_eval(*_a, **_kw):
        raise AssertionError("run_plan_triage_eval must NOT run for a reverted ticket")

    import backend.memory_promotion_eval as mpe
    monkeypatch.setattr(mpe, "run_plan_triage_eval", _boom_eval)

    conn = _CaptureConn(reverted=True)
    decision = await mps.evaluate_version(
        conn,
        {"id": "v-1", "name": "ground-truth:OP-7", "audience": "tenant",
         "tenant_id": "omnisight-self", "rendered_payload": "x",
         "rendered_payload_sha256": "y"},
        now="2026-07-21T00:00:00+00:00",
        ask_state={"client": None, "ask_fn": None},
    )
    assert decision == "reject"
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "memory_eval_runs" in sql
    assert "reject" in args  # decision column
    assert args[5] is None  # model_fingerprint NULL — no client was pinned
    stat = json.loads(args[7])
    assert stat["reason"] == "reverted_later"
    assert stat["ticket"] == "OP-7"


@pytest.mark.asyncio
async def test_null_rendered_payload_terminalizes(monkeypatch):
    """Wiring MINOR-8: a version the sanctioned writer could never have
    produced fails closed as an attempt-capped infra_invalid row."""
    conn = _CaptureConn(reverted=False)
    decision = await mps.evaluate_version(
        conn,
        {"id": "v-null", "name": "ground-truth:OP-9", "audience": "tenant",
         "tenant_id": "omnisight-self", "rendered_payload": None,
         "rendered_payload_sha256": None},
        now="2026-07-21T00:00:00+00:00",
        ask_state={"client": None, "ask_fn": None},
    )
    assert decision == "infra_invalid"
    sql, args = conn.executed[0]
    assert "memory_eval_runs" in sql
    stat = json.loads(args[7])
    assert stat["reason"] == "null_rendered_payload"


@pytest.mark.asyncio
async def test_preflight_no_client_aborts_before_any_scan(monkeypatch):
    """Gate F1: a global no-client outage aborts the tick BEFORE the version
    loop — zero rows written, zero per-version attempts burned."""
    monkeypatch.setattr(
        mps, "_build_ask_state",
        lambda: {"client": None, "ask_fn": None, "neg_case_ids": (),
                 "preflight": "no_client"},
    )

    class _BoomPool:
        def acquire(self):
            raise AssertionError("pool must not be touched on preflight abort")

    result = await mps.run_promotion_eval_once(_BoomPool())
    assert result["preflight"] == "no_client"
    assert result["scanned"] == 0


@pytest.mark.asyncio
async def test_non_ticket_version_skips_revert_check(monkeypatch):
    """Pilot/no-ticket versions go straight to the eval (no join)."""
    called = {}

    async def _fake_eval(conn, **kw):
        called.update(kw)

        class _O:
            decision = "insufficient_evidence"

        return _O()

    import backend.memory_promotion_eval as mpe
    monkeypatch.setattr(mpe, "run_plan_triage_eval", _fake_eval)

    class _NoJoinConn(_SnapConn):
        async def fetchval(self, *_a, **_kw):
            raise AssertionError("reverted_later join must not run without a ticket")

    decision = await mps.evaluate_version(
        _NoJoinConn(None),
        {"id": "v-2", "name": "u4j-pilot", "audience": "tenant",
         "tenant_id": "omnisight-self", "rendered_payload": "x",
         "rendered_payload_sha256": "y"},
        now="2026-07-21T00:00:00+00:00",
        ask_state={"client": None, "ask_fn": None},
    )
    assert decision == "insufficient_evidence"
    assert called["version_id"] == "v-2"


# ── PG-gated: scan finds unevaluated versions, skips evaluated ──────────────

class _PoolWrap:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _CM()


@pytest.mark.asyncio
async def test_scan_evaluates_once_then_skips(pg_test_conn, monkeypatch):
    from backend import db as _db
    from backend.learned_item_producer import submit_quarantined_version

    await pg_test_conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ('omnisight-self', 'x', 'free') "
        "ON CONFLICT DO NOTHING"
    )
    _ = _db  # tenants seeded above; producer writes the version
    res = await submit_quarantined_version(
        pg_test_conn,
        payload={"scope": "s", "procedure_steps": ["p"]},
        kind="playbook", audience="tenant", tenant_id="omnisight-self",
        created_by="ground_truth_curator", name="ground-truth:OP-55",
        now="2026-07-21T00:00:00+00:00",
    )

    async def _fake_eval(conn, *, version_id, live_set_hash, now, **_kw):
        # Write a minimal terminal row exactly like the real eval would.
        import uuid as _uuid

        await conn.execute(
            "INSERT INTO memory_eval_runs (id, version_id, eval_kind, "
            "suite_sha256, live_set_hash, model_fingerprint, decision, "
            "stat_summary, ran_at) VALUES ($1,$2,'plan_triage',NULL,$3,'',"
            "'insufficient_evidence','{}',now())",
            str(_uuid.uuid4()), version_id, live_set_hash,
        )

        class _O:
            decision = "insufficient_evidence"

        return _O()

    import backend.memory_promotion_eval as mpe
    monkeypatch.setattr(mpe, "run_plan_triage_eval", _fake_eval)
    monkeypatch.setenv("OMNISIGHT_MEMORY_PROMOTION_EVAL", "1")
    # Preflight would abort on no_client in the test env — stub it OK.
    monkeypatch.setattr(
        mps, "_build_ask_state",
        lambda: {"client": None, "ask_fn": lambda *a: None,
                 "neg_case_ids": (), "preflight": "ok"},
    )

    first = await mps.run_promotion_eval_once(_PoolWrap(pg_test_conn))
    assert first["scanned"] == 1
    assert first.get("insufficient_evidence") == 1

    rows = await pg_test_conn.fetch(
        "SELECT decision FROM memory_eval_runs WHERE version_id = $1",
        res.version_id,
    )
    assert len(rows) == 1

    second = await mps.run_promotion_eval_once(_PoolWrap(pg_test_conn))
    assert second["scanned"] == 0  # terminal row present → not re-picked
