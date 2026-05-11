"""B3 — Context-reset orchestrator + dispatcher wrapper (OP-830, master plan §2.5).

Reset semantics (per AC #4 and FSM Q1-Q4):

* **In-flight tool call**: awaited, never killed (avoids F3 ambiguous
  sandbox state). Implemented by the ``DetectorAwareDispatcher``: the
  inner ``execute()`` is awaited fully before the detector is consulted,
  so any reset signal fires *after* the call has cleanly returned.

* **On reset**: the runner's conversation history is cleared except for
  (a) the system prompt (caller-bound) and (b) the first user message
  (containing AC + ticket text). One paragraph is injected:

      "PRIOR ATTEMPT (now reset): you tried <tool_call_signature>
       3 times with error <error_class>. Try a different approach."

* **ToM scratchpad** persists across resets — the only continuous trace
  (AC #4 / Q3). The orchestrator appends the scratchpad summary to the
  reset paragraph so the post-reset model sees what hypotheses have
  already been explored.

* **CostGuard tally** is **not** reset (AC #4 / Q4): the orchestrator
  invokes ``on_attempt_usage`` after every successful runner attempt so
  the launcher can record the actual cost. Costs accumulate across
  resets within a single ticket. Partial usage from an attempt that
  raised ``LoopResetRequired`` mid-run is unobservable from outside the
  client (the model call's usage is captured inside ``run_with_tools``
  but never returned), so the recorded total is a slight under-estimate
  on reset paths — preserved as a known limitation.

Reset upper bound = 3 (per AC #5). After 3 resets the orchestrator returns
``ResetOutcome(aborted_terminal=True)``; the launcher surfaces this as
``loop_aborted_terminal`` and surrenders the ticket.

B16 — Outcomes-graded final attempt (OP-847, operator-opt-in).
When the caller passes an enabled ``OutcomesConfig`` + grader callable
to ``run_with_resets``, the attempt that fires at
``detector.reset_count == detector.reset_limit - 1`` (i.e. the last
attempt before terminal abort) is wrapped in an Outcomes envelope:
after the runner returns a ``RunResult`` the grader is invoked with
the rubric; on grader-FAIL the orchestrator returns
``ResetOutcome(aborted_terminal=True, outcomes_verdict=...)``. The
first 2 attempts remain pure B3 — Outcomes never substitutes for a
reset (per the OP-843 spike's disjoint-failure-mode finding). When the
grader raises ``OutcomesGraderUnavailable`` the orchestrator degrades
to pure B3 (final result accepted as-is) and logs the fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.agents.anthropic_native_client import RunResult, TokenUsage
from backend.agents.loop_detector import (
    LoopDetector,
    OutcomesConfig,
    OutcomesGraderUnavailable,
    OutcomesVerdict,
    ToolCallSignature,
    classify_tool_error,
)
from backend.agents.tom_scratchpad import ToMScratchpad
from backend.agents.tool_dispatcher import ToolDispatcher, ToolResult

logger = logging.getLogger(__name__)


class LoopResetRequired(Exception):
    """Raised by ``DetectorAwareDispatcher.execute()`` *after* the in-flight
    tool call has been awaited and the detector flagged a 3x match.

    Carries the looped signature so the orchestrator can build the reset
    paragraph (AC #4 / Q2). Propagates through
    ``AnthropicClient.run_with_tools`` — the client does not catch it
    because it is a control-flow signal, not a tool error.
    """

    def __init__(self, signature: ToolCallSignature) -> None:
        super().__init__(f"loop reset required: {signature.render()}")
        self.signature = signature


class DetectorAwareDispatcher:
    """Wraps a ``ToolDispatcher``: records every call to a ``LoopDetector``
    and raises ``LoopResetRequired`` after the in-flight call has been
    awaited (per AC #4 — the in-flight call is awaited, not killed).

    Acts as a pass-through for ``register`` / ``has_handler`` /
    ``registered_tools`` so callers can compose with existing
    registration helpers (``make_runner_dispatcher``,
    ``bind_built_in_tools``, ``make_skill_handler``, ``Agent``).
    """

    def __init__(
        self,
        *,
        inner: ToolDispatcher,
        detector: LoopDetector,
    ) -> None:
        self._inner = inner
        self._detector = detector

    @property
    def inner(self) -> ToolDispatcher:
        return self._inner

    @property
    def detector(self) -> LoopDetector:
        return self._detector

    @detector.setter
    def detector(self, value: LoopDetector) -> None:
        # Replaceable so the launcher can reuse one wrapper instance
        # across many tickets — each ticket installs its own detector.
        self._detector = value

    def register(self, tool_name: str, handler: Any) -> Any:
        return self._inner.register(tool_name, handler)

    def has_handler(self, tool_name: str) -> bool:
        return self._inner.has_handler(tool_name)

    def registered_tools(self) -> list[str]:
        return self._inner.registered_tools()

    async def execute(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        result = await self._inner.execute(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        # In-flight call has now returned (awaited cleanly). Now safe to
        # consult the detector and possibly raise.
        error_class = classify_tool_error(
            is_error=result.is_error, content=result.content
        )
        self._detector.record_tool_call(
            tool_name=tool_name,
            tool_args=tool_input,
            error_class=error_class,
        )
        if self._detector.is_reset_required():
            sig = self._detector.loop_signature()
            assert sig is not None  # is_reset_required => log non-empty
            raise LoopResetRequired(signature=sig)
        return result


def build_reset_paragraph(
    *,
    signature: ToolCallSignature,
    scratchpad: ToMScratchpad | None = None,
) -> str:
    """Per AC #4 / Q2: wording is fixed.

        "PRIOR ATTEMPT (now reset): you tried <tool_call_signature>
         3 times with error <error_class>. Try a different approach."

    The scratchpad summary is appended (AC #4 / Q3) so the post-reset
    model sees prior hypotheses without seeing the full history.
    """
    paragraph = (
        f"PRIOR ATTEMPT (now reset): you tried "
        f"{signature.tool_name}(args_hash={signature.args_hash}) "
        f"3 times with error {signature.error_class}. "
        f"Try a different approach."
    )
    if scratchpad is not None and len(scratchpad) > 0:
        paragraph += "\n\n" + scratchpad.render_summary()
    return paragraph


def build_reset_user_prompt(
    *,
    first_user_message: str,
    signature: ToolCallSignature,
    scratchpad: ToMScratchpad | None = None,
) -> str:
    """Compose the post-reset user message: original AC + ticket text
    (the "first user message" per AC #4) followed by the reset paragraph.

    The system prompt is *not* re-issued here — it is bound by the
    runner caller via ``functools.partial`` style. ``run_with_resets``
    invokes the runner with a fresh ``messages = [{"role": "user", ...}]``
    list whose single entry is this composed prompt.
    """
    return (
        f"{first_user_message}\n\n"
        f"---\n"
        f"{build_reset_paragraph(signature=signature, scratchpad=scratchpad)}\n"
    )


@dataclass
class ResetOutcome:
    """Aggregated outcome of a multi-attempt orchestration.

    ``outcomes_verdict`` is populated when B16 (OP-847) fired the
    grader on the final attempt. ``None`` means either Outcomes was
    disabled, the orchestration never reached the final attempt, or
    the grader was unavailable (graceful degrade).
    """

    final_result: RunResult | None
    aborted_terminal: bool
    reset_count: int
    total_usage: TokenUsage
    aborted_signature: ToolCallSignature | None
    outcomes_verdict: OutcomesVerdict | None = None


# Type alias for the runner factory the orchestrator calls. Caller binds
# system, model, tools, max_iterations etc. — the orchestrator only
# varies the user prompt across attempts.
RunnerCallable = Callable[..., Awaitable[RunResult]]

# B16 (OP-847) grader callable: given (rubric, runner_result) return an
# ``OutcomesVerdict`` carrying pass/fail + grader usage tokens. Caller
# wires this to ``client.simple(model=grader_model, ...)`` in
# production; tests pass a stub. Raise ``OutcomesGraderUnavailable``
# to opt this attempt out (degrade to pure B3 acceptance).
OutcomesGraderCallable = Callable[[str, RunResult], Awaitable[OutcomesVerdict]]


def _is_outcomes_final_attempt(
    *, detector: LoopDetector, config: OutcomesConfig | None,
) -> bool:
    """B16 trigger gate. ``True`` iff Outcomes is enabled AND the
    orchestrator is about to run the LAST attempt of the budget
    (``reset_count == reset_limit - 1``). The first two attempts in a
    3-attempt budget always run as pure B3 (OP-843 spike: do not
    substitute Outcomes for any reset, only add it as a graded final).
    """
    if config is None or not config.enabled:
        return False
    return detector.reset_count == detector.reset_limit - 1


async def _grade_final_attempt(
    *,
    grader: OutcomesGraderCallable | None,
    config: OutcomesConfig,
    result: RunResult,
    on_attempt_usage: Callable[[TokenUsage], Awaitable[None]] | None,
) -> OutcomesVerdict | None:
    """Invoke the grader on the final-attempt ``RunResult``. Returns
    ``None`` when no grader is wired or the grader is unavailable —
    the orchestrator interprets ``None`` as "degrade to pure B3, accept
    the result as-is" (AC #1 error catalog: ``OutcomesGraderUnavailable``).

    On success, threads the grader's input/output tokens through
    ``on_attempt_usage`` so the launcher's ``_post_call_cost_record``
    captures the Haiku cost alongside the worker's Sonnet cost (AC #4).
    """
    if grader is None:
        logger.warning(
            "[outcomes-final] grader callable not wired; falling back to B3 accept"
        )
        return None
    try:
        verdict = await grader(config.rubric, result)
    except OutcomesGraderUnavailable as e:
        logger.warning(
            "[outcomes-final] OutcomesGraderUnavailable: %s — falling back to B3 accept",
            e,
        )
        return None
    if on_attempt_usage is not None:
        grader_usage = TokenUsage(
            input_tokens=verdict.grader_input_tokens,
            output_tokens=verdict.grader_output_tokens,
        )
        await on_attempt_usage(grader_usage)
    return verdict


async def run_with_resets(
    *,
    runner: RunnerCallable,
    detector: LoopDetector,
    scratchpad: ToMScratchpad,
    first_user_message: str,
    on_attempt_usage: Callable[[TokenUsage], Awaitable[None]] | None = None,
    outcomes_config: OutcomesConfig | None = None,
    outcomes_grader: OutcomesGraderCallable | None = None,
) -> ResetOutcome:
    """Drive ``runner(prompt=...)`` attempts until either:

      - the runner returns a ``RunResult`` without ``LoopResetRequired``,
      - or the detector exceeds ``reset_limit`` (default 3) — caught and
        surfaced as ``ResetOutcome(aborted_terminal=True)``.

    Each fresh attempt starts from the original ``first_user_message``
    plus the reset paragraph (AC #4). The system prompt is unchanged
    across attempts (caller binds it via the runner closure).

    ``on_attempt_usage`` fires after every *successful* runner return so
    the launcher can record the actual cost. CostGuard tally is **not**
    reset across attempts — the same ticket shares one budget.

    B16 (OP-847): when ``outcomes_config.enabled`` and the orchestrator
    is about to start the LAST attempt, the runner's ``RunResult`` is
    passed through ``outcomes_grader`` (Haiku rubric+grader). A
    grader-FAIL produces ``aborted_terminal=True`` with
    ``outcomes_verdict`` carrying the reasoning. A grader-PASS or
    grader-unavailable accepts the result identically to pure B3.
    Per AC #7, ``outcomes_config=None`` (or ``enabled=False``) restores
    pure B3 behaviour bit-for-bit.
    """
    current_prompt = first_user_message
    total_usage = TokenUsage()
    last_result: RunResult | None = None

    while True:
        is_final = _is_outcomes_final_attempt(
            detector=detector, config=outcomes_config,
        )
        try:
            result = await runner(prompt=current_prompt)
        except LoopResetRequired as e:
            # In-flight tool call has already been awaited inside the
            # dispatcher wrapper (AC #4). Decide reset vs terminal abort.
            if not detector.can_reset():
                return ResetOutcome(
                    final_result=last_result,
                    aborted_terminal=True,
                    reset_count=detector.reset_count,
                    total_usage=total_usage,
                    aborted_signature=e.signature,
                )
            detector.mark_reset()
            current_prompt = build_reset_user_prompt(
                first_user_message=first_user_message,
                signature=e.signature,
                scratchpad=scratchpad,
            )
            continue

        # Runner returned a RunResult — happy path or model gave up.
        total_usage = total_usage + result.usage
        last_result = result
        if on_attempt_usage is not None:
            await on_attempt_usage(result.usage)

        # Theoretical: detector flagged reset on the very last tool call
        # and the runner happened to terminate at the same iteration
        # (e.g. dispatcher.execute raised after the model decided to
        # stop emitting tool_use). Defensive branch.
        if detector.is_reset_required():
            sig = detector.loop_signature()
            if not detector.can_reset():
                return ResetOutcome(
                    final_result=last_result,
                    aborted_terminal=True,
                    reset_count=detector.reset_count,
                    total_usage=total_usage,
                    aborted_signature=sig,
                )
            detector.mark_reset()
            assert sig is not None
            current_prompt = build_reset_user_prompt(
                first_user_message=first_user_message,
                signature=sig,
                scratchpad=scratchpad,
            )
            continue

        # B16 — Outcomes-graded final attempt. The grader only fires
        # when the runner produced a completion (we reached this point,
        # so neither LoopResetRequired nor a deferred reset_required
        # branched out). Grader hallucinated-pass is out of scope here
        # (caught downstream by B4 critic — AC #1 error catalog).
        if is_final and outcomes_config is not None and outcomes_config.enabled:
            verdict = await _grade_final_attempt(
                grader=outcomes_grader,
                config=outcomes_config,
                result=result,
                on_attempt_usage=on_attempt_usage,
            )
            if verdict is not None and not verdict.passed:
                logger.info(
                    "[outcomes-final] grader FAIL — terminal abort. reasoning=%s",
                    verdict.grader_reasoning[:200],
                )
                return ResetOutcome(
                    final_result=last_result,
                    aborted_terminal=True,
                    reset_count=detector.reset_count,
                    total_usage=total_usage,
                    aborted_signature=None,
                    outcomes_verdict=verdict,
                )
            return ResetOutcome(
                final_result=last_result,
                aborted_terminal=False,
                reset_count=detector.reset_count,
                total_usage=total_usage,
                aborted_signature=None,
                outcomes_verdict=verdict,
            )

        return ResetOutcome(
            final_result=last_result,
            aborted_terminal=False,
            reset_count=detector.reset_count,
            total_usage=total_usage,
            aborted_signature=None,
        )
