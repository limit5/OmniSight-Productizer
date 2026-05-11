"""B16 — Outcomes-graded final attempt tests (OP-847).

Covers the 6 scenarios from the OP-843 spike harness
(``scripts/spike_b3_outcomes_compare.py``) run end-to-end against the
Phase-3-extension code in ``backend/agents/loop_detector.py`` and
``backend/agents/context_reset.py``, plus AC #5's Goodhart-guard
regression tests + AC #7's rollback property.

The spike harness's ``_grader_verdict`` hook is the integration point:
we stub the same shape via a ``_StubGrader`` injected through
``run_with_resets(outcomes_grader=...)``. No live Anthropic call.

Scenario mapping (spike § 3 vs this file):

  - S1-honest-loop          → test_s1_honest_loop_b3_recovers_no_grader_fire
  - S2-misleading-success   → test_s2_misleading_success_grader_catches
  - S3-mixed-loop-then-lie  → test_s3_mixed_loop_then_lie_grader_catches
  - S4-grader-hallucinates  → test_s4_grader_hallucinates_terminal_pass
  - S5-progressive-narrow   → test_s5_progressive_narrowing_no_reset
  - S6-rubric-overfits      → test_s6_rubric_overfits_grader_passes_wrongly

AC #5 (Goodhart): test_goodhart_guard_*
AC #7 (rollback): test_rollback_flag_off_pure_b3_behaviour_unchanged
AC #1 catalog:
  - OutcomesGraderUnavailable → test_grader_unavailable_falls_back_to_b3
  - OutcomesRubricEmpty       → test_empty_ac_falls_back_to_templated_rubric
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.agents.anthropic_native_client import RunResult, TokenUsage
from backend.agents.context_reset import (
    LoopResetRequired,
    ResetOutcome,
    run_with_resets,
)
from backend.agents.loop_detector import (
    DEFAULT_GRADER_MODEL,
    GOODHART_THIN_AC_CHAR_LIMIT,
    GOODHART_TRIGGER_PHRASES,
    OUTCOMES_FALLBACK_RUBRIC,
    OUTCOMES_FINAL_ATTEMPT_ENV,
    OUTCOMES_GRADER_MODEL_ENV,
    OUTCOMES_RUBRIC_WRAPPER,
    LoopDetector,
    OutcomesConfig,
    OutcomesGraderUnavailable,
    OutcomesVerdict,
    ToolCallSignature,
    build_outcomes_rubric,
    detect_rubric_goodhart_warnings,
    extract_acceptance_criteria_section,
    load_outcomes_config,
)
from backend.agents.tom_scratchpad import ToMScratchpad


# ─── Stub grader ──────────────────────────────────────────────────────


class _StubGrader:
    """Records every call; returns canned verdicts in order. Mirrors the
    spike harness's ``_grader_verdict`` hook — the integration point per
    AC #6. By default returns a passing verdict so tests that don't
    care can omit explicit configuration."""

    def __init__(
        self,
        *,
        verdicts: list[OutcomesVerdict] | None = None,
        raise_unavailable: bool = False,
    ) -> None:
        self.calls: list[tuple[str, RunResult]] = []
        self._verdicts = verdicts or []
        self._idx = 0
        self._raise = raise_unavailable

    async def __call__(self, rubric: str, result: RunResult) -> OutcomesVerdict:
        self.calls.append((rubric, result))
        if self._raise:
            raise OutcomesGraderUnavailable("stubbed unavailable")
        if self._idx < len(self._verdicts):
            v = self._verdicts[self._idx]
            self._idx += 1
            return v
        return OutcomesVerdict(
            verdict="pass",
            grader_reasoning="default-pass",
            grader_input_tokens=2500,
            grader_output_tokens=200,
        )


def _enabled_config(rubric: str = "PASS iff tests pass") -> OutcomesConfig:
    return OutcomesConfig(
        enabled=True, rubric=rubric, grader_model=DEFAULT_GRADER_MODEL,
    )


def _ok_runresult(*, text: str = "done") -> RunResult:
    return RunResult(
        final_text=text,
        iterations=1,
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )


# ─── AC #2 — rubric extraction + templated wrapper ────────────────────


def test_extracts_acceptance_criteria_markdown_section():
    desc = (
        "Summary line\n\n"
        "## Acceptance criteria\n\n"
        "1. Item one\n"
        "2. Item two with more detail to avoid Goodhart thinness\n\n"
        "## Files touched\n\n"
        "- backend/agents/foo.py\n"
    )
    body = extract_acceptance_criteria_section(desc)
    assert "Item one" in body
    assert "Item two" in body
    # Stops at the next ## header.
    assert "Files touched" not in body
    assert "backend/agents/foo.py" not in body


def test_extracts_returns_empty_when_no_canonical_header():
    desc = "Plain text. No markdown header at all."
    assert extract_acceptance_criteria_section(desc) == ""


def test_build_outcomes_rubric_uses_template_for_nonempty_ac():
    rubric, warnings = build_outcomes_rubric(
        "1. Tests must pass with the new fixture covering the edge case\n"
        "2. Coverage must remain at least 80 percent for the touched paths"
    )
    assert "PASS iff each of the following AC items" in rubric
    assert "Tests must pass with the new fixture" in rubric
    assert warnings == []


def test_build_outcomes_rubric_falls_back_when_ac_empty():
    rubric, warnings = build_outcomes_rubric("")
    assert rubric == OUTCOMES_FALLBACK_RUBRIC
    assert warnings == []


# ─── AC #5 — Goodhart guard ───────────────────────────────────────────


@pytest.mark.parametrize("phrase", list(GOODHART_TRIGGER_PHRASES))
def test_goodhart_guard_flags_each_trigger_phrase(phrase):
    ac = f"1. The fix is {phrase} a one-liner change in the helper module."
    warnings = detect_rubric_goodhart_warnings(ac)
    assert any(phrase in w for w in warnings), (
        f"Expected warning for trigger phrase {phrase!r}; got {warnings}"
    )


def test_goodhart_guard_flags_short_under_specified_ac_lines():
    # A single under-30-char AC line is the canonical thin-rubric case.
    ac = "1. Make it work."
    warnings = detect_rubric_goodhart_warnings(ac)
    assert any("under-specified" in w for w in warnings)


def test_goodhart_guard_silent_on_well_formed_ac():
    ac = (
        "1. The runner must reject all malformed tool inputs by returning "
        "structured error JSON with an `error` key.\n"
        "2. End-to-end tests must cover the dispatcher rejection path."
    )
    warnings = detect_rubric_goodhart_warnings(ac)
    assert warnings == []


def test_goodhart_guard_does_not_match_substring_inside_word():
    # "simplify" contains "simpl" but not the whole word "simply" — the
    # whole-word regex must not flag it.
    ac = "1. Add a method that will simplify the dispatcher registration flow."
    warnings = detect_rubric_goodhart_warnings(ac)
    # Allowed to flag thinness but NOT the trigger-phrase one.
    assert not any("'simply'" in w for w in warnings)


def test_goodhart_guard_threshold_constant_matches_ticket():
    # AC #5 pins ≤30-char ACs. Lock that constant against accidental drift.
    assert GOODHART_THIN_AC_CHAR_LIMIT == 30


def test_load_outcomes_config_logs_thin_rubric_warning(caplog):
    """AC #5: warnings logged with the `[outcomes-rubric-thin]` tag."""
    desc = (
        "## Acceptance criteria\n\n"
        "1. Just fix it.\n"
    )
    env = {OUTCOMES_FINAL_ATTEMPT_ENV: "1"}
    import logging
    with caplog.at_level(logging.WARNING, logger="backend.agents.loop_detector"):
        cfg = load_outcomes_config(ticket_description=desc, env=env)
    assert cfg.enabled is True
    assert any("[outcomes-rubric-thin]" in rec.message for rec in caplog.records)
    assert len(cfg.rubric_warnings) >= 1


