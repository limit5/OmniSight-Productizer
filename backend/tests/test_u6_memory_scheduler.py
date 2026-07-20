"""U6-8 — memory-scheduler tests: gating, arm isolation, structural outcome, decay.

Offline: fake pool/conn drive the tick; the REAL L2 writer is spied at the
scheduler's seam (its own exactly-once/lock behavior is covered by
``test_u6_l2_writer.py``; the real end-to-end write runs in the increment's PG
verification + the CI PG-gated suites).
"""

from __future__ import annotations


import pytest

from backend import metrics
from backend.agents import u6_memory_scheduler as sched
from backend.agents.u6_l2_outcome import OutcomeKind
from backend.agents.u6_l2_writer import SessionEndReason, WriteResult

_ENV = sched._ENABLE_ENV
_L2_FLAG = "OMNISIGHT_U6_L2_WRITE"


class _FakeConn:
    """Dispatches on SQL shape: candidate GROUP BY / per-session messages
    (returned NEWEST-first, like the real DESC query) / the two decay UPDATEs
    / the leader try-lock. Records every call for assertions; an unmatched
    SQL fails LOUDLY (a query-shape edit must break dispatch visibly)."""

    def __init__(self, *, candidates=None, messages=None, expired=None, stale=None,
                 leader=True):
        self.candidates = list(candidates or [])
        self.messages = dict(messages or {})  # session_id -> rows (chronological)
        self.expired = list(expired or [])
        self.stale = list(stale or [])
        self.leader = leader
        self.calls: list[tuple[str, tuple]] = []
        self.decay_boom = False

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            self.calls.append(("SELECT:try_lock", args))
            return self.leader
        raise AssertionError(f"unexpected fetchval SQL: {sql[:60]}")

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            self.calls.append(("SELECT:unlock", args))
            return "SELECT 1"
        raise AssertionError(f"unexpected execute SQL: {sql[:60]}")

    async def fetch(self, sql, *args):
        self.calls.append((sql.strip().split()[0] + ":" + self._tag(sql), args))
        if "GROUP BY" in sql:
            return self.candidates
        if "ORDER BY timestamp DESC, id DESC" in sql:
            return list(reversed(self.messages.get(args[2], [])))  # newest-first
        if "state = 'superseded'" in sql:
            if self.decay_boom:
                raise RuntimeError("decay exploded")
            return self.expired
        if "state = 'rejected'" in sql:
            return self.stale
        raise AssertionError(f"unexpected SQL: {sql[:60]}")

    @staticmethod
    def _tag(sql: str) -> str:
        if "GROUP BY" in sql:
            return "candidates"
        if "ORDER BY timestamp DESC, id DESC" in sql:
            return "messages"
        if "superseded" in sql:
            return "decay_expired"
        return "decay_stale"


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_a):
                return False

        return _CM()


def _cand(session="s1", tenant="t1", user="u1", last_ts=100.0, n=4, bytes_=200):
    return {"tenant_id": tenant, "user_id": user, "session_id": session,
            "last_ts": last_ts, "n": n, "bytes": bytes_}


def _msgs():
    return [
        {"id": "m1", "role": "user", "content": "hello there"},
        {"id": "m2", "role": "orchestrator", "content": "hi!"},
        {"id": "m3", "role": "user", "content": "do the thing"},
    ]


async def _nosleep(_s: float) -> None:
    return None


