"""W15.4 — Contract tests for ``backend.web.vite_retry_budget`` plus
the specialist-node wiring that emits the operator escalation when
the same Vite build error pattern repeats 3× in a row.

W15.1 shipped the wire shape + per-workspace ring buffer.  W15.2
shipped the projection that folds each ``ViteBuildError`` into a
single-line ``state.error_history`` entry shaped::

    vite[<phase>] <file>:<line>: <kind>: <message>

W15.3 quotes the most recent such entry back to the agent on every
LLM turn via the Chinese-localised system-prompt banner.  W15.4 (this
row) closes the self-healing loop: when the agent retries the same
Vite build error 3 times in a row without progress, the runtime
escalates to the operator instead of letting the loop spin.

§A — Drift guards (threshold literal, finding_type / pipeline_phase
     literals, banner template, byte cap — pinned so the W15.6
     self-fix tests and the operator UI's debug-feed filter stay
     byte-stable across rows).
§B — :func:`count_trailing_same_vite_signature` (empty history,
     no-vite history, single entry, all-same trail, partial trail,
     mixed-source trail with non-vite gap, head-only signature
     comparison).
§C — :func:`should_escalate_vite_pattern` (under threshold, exact
     threshold, above threshold, idempotency via already_escalated,
     custom threshold override, threshold validation).
§D — :func:`format_vite_escalation_banner` (template fill, byte cap,
     multi-byte safety).
§E — :func:`emit_vite_pattern_escalation` (publishes both finding +
     pipeline-phase, severity is "error", context carries pattern /
     count / threshold).
§F — Specialist-node wiring (:func:`_maybe_emit_vite_retry_budget`
     returns ``{}`` for non-vite history, returns escalation dict for
     3-strike trail, idempotency across two consecutive turns).
§G — End-to-end (specialist node calls the gate before LLM invoke
     and includes ``vite_escalated_signatures`` in its return dict
     when escalation fires; empty history → no escalation key in
     return).
§H — Re-export surface (10 W15.4 symbols accessible via
     ``backend.web``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.web.vite_error_relay import (
    VITE_ERROR_HISTORY_KEY_PREFIX,
    format_vite_error_for_history,
    vite_error_history_signature,
)
from backend.web.vite_retry_budget import (
    MAX_VITE_ESCALATION_BANNER_BYTES,
    VITE_ESCALATION_BANNER_TEMPLATE,
    VITE_ESCALATION_FINDING_TYPE,
    VITE_ESCALATION_PIPELINE_PHASE,
    VITE_RETRY_BUDGET_THRESHOLD,
    ViteRetryBudgetEscalation,
    count_trailing_same_vite_signature,
    emit_vite_pattern_escalation,
    format_vite_escalation_banner,
    should_escalate_vite_pattern,
)
from backend.web_sandbox_vite_errors import (
    VITE_ERROR_PLUGIN_NAME,
    VITE_ERROR_PLUGIN_VERSION,
    WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
    ViteBuildError,
)


# ── Fixtures ────────────────────────────────────────────────────────


def _make_error(**overrides: Any) -> ViteBuildError:
    base: dict[str, Any] = {
        "schema_version": WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION,
        "kind": "compile",
        "phase": "transform",
        "message": "Failed to parse module",
        "file": "src/App.tsx",
        "line": 42,
        "column": 7,
        "stack": None,
        "plugin": VITE_ERROR_PLUGIN_NAME,
        "plugin_version": VITE_ERROR_PLUGIN_VERSION,
        "occurred_at": 1714760400.123,
        "received_at": 1714760400.456,
    }
    base.update(overrides)
    return ViteBuildError(**base)


def _entry(**overrides: Any) -> str:
    """Build a W15.2-formatted history entry by routing through the
    real :func:`format_vite_error_for_history` so the W15.4 detector
    exercises the producer/consumer contract end-to-end (no
    hand-rolled strings that could drift from the actual wire shape)."""

    return format_vite_error_for_history(_make_error(**overrides))


# ────────────────────────────────────────────────────────────────────
# §A — Drift guards
# ────────────────────────────────────────────────────────────────────


def test_threshold_literal_is_three() -> None:
    """The W15.4 row spec calls out "連 3 次失敗" verbatim.  Pinning
    the literal here means a tuning drift fails red and the W15.6
    self-fix tests (which assume a 3-strike threshold) keep working."""

    assert VITE_RETRY_BUDGET_THRESHOLD == 3


def test_finding_type_literal_is_stable() -> None:
    """The operator UI's debug-feed filter pins this literal as the
    discriminator.  Lock-step with the existing
    ``<bucket>_exhausted`` naming pattern in
    ``backend/agents/nodes.py`` (``retries_exhausted`` /
    ``verification_exhausted``)."""

    assert VITE_ESCALATION_FINDING_TYPE == "vite_retry_budget_exhausted"


def test_pipeline_phase_literal_is_stable() -> None:
    """The SSE timeline UI keys on this string for the colour map."""

    assert VITE_ESCALATION_PIPELINE_PHASE == "vite_retry_budget_exhausted"


def test_banner_template_uses_format_kwargs_only() -> None:
    """The template uses named substitutions ``{count}`` / ``{threshold}``
    / ``{pattern}``.  Pinning the kwarg names defends against a
    positional-rewrite that would silently swap them."""

    rendered = VITE_ESCALATION_BANNER_TEMPLATE.format(
        count=3, threshold=3, pattern="P",
    )
    assert "3" in rendered
    assert "P" in rendered


def test_banner_template_substring_for_w15_6_grep() -> None:
    """W15.6 self-fix tests grep for this substring to confirm the
    operator escalation fired.  Drift here breaks the W15.6 row."""

    rendered = VITE_ESCALATION_BANNER_TEMPLATE.format(
        count=3, threshold=3, pattern="vite[transform] x.tsx:1: compile:",
    )
    assert "Vite build error pattern repeated 3×" in rendered
    assert "threshold 3" in rendered
    assert "escalating to operator" in rendered


def test_max_banner_bytes_is_bounded() -> None:
    smallest = VITE_ESCALATION_BANNER_TEMPLATE.format(
        count=3, threshold=3, pattern="x",
    )
    assert MAX_VITE_ESCALATION_BANNER_BYTES >= len(smallest.encode("utf-8"))
    assert MAX_VITE_ESCALATION_BANNER_BYTES <= 4096


def test_escalation_dataclass_is_frozen() -> None:
    """``ViteRetryBudgetEscalation`` is a frozen dataclass — mutating
    a field must raise so the value object can sit in
    ``vite_escalated_signatures`` lookups without aliasing concerns."""

    decision = ViteRetryBudgetEscalation(pattern="x", count=3, threshold=3)
    with pytest.raises(Exception):  # FrozenInstanceError
        decision.pattern = "y"  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────
# §B — count_trailing_same_vite_signature
# ────────────────────────────────────────────────────────────────────


def test_count_returns_zero_none_for_empty_history() -> None:
    assert count_trailing_same_vite_signature([]) == (0, None)


def test_count_returns_zero_none_for_history_without_vite_entries() -> None:
    history = [
        "ToolError: read_file failed",
        "ToolError: write_file denied",
    ]
    assert count_trailing_same_vite_signature(history) == (0, None)


def test_count_single_vite_entry_returns_one() -> None:
    history = [_entry()]
    count, sig = count_trailing_same_vite_signature(history)
    assert count == 1
    assert sig is not None
    assert sig.startswith(VITE_ERROR_HISTORY_KEY_PREFIX)


def test_count_three_same_entries_returns_three() -> None:
    history = [_entry()] * 3
    count, sig = count_trailing_same_vite_signature(history)
    assert count == 3
    assert sig is not None


def test_count_partial_trail_only_counts_trailing_run() -> None:
    """Different sig in the middle resets the trailing count.  The
    detector counts the trailing run only — older same-sig entries
    above a different sig do NOT contribute."""

    history = [
        _entry(file="src/A.tsx", line=1, message="a"),
        _entry(file="src/A.tsx", line=1, message="a"),
        _entry(file="src/B.tsx", line=2, message="b"),  # interrupts
        _entry(file="src/A.tsx", line=1, message="a"),  # trailing
    ]
    count, sig = count_trailing_same_vite_signature(history)
    assert count == 1  # only the very last
    assert sig is not None
    assert "src/A.tsx:1" in sig


def test_count_message_body_difference_does_not_break_bucket() -> None:
    """Two errors with same file/line/phase/kind but different message
    body MUST bucket as the same signature (W15.2 head-only signature
    drops the message body)."""

    history = [
        _entry(message="foo is not defined"),
        _entry(message="'foo' is not defined"),
        _entry(message="ReferenceError: foo is not defined at L42"),
    ]
    count, sig = count_trailing_same_vite_signature(history)
    assert count == 3, (
        "head-only signature should bucket these together "
        "(W15.2 vite_error_history_signature contract)"
    )
    assert sig is not None


def test_count_skips_non_vite_entries_in_filter() -> None:
    """Tool-error entries between Vite entries do NOT reset the
    trailing run — the detector only looks at Vite-prefixed entries.
    This matches the row-spec semantics: the budget is for the Vite
    pattern, independent of unrelated tool-channel noise."""

    history = [
        _entry(file="src/X.tsx", line=1, message="x"),
        "ToolError: unrelated noise",
        _entry(file="src/X.tsx", line=1, message="x"),
        "ToolError: more noise",
        _entry(file="src/X.tsx", line=1, message="x"),
    ]
    count, sig = count_trailing_same_vite_signature(history)
    assert count == 3


def test_count_returns_signature_in_lock_step_with_w15_2() -> None:
    """The returned signature MUST equal the W15.2
    ``vite_error_history_signature`` of the trailing entry — same
    head, no parallel parsing."""

    entry = _entry(file="src/Z.tsx", line=99, message="z")
    history = [entry]
    _, sig = count_trailing_same_vite_signature(history)
    expected = vite_error_history_signature([entry])[0]
    assert sig == expected


def test_count_ignores_non_string_entries_defensively() -> None:
    """Non-string entries in error_history (defence in depth) are
    skipped without raising — same posture as the W15.3 parser."""

    history = [_entry(), 42, _entry(), None, _entry()]  # type: ignore[list-item]
    count, _sig = count_trailing_same_vite_signature(history)
    assert count == 3


# ────────────────────────────────────────────────────────────────────
# §C — should_escalate_vite_pattern
# ────────────────────────────────────────────────────────────────────


def test_should_escalate_returns_none_below_threshold() -> None:
    history = [_entry()] * 2  # threshold is 3
    assert should_escalate_vite_pattern(history) is None


def test_should_escalate_returns_decision_at_exact_threshold() -> None:
    history = [_entry()] * VITE_RETRY_BUDGET_THRESHOLD
    decision = should_escalate_vite_pattern(history)
    assert decision is not None
    assert decision.count == VITE_RETRY_BUDGET_THRESHOLD
    assert decision.threshold == VITE_RETRY_BUDGET_THRESHOLD
    assert decision.pattern.startswith(VITE_ERROR_HISTORY_KEY_PREFIX)


def test_should_escalate_returns_decision_above_threshold() -> None:
    """Above-threshold trail still escalates (count carries the actual
    observed value, not the threshold)."""

    history = [_entry()] * 5
    decision = should_escalate_vite_pattern(history)
    assert decision is not None
    assert decision.count == 5


def test_should_escalate_returns_none_when_already_escalated() -> None:
    """Idempotency gate: signature in ``already_escalated`` short-
    circuits to None so the same pattern does not page the operator
    twice in one graph run."""

    history = [_entry()] * 3
    decision = should_escalate_vite_pattern(history)
    assert decision is not None
    decision2 = should_escalate_vite_pattern(
        history, already_escalated=[decision.pattern],
    )
    assert decision2 is None


def test_should_escalate_returns_decision_for_new_pattern_after_old_escalated() -> None:
    """If a *different* pattern reaches threshold after a prior one
    was escalated, the new pattern escalates fresh."""

    old_history = [_entry(file="src/A.tsx", line=1, message="a")] * 3
    decision_a = should_escalate_vite_pattern(old_history)
    assert decision_a is not None

    new_history = old_history + [
        _entry(file="src/B.tsx", line=2, message="b"),
    ] * 3
    decision_b = should_escalate_vite_pattern(
        new_history, already_escalated=[decision_a.pattern],
    )
    assert decision_b is not None
    assert decision_b.pattern != decision_a.pattern


def test_should_escalate_respects_custom_threshold_override() -> None:
    """Threshold can be overridden for testing — at threshold=2, two
    consecutive same-sig entries escalate."""

    history = [_entry()] * 2
    decision = should_escalate_vite_pattern(history, threshold=2)
    assert decision is not None
    assert decision.threshold == 2


def test_should_escalate_rejects_non_int_threshold() -> None:
    with pytest.raises(TypeError):
        should_escalate_vite_pattern([_entry()] * 3, threshold="3")  # type: ignore[arg-type]


def test_should_escalate_rejects_zero_threshold() -> None:
    with pytest.raises(ValueError):
        should_escalate_vite_pattern([_entry()] * 3, threshold=0)


def test_should_escalate_rejects_negative_threshold() -> None:
    with pytest.raises(ValueError):
        should_escalate_vite_pattern([_entry()] * 3, threshold=-1)


def test_should_escalate_returns_none_for_empty_history() -> None:
    assert should_escalate_vite_pattern([]) is None


def test_should_escalate_returns_none_for_no_vite_entries() -> None:
    assert should_escalate_vite_pattern(["tool: x", "tool: y"]) is None


# ────────────────────────────────────────────────────────────────────
# §D — format_vite_escalation_banner
# ────────────────────────────────────────────────────────────────────


def test_banner_renders_with_canonical_inputs() -> None:
    rendered = format_vite_escalation_banner(
        pattern="vite[transform] src/App.tsx:42: compile:",
        count=3,
        threshold=3,
    )
    assert "Vite build error pattern repeated 3×" in rendered
    assert "vite[transform] src/App.tsx:42: compile:" in rendered
    assert "threshold 3" in rendered


def test_banner_truncates_at_byte_cap() -> None:
    """A pathological signature longer than the cap must truncate
    without raising."""

    huge_pattern = "vite[transform] " + "A" * 4096 + ":1: compile:"
    rendered = format_vite_escalation_banner(
        pattern=huge_pattern, count=3, threshold=3,
    )
    assert len(rendered.encode("utf-8")) <= MAX_VITE_ESCALATION_BANNER_BYTES


def test_banner_multibyte_codepoint_safe() -> None:
    """CJK + emoji codepoints in the pattern must truncate on a
    codepoint boundary so the rendered banner stays decodable."""

    cjk_pattern = "vite[transform] " + ("中文" * 200) + ":1: compile:"
    rendered = format_vite_escalation_banner(
        pattern=cjk_pattern, count=3, threshold=3,
    )
    rendered.encode("utf-8")  # round-trip — will raise if mid-codepoint


# ────────────────────────────────────────────────────────────────────
# §E — emit_vite_pattern_escalation
# ────────────────────────────────────────────────────────────────────


class TestEmitViteEscalation:

    def test_emits_one_pipeline_phase_and_one_debug_finding(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One escalation publishes one pipeline_phase event AND one
        debug_finding event — not just one or the other."""

        from backend.web import vite_retry_budget as mod
        from backend import events as events_mod

        captured_phases: list[tuple[str, str]] = []
        captured_findings: list[dict[str, Any]] = []

        def _fake_phase(phase: str, detail: str = "", **_kw: Any) -> None:
            captured_phases.append((phase, detail))

        def _fake_finding(
            *, task_id: str, agent_id: str, finding_type: str,
            severity: str, message: str, context: dict | None = None,
            **_kw: Any,
        ) -> None:
            captured_findings.append({
                "task_id": task_id, "agent_id": agent_id,
                "finding_type": finding_type, "severity": severity,
                "message": message, "context": context or {},
            })

        monkeypatch.setattr(events_mod, "emit_pipeline_phase", _fake_phase)
        monkeypatch.setattr(events_mod, "emit_debug_finding", _fake_finding)

        decision = ViteRetryBudgetEscalation(
            pattern="vite[transform] src/App.tsx:42: compile:",
            count=3, threshold=3,
        )
        emit_vite_pattern_escalation(
            task_id="task-99", agent_id="general", decision=decision,
        )

        assert len(captured_phases) == 1
        assert captured_phases[0][0] == VITE_ESCALATION_PIPELINE_PHASE
        assert "Vite build error pattern repeated 3×" in captured_phases[0][1]

        assert len(captured_findings) == 1
        f = captured_findings[0]
        assert f["task_id"] == "task-99"
        assert f["agent_id"] == "general"
        assert f["finding_type"] == VITE_ESCALATION_FINDING_TYPE
        assert f["severity"] == "error"
        assert "Vite build error pattern repeated" in f["message"]
        assert f["context"]["pattern"] == decision.pattern
        assert f["context"]["count"] == 3
        assert f["context"]["threshold"] == 3

    def test_emits_with_empty_task_and_agent_ids(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing task_id / agent_id (anonymous graph runs) must not
        crash — the emission still happens with empty strings."""

        from backend import events as events_mod
        captured: list[dict[str, Any]] = []

        def _fake_finding(**kw: Any) -> None:
            captured.append(kw)

        monkeypatch.setattr(events_mod, "emit_pipeline_phase", lambda *a, **k: None)
        monkeypatch.setattr(events_mod, "emit_debug_finding", _fake_finding)

        decision = ViteRetryBudgetEscalation(
            pattern="vite[transform] x:1: compile:", count=3, threshold=3,
        )
        emit_vite_pattern_escalation(
            task_id="", agent_id="", decision=decision,
        )

        assert len(captured) == 1
        assert captured[0]["task_id"] == ""
        assert captured[0]["agent_id"] == ""


# ────────────────────────────────────────────────────────────────────
# §F — _maybe_emit_vite_retry_budget helper in agents.nodes
# ────────────────────────────────────────────────────────────────────


class TestMaybeEmitViteRetryBudget:

    def test_returns_empty_dict_for_empty_history(self) -> None:
        from backend.agents.state import GraphState
        from backend.agents import nodes

        state = GraphState(user_command="x", routed_to="general")
        assert nodes._maybe_emit_vite_retry_budget(state) == {}

    def test_returns_empty_dict_for_non_vite_history(self) -> None:
        from backend.agents.state import GraphState
        from backend.agents import nodes

        state = GraphState(
            user_command="x", routed_to="general",
            error_history=["ToolError: foo"] * 5,
        )
        assert nodes._maybe_emit_vite_retry_budget(state) == {}

    def test_returns_empty_dict_under_threshold(self) -> None:
        from backend.agents.state import GraphState
        from backend.agents import nodes

        state = GraphState(
            user_command="x", routed_to="general",
            error_history=[_entry()] * 2,
        )
        assert nodes._maybe_emit_vite_retry_budget(state) == {}

    def test_returns_escalation_dict_at_threshold(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend import events as events_mod
        from backend.agents.state import GraphState
        from backend.agents import nodes

        monkeypatch.setattr(events_mod, "emit_pipeline_phase", lambda *a, **k: None)
        monkeypatch.setattr(events_mod, "emit_debug_finding", lambda **kw: None)

        state = GraphState(
            user_command="x", routed_to="general",
            error_history=[_entry()] * 3,
        )
        result = nodes._maybe_emit_vite_retry_budget(state)
        assert "vite_escalated_signatures" in result
        assert len(result["vite_escalated_signatures"]) == 1
        assert result["vite_escalated_signatures"][0].startswith(
            VITE_ERROR_HISTORY_KEY_PREFIX
        )

    def test_idempotent_across_two_consecutive_turns(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two consecutive specialist-node calls observing the same
        trailing-3 trail must escalate exactly once.  The first call
        emits and seeds ``vite_escalated_signatures``; the second
        call sees the seed and short-circuits to ``{}``."""

        from backend import events as events_mod
        from backend.agents.state import GraphState
        from backend.agents import nodes

        emit_count = {"finding": 0, "phase": 0}

        def _bump_phase(*a: Any, **k: Any) -> None:
            emit_count["phase"] += 1

        def _bump_finding(**kw: Any) -> None:
            emit_count["finding"] += 1

        monkeypatch.setattr(events_mod, "emit_pipeline_phase", _bump_phase)
        monkeypatch.setattr(events_mod, "emit_debug_finding", _bump_finding)

        history = [_entry()] * 3
        state1 = GraphState(
            user_command="x", routed_to="general",
            error_history=history,
        )
        result1 = nodes._maybe_emit_vite_retry_budget(state1)
        assert result1 != {}
        assert emit_count == {"finding": 1, "phase": 1}

        # Simulate the next LangGraph turn: state.vite_escalated_signatures
        # carries the prior emission's signature, error_history unchanged.
        state2 = GraphState(
            user_command="x", routed_to="general",
            error_history=history,
            vite_escalated_signatures=result1["vite_escalated_signatures"],
        )
        result2 = nodes._maybe_emit_vite_retry_budget(state2)
        assert result2 == {}
        assert emit_count == {"finding": 1, "phase": 1}

    def test_appends_not_replaces_when_new_pattern_escalates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A second pattern reaching threshold appends to the
        existing ``vite_escalated_signatures`` rather than replacing
        it.  The reducer is REPLACE (per state.py:115-121) so the
        helper must return the *full* accumulated list."""

        from backend import events as events_mod
        from backend.agents.state import GraphState
        from backend.agents import nodes

        monkeypatch.setattr(events_mod, "emit_pipeline_phase", lambda *a, **k: None)
        monkeypatch.setattr(events_mod, "emit_debug_finding", lambda **kw: None)

        prior_sig = "vite[transform] src/Old.tsx:1: compile:"
        history = [_entry(file="src/New.tsx", line=2, message="new")] * 3
        state = GraphState(
            user_command="x", routed_to="general",
            error_history=history,
            vite_escalated_signatures=[prior_sig],
        )
        result = nodes._maybe_emit_vite_retry_budget(state)
        assert prior_sig in result["vite_escalated_signatures"]
        assert len(result["vite_escalated_signatures"]) == 2


# ────────────────────────────────────────────────────────────────────
# §G — Specialist node end-to-end
# ────────────────────────────────────────────────────────────────────


class TestSpecialistNodeIncludesViteEscalation:

    def test_specialist_node_returns_vite_escalated_signatures_at_threshold(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The specialist node must spread ``vite_escalated_signatures``
        into its return dict whenever the gate fires.  Pinned via a
        fake LLM that returns a no-tool answer so we exercise the
        direct-answer return path."""

        from backend import events as events_mod
        from backend.agents.state import GraphState
        from backend.agents import nodes

        monkeypatch.setattr(events_mod, "emit_pipeline_phase", lambda *a, **k: None)
        monkeypatch.setattr(events_mod, "emit_debug_finding", lambda **kw: None)

        class _Resp:
            content = "hello"
            tool_calls: list[Any] = []

        class _FakeLLM:
            def invoke(self, _msgs: Any) -> Any:
                return _Resp()

        monkeypatch.setattr(nodes, "_get_llm", lambda **kw: _FakeLLM())
        monkeypatch.setattr(nodes, "build_system_prompt", lambda **kw: "fake prompt")
        monkeypatch.setattr(nodes, "_resolve_skill_loading_mode", lambda _x: "eager")

        state = GraphState(
            user_command="fix",
            routed_to="general",
            error_history=[_entry()] * 3,
        )
        node = nodes._specialist_node_factory("general")
        ret = asyncio.run(node(state))

        assert "vite_escalated_signatures" in ret
        assert len(ret["vite_escalated_signatures"]) == 1

    def test_specialist_node_omits_vite_escalation_key_when_no_emission(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-Vite or under-threshold history must NOT add the
        ``vite_escalated_signatures`` key — the LangGraph reducer is
        REPLACE so an empty list would wipe any prior accumulated
        list (defence in depth)."""

        from backend.agents.state import GraphState
        from backend.agents import nodes

        class _Resp:
            content = "hello"
            tool_calls: list[Any] = []

        class _FakeLLM:
            def invoke(self, _msgs: Any) -> Any:
                return _Resp()

        monkeypatch.setattr(nodes, "_get_llm", lambda **kw: _FakeLLM())
        monkeypatch.setattr(nodes, "build_system_prompt", lambda **kw: "fake prompt")
        monkeypatch.setattr(nodes, "_resolve_skill_loading_mode", lambda _x: "eager")

        state = GraphState(
            user_command="fix",
            routed_to="general",
            error_history=[_entry()] * 2,  # under threshold
        )
        node = nodes._specialist_node_factory("general")
        ret = asyncio.run(node(state))

        assert "vite_escalated_signatures" not in ret

    def test_specialist_node_tool_call_path_includes_escalation(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The escalation must also be carried on the LLM-tool-call
        return path (not just the direct-answer path) so the next
        graph turn observes the gate when the agent retries via a
        tool call."""

        from backend import events as events_mod
        from backend.agents.state import GraphState
        from backend.agents import nodes

        monkeypatch.setattr(events_mod, "emit_pipeline_phase", lambda *a, **k: None)
        monkeypatch.setattr(events_mod, "emit_debug_finding", lambda **kw: None)

        class _Resp:
            content = "calling tool"
            tool_calls: list[Any] = [
                {"name": "read_file", "args": {"path": "x"}},
            ]

        class _FakeLLM:
            def invoke(self, _msgs: Any) -> Any:
                return _Resp()

        monkeypatch.setattr(nodes, "_get_llm", lambda **kw: _FakeLLM())
        monkeypatch.setattr(nodes, "build_system_prompt", lambda **kw: "fake prompt")
        monkeypatch.setattr(nodes, "_resolve_skill_loading_mode", lambda _x: "eager")

        state = GraphState(
            user_command="fix",
            routed_to="general",
            error_history=[_entry()] * 3,
        )
        node = nodes._specialist_node_factory("general")
        ret = asyncio.run(node(state))

        assert ret.get("tool_calls"), "tool-call path expected"
        assert "vite_escalated_signatures" in ret


# ────────────────────────────────────────────────────────────────────
# §H — Re-export surface
# ────────────────────────────────────────────────────────────────────


W15_4_SYMBOLS = [
    "MAX_VITE_ESCALATION_BANNER_BYTES",
    "VITE_ESCALATION_BANNER_TEMPLATE",
    "VITE_ESCALATION_FINDING_TYPE",
    "VITE_ESCALATION_PIPELINE_PHASE",
    "VITE_RETRY_BUDGET_THRESHOLD",
    "ViteRetryBudgetEscalation",
    "count_trailing_same_vite_signature",
    "emit_vite_pattern_escalation",
    "format_vite_escalation_banner",
    "should_escalate_vite_pattern",
]


@pytest.mark.parametrize("symbol", W15_4_SYMBOLS)
def test_w15_4_symbol_re_exported_via_package(symbol: str) -> None:
    from backend import web as web_pkg

    assert symbol in web_pkg.__all__, (
        f"{symbol} missing from backend.web.__all__"
    )
    assert hasattr(web_pkg, symbol), (
        f"{symbol} not attribute of backend.web"
    )


def test_state_field_added_to_graphstate() -> None:
    """The ``vite_escalated_signatures`` field must appear on the
    GraphState model so the LangGraph reducer plumbs it through."""

    from backend.agents.state import GraphState

    state = GraphState(user_command="x", routed_to="general")
    assert hasattr(state, "vite_escalated_signatures")
    assert state.vite_escalated_signatures == []
    # Defensive: the field must be a list (not a tuple) so the
    # specialist-node helper can append to a copy without TypeError.
    assert isinstance(state.vite_escalated_signatures, list)