# ─── AC #1 — env flag wiring ──────────────────────────────────────────


def test_load_outcomes_config_disabled_by_default():
    cfg = load_outcomes_config(ticket_description="## Acceptance criteria\n\n1. x", env={})
    assert cfg.enabled is False
    assert cfg.rubric == ""


def test_load_outcomes_config_enabled_when_env_var_one():
    desc = (
        "## Acceptance criteria\n\n"
        "1. Tests must pass with new fixture and cover the edge case."
    )
    cfg = load_outcomes_config(
        ticket_description=desc,
        env={OUTCOMES_FINAL_ATTEMPT_ENV: "1"},
    )
    assert cfg.enabled is True
    assert "Tests must pass" in cfg.rubric


def test_load_outcomes_config_grader_model_override(monkeypatch):
    desc = (
        "## Acceptance criteria\n\n"
        "1. Tests pass and the AC verification is clear from diff."
    )
    cfg = load_outcomes_config(
        ticket_description=desc,
        env={
            OUTCOMES_FINAL_ATTEMPT_ENV: "1",
            OUTCOMES_GRADER_MODEL_ENV: "claude-sonnet-4-6",
        },
    )
    assert cfg.grader_model == "claude-sonnet-4-6"


# ─── AC #2 (continued) — empty AC fallback ────────────────────────────