# ── flag + loop contract (mirrors the metrics-refresh loop) ──────────────
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("1", True), ("TRUE", True), ("on", True)],
)
def test_scheduler_enabled_flag(monkeypatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv(_ENV, raising=False)
    else:
        monkeypatch.setenv(_ENV, value)
    assert sched.memory_scheduler_enabled() is expected


@pytest.mark.asyncio
async def test_disabled_loop_is_inert(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)

    def _boom_pool():
        raise AssertionError("get_pool called while disabled")

    ticks = await sched.run_memory_scheduler_loop(get_pool=_boom_pool, sleep=_nosleep)
    assert ticks == 0


@pytest.mark.asyncio
async def test_enabled_loop_ticks_and_counts(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    metrics.reset_for_tests()

    async def _spy_once(pool, **_kw):
        return sched.TickResult()

    monkeypatch.setattr(sched, "run_memory_scheduler_once", _spy_once)
    ticks = await sched.run_memory_scheduler_loop(
        get_pool=lambda: "POOL", max_ticks=3, sleep=_nosleep, interval_s=1.0,
    )
    assert ticks == 3
    assert metrics.REGISTRY.get_sample_value(
        "omnisight_u6_memory_scheduler_ticks_total", {"outcome": "ok"}
    ) == 3


@pytest.mark.asyncio
async def test_loop_survives_missing_pool(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    metrics.reset_for_tests()

    def _no_pool():
        raise RuntimeError("no pool yet")

    ticks = await sched.run_memory_scheduler_loop(
        get_pool=_no_pool, max_ticks=2, sleep=_nosleep, interval_s=1.0,
    )
    assert ticks == 2
    assert metrics.REGISTRY.get_sample_value(
        "omnisight_u6_memory_scheduler_ticks_total", {"outcome": "error"}
    ) == 2


@pytest.mark.asyncio
async def test_invalid_interval_raises(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    with pytest.raises(ValueError, match="interval_s"):
        await sched.run_memory_scheduler_loop(get_pool=lambda: "P", interval_s=0.0)


# ── tick: layered kill switches ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_l2_write_off_skips_summarize_but_decays(monkeypatch) -> None:
    monkeypatch.delenv(_L2_FLAG, raising=False)
    conn = _FakeConn(expired=[{"id": "a"}], stale=[{"id": "b"}, {"id": "c"}])
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1_000_000.0)
    tags = [t for t, _ in conn.calls]
    assert "SELECT:candidates" not in tags  # summarize arm never scanned
    assert out.summarized == 0
    assert out.decayed_expired == 1 and out.decayed_quarantine == 2
    assert out.ok


@pytest.mark.asyncio
async def test_summarize_calls_writer_with_canonical_order_and_structural_outcome(
    monkeypatch,
) -> None:
    monkeypatch.setenv(_L2_FLAG, "1")
    metrics.reset_for_tests()
    conn = _FakeConn(candidates=[_cand()], messages={"s1": _msgs()})
    seen = {}

    async def _spy_writer(c, *, scope, session_id, reason, outcome, messages,
                          token_count, model_fingerprint):
        seen.update(scope=scope, session_id=session_id, reason=reason,
                    outcome=outcome, messages=messages, token_count=token_count,
                    fingerprint=model_fingerprint)
        return WriteResult(True, 0, "csum_x", "written")

    monkeypatch.setattr(sched, "write_session_summary", _spy_writer)
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1_000_000.0)

    assert out.summarized == 1 and out.ok
    assert seen["session_id"] == "s1"
    assert seen["scope"].tenant_id == "t1" and seen["scope"].user_id == "u1"
    assert seen["reason"] is SessionEndReason.INACTIVITY_TIMEOUT
    # messages passed in the STABLE canonical order the fake returned
    assert seen["messages"] == [("m1", "hello there"), ("m2", "hi!"), ("m3", "do the thing")]
    assert seen["token_count"] == sum(len(c) for _, c in seen["messages"]) // 3
    assert seen["fingerprint"] == sched.STRUCTURAL_FINGERPRINT
    # v1 structural outcome: NO_OUTCOME + server-counted USER turns, unresolved
    assert seen["outcome"].outcome_kind is OutcomeKind.NO_OUTCOME
    assert seen["outcome"].turn_count == 2
    assert seen["outcome"].resolved is False
    assert seen["outcome"].tasks_created == () and seen["outcome"].topics == ()
    # candidate query received (cutoff, floor, cap) with cutoff = now - inactivity
    cand_args = dict(conn.calls)["SELECT:candidates"]
    assert cand_args[0] == 1_000_000.0 - sched._DEFAULT_INACTIVITY_S
    assert cand_args[2] == sched._MAX_SESSIONS_PER_TICK
    assert metrics.REGISTRY.get_sample_value(
        "omnisight_u6_l2_summaries_written_total", {"result": "written"}
    ) == 1


@pytest.mark.asyncio
async def test_duplicate_watermark_counts_separately(monkeypatch) -> None:
    monkeypatch.setenv(_L2_FLAG, "1")
    metrics.reset_for_tests()
    conn = _FakeConn(candidates=[_cand()], messages={"s1": _msgs()})

    async def _dup_writer(c, **_kw):
        return WriteResult(False, 0, "csum_x", "duplicate_watermark")

    monkeypatch.setattr(sched, "write_session_summary", _dup_writer)
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1e6)
    assert out.summarized == 0 and out.duplicates == 1 and out.ok


@pytest.mark.asyncio
async def test_one_broken_candidate_never_blocks_the_rest(monkeypatch) -> None:
    monkeypatch.setenv(_L2_FLAG, "1")
    metrics.reset_for_tests()
    conn = _FakeConn(
        candidates=[_cand("s-bad"), _cand("s-good")],
        messages={"s-bad": _msgs(), "s-good": _msgs()},
    )
    calls = []

    async def _flaky_writer(c, *, session_id, **_kw):
        calls.append(session_id)
        if session_id == "s-bad":
            raise RuntimeError("hostile session")
        return WriteResult(True, 0, "csum_y", "written")

    monkeypatch.setattr(sched, "write_session_summary", _flaky_writer)
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1e6)
    assert calls == ["s-bad", "s-good"]
    assert out.summarized == 1 and out.summarize_errors == 1
    assert not out.ok  # errors surface in the tick outcome (LOUD, not swallowed)


