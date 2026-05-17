"""RPG.W13 -- contract tests for ``backend/agents/tool_proficiency.py``.

Covers the 9 AC cases listed on OP-218:

1. ``record_tool_invocation`` increments invocation_count + success_count.
2. Lv 1→2 threshold transition (≥ 10 success / ≥ 0.70 ratio).
3. Lv 2→3 threshold transition (≥ 50 success / ≥ 0.80 ratio).
4. Lv 3→4 threshold transition (≥ 200 success / ≥ 0.85 ratio).
5. Lv 4→5 threshold transition (≥ 500 success / ≥ 0.90 ratio).
6. ``can_invoke_at_level`` refuses when current Lv < required.
7. ``can_invoke_at_level`` allows when current Lv ≥ required.
8. Telemetry lag (>24h) fail-open: invocation allowed but warning logged.
9. Missing-config permissive default + rebuild idempotent replay.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.agents.mp_w17_telemetry_consumer import consume_batch, consume_event
from backend.agents.tool_proficiency import (
    InMemoryToolProficiencyStore,
    LEVEL_REQUIREMENTS,
    MAX_TOOL_LEVEL,
    ProficiencyGateConfigMissing,
    ToolProficiencyState,
    build_feature_unlock_gate,
    can_invoke_at_level,
    capability_for_level,
    compute_tool_level,
    get_required_level,
    install_feature_unlock_gate,
    list_proficiencies,
    record_tool_invocation,
    reset_gate_config_cache_for_tests,
)


AGENT = "agent-alpha"
TOOL = "Read"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _seed_invocations(success: int, fail: int) -> tuple[int, int]:
    return success + fail, success


# ── compute_tool_level ──────────────────────────────────────────────


def test_compute_tool_level_lv1_floor():
    assert compute_tool_level(0, 0) == 1
    assert compute_tool_level(1, 0) == 1
    assert compute_tool_level(1, 1) == 1
    assert compute_tool_level(9, 9) == 1  # below the 10-success threshold


def test_compute_tool_level_lv2_threshold_transition():
    # AC #2 — Lv 1→2: ≥ 10 success and ≥ 0.70 ratio.
    invocation_count, success_count = _seed_invocations(success=10, fail=3)
    assert compute_tool_level(invocation_count, success_count) == 2
    # Just below ratio threshold.
    invocation_count, success_count = _seed_invocations(success=10, fail=5)
    # 10 / 15 = 0.6667 < 0.70 → stays at Lv 1.
    assert compute_tool_level(invocation_count, success_count) == 1


def test_compute_tool_level_lv3_threshold_transition():
    # AC #3 — Lv 2→3: ≥ 50 success and ≥ 0.80 ratio.
    invocation_count, success_count = _seed_invocations(success=50, fail=10)
    # 50 / 60 ≈ 0.833 ≥ 0.80 → Lv 3.
    assert compute_tool_level(invocation_count, success_count) == 3
    # Just under success count.
    invocation_count, success_count = _seed_invocations(success=49, fail=10)
    assert compute_tool_level(invocation_count, success_count) == 2


def test_compute_tool_level_lv4_threshold_transition():
    # AC #4 — Lv 3→4: ≥ 200 success and ≥ 0.85 ratio.
    invocation_count, success_count = _seed_invocations(success=200, fail=30)
    # 200 / 230 ≈ 0.8696 ≥ 0.85 → Lv 4.
    assert compute_tool_level(invocation_count, success_count) == 4


def test_compute_tool_level_lv5_threshold_transition():
    # AC #5 — Lv 4→5: ≥ 500 success and ≥ 0.90 ratio.
    invocation_count, success_count = _seed_invocations(success=500, fail=50)
    # 500 / 550 ≈ 0.909 ≥ 0.90 → Lv 5.
    assert compute_tool_level(invocation_count, success_count) == 5


def test_compute_tool_level_invalid_inputs_raise():
    with pytest.raises(TypeError):
        compute_tool_level(True, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        compute_tool_level(-1, 0)
    with pytest.raises(ValueError):
        compute_tool_level(5, 6)  # success > invocations


def test_capability_for_level_covers_full_ladder():
    for level in range(1, MAX_TOOL_LEVEL + 1):
        assert isinstance(capability_for_level(level), str)
    with pytest.raises(ValueError):
        capability_for_level(0)
    with pytest.raises(ValueError):
        capability_for_level(99)


# ── record_tool_invocation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_tool_invocation_increments_and_bootstraps():
    # AC #1 — first invocation creates a row, increments counters.
    store = InMemoryToolProficiencyStore()
    recorded = await record_tool_invocation(
        store, AGENT, TOOL, "success", now=T0
    )
    assert recorded.bootstrapped is True
    assert recorded.invocation_count == 1
    assert recorded.success_count == 1
    assert recorded.previous_level == 1
    assert recorded.new_level == 1

    state = await store.get_state(AGENT, TOOL)
    assert state is not None
    assert state.invocation_count == 1
    assert state.success_count == 1


@pytest.mark.asyncio
async def test_record_tool_invocation_counts_failures_only_in_invocation():
    store = InMemoryToolProficiencyStore()
    await record_tool_invocation(store, AGENT, TOOL, "success", now=T0)
    await record_tool_invocation(store, AGENT, TOOL, "fail", now=T0)
    state = await store.get_state(AGENT, TOOL)
    assert state is not None
    assert state.invocation_count == 2
    assert state.success_count == 1


@pytest.mark.asyncio
async def test_record_tool_invocation_crosses_lv1_to_lv2_emits_level_up():
    # AC #2 — driving the recorder past the Lv 2 threshold emits a level-up.
    store = InMemoryToolProficiencyStore()
    level_ups: list[tuple[str, str, int, int]] = []

    def _emit(agent_id: str, tool_id: str, prev: int, new: int) -> None:
        level_ups.append((agent_id, tool_id, prev, new))

    # 10 successes + 3 failures = ratio 0.769 ≥ 0.70 + 10 successes ≥ 10.
    for _ in range(10):
        await record_tool_invocation(
            store, AGENT, TOOL, "success", now=T0, emit_level_up=_emit
        )
    for _ in range(3):
        await record_tool_invocation(
            store, AGENT, TOOL, "fail", now=T0, emit_level_up=_emit
        )

    assert any(transition[3] >= 2 for transition in level_ups)
    state = await store.get_state(AGENT, TOOL)
    assert state is not None
    assert state.level == 2


# ── can_invoke_at_level ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_can_invoke_refuses_when_under_required_level():
    # AC #6 — gate refuse when under level.
    store = InMemoryToolProficiencyStore()
    # Seed an agent at Lv 1 by recording one invocation.
    await record_tool_invocation(store, AGENT, TOOL, "success", now=T0)

    blocked: list[tuple[str, str, int, int]] = []

    def _emit(agent_id: str, tool_id: str, current: int, required: int) -> None:
        blocked.append((agent_id, tool_id, current, required))

    allowed = await can_invoke_at_level(
        store, AGENT, TOOL, required_level=3, now=T0,
        emit_gate_blocked=_emit,
    )
    assert allowed is False
    assert blocked == [(AGENT, TOOL, 1, 3)]


@pytest.mark.asyncio
async def test_can_invoke_allows_when_at_or_above_required_level():
    # AC #7 — gate allow when at/above level.
    store = InMemoryToolProficiencyStore()
    # Force a Lv 3 row directly.
    await store.upsert_state(
        ToolProficiencyState(
            agent_id=AGENT,
            tool_id=TOOL,
            level=3,
            invocation_count=60,
            success_count=50,
            last_used_at=T0,
        )
    )
    assert await can_invoke_at_level(
        store, AGENT, TOOL, required_level=3, now=T0
    )
    assert await can_invoke_at_level(
        store, AGENT, TOOL, required_level=2, now=T0
    )


@pytest.mark.asyncio
async def test_can_invoke_first_time_bootstrap_allows_lv1():
    # First-time invocation: no row exists, Lv 1 bootstrap allows when required <= 1.
    store = InMemoryToolProficiencyStore()
    assert await can_invoke_at_level(
        store, AGENT, TOOL, required_level=1, now=T0
    )
    assert not await can_invoke_at_level(
        store, AGENT, TOOL, required_level=2, now=T0
    )


@pytest.mark.asyncio
async def test_can_invoke_telemetry_lag_fails_open(caplog):
    # AC #8 — proficiency state >24h stale, allow but warn.
    store = InMemoryToolProficiencyStore()
    stale_when = T0 - timedelta(days=2)
    await store.upsert_state(
        ToolProficiencyState(
            agent_id=AGENT,
            tool_id=TOOL,
            level=2,
            invocation_count=20,
            success_count=15,
            last_used_at=stale_when,
        )
    )
    with caplog.at_level("WARNING"):
        allowed = await can_invoke_at_level(
            store, AGENT, TOOL, required_level=2, now=T0
        )
    assert allowed is True
    assert any("TelemetryConsumerLag" in record.message for record in caplog.records)


# ── gate config loading ─────────────────────────────────────────────


def test_get_required_level_missing_entry_defaults_to_lv1(tmp_path: Path):
    # AC #9 — missing config entry → Lv 1 (permissive).
    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    config.write_text("gates:\n  Read: 1\n", encoding="utf-8")
    assert get_required_level("Read", config_path=config) == 1
    assert get_required_level("AnUnknownTool", config_path=config) == 1


def test_get_required_level_lv3_entry(tmp_path: Path):
    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    config.write_text(
        "gates:\n  mcp__filesystem__write_multiple_files: 3\n",
        encoding="utf-8",
    )
    assert get_required_level(
        "mcp__filesystem__write_multiple_files", config_path=config
    ) == 3


def test_get_required_level_missing_file_raises(tmp_path: Path):
    reset_gate_config_cache_for_tests()
    with pytest.raises(ProficiencyGateConfigMissing):
        get_required_level("Read", config_path=tmp_path / "nope.yaml")


def test_shipped_gates_yaml_loads_with_top10_tools():
    # DoD — ``config/tool_proficiency_gates.yaml`` ships with the
    # W17.2 hardened top-10 (Read/Edit/Bash/Grep/Glob/...).
    reset_gate_config_cache_for_tests()
    repo_root = Path(__file__).resolve().parents[2]
    shipped = repo_root / "config" / "tool_proficiency_gates.yaml"
    assert shipped.is_file()
    for tool in ("Read", "Edit", "Bash", "Grep", "Glob",
                 "Write", "Agent", "WebFetch", "Skill", "ToolSearch"):
        assert get_required_level(tool, config_path=shipped) == 1
    assert get_required_level(
        "mcp__filesystem__write_multiple_files", config_path=shipped
    ) == 3


# ── W17.7 consumer + idempotent rebuild ─────────────────────────────


@pytest.mark.asyncio
async def test_telemetry_consumer_consumes_w17_event_shape():
    store = InMemoryToolProficiencyStore()
    recorded = await consume_event(
        store,
        {
            "tool_name": TOOL,
            "agent_id": AGENT,
            "success": True,
            "timestamp": T0.isoformat(),
        },
    )
    assert recorded is not None
    assert recorded.invocation_count == 1
    assert recorded.success_count == 1


@pytest.mark.asyncio
async def test_telemetry_consumer_drops_events_missing_agent_or_tool():
    store = InMemoryToolProficiencyStore()
    payloads = [
        {"tool_name": TOOL, "success": True},          # no agent_id → drop
        {"agent_id": AGENT, "success": True},          # no tool_name → drop
        {"agent_id": AGENT, "tool_name": TOOL, "success": True},
    ]
    stats = await consume_batch(store, payloads)
    assert stats.received == 3
    assert stats.applied == 1
    assert stats.dropped_missing_agent == 1
    assert stats.dropped_missing_tool == 1


@pytest.mark.asyncio
async def test_rebuild_is_idempotent_when_replayed_twice():
    # AC #9 — replay from history is idempotent.
    payloads = [
        {"tool_name": TOOL, "agent_id": AGENT, "success": True, "timestamp": T0.isoformat()},
        {"tool_name": TOOL, "agent_id": AGENT, "success": True, "timestamp": T0.isoformat()},
        {"tool_name": TOOL, "agent_id": AGENT, "success": False, "timestamp": T0.isoformat()},
    ]
    first = InMemoryToolProficiencyStore()
    await consume_batch(first, payloads)
    first_state = await first.get_state(AGENT, TOOL)

    second = InMemoryToolProficiencyStore()
    await consume_batch(second, payloads)
    second_state = await second.get_state(AGENT, TOOL)

    assert first_state is not None and second_state is not None
    assert first_state.invocation_count == second_state.invocation_count == 3
    assert first_state.success_count == second_state.success_count == 2
    assert first_state.level == second_state.level


@pytest.mark.asyncio
async def test_list_proficiencies_returns_only_target_agent_rows():
    store = InMemoryToolProficiencyStore()
    await record_tool_invocation(store, AGENT, TOOL, "success", now=T0)
    await record_tool_invocation(store, AGENT, "Edit", "success", now=T0)
    await record_tool_invocation(store, "agent-beta", TOOL, "success", now=T0)

    rows = await list_proficiencies(store, AGENT)
    tool_ids = sorted(row.tool_id for row in rows)
    assert tool_ids == ["Edit", "Read"]


# ── Dispatcher gate wiring (W13 §"Feature-unlock gating") ───────────


@pytest.mark.asyncio
async def test_dispatcher_proficiency_gate_blocks_when_under_required_level():
    """AC #3 -- low-Lv agent attempts a Lv-3 batch op → refused.

    Wires the helper into the dispatcher and asserts the dispatcher
    returns a ``tool_proficiency_insufficient`` error envelope when the
    gate refuses. This is the operator-visible refusal path.
    """
    import json

    from backend.agents.tool_dispatcher import ToolDispatcher

    store = InMemoryToolProficiencyStore()
    # Seed the agent at Lv 1 so the gate against required_level=3 refuses.
    await record_tool_invocation(store, AGENT, TOOL, "success", now=T0)

    dispatcher = ToolDispatcher()

    # Use a Read handler to avoid registering a real batch tool.
    async def _read(payload):  # noqa: ANN001 - test handler
        return "ok"

    dispatcher.register("Read", _read)

    async def _gate(tool_name: str, agent_id: str) -> bool:
        return await can_invoke_at_level(
            store, agent_id, tool_name, required_level=3, now=T0
        )

    dispatcher.set_proficiency_gate(_gate, agent_id=AGENT)
    result = await dispatcher.execute("u-1", "Read", {"file_path": "/tmp/x"})
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["error"] == "tool_proficiency_insufficient"
    assert payload["agent_id"] == AGENT


@pytest.mark.asyncio
async def test_dispatcher_proficiency_gate_allows_at_or_above_level():
    from backend.agents.tool_dispatcher import ToolDispatcher

    store = InMemoryToolProficiencyStore()
    await store.upsert_state(
        ToolProficiencyState(
            agent_id=AGENT,
            tool_id=TOOL,
            level=3,
            invocation_count=60,
            success_count=50,
            last_used_at=T0,
        )
    )
    dispatcher = ToolDispatcher()

    async def _read(payload):  # noqa: ANN001
        return "ok"

    dispatcher.register("Read", _read)

    async def _gate(tool_name: str, agent_id: str) -> bool:
        return await can_invoke_at_level(
            store, agent_id, tool_name, required_level=3, now=T0
        )

    dispatcher.set_proficiency_gate(_gate, agent_id=AGENT)
    result = await dispatcher.execute("u-2", "Read", {"file_path": "/tmp/x"})
    assert not result.is_error
    assert result.content == "ok"


# ── W13.3 (OP-180) feature-unlock gate factory + dispatcher install ─


@pytest.mark.asyncio
async def test_build_feature_unlock_gate_reads_required_level_from_yaml(
    tmp_path: Path,
):
    """W13.3 — the factory closure must consult the YAML per-call.

    A Lv-1 agent against ``mcp__filesystem__write_multiple_files`` (Lv 3)
    refuses; the same agent against an un-listed tool (defaults to Lv 1)
    is allowed. Both decisions ride through the same closure.
    """
    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    config.write_text(
        "gates:\n"
        "  mcp__filesystem__write_multiple_files: 3\n"
        "  Read: 1\n",
        encoding="utf-8",
    )
    store = InMemoryToolProficiencyStore()
    await record_tool_invocation(store, AGENT, TOOL, "success", now=T0)

    gate = build_feature_unlock_gate(store, config_path=config, now=T0)

    # Lv-3 gate refuses a Lv-1 agent.
    assert (
        await gate("mcp__filesystem__write_multiple_files", AGENT)
    ) is False
    # Lv-1 gate (Read) allows the same agent.
    assert await gate("Read", AGENT) is True
    # Un-listed tool defaults to Lv 1 — allowed.
    assert await gate("AnUnknownTool", AGENT) is True


@pytest.mark.asyncio
async def test_build_feature_unlock_gate_allows_when_agent_at_required_lv(
    tmp_path: Path,
):
    """W13.3 — once the agent reaches the gate's Lv, the gate allows."""
    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    config.write_text(
        "gates:\n  mcp__filesystem__write_multiple_files: 3\n",
        encoding="utf-8",
    )
    store = InMemoryToolProficiencyStore()
    await store.upsert_state(
        ToolProficiencyState(
            agent_id=AGENT,
            tool_id="mcp__filesystem__write_multiple_files",
            level=3,
            invocation_count=60,
            success_count=50,
            last_used_at=T0,
        )
    )
    gate = build_feature_unlock_gate(store, config_path=config, now=T0)
    assert (
        await gate("mcp__filesystem__write_multiple_files", AGENT)
    ) is True