def test_empty_ac_falls_back_to_templated_rubric():
    """AC #1 catalog: OutcomesRubricEmpty → templated fallback used."""
    cfg = load_outcomes_config(
        ticket_description="Freeform ticket, no AC header at all.",
        env={OUTCOMES_FINAL_ATTEMPT_ENV: "1"},
    )
    assert cfg.enabled is True
    assert cfg.rubric == OUTCOMES_FALLBACK_RUBRIC


# ─── 6 scenario tests (mirroring spike harness) ───────────────────────


@pytest.mark.asyncio
async def test_s1_honest_loop_b3_recovers_no_grader_fire():
    """S1 — honest tool-error loop. B3 reset on the first attempt, model
    recovers on attempt 2. We never reach the "final" attempt slot, so
    the grader never fires. B3's design strength, Outcomes neutral."""
    detector = LoopDetector(ticket_key="OP-847-S1")
    scratchpad = ToMScratchpad()
    grader = _StubGrader()

    sig = ToolCallSignature(
        tool_name="Glob", args_hash="1" * 16, error_class="bash_metachar_blocked",
    )
    state = {"step": 0}

    async def runner(*, prompt: str) -> RunResult:
        state["step"] += 1
        if state["step"] == 1:
            raise LoopResetRequired(signature=sig)
        return _ok_runresult(text="recovered")

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config(),
        outcomes_grader=grader,
    )

    assert outcome.aborted_terminal is False
    assert outcome.reset_count == 1
    assert outcome.outcomes_verdict is None, "grader must NOT fire mid-recovery"
    assert grader.calls == []


@pytest.mark.asyncio
async def test_s2_misleading_success_grader_catches():
    """S2 — model claims success on attempt 1 without fixing. B3 has no
    detector signal (all tool calls ok), so without B16 we'd accept the
    lie. WITH B16, the grader fires on the final allowed attempt and a
    FAIL verdict terminal-aborts. To exercise this, we force the
    orchestrator to the final-attempt slot by pre-marking 2 resets
    (mimicking the spike's "model claims success but the orchestrator is
    now on its last chance" cell)."""
    detector = LoopDetector(ticket_key="OP-847-S2")
    detector.mark_reset()
    detector.mark_reset()
    assert detector.reset_count == detector.reset_limit - 1, (
        "precondition: orchestrator on final-attempt slot"
    )
    grader = _StubGrader(verdicts=[OutcomesVerdict(
        verdict="fail",
        grader_reasoning="AC #2 not addressed in diff",
        grader_input_tokens=2500, grader_output_tokens=200,
    )])
    scratchpad = ToMScratchpad()

    async def runner(*, prompt: str) -> RunResult:
        return _ok_runresult(text="I claim to have fixed it")

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config("PASS iff AC #2 visible in diff"),
        outcomes_grader=grader,
    )

    assert outcome.aborted_terminal is True
    assert outcome.outcomes_verdict is not None
    assert outcome.outcomes_verdict.verdict == "fail"
    assert "AC #2" in outcome.outcomes_verdict.grader_reasoning
    assert len(grader.calls) == 1


