"""B3 — context-reset orchestrator integration tests (OP-830).

Exercises:
  - DetectorAwareDispatcher records tool calls and raises after 3x match
  - In-flight tool call is awaited before reset (ordering invariant)
  - run_with_resets restarts conversation with system+first-user preserved
  - Reset paragraph carries the looped tool_call_signature + error_class
  - Scratchpad persists across reset (AC #4 / Q3)
  - 3-reset abort -> ResetOutcome.aborted_terminal=True (AC #5)
  - on_attempt_usage fires for each successful runner attempt
    (CostGuard tally not reset — AC #4 / Q4)
  - Mid-resetting crash recovery: re-issuing reset is idempotent
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.agents.anthropic_native_client import RunResult, TokenUsage
from backend.agents.context_reset import (
    DetectorAwareDispatcher,
    LoopResetRequired,
    ResetOutcome,
    build_reset_paragraph,
    build_reset_user_prompt,
    run_with_resets,
)
from backend.agents.loop_detector import LoopDetector, ToolCallSignature
from backend.agents.tom_scratchpad import ToMScratchpad
from backend.agents.tool_dispatcher import ToolDispatcher, ToolResult


# ─── Test scaffolding ────────────────────────────────────────────────


class _StubInnerDispatcher:
    """Minimal duck-typed dispatcher: records inputs, returns canned
    tool results. Tracks whether ``execute()`` is awaited before the
    detector raises (used to verify the in-flight-await invariant)."""

    def __init__(
        self,
        *,
        result_sequence: list[ToolResult] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.completed_calls: list[str] = []
        self._results = result_sequence or []
        self._idx = 0

    def register(self, tool_name: str, handler: Any) -> Any:
        return handler

    def has_handler(self, tool_name: str) -> bool:
        return True

    def registered_tools(self) -> list[str]:
        return []

    async def execute(
        self, *, tool_use_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> ToolResult:
        self.calls.append((tool_name, dict(tool_input)))
        await asyncio.sleep(0)  # simulate real I/O — yield to event loop
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
        else:
            r = ToolResult(tool_use_id=tool_use_id, content="ok", is_error=False)
        self.completed_calls.append(tool_name)
        return r


# ─── DetectorAwareDispatcher unit ────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_records_each_tool_call():
    inner = _StubInnerDispatcher()
    detector = LoopDetector(ticket_key="OP-830")
    wrapped = DetectorAwareDispatcher(inner=inner, detector=detector)

    await wrapped.execute(
        tool_use_id="t1", tool_name="Glob", tool_input={"pattern": "**/*.py"},
    )
    assert len(detector.log) == 1
    assert detector.log[0].tool_name == "Glob"


@pytest.mark.asyncio
async def test_dispatcher_raises_after_third_match_only():
    inner = _StubInnerDispatcher()
    detector = LoopDetector(ticket_key="OP-830")
    wrapped = DetectorAwareDispatcher(inner=inner, detector=detector)

    # First two pass through cleanly.
    await wrapped.execute(
        tool_use_id="t1", tool_name="Glob", tool_input={"pattern": "x"},
    )
    await wrapped.execute(
        tool_use_id="t2", tool_name="Glob", tool_input={"pattern": "x"},
    )
    # Third raises.
    with pytest.raises(LoopResetRequired) as exc:
        await wrapped.execute(
            tool_use_id="t3", tool_name="Glob", tool_input={"pattern": "x"},
        )
    assert exc.value.signature.tool_name == "Glob"


@pytest.mark.asyncio
async def test_dispatcher_awaits_inflight_before_raising():
    """AC #4 invariant: 'In-flight tool call: awaited, not killed'.

    The 3rd call's ``inner.execute`` MUST complete (recorded in
    ``completed_calls``) before LoopResetRequired propagates.
    """
    inner = _StubInnerDispatcher()
    detector = LoopDetector(ticket_key="OP-830")
    wrapped = DetectorAwareDispatcher(inner=inner, detector=detector)

    args = {"pattern": "stuck"}
    for i in range(2):
        await wrapped.execute(
            tool_use_id=f"t{i}", tool_name="Bash", tool_input=args,
        )
    with pytest.raises(LoopResetRequired):
        await wrapped.execute(
            tool_use_id="t-final", tool_name="Bash", tool_input=args,
        )
    # Critical: the inner dispatcher saw the 3rd call land cleanly.
    assert len(inner.completed_calls) == 3


@pytest.mark.asyncio
async def test_dispatcher_classifies_error_class_from_tool_result():
    err_payload = json.dumps({"error": "bash_metachar_blocked"})
    inner = _StubInnerDispatcher(
        result_sequence=[
            ToolResult(tool_use_id="t1", content=err_payload, is_error=True),
            ToolResult(tool_use_id="t2", content=err_payload, is_error=True),
            ToolResult(tool_use_id="t3", content=err_payload, is_error=True),
        ]
    )
    detector = LoopDetector(ticket_key="OP-830")
    wrapped = DetectorAwareDispatcher(inner=inner, detector=detector)

    args = {"command": "ls | grep x"}
    for i in range(2):
        await wrapped.execute(
            tool_use_id=f"t{i+1}", tool_name="Bash", tool_input=args,
        )
    with pytest.raises(LoopResetRequired) as exc:
        await wrapped.execute(
            tool_use_id="t3", tool_name="Bash", tool_input=args,
        )
    assert exc.value.signature.error_class == "bash_metachar_blocked"


# ─── run_with_resets — happy path / single reset ─────────────────────


@pytest.mark.asyncio
async def test_run_with_resets_single_attempt_no_reset_returns_runresult():
    detector = LoopDetector(ticket_key="OP-830")
    scratchpad = ToMScratchpad()

    canned = RunResult(
        final_text="done",
        iterations=1,
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )

    async def runner(*, prompt: str) -> RunResult:
        return canned

    outcome = await run_with_resets(
        runner=runner,
        detector=detector,
        scratchpad=scratchpad,
        first_user_message="implement OP-830",
    )
    assert outcome.aborted_terminal is False
    assert outcome.reset_count == 0
    assert outcome.final_result is canned
    assert outcome.total_usage.input_tokens == 10


@pytest.mark.asyncio
async def test_run_with_resets_one_reset_then_succeeds():
    """First runner raise => reset; second runner returns RunResult."""
    detector = LoopDetector(ticket_key="OP-830")
    scratchpad = ToMScratchpad()

    sig = ToolCallSignature(
        tool_name="Glob", args_hash="a" * 16, error_class="bash_metachar_blocked",
    )
    attempts: list[str] = []

    async def runner(*, prompt: str) -> RunResult:
        attempts.append(prompt)
        if len(attempts) == 1:
            raise LoopResetRequired(signature=sig)
        return RunResult(
            final_text="recovered", iterations=1, stop_reason="end_turn",
            usage=TokenUsage(input_tokens=20, output_tokens=10),
        )

    outcome = await run_with_resets(
        runner=runner,
        detector=detector,
        scratchpad=scratchpad,
        first_user_message="ORIGINAL TICKET TEXT",
    )
    assert outcome.aborted_terminal is False
    assert outcome.reset_count == 1
    assert outcome.final_result is not None
    assert outcome.final_result.final_text == "recovered"
    # Second attempt's prompt must contain the original first user message
    # AND the reset paragraph (AC #4 — keep first user message + inject).
    assert "ORIGINAL TICKET TEXT" in attempts[1]
    assert "PRIOR ATTEMPT (now reset)" in attempts[1]
    assert "Glob" in attempts[1]
    assert "bash_metachar_blocked" in attempts[1]


# ─── 3-reset abort (AC #5) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_resets_then_terminal_abort():
    detector = LoopDetector(ticket_key="OP-830")
    scratchpad = ToMScratchpad()

    sig = ToolCallSignature(
        tool_name="Bash", args_hash="b" * 16, error_class="ok",
    )

    async def runner(*, prompt: str) -> RunResult:
        # Always loops — orchestrator must abort after 3 resets.
        raise LoopResetRequired(signature=sig)

    outcome = await run_with_resets(
        runner=runner,
        detector=detector,
        scratchpad=scratchpad,
        first_user_message="x",
    )
    assert outcome.aborted_terminal is True
    assert outcome.reset_count == detector.reset_limit
    assert outcome.aborted_signature == sig


# ─── Scratchpad survives reset (AC #4 / Q3) ──────────────────────────


@pytest.mark.asyncio
async def test_scratchpad_persists_across_reset(tmp_path):
    progress_path = tmp_path / "progress.txt"
    detector = LoopDetector(ticket_key="OP-830")
    scratchpad = ToMScratchpad(progress_path=progress_path)
    scratchpad.append(
        {"hypothesis": "wrong path", "verifying": "Glob", "outcome": "fail"}
    )

    sig = ToolCallSignature(
        tool_name="Glob", args_hash="c" * 16, error_class="ok",
    )

    async def runner(*, prompt: str) -> RunResult:
        # On the 2nd call the scratchpad should already contain the
        # pre-reset entry (it was appended before the reset fired).
        return RunResult(
            final_text=prompt, iterations=1, stop_reason="end_turn",
            usage=TokenUsage(),
        )

    # Trigger one reset by raising once.
    state = {"raised": False}

    async def loopy_runner(*, prompt: str) -> RunResult:
        if not state["raised"]:
            state["raised"] = True
            raise LoopResetRequired(signature=sig)
        return RunResult(
            final_text=prompt, iterations=1, stop_reason="end_turn",
            usage=TokenUsage(),
        )

    outcome = await run_with_resets(
        runner=loopy_runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ORIG",
    )
    assert outcome.final_result is not None
    # The reset paragraph contains the scratchpad summary.
    assert "Prior ToM scratchpad" in outcome.final_result.final_text
    assert "wrong path" in outcome.final_result.final_text
    # Persistence on disk survives independently of the reset.
    reloaded = ToMScratchpad.load(progress_path)
    assert len(reloaded) == 1


# ─── on_attempt_usage fires per attempt (AC #4 / Q4) ─────────────────


@pytest.mark.asyncio
async def test_on_attempt_usage_called_for_each_successful_runner_return():
    """CostGuard tally is NOT reset across resets — every runner return
    must surface its TokenUsage to the launcher's cost recorder."""
    detector = LoopDetector(ticket_key="OP-830")
    scratchpad = ToMScratchpad()

    sig = ToolCallSignature(
        tool_name="Bash", args_hash="d" * 16, error_class="ok",
    )

    state = {"step": 0}
    usages_seen: list[TokenUsage] = []

    async def runner(*, prompt: str) -> RunResult:
        state["step"] += 1
        if state["step"] == 1:
            raise LoopResetRequired(signature=sig)
        if state["step"] == 2:
            return RunResult(
                final_text="ok", iterations=1, stop_reason="end_turn",
                usage=TokenUsage(input_tokens=100, output_tokens=50),
            )
        raise AssertionError("runner called more than expected")

    async def record(u: TokenUsage) -> None:
        usages_seen.append(u)

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="x", on_attempt_usage=record,
    )
    assert outcome.aborted_terminal is False
    # Only the 2nd attempt returned a RunResult — that one's usage was
    # recorded. The 1st attempt raised LoopResetRequired and produced
    # no observable usage (documented limitation in module docstring).
    assert len(usages_seen) == 1
    assert usages_seen[0].input_tokens == 100
    assert outcome.total_usage.input_tokens == 100


