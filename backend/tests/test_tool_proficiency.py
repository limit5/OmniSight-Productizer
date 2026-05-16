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
from backend.agents.mp_w17_telemetry_consumer import consume_invocation_log_line
from backend.agents.mp_w17_telemetry_consumer import payload_from_invocation_log
from backend.agents.tool_proficiency import (
    CROSS_GUILD_HANDOFF_REQUIRED_LEVEL,
    InMemoryToolProficiencyStore,
    LEVEL_REQUIREMENTS,
    MAX_TOOL_LEVEL,
    PeerHandoffSuccessRate,
    ProficiencyGateConfigMissing,
    TOOL_LEVEL_SPECS,
    ToolLevelSpec,
    TOOL_ID_CROSS_GUILD_HANDOFF,
    TOOL_ID_PEER_HANDOFF,
    ToolProficiencyState,
    build_feature_unlock_gate,
    can_invoke_at_level,
    can_perform_cross_guild_handoff,
    capability_for_level,
    compute_tool_level,
    get_required_level,
    install_feature_unlock_gate,
    is_cross_guild_handoff,
    list_proficiencies,
    peer_handoff_success_rate,
    peer_handoff_tool_id,
    record_peer_handoff_outcome,
    record_tool_invocation,
    reset_gate_config_cache_for_tests,
    tool_level_spec,
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


def test_tool_level_specs_pin_op179_ladder_semantics():
    assert tuple(TOOL_LEVEL_SPECS) == (1, 2, 3, 4, 5)
    assert tool_level_spec(1) == ToolLevelSpec(1, 0, 0.0, "basic_invoke")
    assert tool_level_spec(2) == ToolLevelSpec(2, 10, 0.70, "chain_two_calls")
    assert tool_level_spec(3) == ToolLevelSpec(3, 50, 0.80, "batch_ops")
    assert tool_level_spec(4) == ToolLevelSpec(
        4, 200, 0.85, "advanced_flags_cross_guild_a2a"
    )
    assert tool_level_spec(5) == ToolLevelSpec(
        5, 500, 0.90, "author_new_mcp_wrapper"
    )
    assert all(
        tool_level_spec(level).capability == capability_for_level(level)
        for level in range(1, MAX_TOOL_LEVEL + 1)
    )


def test_tool_level_spec_rejects_out_of_range_level():
    with pytest.raises(ValueError):
        tool_level_spec(0)
    with pytest.raises(ValueError):
        tool_level_spec(6)


def test_tool_level_spec_dataclass_validates_shape():
    with pytest.raises(ValueError):
        ToolLevelSpec(0, 0, 0.0, "basic_invoke")
    with pytest.raises(ValueError):
        ToolLevelSpec(1, -1, 0.0, "basic_invoke")
    with pytest.raises(ValueError):
        ToolLevelSpec(1, 0, 1.1, "basic_invoke")
    with pytest.raises(ValueError):
        ToolLevelSpec(1, 0, 0.0, "   ")


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
    # W17.2 hardened top-10 (Read/Edit/Bash/Grep/Glob/...) plus one
    # representative OP-179 gate at each higher tool level.
    reset_gate_config_cache_for_tests()
    repo_root = Path(__file__).resolve().parents[2]
    shipped = repo_root / "config" / "tool_proficiency_gates.yaml"
    assert shipped.is_file()
    for tool in ("Read", "Edit", "Bash", "Grep", "Glob",
                 "Write", "Agent", "WebFetch", "Skill", "ToolSearch"):
        assert get_required_level(tool, config_path=shipped) == 1
    assert get_required_level("Task", config_path=shipped) == 2
    assert get_required_level(
        "mcp__filesystem__write_multiple_files", config_path=shipped
    ) == 3
    assert get_required_level("CrossGuildHandoff", config_path=shipped) == 4
    assert get_required_level("AuthorMcpWrapper", config_path=shipped) == 5


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
async def test_telemetry_consumer_detects_nested_invocation_and_outcome():
    """OP-182 — W13 can derive proficiency from invocation + outcome payloads."""
    store = InMemoryToolProficiencyStore()
    recorded = await consume_event(
        store,
        {
            "invocation": {
                "tool_name": TOOL,
                "agent_id": AGENT,
                "timestamp": T0.isoformat(),
            },
            "outcome": {"is_error": False},
        },
    )
    assert recorded is not None
    assert recorded.invocation_count == 1
    assert recorded.success_count == 1

    recorded = await consume_event(
        store,
        {
            "invocation": {
                "tool_name": TOOL,
                "agent_id": AGENT,
                "timestamp": T0.isoformat(),
            },
            "outcome": {"is_error": True},
        },
    )
    assert recorded is not None
    assert recorded.invocation_count == 2
    assert recorded.success_count == 1


@pytest.mark.asyncio
async def test_telemetry_consumer_detects_invocation_log_line():
    """OP-182 — replaying logged tool_invocation JSON updates proficiency."""
    store = InMemoryToolProficiencyStore()
    line = (
        'INFO events.tool_invocation {"agent_id": "agent-alpha", '
        '"tool_name": "Read", "outcome": "success", '
        '"timestamp": "2026-01-01T00:00:00+00:00"}'
    )
    payload = payload_from_invocation_log(line)
    assert payload is not None
    assert payload["agent_id"] == AGENT

    recorded = await consume_invocation_log_line(store, line)

    assert recorded is not None
    assert recorded.agent_id == AGENT
    assert recorded.tool_id == TOOL
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


@pytest.mark.asyncio
async def test_dispatcher_tool_invocation_telemetry_includes_agent_id(
    monkeypatch: pytest.MonkeyPatch,
):
    """OP-182 — dispatcher telemetry carries the agent_id needed by W13."""
    from backend.agents import tool_dispatcher as td

    calls: list[dict[str, object]] = []

    def _emit(
        tool_name: str,
        duration_ms: float,
        success: bool,
        args_size_bytes: int,
        *,
        agent_id: str | None = None,
    ) -> None:
        calls.append({
            "tool_name": tool_name,
            "success": success,
            "agent_id": agent_id,
        })

    monkeypatch.setattr(td, "emit_tool_invocation", _emit)

    dispatcher = td.ToolDispatcher()

    async def _read(payload):  # noqa: ANN001
        return "ok"

    dispatcher.register("Read", _read)
    dispatcher.set_proficiency_gate(None, agent_id=AGENT)

    result = await dispatcher.execute("u-op-182", "Read", {"file_path": "/tmp/x"})

    assert not result.is_error
    assert calls == [{
        "tool_name": "Read",
        "success": True,
        "agent_id": AGENT,
    }]
# ── W13.4 (OP-181) A2A peer-handoff + cross-Guild Lv-4 gate ─────────


def test_w13_4_constants_match_adr_and_shipped_yaml():
    """W13.4 — the helper constants must agree with the shipped YAML
    (``mcp__a2a__cross_guild_handoff: 4``) and with the ADR-0008 Lv-4
    row. Drift here would silently disable the Lv-4 gate (helper says
    Lv 3, YAML stays at Lv 4) without any other test failing."""
    reset_gate_config_cache_for_tests()
    repo_root = Path(__file__).resolve().parents[2]
    shipped = repo_root / "config" / "tool_proficiency_gates.yaml"
    assert get_required_level(
        TOOL_ID_CROSS_GUILD_HANDOFF, config_path=shipped
    ) == CROSS_GUILD_HANDOFF_REQUIRED_LEVEL == 4
    # Same-Guild peer-handoff stays at bootstrap Lv 1 — the success-rate
    # accrues but the gate is permissive.
    assert get_required_level(
        TOOL_ID_PEER_HANDOFF, config_path=shipped
    ) == 1


def test_is_cross_guild_handoff_handles_case_and_whitespace():
    """W13.4 — same Guild with different casing / whitespace is NOT a
    cross-Guild handoff; different Guilds are. Blank slugs raise so a
    mis-classified handoff cannot escape into the cross-Guild bucket."""
    assert is_cross_guild_handoff("backend", "frontend") is True
    assert is_cross_guild_handoff("backend", "backend") is False
    # Case + whitespace tolerance: "Backend" vs "backend " must NOT be
    # classified as cross-Guild.
    assert is_cross_guild_handoff("Backend", "backend ") is False
    assert is_cross_guild_handoff(" BACKEND ", "Backend") is False
    # Blank slug refuses — better than silently picking a bucket.
    with pytest.raises(ValueError):
        is_cross_guild_handoff("", "backend")
    with pytest.raises(ValueError):
        is_cross_guild_handoff("backend", "  ")
    with pytest.raises(TypeError):
        is_cross_guild_handoff(None, "backend")  # type: ignore[arg-type]


def test_peer_handoff_tool_id_routes_to_constants():
    """W13.4 — the bucket helper must return the two stable tool_ids
    so a typo in either constant is caught immediately."""
    assert peer_handoff_tool_id(cross_guild=False) == TOOL_ID_PEER_HANDOFF
    assert (
        peer_handoff_tool_id(cross_guild=True)
        == TOOL_ID_CROSS_GUILD_HANDOFF
    )


@pytest.mark.asyncio
async def test_record_peer_handoff_outcome_buckets_same_and_cross_guild():
    """W13.4 — same-Guild outcomes accrue on ``mcp__a2a__peer_handoff``;
    cross-Guild outcomes accrue on ``mcp__a2a__cross_guild_handoff``.
    The two buckets are independent so a Lv-1 sender's cross-Guild
    failures cannot poison the same-Guild row's success ratio."""
    store = InMemoryToolProficiencyStore()
    sender = "agent-sender"

    # 3 same-Guild successes.
    for _ in range(3):
        await record_peer_handoff_outcome(
            store, sender, cross_guild=False, outcome="success", now=T0
        )
    # 2 cross-Guild fails.
    for _ in range(2):
        await record_peer_handoff_outcome(
            store, sender, cross_guild=True, outcome="fail", now=T0
        )

    same = await store.get_state(sender, TOOL_ID_PEER_HANDOFF)
    cross = await store.get_state(sender, TOOL_ID_CROSS_GUILD_HANDOFF)
    assert same is not None
    assert same.invocation_count == 3 and same.success_count == 3
    assert cross is not None
    assert cross.invocation_count == 2 and cross.success_count == 0


@pytest.mark.asyncio
async def test_peer_handoff_success_rate_summarises_sender_row():
    """W13.4 — the summary view returns the underlying counters + the
    derived ratio + level so a UI card can render without re-computing.
    No row → zeroed summary with Lv 1 (bootstrap floor)."""
    store = InMemoryToolProficiencyStore()
    sender = "agent-sender"

    # No row yet → zeroed summary at Lv 1.
    empty = await peer_handoff_success_rate(
        store, sender, cross_guild=False
    )
    assert empty == PeerHandoffSuccessRate(
        agent_id=sender,
        cross_guild=False,
        invocations=0,
        successes=0,
        success_ratio=0.0,
        level=1,
    )

    # 10 success, 3 fail → 0.769 ratio, Lv 2 (≥ 10 success ≥ 0.70 ratio).
    for _ in range(10):
        await record_peer_handoff_outcome(
            store, sender, cross_guild=False, outcome="success", now=T0
        )
    for _ in range(3):
        await record_peer_handoff_outcome(
            store, sender, cross_guild=False, outcome="fail", now=T0
        )

    summary = await peer_handoff_success_rate(
        store, sender, cross_guild=False
    )
    assert summary.invocations == 13
    assert summary.successes == 10
    assert summary.success_ratio == pytest.approx(10 / 13)
    assert summary.level == 2  # 10 successes, ratio 0.769 ≥ 0.70


@pytest.mark.asyncio
async def test_can_perform_cross_guild_handoff_refuses_under_lv4():
    """W13.4 — a Lv-1 sender attempting a cross-Guild handoff is refused
    by the in-process pre-check (mirrors the dispatcher refusal but
    runs before the round-trip)."""
    store = InMemoryToolProficiencyStore()
    sender = "agent-sender"
    # Lv-1 sender (single bootstrap row at TOOL_ID_CROSS_GUILD_HANDOFF).
    await record_peer_handoff_outcome(
        store, sender, cross_guild=True, outcome="success", now=T0
    )

    blocked: list[tuple[str, str, int, int]] = []

    def _emit(agent_id: str, tool_id: str, current: int, required: int) -> None:
        blocked.append((agent_id, tool_id, current, required))

    allowed = await can_perform_cross_guild_handoff(
        store, sender, now=T0, emit_gate_blocked=_emit
    )
    assert allowed is False
    assert blocked == [(sender, TOOL_ID_CROSS_GUILD_HANDOFF, 1, 4)]


@pytest.mark.asyncio
async def test_can_perform_cross_guild_handoff_allows_lv4_sender():
    """W13.4 — once the sender reaches Lv 4 on the cross-Guild handoff
    tool, the pre-check allows. Verified against the ADR-0008 Lv-4
    threshold (≥ 200 success / ≥ 0.85 ratio)."""
    store = InMemoryToolProficiencyStore()
    sender = "agent-sender"
    await store.upsert_state(
        ToolProficiencyState(
            agent_id=sender,
            tool_id=TOOL_ID_CROSS_GUILD_HANDOFF,
            level=4,
            invocation_count=230,
            success_count=200,  # 200/230 ≈ 0.870 ≥ 0.85
            last_used_at=T0,
        )
    )
    assert await can_perform_cross_guild_handoff(
        store, sender, now=T0
    ) is True


@pytest.mark.asyncio
async def test_w13_4_cross_guild_dispatcher_refuses_lv1_sender(tmp_path: Path):
    """W13.4 — end-to-end through ``install_feature_unlock_gate`` against
    the canonical YAML entry ``mcp__a2a__cross_guild_handoff: 4``. The
    canonical W13.4 acceptance: a fresh Lv-1 sender attempting the
    cross-Guild handoff tool surfaces a structured
    ``tool_proficiency_insufficient`` refusal from the dispatcher
    instead of forwarding to the handler."""
    import json

    from backend.agents.tool_dispatcher import ToolDispatcher

    reset_gate_config_cache_for_tests()
    config = tmp_path / "tool_proficiency_gates.yaml"
    # Use "Read" as the registered handler name (matches the schema
    # registry) but gate it at Lv 4 to re-create the
    # ``mcp__a2a__cross_guild_handoff: 4`` semantics without registering
    # a new schema.
    config.write_text("gates:\n  Read: 4\n", encoding="utf-8")
    store = InMemoryToolProficiencyStore()
    sender = "agent-sender"
    # Seed the sender at Lv 1.
    await record_tool_invocation(store, sender, "Read", "success", now=T0)

    dispatcher = ToolDispatcher()

    async def _read(payload):  # noqa: ANN001 - test handler
        return "ok"

    dispatcher.register("Read", _read)

    install_feature_unlock_gate(
        dispatcher, store=store, agent_id=sender,
        config_path=config, now=T0,
    )

    result = await dispatcher.execute("u-x", "Read", {"file_path": "/x"})
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["error"] == "tool_proficiency_insufficient"
    assert payload["agent_id"] == sender


# ── Constants sanity ─────────────────────────────────────────────────


def test_level_requirements_monotonic():
    # ADR sanity: thresholds must be monotonic both on counts and ratio.
    levels = sorted(LEVEL_REQUIREMENTS)
    counts = [LEVEL_REQUIREMENTS[lv][0] for lv in levels]
    ratios = [LEVEL_REQUIREMENTS[lv][1] for lv in levels]
    assert counts == sorted(counts)
    assert ratios == sorted(ratios)