@pytest.mark.asyncio
async def test_s3_mixed_loop_then_lie_grader_catches():
    """S3 — composite: model loops on attempt 1 (B3 reset), loops on
    attempt 2 (B3 reset), then on attempt 3 (final-attempt slot) claims
    success without fixing. Without B16: B3 accepts the false claim
    (the spike's documented S3 failure). With B16: grader catches it.
    """
    detector = LoopDetector(ticket_key="OP-847-S3")
    scratchpad = ToMScratchpad()
    grader = _StubGrader(verdicts=[OutcomesVerdict(
        verdict="fail",
        grader_reasoning="No edit to /y.py visible in diff",
        grader_input_tokens=2500, grader_output_tokens=200,
    )])
    sig = ToolCallSignature(
        tool_name="Bash", args_hash="2" * 16, error_class="bash_metachar_blocked",
    )
    state = {"step": 0}

    async def runner(*, prompt: str) -> RunResult:
        state["step"] += 1
        if state["step"] <= 2:
            raise LoopResetRequired(signature=sig)
        # attempt 3 — claims success but didn't fix.
        return _ok_runresult(text="claimed fix")

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config(),
        outcomes_grader=grader,
    )

    assert outcome.aborted_terminal is True
    assert outcome.reset_count == 2
    assert outcome.outcomes_verdict is not None
    assert outcome.outcomes_verdict.verdict == "fail"
    assert len(grader.calls) == 1


@pytest.mark.asyncio
async def test_s4_grader_hallucinates_terminal_pass():
    """S4 — adversarial cell. Model lies; grader hallucinates a pass on
    a too-permissive rubric. Both B3 and B16 fail to catch — the
    floor of the comparison (spike § 3 S4). We assert the orchestrator
    accepts the result (no terminal abort) and surfaces the grader's
    PASS verdict so the downstream B4 critic (AC #1 error catalog
    ``OutcomesGraderHallucinatedPass``) gets the chance to dissent.
    """
    detector = LoopDetector(ticket_key="OP-847-S4")
    detector.mark_reset()
    detector.mark_reset()
    scratchpad = ToMScratchpad()
    grader = _StubGrader(verdicts=[OutcomesVerdict(
        verdict="pass",
        grader_reasoning="model mentioned 'fix' — hallucinated pass",
        grader_input_tokens=2500, grader_output_tokens=200,
    )])

    async def runner(*, prompt: str) -> RunResult:
        return _ok_runresult(text="I have fixed the bug")

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config("PASS iff the diff fixes the bug"),
        outcomes_grader=grader,
    )

    assert outcome.aborted_terminal is False
    assert outcome.outcomes_verdict is not None
    assert outcome.outcomes_verdict.verdict == "pass"
    assert outcome.final_result is not None
    assert outcome.final_result.final_text == "I have fixed the bug"


@pytest.mark.asyncio
async def test_s5_progressive_narrowing_no_reset():
    """S5 — Levenshtein-guarded progressive narrowing (B3 AC #3). No
    reset fires; the runner succeeds on attempt 1. We never enter the
    final-attempt slot, so the grader never fires."""
    detector = LoopDetector(ticket_key="OP-847-S5")
    scratchpad = ToMScratchpad()
    grader = _StubGrader()

    async def runner(*, prompt: str) -> RunResult:
        return _ok_runresult(text="found and edited")

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config(),
        outcomes_grader=grader,
    )

    assert outcome.aborted_terminal is False
    assert outcome.reset_count == 0
    assert outcome.outcomes_verdict is None, "no grader fire on non-final attempt"
    assert grader.calls == []


