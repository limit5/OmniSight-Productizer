"""AB.9 — Batch eligibility + lane routing + auto-batch accumulator tests.

Locks:
  - DEFAULT_ROUTING covers HD parsers / HD diff / HD sensor KB / HD CVE /
    L4 invariants / TODO routine / chat UI / w14 sandbox / hd_bringup_live
    / planning / hd_bringup / generic_dev with the expected eligibility flags
  - EligibilityRule rejects realtime_required + batch_eligible mutex
  - EligibilityRegistry: get returns default, override takes precedence,
    clear_override, unknown task_kind falls back to generic_dev
  - route(): default path picks batch when eligible, force_lane override
    works, realtime_required vetoes force_lane="batch"
  - AutoBatchAccumulator: add accumulates, threshold triggers flush,
    age timeout triggers flush_due, flush_all force-flushes everything,
    bucketed by (task_kind, model, tools_signature) tuple,
    realtime task forwarded immediately not buffered

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §7
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents.batch_dispatcher import BatchableTask
from backend.agents.batch_eligibility import (
    DEFAULT_ROUTING,
    AutoBatchAccumulator,
    EligibilityRegistry,
    EligibilityRule,
)


# ─── DEFAULT_ROUTING coverage ────────────────────────────────────


def test_default_routing_covers_hd_parsers():
    for kind in ("hd_parse_kicad", "hd_parse_altium", "hd_parse_odb", "hd_parse_eagle"):
        assert kind in DEFAULT_ROUTING
        assert DEFAULT_ROUTING[kind].batch_eligible


def test_default_routing_covers_l4_ci_tasks():
    for kind in ("l4_determinism_regression", "l4_adversarial_ci"):
        assert kind in DEFAULT_ROUTING
        assert DEFAULT_ROUTING[kind].batch_eligible
        assert DEFAULT_ROUTING[kind].batch_priority == "P3"


def test_default_routing_realtime_required_set_correctly():
    realtime_kinds = {"chat_ui", "w14_sandbox", "hd_bringup_live"}
    for kind in realtime_kinds:
        rule = DEFAULT_ROUTING[kind]
        assert rule.realtime_required, f"{kind} should be realtime_required"
        assert not rule.batch_eligible, f"{kind} cannot be batch_eligible"


def test_default_routing_planning_soft_realtime():
    """planning prefers realtime but is NOT realtime_required (operator can override)."""
    rule = DEFAULT_ROUTING["planning"]
    assert not rule.batch_eligible
    assert not rule.realtime_required


def test_default_routing_todo_routine_has_threshold():
    """todo_routine should auto-batch — ship default threshold."""
    rule = DEFAULT_ROUTING["todo_routine"]
    assert rule.batch_eligible
    assert rule.auto_batch_threshold is not None
    assert rule.auto_batch_threshold >= 5


def test_default_routing_no_kind_overlaps():
    """Each kind appears at most once."""
    seen: set[str] = set()
    for kind, rule in DEFAULT_ROUTING.items():
        assert rule.task_kind == kind, f"key {kind} != rule.task_kind {rule.task_kind}"
        assert kind not in seen
        seen.add(kind)


# ─── EligibilityRule validation ──────────────────────────────────


def test_eligibility_rule_realtime_required_with_batch_eligible_raises():
    with pytest.raises(ValueError, match="cannot be both"):
        EligibilityRule(
            task_kind="bogus",
            batch_eligible=True,
            realtime_required=True,
        )


def test_eligibility_rule_realtime_required_with_batch_false_ok():
    """The valid combo: realtime_required=True + batch_eligible=False."""
    rule = EligibilityRule(
        task_kind="chat",
        batch_eligible=False,
        realtime_required=True,
    )
    assert rule.realtime_required


# ─── EligibilityRegistry ─────────────────────────────────────────


def test_registry_get_returns_default():
    reg = EligibilityRegistry()
    rule = reg.get("hd_parse_kicad")
    assert rule.task_kind == "hd_parse_kicad"
    assert rule.batch_eligible


def test_registry_get_unknown_falls_back_to_generic_dev():
    reg = EligibilityRegistry()
    rule = reg.get("invented_kind_xyz")
    # Falls through to generic_dev's behaviour even though task_kind
    # field shows "generic_dev" (the rule we returned, not the
    # requested kind).
    assert rule.task_kind == "generic_dev"
    assert not rule.batch_eligible


def test_registry_override_takes_precedence():
    reg = EligibilityRegistry()
    custom = EligibilityRule(
        task_kind="hd_parse_kicad",
        batch_eligible=False,
        batch_priority="P0",
        reason="emergency: real-time only this week",
    )
    reg.set_override(custom)
    rule = reg.get("hd_parse_kicad")
    assert not rule.batch_eligible
    assert rule.reason.startswith("emergency")


def test_registry_clear_override():
    reg = EligibilityRegistry()
    reg.set_override(EligibilityRule(
        task_kind="hd_parse_kicad",
        batch_eligible=False,
        reason="x",
    ))
    assert reg.clear_override("hd_parse_kicad") is True
    # default restored
    assert reg.get("hd_parse_kicad").batch_eligible
    # second clear returns False
    assert reg.clear_override("hd_parse_kicad") is False


def test_registry_list_overrides():
    reg = EligibilityRegistry()
    reg.set_override(EligibilityRule("foo", batch_eligible=False, reason="x"))
    overrides = reg.list_overrides()
    assert "foo" in overrides


# ─── route() decisions ───────────────────────────────────────────


def test_route_default_batch_eligible():
    reg = EligibilityRegistry()
    decision = reg.route("hd_parse_kicad")
    assert decision.lane == "batch"
    assert decision.priority == "P2"
    assert "default batch-eligible" in decision.reason


def test_route_default_realtime():
    reg = EligibilityRegistry()
    decision = reg.route("chat_ui")
    assert decision.lane == "realtime"
    assert decision.priority == "P0"
    assert "default realtime" in decision.reason


def test_route_force_lane_realtime_overrides_batch_default():
    """Operator says 'I want this batch task right now'."""
    reg = EligibilityRegistry()
    decision = reg.route("hd_parse_kicad", force_lane="realtime")
    assert decision.lane == "realtime"
    assert "operator override" in decision.reason


def test_route_force_lane_batch_overrides_soft_realtime_default():
    """Operator says 'queue this planning task as batch'."""
    reg = EligibilityRegistry()
    decision = reg.route("planning", force_lane="batch")
    assert decision.lane == "batch"
    assert decision.priority in ("P0", "P1", "P2", "P3")  # uses rule.batch_priority


def test_route_realtime_required_vetoes_force_batch():
    """Chat UI cannot be batched even with operator override."""
    reg = EligibilityRegistry()
    decision = reg.route("chat_ui", force_lane="batch")
    assert decision.lane == "realtime"
    assert "VETOED" in decision.reason
    assert "realtime_required" in decision.reason


def test_route_unknown_kind_uses_generic_dev_fallback():
    reg = EligibilityRegistry()
    decision = reg.route("definitely_not_a_known_kind")
    assert decision.lane == "realtime"  # generic_dev defaults to realtime


# ─── AutoBatchAccumulator ────────────────────────────────────────


def _task(
    task_id: str, *, task_kind: str = "todo_routine",
    model: str = "claude-sonnet-4-6", tools: list[str] | None = None,
) -> BatchableTask:
    params: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": f"x {task_id}"}],
    }
    if tools:
        params["tools"] = [
            {"name": t, "description": "x", "input_schema": {"type": "object"}}
            for t in tools
        ]
    return BatchableTask(
        task_id=task_id,
        params=params,
        metadata={"task_kind": task_kind},
    )


@pytest.mark.asyncio
async def test_accumulator_threshold_triggers_flush():
    received: list[BatchableTask] = []

    async def enqueue(t: BatchableTask) -> None:
        received.append(t)

    reg = EligibilityRegistry()
    # todo_routine threshold is 10; smaller bucket for test speed
    reg.set_override(EligibilityRule(
        task_kind="todo_routine",
        batch_eligible=True,
        batch_priority="P3",
        reason="test override",
        auto_batch_threshold=3,
    ))

    acc = AutoBatchAccumulator(
        reg, dispatcher_enqueue=enqueue, max_age_seconds=999.0,
    )

    # First 2 accumulate, no flush
    for i in range(2):
        flushed = await acc.add(_task(f"t{i}"))
        assert flushed == 0
    assert received == []

    # 3rd hits threshold → flush
    flushed = await acc.add(_task("t2"))
    assert flushed == 3
    assert len(received) == 3


@pytest.mark.asyncio
async def test_accumulator_age_timeout_triggers_flush():
    received: list[BatchableTask] = []

    async def enqueue(t: BatchableTask) -> None:
        received.append(t)

    fake_t = [1000.0]

    def clock():
        return fake_t[0]

    reg = EligibilityRegistry()
    reg.set_override(EligibilityRule(
        task_kind="todo_routine",
        batch_eligible=True,
        auto_batch_threshold=100,  # never reached
        reason="test",
    ))
    acc = AutoBatchAccumulator(
        reg, dispatcher_enqueue=enqueue,
        max_age_seconds=10.0, clock=clock,
    )

    # Add one task at t=1000
    await acc.add(_task("t1"))
    assert received == []

    # At t=1005 (5s old), not yet due
    fake_t[0] = 1005.0
    flushed = await acc.flush_due()
    assert flushed == 0

    # At t=1011 (11s old), due
    fake_t[0] = 1011.0
    flushed = await acc.flush_due()
    assert flushed == 1
    assert len(received) == 1


@pytest.mark.asyncio
async def test_accumulator_buckets_by_kind_model_tools():
    received: list[BatchableTask] = []

    async def enqueue(t: BatchableTask) -> None:
        received.append(t)

    reg = EligibilityRegistry()
    acc = AutoBatchAccumulator(reg, dispatcher_enqueue=enqueue)

    await acc.add(_task("a", task_kind="hd_parse_kicad", model="claude-opus-4-7"))
    await acc.add(_task("b", task_kind="hd_parse_kicad", model="claude-sonnet-4-6"))
    await acc.add(_task("c", task_kind="hd_parse_altium", model="claude-opus-4-7"))
    await acc.add(_task("d", task_kind="hd_parse_kicad", model="claude-opus-4-7", tools=["Read"]))

    # 4 distinct buckets: (kicad, opus, ""), (kicad, sonnet, ""),
    # (altium, opus, ""), (kicad, opus, "Read")
    keys = acc.bucket_keys()
    assert len(keys) == 4


@pytest.mark.asyncio
async def test_accumulator_flush_due_only_flushes_expired_buckets():
    received: list[BatchableTask] = []

    async def enqueue(t: BatchableTask) -> None:
        received.append(t)

    fake_t = [1000.0]
    reg = EligibilityRegistry()
    acc = AutoBatchAccumulator(
        reg,
        dispatcher_enqueue=enqueue,
        max_age_seconds=10.0,
        clock=lambda: fake_t[0],
    )

    await acc.add(_task("old", task_kind="hd_parse_kicad", model="claude-opus-4-7"))
    fake_t[0] = 1005.0
    await acc.add(_task("new", task_kind="hd_parse_altium", model="claude-opus-4-7"))

    fake_t[0] = 1011.0
    flushed = await acc.flush_due()

    assert flushed == 1
    assert [task.task_id for task in received] == ["old"]
    assert acc.pending_count == 1
    assert acc.bucket_keys() == [("hd_parse_altium", "claude-opus-4-7", "")]


@pytest.mark.asyncio
async def test_accumulator_realtime_task_forwarded_not_buffered():
    """If caller hands a non-batch-eligible task, accumulator forwards it."""
    received: list[BatchableTask] = []

    async def enqueue(t: BatchableTask) -> None:
        received.append(t)

    reg = EligibilityRegistry()
    acc = AutoBatchAccumulator(reg, dispatcher_enqueue=enqueue)

    flushed = await acc.add(_task("rt", task_kind="chat_ui"))
    assert flushed == 1  # immediate forward
    assert len(received) == 1
    assert acc.pending_count == 0


@pytest.mark.asyncio
async def test_accumulator_flush_all():
    received: list[BatchableTask] = []

    async def enqueue(t: BatchableTask) -> None:
        received.append(t)

    reg = EligibilityRegistry()
    reg.set_override(EligibilityRule(
        task_kind="todo_routine",
        batch_eligible=True,
        auto_batch_threshold=100,  # never auto-flushes
        reason="test",
    ))
    acc = AutoBatchAccumulator(
        reg, dispatcher_enqueue=enqueue, max_age_seconds=999.0,
    )

    for i in range(5):
        await acc.add(_task(f"t{i}"))
    assert acc.pending_count == 5

    flushed = await acc.flush_all()
    assert flushed == 5
    assert len(received) == 5
    assert acc.pending_count == 0


@pytest.mark.asyncio
async def test_accumulator_pending_count_decreases_on_flush():
    received: list[BatchableTask] = []

    async def enqueue(t):
        received.append(t)

    reg = EligibilityRegistry()
    reg.set_override(EligibilityRule(
        "todo_routine", batch_eligible=True,
        auto_batch_threshold=2, reason="test",
    ))
    acc = AutoBatchAccumulator(reg, dispatcher_enqueue=enqueue)

    await acc.add(_task("a"))
    assert acc.pending_count == 1
    await acc.add(_task("b"))  # threshold hits → flush
    assert acc.pending_count == 0
    assert len(received) == 2


@pytest.mark.asyncio
async def test_accumulator_unknown_kind_falls_back_to_generic_realtime():
    """Unknown task_kind → registry falls back to generic_dev (realtime),
    accumulator forwards immediately."""
    received: list[BatchableTask] = []

    async def enqueue(t):
        received.append(t)

    reg = EligibilityRegistry()
    acc = AutoBatchAccumulator(reg, dispatcher_enqueue=enqueue)

    flushed = await acc.add(_task("x", task_kind="never_seen"))
    assert flushed == 1  # forwarded immediately
    assert len(received) == 1


@pytest.mark.asyncio
async def test_accumulator_no_threshold_uses_age_only():
    """Rule with auto_batch_threshold=None never triggers count-based flush."""
    received: list[BatchableTask] = []

    async def enqueue(t):
        received.append(t)

    fake_t = [0.0]
    reg = EligibilityRegistry()
    reg.set_override(EligibilityRule(
        "todo_routine", batch_eligible=True,
        auto_batch_threshold=None, reason="test no-threshold",
    ))
    acc = AutoBatchAccumulator(
        reg, dispatcher_enqueue=enqueue,
        max_age_seconds=5.0, clock=lambda: fake_t[0],
    )

    # Add 100 tasks — none flushed (no threshold)
    for i in range(100):
        flushed = await acc.add(_task(f"t{i}"))
        assert flushed == 0
    assert acc.pending_count == 100

    # Advance time — all flush via age
    fake_t[0] = 100.0
    flushed = await acc.flush_due()
    assert flushed == 100
    assert len(received) == 100
