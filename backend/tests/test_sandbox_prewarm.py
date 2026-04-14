"""Phase 67-C S1 — speculative pre-warm registry + lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from backend import sandbox_prewarm as pw
from backend.dag_schema import DAG, Task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _reset():
    pw._reset_for_tests()
    yield
    pw._reset_for_tests()


def _t(task_id: str, *, tier: str = "t1", toolchain: str = "cmake",
       depends_on=None, expected_output: str | None = None) -> Task:
    return Task(
        task_id=task_id,
        description=f"t {task_id}",
        required_tier=tier,
        toolchain=toolchain,
        inputs=[],
        expected_output=expected_output or f"build/{task_id}.bin",
        depends_on=depends_on or [],
    )


def _dag(tasks: list[Task], dag_id: str = "REQ-pw") -> DAG:
    return DAG(dag_id=dag_id, tasks=tasks)


@dataclass
class _FakeInfo:
    agent_id: str
    container_id: str = "cid-fake"


def _make_starter(store: dict[str, _FakeInfo]):
    """Return an injectable starter that records every call."""
    async def starter(agent_id, workspace_path):
        info = _FakeInfo(agent_id=agent_id)
        store[agent_id] = info
        return info
    return starter


def _make_stopper(stopped: list[str]):
    async def stopper(agent_id):
        stopped.append(agent_id)
        return True
    return stopper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  pick_prewarm_candidates
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_picks_only_in_degree_zero():
    dag = _dag([
        _t("A"),
        _t("B", depends_on=["A"]),
    ])
    ids = [t.task_id for t in pw.pick_prewarm_candidates(dag, depth=5)]
    assert ids == ["A"]


def test_respects_depth_cap():
    dag = _dag([_t("A"), _t("B"), _t("C"), _t("D")])
    # 4 ready tasks but depth=2 → only first 2.
    ids = [t.task_id for t in pw.pick_prewarm_candidates(dag, depth=2)]
    assert ids == ["A", "B"]


def test_depth_zero_returns_nothing():
    dag = _dag([_t("A")])
    assert pw.pick_prewarm_candidates(dag, depth=0) == []


def test_skips_tier_3_and_networked():
    """Only Tier-1 tasks benefit from pre-warm per the module doc."""
    dag = _dag([
        _t("A", tier="t3", toolchain="flash_board",
           expected_output="logs/a.log"),
        _t("B", tier="networked", toolchain="pip",
           expected_output="data/b.tar.gz"),
        _t("C"),  # t1 default
    ])
    ids = [t.task_id for t in pw.pick_prewarm_candidates(dag, depth=5)]
    assert ids == ["C"]


def test_default_depth_is_two(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_PREWARM_DEPTH", raising=False)
    dag = _dag([_t("A"), _t("B"), _t("C")])
    ids = [t.task_id for t in pw.pick_prewarm_candidates(dag)]
    assert ids == ["A", "B"]


@pytest.mark.parametrize("raw,expected", [
    ("1", 1), ("3", 3), ("8", 8),  # clamped to 8 max
    ("99", 8), ("-1", 0), ("bad", 2),
])
def test_depth_env_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("OMNISIGHT_PREWARM_DEPTH", raw)
    assert pw._prewarm_depth() == expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  prewarm_for — launch lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_prewarm_launches_and_registers(tmp_path):
    started: dict = {}
    dag = _dag([_t("A"), _t("B"), _t("C", depends_on=["A"])])
    slots = await pw.prewarm_for(
        dag, tmp_path, depth=3, starter=_make_starter(started),
    )
    # Two ready tasks → two slots.
    assert {s.task_id for s in slots} == {"A", "B"}
    # Registry mirrors.
    assert set(pw.snapshot().keys()) == {"A", "B"}
    # Each slot has a distinct synthesised agent_id.
    assert slots[0].agent_id != slots[1].agent_id


@pytest.mark.asyncio
async def test_prewarm_uses_image_trust_via_start_container_injection(tmp_path):
    """The starter we inject IS the contract — tests that we route
    through the caller's passed function (which in prod is
    container.start_container carrying the trust check)."""
    calls = []

    async def starter(agent_id, workspace_path):
        calls.append(agent_id)
        return _FakeInfo(agent_id=agent_id)

    await pw.prewarm_for(
        _dag([_t("A")]), tmp_path, depth=1, starter=starter,
    )
    assert len(calls) == 1
    assert calls[0].startswith("prewarm-A-")


@pytest.mark.asyncio
async def test_prewarm_second_call_deduplicates(tmp_path):
    """Calling prewarm_for twice for the same DAG must not launch
    duplicate containers for the same task_id."""
    started: dict = {}
    dag = _dag([_t("A")])
    s1 = await pw.prewarm_for(dag, tmp_path, depth=2,
                                starter=_make_starter(started))
    s2 = await pw.prewarm_for(dag, tmp_path, depth=2,
                                starter=_make_starter(started))
    assert len(started) == 1  # only one container started total
    assert s1[0].agent_id == s2[0].agent_id  # same slot returned


@pytest.mark.asyncio
async def test_prewarm_start_failure_is_swallowed(tmp_path, caplog):
    """If start_container raises, pre-warm skips that task and continues
    with the rest — pre-warm is an optimisation, not correctness."""
    import logging

    async def starter(agent_id, workspace_path):
        if "A" in agent_id:
            raise RuntimeError("image trust rejected")
        return _FakeInfo(agent_id=agent_id)

    caplog.set_level(logging.WARNING, logger="backend.sandbox_prewarm")
    dag = _dag([_t("A"), _t("B")])
    slots = await pw.prewarm_for(dag, tmp_path, depth=2, starter=starter)
    # Only B succeeded.
    assert [s.task_id for s in slots] == ["B"]
    assert any("image trust" in r.getMessage() for r in caplog.records)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  consume
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_consume_hits_and_pops(tmp_path):
    started: dict = {}
    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                          starter=_make_starter(started))
    slot = await pw.consume("A")
    assert slot is not None
    assert slot.task_id == "A"
    # Next consume misses.
    assert await pw.consume("A") is None


@pytest.mark.asyncio
async def test_consume_miss_returns_none():
    assert await pw.consume("Z") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  cancel_all
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_cancel_all_stops_remaining(tmp_path):
    started: dict = {}
    stopped: list[str] = []
    await pw.prewarm_for(_dag([_t("A"), _t("B")]), tmp_path, depth=2,
                          starter=_make_starter(started))
    n = await pw.cancel_all(stopper=_make_stopper(stopped))
    assert n == 2
    assert sorted(stopped) == sorted([s.agent_id for s in started.values()])
    assert pw.snapshot() == {}


@pytest.mark.asyncio
async def test_cancel_all_does_not_touch_consumed(tmp_path):
    """Consumed slots are handed off to the real dispatcher. cancel_all
    must NOT then stop those (would kill a live task)."""
    started: dict = {}
    stopped: list[str] = []
    await pw.prewarm_for(_dag([_t("A"), _t("B")]), tmp_path, depth=2,
                          starter=_make_starter(started))
    consumed = await pw.consume("A")
    assert consumed is not None

    n = await pw.cancel_all(stopper=_make_stopper(stopped))
    assert n == 1  # only B left to stop
    assert consumed.agent_id not in stopped


@pytest.mark.asyncio
async def test_cancel_all_survives_stopper_failure(tmp_path):
    """If stop_container raises, we still clear the registry and
    continue to other slots."""
    started: dict = {}
    attempts: list[str] = []

    async def flaky_stop(agent_id):
        attempts.append(agent_id)
        raise RuntimeError("docker rm failed")

    await pw.prewarm_for(_dag([_t("A"), _t("B")]), tmp_path, depth=2,
                          starter=_make_starter(started))
    n = await pw.cancel_all(stopper=flaky_stop)
    # Both attempted, neither counted as successful stop.
    assert n == 0
    assert len(attempts) == 2
    assert pw.snapshot() == {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_metrics_roll_up_through_lifecycle(tmp_path):
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()

    started: dict = {}
    await pw.prewarm_for(_dag([_t("A"), _t("B")]), tmp_path, depth=2,
                          starter=_make_starter(started))
    await pw.consume("A")               # hit
    await pw.consume("C")               # miss
    await pw.cancel_all(stopper=_make_stopper([]))  # cancelled B

    started_samples = list(m.prewarm_started_total.collect()[0].samples)
    started_total = sum(s.value for s in started_samples
                        if s.name.endswith("_total"))
    assert started_total == 2

    consumed = {s.labels.get("result"): s.value
                for s in m.prewarm_consumed_total.collect()[0].samples
                if s.name.endswith("_total")}
    assert consumed.get("hit") == 1
    assert consumed.get("miss") == 1
    assert consumed.get("cancelled") == 1


@pytest.mark.asyncio
async def test_metrics_record_start_error(tmp_path):
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()

    async def starter(agent_id, workspace_path):
        raise RuntimeError("boom")

    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1, starter=starter)

    consumed = {s.labels.get("result"): s.value
                for s in m.prewarm_consumed_total.collect()[0].samples
                if s.name.endswith("_total")}
    assert consumed.get("start_error") == 1