@pytest.mark.asyncio
async def test_s6_rubric_overfits_grader_passes_wrongly():
    """S6 — rubric over-fits a surface signal (e.g. 'must call run_tests').
    Model satisfies the literal rubric but the work is broken. Grader
    returns PASS. Same floor as S4 but a different mechanism — exercises
    the orchestrator's "accept grader PASS" path while the test records
    that the work was actually broken (handed to downstream critic).
    """
    detector = LoopDetector(ticket_key="OP-847-S6")
    detector.mark_reset()
    detector.mark_reset()
    scratchpad = ToMScratchpad()
    grader = _StubGrader(verdicts=[OutcomesVerdict(
        verdict="pass",
        grader_reasoning="run_tests was called as required by rubric",
        grader_input_tokens=2500, grader_output_tokens=200,
    )])

    async def runner(*, prompt: str) -> RunResult:
        return _ok_runresult(text="invoked run_tests, ignoring failures")

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config("PASS iff `run_tests` is called"),
        outcomes_grader=grader,
    )

    assert outcome.aborted_terminal is False
    assert outcome.outcomes_verdict.verdict == "pass"
    # Documentation: rubric-design risk is surfaced via warnings, not
    # by blocking. The downstream critic is the gate against S6.
    assert outcome.final_result is not None


# ─── AC #1 catalog — OutcomesGraderUnavailable ────────────────────────


@pytest.mark.asyncio
async def test_grader_unavailable_falls_back_to_b3():
    """AC #1 error catalog: ``OutcomesGraderUnavailable`` → fall back to
    B3 hard-reset semantics (accept the runner's result on the final
    attempt; do NOT terminal-abort)."""
    detector = LoopDetector(ticket_key="OP-847-graceful")
    detector.mark_reset()
    detector.mark_reset()
    scratchpad = ToMScratchpad()
    grader = _StubGrader(raise_unavailable=True)

    async def runner(*, prompt: str) -> RunResult:
        return _ok_runresult(text="done")

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config(),
        outcomes_grader=grader,
    )

    assert outcome.aborted_terminal is False
    assert outcome.outcomes_verdict is None  # grader never produced a verdict
    assert outcome.final_result is not None
    assert outcome.final_result.final_text == "done"


@pytest.mark.asyncio
async def test_grader_callable_missing_falls_back_to_b3():
    """Defensive: even with config enabled, if the launcher forgot to
    wire the grader callable, we accept the result rather than crash."""
    detector = LoopDetector(ticket_key="OP-847-nogradercb")
    detector.mark_reset()
    detector.mark_reset()
    scratchpad = ToMScratchpad()

    async def runner(*, prompt: str) -> RunResult:
        return _ok_runresult()

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config(),
        outcomes_grader=None,
    )
    assert outcome.aborted_terminal is False
    assert outcome.outcomes_verdict is None


# ─── AC #4 — grader cost threads through on_attempt_usage ─────────────


@pytest.mark.asyncio
async def test_grader_usage_threads_through_on_attempt_usage():
    """AC #4: grader's input/output tokens must surface through the
    existing ``on_attempt_usage`` hook unchanged, so the launcher's
    ``_post_call_cost_record`` captures the Haiku grader cost
    alongside the worker's Sonnet cost. We assert TWO usage calls fire
    for the final attempt: 1 for the runner, 1 for the grader."""
    detector = LoopDetector(ticket_key="OP-847-cost")
    detector.mark_reset()
    detector.mark_reset()
    scratchpad = ToMScratchpad()
    grader = _StubGrader(verdicts=[OutcomesVerdict(
        verdict="pass",
        grader_reasoning="ok",
        grader_input_tokens=2500,
        grader_output_tokens=200,
    )])

    async def runner(*, prompt: str) -> RunResult:
        return RunResult(
            final_text="done", iterations=1, stop_reason="end_turn",
            usage=TokenUsage(input_tokens=18_000, output_tokens=3_500),
        )

    seen: list[TokenUsage] = []

    async def record(u: TokenUsage) -> None:
        seen.append(u)

    await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=_enabled_config(),
        outcomes_grader=grader,
        on_attempt_usage=record,
    )

    assert len(seen) == 2, (
        f"Expected 2 usage records (worker + grader); got {len(seen)}"
    )
    assert seen[0].input_tokens == 18_000  # worker
    assert seen[1].input_tokens == 2_500   # grader
    assert seen[1].output_tokens == 200