@pytest.mark.asyncio
async def test_install_feature_unlock_gate_wires_dispatcher_refusal(
    tmp_path: Path,
):
    """W13.3 — ``install_feature_unlock_gate`` wires the dispatcher so a
    Lv-1 agent attempting a Lv-3 gated tool surfaces a structured
    ``tool_proficiency_insufficient`` ``tool_result`` instead of running
    the handler.

    This is the canonical W13.3 acceptance: a fresh agent attempts a
    tool the YAML has gated at Lv 3 and the dispatcher refuses the call
    instead of forwarding to the handler. The test uses ``Read`` as the
    handler name (the dispatcher validates the name against the schema
    registry on ``register``); the YAML in this test maps ``Read: 3`` to
    re-create the same semantics as the shipped
    ``mcp__filesystem__write_multiple_files: 3`` entry without needing
    to register a new schema.
    """
    import json

    from backend.agents.tool_dispatcher import ToolDispatcher

    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    config.write_text("gates:\n  Read: 3\n", encoding="utf-8")
    store = InMemoryToolProficiencyStore()
    await record_tool_invocation(store, AGENT, TOOL, "success", now=T0)

    dispatcher = ToolDispatcher()

    handler_calls: list[dict] = []

    async def _read_handler(payload):  # noqa: ANN001 - test handler
        handler_calls.append(payload)
        return "ok"

    dispatcher.register("Read", _read_handler)

    install_feature_unlock_gate(
        dispatcher, store=store, agent_id=AGENT,
        config_path=config, now=T0,
    )

    # Lv-1 agent against a Lv-3-required tool → refused.
    result = await dispatcher.execute("u-1", "Read", {"file_path": "/x"})
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["error"] == "tool_proficiency_insufficient"
    assert payload["agent_id"] == AGENT
    assert payload["tool_name"] == "Read"
    assert handler_calls == []  # gate refused before handler ran


