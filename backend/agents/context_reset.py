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
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.agents.anthropic_native_client import RunResult, TokenUsage
from backend.agents.loop_detector import (
    LoopDetector,
    ToolCallSignature,
    classify_tool_error,
)
from backend.agents.tom_scratchpad import ToMScratchpad
from backend.agents.tool_dispatcher import ToolDispatcher, ToolResult


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
    """Aggregated outcome of a multi-attempt orchestration."""

    final_result: RunResult | None
    aborted_terminal: bool
    reset_count: int
    total_usage: TokenUsage
    aborted_signature: ToolCallSignature | None


# Type alias for the runner factory the orchestrator calls. Caller binds
# system, model, tools, max_iterations etc. — the orchestrator only
# varies the user prompt across attempts.
RunnerCallable = Callable[..., Awaitable[RunResult]]


async def run_with_resets(
    *,
    runner: RunnerCallable,
    detector: LoopDetector,
    scratchpad: ToMScratchpad,
    first_user_message: str,
    on_attempt_usage: Callable[[TokenUsage], Awaitable[None]] | None = None,
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
    """
    current_prompt = first_user_message
    total_usage = TokenUsage()
    last_result: RunResult | None = None

    while True:
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

        return ResetOutcome(
            final_result=last_result,
            aborted_terminal=False,
            reset_count=detector.reset_count,
            total_usage=total_usage,
            aborted_signature=None,
        )