# ─── AC #7 — rollback / pure-B3 parity ────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_flag_off_pure_b3_behaviour_unchanged():
    """AC #7: with ``OMNISIGHT_OUTCOMES_FINAL_ATTEMPT=0`` (or
    outcomes_config omitted), the orchestrator's behaviour is
    bit-for-bit identical to pure B3. We assert by exercising the
    canonical 3-reset-terminal-abort path with the grader present but
    config-disabled, and confirming neither the grader nor
    ``outcomes_verdict`` ever fires."""
    detector = LoopDetector(ticket_key="OP-847-rollback")
    scratchpad = ToMScratchpad()
    grader = _StubGrader()
    sig = ToolCallSignature(
        tool_name="Bash", args_hash="0" * 16, error_class="ok",
    )

    async def runner(*, prompt: str) -> RunResult:
        raise LoopResetRequired(signature=sig)

    # Disabled config — orchestrator should never invoke the grader,
    # even though we wired one.
    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
        outcomes_config=OutcomesConfig(enabled=False, rubric=""),
        outcomes_grader=grader,
    )

    assert outcome.aborted_terminal is True
    assert outcome.reset_count == detector.reset_limit
    assert outcome.outcomes_verdict is None
    assert grader.calls == []


@pytest.mark.asyncio
async def test_rollback_outcomes_config_none_identical_to_pure_b3():
    """AC #7 alt: passing ``outcomes_config=None`` (the new param's
    default) must yield identical behaviour to the pre-B16 call. We
    re-run the canonical happy-path test from test_context_reset.py."""
    detector = LoopDetector(ticket_key="OP-847-rollback-none")
    scratchpad = ToMScratchpad()

    async def runner(*, prompt: str) -> RunResult:
        return _ok_runresult()

    outcome = await run_with_resets(
        runner=runner, detector=detector, scratchpad=scratchpad,
        first_user_message="ticket",
    )

    assert outcome.aborted_terminal is False
    assert outcome.reset_count == 0
    assert outcome.outcomes_verdict is None
    assert outcome.final_result is not None


# ─── Runner script integration shim ───────────────────────────────────


def test_runner_script_parse_grader_verdict_strict_json():
    """The launcher's ``_parse_grader_verdict`` accepts the canonical
    one-line JSON shape and surfaces verdict + reasoning. Parses the
    exact shape produced by the documented prompt template."""
    from scripts.run_s1_via_anthropic_sdk import _parse_grader_verdict

    line = json.dumps({"verdict": "pass", "grader_reasoning": "AC met"})
    verdict, reasoning = _parse_grader_verdict(line)
    assert verdict == "pass"
    assert reasoning == "AC met"


def test_runner_script_parse_grader_verdict_tolerates_prose_wrapper():
    """Haiku occasionally prepends a sentence despite the prompt; the
    launcher must still find the inner JSON object so an otherwise-good
    grader response doesn't degrade to OutcomesGraderUnavailable."""
    from scripts.run_s1_via_anthropic_sdk import _parse_grader_verdict

    text = (
        "Here's my grading:\n"
        '{"verdict": "fail", "grader_reasoning": "no test added"}\n'
        "Hope that helps."
    )
    verdict, reasoning = _parse_grader_verdict(text)
    assert verdict == "fail"
    assert "no test added" in reasoning


def test_runner_script_parse_grader_verdict_rejects_garbage():
    """Anything that doesn't parse to a pass/fail JSON object raises
    OutcomesGraderUnavailable so the orchestrator degrades gracefully."""
    from scripts.run_s1_via_anthropic_sdk import _parse_grader_verdict

    with pytest.raises(OutcomesGraderUnavailable):
        _parse_grader_verdict("the model said pass kinda")


def test_runner_script_parse_grader_verdict_rejects_unknown_verdict():
    from scripts.run_s1_via_anthropic_sdk import _parse_grader_verdict

    line = json.dumps({"verdict": "maybe", "grader_reasoning": "unsure"})
    with pytest.raises(OutcomesGraderUnavailable):
        _parse_grader_verdict(line)