@pytest.mark.asyncio
async def test_empty_session_rows_skipped(monkeypatch) -> None:
    monkeypatch.setenv(_L2_FLAG, "1")
    conn = _FakeConn(candidates=[_cand("s-empty")], messages={})

    async def _never(c, **_kw):
        raise AssertionError("writer must not run for an empty session")

    monkeypatch.setattr(sched, "write_session_summary", _never)
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1e6)
    assert out.summarized == 0 and out.ok


# ── tick: leader gate ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_non_leader_tick_does_no_work(monkeypatch) -> None:
    monkeypatch.setenv(_L2_FLAG, "1")
    conn = _FakeConn(candidates=[_cand()], leader=False)
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1e6)
    assert out.leader is False and out.ok
    tags = [t for t, _ in conn.calls]
    assert tags == ["SELECT:try_lock"]  # no scan, no decay, no unlock needed


@pytest.mark.asyncio
async def test_leader_lock_released_even_on_arm_failure(monkeypatch) -> None:
    monkeypatch.delenv(_L2_FLAG, raising=False)
    conn = _FakeConn(expired=[{"id": "x"}])
    conn.decay_boom = True
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1e6)
    tags = [t for t, _ in conn.calls]
    assert tags[0] == "SELECT:try_lock" and tags[-1] == "SELECT:unlock"
    assert out.arm_errors == ("decay",)


@pytest.mark.asyncio
async def test_loop_counts_skipped_for_non_leader(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    metrics.reset_for_tests()

    async def _not_leader(pool, **_kw):
        return sched.TickResult(leader=False)

    monkeypatch.setattr(sched, "run_memory_scheduler_once", _not_leader)
    ticks = await sched.run_memory_scheduler_loop(
        get_pool=lambda: "POOL", max_ticks=2, sleep=_nosleep, interval_s=1.0,
    )
    assert ticks == 2
    assert metrics.REGISTRY.get_sample_value(
        "omnisight_u6_memory_scheduler_ticks_total", {"outcome": "skipped"}
    ) == 2


# ── tick: oversize skip (audit F1) ───────────────────────────────────────
@pytest.mark.asyncio
async def test_oversize_session_skipped_before_any_fetch(monkeypatch) -> None:
    monkeypatch.setenv(_L2_FLAG, "1")
    metrics.reset_for_tests()
    huge = _cand("s-huge", bytes_=sched._MAX_SESSION_BYTES + 1)
    ok = _cand("s-ok")
    conn = _FakeConn(candidates=[huge, ok], messages={"s-ok": _msgs(), "s-huge": _msgs()})

    async def _spy_writer(c, *, session_id, **_kw):
        return WriteResult(True, 0, "csum", "written")

    monkeypatch.setattr(sched, "write_session_summary", _spy_writer)
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1e6)
    assert out.skipped_oversize == 1 and out.summarized == 1 and out.ok
    # the huge session's MESSAGES were never fetched (only s-ok's were)
    msg_fetches = [args for tag, args in conn.calls if tag == "SELECT:messages"]
    assert [a[2] for a in msg_fetches] == ["s-ok"]
    assert metrics.REGISTRY.get_sample_value(
        "omnisight_u6_l2_summaries_written_total", {"result": "skipped_oversize"}
    ) == 1