@pytest.mark.asyncio
async def test_install_feature_unlock_gate_allows_when_agent_qualified(
    tmp_path: Path,
):
    """W13.3 — once the agent's proficiency clears the YAML gate, the
    dispatcher forwards to the handler unchanged. YAML at ``Read: 3``,
    seeded agent at Lv 3 → handler runs."""
    from backend.agents.tool_dispatcher import ToolDispatcher

    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    config.write_text("gates:\n  Read: 3\n", encoding="utf-8")
    store = InMemoryToolProficiencyStore()
    await store.upsert_state(
        ToolProficiencyState(
            agent_id=AGENT,
            tool_id=TOOL,
            level=3,
            invocation_count=60,
            success_count=50,
            last_used_at=T0,
        )
    )

    dispatcher = ToolDispatcher()

    async def _read(payload):  # noqa: ANN001
        return "ok"

    dispatcher.register("Read", _read)

    install_feature_unlock_gate(
        dispatcher, store=store, agent_id=AGENT,
        config_path=config, now=T0,
    )

    result = await dispatcher.execute("u-2", "Read", {"file_path": "/tmp/x"})
    assert not result.is_error
    assert result.content == "ok"


def test_install_feature_unlock_gate_rejects_blank_agent_id(tmp_path: Path):
    """W13.3 — install refuses an empty agent_id because the dispatcher
    only consults the gate when ``_current_agent_id is not None``.
    Letting a blank string slip through would silently bypass the gate
    for every dispatch on that dispatcher instance."""
    from backend.agents.tool_dispatcher import ToolDispatcher

    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    config.write_text("gates:\n  Read: 1\n", encoding="utf-8")
    store = InMemoryToolProficiencyStore()
    dispatcher = ToolDispatcher()
    with pytest.raises(ValueError):
        install_feature_unlock_gate(
            dispatcher, store=store, agent_id="",
            config_path=config, now=T0,
        )