# ─── Reset paragraph wording (AC #4 / Q2) ────────────────────────────


def test_reset_paragraph_wording_matches_spec():
    sig = ToolCallSignature(
        tool_name="Glob", args_hash="e" * 16, error_class="bash_metachar_blocked",
    )
    p = build_reset_paragraph(signature=sig)
    assert "PRIOR ATTEMPT (now reset)" in p
    assert "you tried Glob" in p
    assert "3 times with error bash_metachar_blocked" in p
    assert "Try a different approach" in p


def test_reset_user_prompt_keeps_original_first_user_message():
    sig = ToolCallSignature(
        tool_name="Bash", args_hash="f" * 16, error_class="ok",
    )
    out = build_reset_user_prompt(
        first_user_message="### Implement OP-830\n... full ticket body ...",
        signature=sig,
    )
    assert "Implement OP-830" in out
    assert "PRIOR ATTEMPT" in out


# ─── Crash-recovery idempotency ──────────────────────────────────────


@pytest.mark.asyncio
async def test_re_issuing_reset_is_idempotent_in_count():
    """Per master plan §2.5 Recovery: 'On resume mid-resetting, re-issue
    reset (idempotent); failed-reset attempt counts toward N=3 cap.'

    Concretely: each ``mark_reset`` increments by 1, regardless of
    in-flight state at the time of crash. We assert two consecutive
    ``mark_reset`` calls increment by 2, never accidentally collapse
    into 1 (which would let the model exceed the raw-attempts cap)."""
    d = LoopDetector(ticket_key="OP-830")
    d.mark_reset()
    d.mark_reset()
    assert d.reset_count == 2
    d.mark_reset()
    assert d.reset_count == 3
    assert d.is_terminal is True