# ── tick: decay ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_decay_params_and_counts(monkeypatch) -> None:
    monkeypatch.delenv(_L2_FLAG, raising=False)
    monkeypatch.setenv(sched._QUARANTINE_TTL_ENV, "7")
    metrics.reset_for_tests()
    conn = _FakeConn(expired=[{"id": "x"}], stale=[])
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1e6)
    by_tag = dict(conn.calls)
    today_arg, batch_arg = by_tag["UPDATE:decay_expired"]
    assert len(today_arg) == 10 and today_arg[4] == "-"  # YYYY-MM-DD (UTC) date
    assert batch_arg == sched._DECAY_BATCH
    assert by_tag["UPDATE:decay_stale"] == (7.0, sched._DECAY_BATCH)
    assert out.decayed_expired == 1 and out.decayed_quarantine == 0
    assert metrics.REGISTRY.get_sample_value(
        "omnisight_u6_l3_decay_total", {"kind": "expired"}
    ) == 1


@pytest.mark.asyncio
async def test_decay_failure_is_arm_isolated(monkeypatch) -> None:
    monkeypatch.setenv(_L2_FLAG, "1")
    conn = _FakeConn(candidates=[], expired=[{"id": "x"}])
    conn.decay_boom = True
    out = await sched.run_memory_scheduler_once(_FakePool(conn), now_ts=1e6)
    assert out.arm_errors == ("decay",)
    assert not out.ok
    # the summarize arm still ran (its candidate scan happened before the boom)
    assert "SELECT:candidates" in dict(conn.calls)


# ── helpers ──────────────────────────────────────────────────────────────
def test_build_outcome_counts_user_turns_and_clamps() -> None:
    rows = [{"role": "user"}, {"role": "operator"}, {"role": "orchestrator"},
            {"role": "human"}, {"role": "system"}]
    out = sched._build_outcome(rows)
    assert out.turn_count == 3
    big = [{"role": "user"}] * 10
    assert sched._build_outcome(big).turn_count == 10  # no distortion below the cap


def test_env_float_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("X_NUM", "not-a-number")
    assert sched._env_float("X_NUM", 5.0, minimum=1.0) == 5.0
    monkeypatch.setenv("X_NUM", "0.5")
    assert sched._env_float("X_NUM", 5.0, minimum=1.0) == 5.0  # below minimum
    monkeypatch.setenv("X_NUM", "120")
    assert sched._env_float("X_NUM", 5.0, minimum=1.0) == 120.0
    monkeypatch.delenv("X_NUM", raising=False)
    assert sched._env_float("X_NUM", 5.0, minimum=1.0) == 5.0


def test_tick_result_ok_logic() -> None:
    assert sched.TickResult().ok
    assert not sched.TickResult(summarize_errors=1).ok
    assert not sched.TickResult(arm_errors=("decay",)).ok


# ── real-PG tick (audit F6/F7: the real SQL must execute somewhere) ──────
@pytest.mark.asyncio
async def test_real_pg_tick_end_to_end(pg_test_pool, monkeypatch) -> None:
    """One REAL tick on live PG at alembic head: the candidate GROUP-BY +
    correlated NOT EXISTS, the newest-N window, the REAL writer (advisory
    lock + exactly-once), and both decay UPDATEs execute for real."""
    import time as _time
    import uuid as _uuid

    monkeypatch.setenv(_L2_FLAG, "1")
    tenant = "t-u68t-" + _uuid.uuid4().hex[:8]
    session = "sess-" + _uuid.uuid4().hex[:8]
    now = _time.time()
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1,$1,'test') ON CONFLICT DO NOTHING",
            tenant,
        )
        for i, (role, content, ts) in enumerate([
            ("user", "hello", now - 7500),
            ("orchestrator", "hi!", now - 7450),
            ("user", "check the fleet", now - 7200),
        ]):
            await conn.execute(
                "INSERT INTO chat_messages (id, user_id, session_id, role, content, timestamp, tenant_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                f"m-{session}-{i}", "u-test", session, role, content, ts, tenant,
            )
    try:
        out = await sched.run_memory_scheduler_once(pg_test_pool)
        assert out.leader and out.ok, out
        assert out.summarized == 1, out
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT revision, session_end_reason, model_fingerprint "
                "FROM chat_session_summaries WHERE tenant_id=$1 AND session_id=$2",
                tenant, session,
            )
        assert row is not None
        assert row["session_end_reason"] == "inactivity_timeout"
        assert row["model_fingerprint"] == sched.STRUCTURAL_FINGERPRINT
        # a second tick is a no-op for this covered session (work-bound)
        out2 = await sched.run_memory_scheduler_once(pg_test_pool)
        assert out2.summarized == 0 and out2.duplicates == 0 and out2.ok, out2
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM chat_session_summaries WHERE tenant_id = $1", tenant
            )
            await conn.execute("DELETE FROM chat_messages WHERE tenant_id = $1", tenant)