def test_install_feature_unlock_gate_rescopes_to_new_agent(tmp_path: Path):
    """W13.3 — re-installing with a different ``agent_id`` is the
    supported way to re-scope an already-running dispatcher; the public
    contract advertises this so a long-lived dispatcher can switch
    between agents without rebuilding."""
    from backend.agents.tool_dispatcher import ToolDispatcher

    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    config.write_text("gates:\n  Read: 1\n", encoding="utf-8")
    store = InMemoryToolProficiencyStore()
    dispatcher = ToolDispatcher()
    install_feature_unlock_gate(
        dispatcher, store=store, agent_id="agent-one",
        config_path=config, now=T0,
    )
    assert dispatcher._current_agent_id == "agent-one"
    install_feature_unlock_gate(
        dispatcher, store=store, agent_id="agent-two",
        config_path=config, now=T0,
    )
    assert dispatcher._current_agent_id == "agent-two"


# ── Constants sanity ─────────────────────────────────────────────────


def test_level_requirements_monotonic():
    # ADR sanity: thresholds must be monotonic both on counts and ratio.
    levels = sorted(LEVEL_REQUIREMENTS)
    counts = [LEVEL_REQUIREMENTS[lv][0] for lv in levels]
    ratios = [LEVEL_REQUIREMENTS[lv][1] for lv in levels]
    assert counts == sorted(counts)
    assert ratios == sorted(ratios)
