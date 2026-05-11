"""B7 (OP-839) — Coder (Sonnet) phase of the locator → coder pipeline.

The coder agent runs SECOND, after the locator has produced a
schema-frozen handoff (or after the runner decided to fall back to
the monolithic full-repo coder). Its job is to translate the locator's
candidate set + the ticket's Acceptance Criteria into a set of file
edits, then surrender control so the runner can commit + push.

Critical invariants:

* **Context isolation** (AC #3) — the focused coder receives ONLY:
  - the parsed locator JSON envelope
  - the ticket AC
  - the coder system prompt
  It does NOT receive the locator's tool-call history. This is the
  whole point of the two-phase split.
* **No Gerrit-push side-effects**. The coder returns a
  :class:`CoderOutcome`; the runner owns the push. This module never
  imports a Gerrit client.
* **Single retry on "insufficient context"** (error catalog
  ``coder_rejects_locator_input``). The retry expands the candidate
  set via a caller-supplied callback; if the coder still rejects, we
  fall back to monolithic.
* **Monolithic fallback** (AC #5/#7) — when the locator returns 0
  candidates OR times out, ``run_monolithic_coder`` runs the same
  Sonnet model with no locator handoff, only AC + system prompt.

The coder's "output" in this module is a structured outcome (final
text + diff signal + tool-call trace). Actually applying file edits
is done by Anthropic's built-in ``text_editor`` tool (B1), which is
threaded through the backend.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Protocol

from backend.agents.locator_handoff_schema import (
    SCHEMA_VERSION,
    LocatorCandidate,
    LocatorHandoff,
)

log = logging.getLogger(__name__)

DEFAULT_CODER_MODEL = "claude-sonnet-4-6"
CODER_MODEL_ENV_VAR = "OMNISIGHT_CODER_MODEL"
CODER_TIMEOUT_SECONDS = 600.0  # full coding loop budget, not per-turn
CODER_MAX_INSUFFICIENT_CONTEXT_RETRIES = 1

# Coder tool set — write capability is enabled here, unlike locator.
# Names match the runner's tool-dispatcher keys.
CODER_TOOLS: tuple[str, ...] = (
    "text_editor",   # B1 — Anthropic built-in, view/create/str_replace/insert
    "Grep",
    "Glob",
    "Bash",          # for `pytest`, `ruff`, etc. — read-only-by-policy
)

CoderStatus = Literal[
    "ok",
    "insufficient_context",
    "fallback_monolithic_required",
    "timeout",
    "backend_error",
]

# Regex that detects the coder's structured "insufficient context"
# escape hatch. The system prompt tells the model to emit
# ``[[INSUFFICIENT_CONTEXT]]`` (no whitespace, on its own line) when
# it cannot proceed; a substring search is robust to surrounding
# narration.
INSUFFICIENT_CONTEXT_SENTINEL = "[[INSUFFICIENT_CONTEXT]]"
_INSUFFICIENT_RE = re.compile(re.escape(INSUFFICIENT_CONTEXT_SENTINEL))


@dataclass(frozen=True)
class CoderRunRequest:
    """Inputs the coder backend needs for one invocation."""

    prompt: str
    system: str
    tools: tuple[str, ...]
    model: str


@dataclass(frozen=True)
class CoderBackendResult:
    """What the backend returns from one Sonnet round-trip."""

    final_text: str
    tool_calls_made: int = 0


@dataclass(frozen=True)
class CoderOutcome:
    """End-to-end coder result. Runner consumes this to decide push vs. escalate."""

    status: CoderStatus
    final_text: str = ""
    tool_calls_made: int = 0
    retries_used: int = 0
    fallback_reason: str = ""
    history: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_monolithic_fallback(self) -> bool:
        return self.status == "fallback_monolithic_required"

    @property
    def proceed_to_push(self) -> bool:
        return self.status == "ok"


class CoderBackend(Protocol):
    """Async interface the coder needs from the LLM client.

    Production: a wrapper over ``AnthropicClient.run_with_tools``
    that exposes the coder-allowed tool subset and surfaces a
    summarised final-text + tool-call count. Tests pass scripted
    coroutines directly.
    """

    async def run(
        self,
        *,
        request: CoderRunRequest,
        timeout_s: float,
    ) -> CoderBackendResult: ...


CandidateExpander = Callable[
    [LocatorHandoff],
    Awaitable[LocatorHandoff] | LocatorHandoff,
]
"""Callback the runner provides to widen the candidate set after a
coder rejection (AC error-catalog ``coder_rejects_locator_input``).

A trivial implementation returns the original handoff unchanged
(no expansion possible) — the coder then falls through to monolithic.
"""


def resolve_coder_model() -> str:
    return os.environ.get(CODER_MODEL_ENV_VAR) or DEFAULT_CODER_MODEL


def build_coder_system_prompt() -> str:
    """System message for the focused coder (AC #3).

    The "insufficient context" escape hatch is deliberately strict:
    the coder must emit the literal sentinel on its own line, with no
    surrounding code fences, so the runner can parse without an LLM.
    """
    return (
        "You are the CODER phase of a two-phase coding pipeline. The "
        "LOCATOR phase has already narrowed the relevant files for you. "
        "You will receive a JSON handoff (frozen schema "
        f"{SCHEMA_VERSION}) listing the files + line ranges + rationale. "
        "Read those files via text_editor.view, make the edits required "
        "to satisfy the Acceptance Criteria, and run tests where "
        "appropriate.\n\n"
        "If — and ONLY if — the locator's candidate set is genuinely "
        "insufficient (e.g., the AC references a feature that touches a "
        "file the locator omitted), reply with EXACTLY the sentinel\n"
        f"  {INSUFFICIENT_CONTEXT_SENTINEL}\n"
        "on its own line and STOP. Do not continue editing. Do not "
        "speculate about missing files in prose — the sentinel triggers "
        "a structured retry with an expanded candidate set.\n\n"
        "Otherwise, complete the edits and end your final response with "
        "a one-line summary of the changes you made."
    )


def build_focused_coder_prompt(
    *,
    handoff: LocatorHandoff,
    ac_text: str,
    ticket_key: str,
    ticket_summary: str,
) -> str:
    """Compose the user prompt fed to the focused coder.

    The coder receives ONLY the parsed JSON (not the locator's tool
    history) per AC #3. ``LocatorHandoff.to_coder_prompt`` is the
    single source of truth for the envelope format.
    """
    header = f"Ticket: {ticket_key} — {ticket_summary}\n\n"
    return header + handoff.to_coder_prompt(ac_text)


def build_monolithic_coder_prompt(
    *,
    ac_text: str,
    ticket_key: str,
    ticket_summary: str,
    fallback_reason: str,
) -> str:
    """User prompt for the monolithic (full-repo) Sonnet fallback.

    Triggered by ``locator_zero_candidates`` (AC #5) and
    ``locator_timeout`` (AC #7). The model gets only the AC; no
    candidate hints. Logged at WARNING level by the caller.
    """
    return (
        f"Ticket: {ticket_key} — {ticket_summary}\n\n"
        f"[Locator phase fell back: {fallback_reason}. "
        "You have no candidate-file hints; you must use Grep/Glob to "
        "locate the relevant files yourself before editing.]\n\n"
        f"=== Acceptance Criteria ===\n{ac_text}\n"
    )


def detect_insufficient_context(text: str) -> bool:
    """True iff the coder emitted the structured escape sentinel."""
    return bool(_INSUFFICIENT_RE.search(text or ""))


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _run_once(
    backend: CoderBackend,
    *,
    prompt: str,
    system: str,
    tools: tuple[str, ...],
    model: str,
    timeout_s: float,
) -> tuple[CoderBackendResult | None, str]:
    """One Sonnet invocation with timeout + error trapping.

    Returns ``(result, failure_code)``; exactly one is non-empty.
    """
    request = CoderRunRequest(
        prompt=prompt, system=system, tools=tools, model=model,
    )
    try:
        result = await asyncio.wait_for(
            backend.run(request=request, timeout_s=timeout_s),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return None, "coder_timeout"
    except Exception as exc:  # noqa: BLE001 — backend bug ≠ pipeline blocker
        log.warning("coder backend raised %s: %s", type(exc).__name__, exc)
        return None, "coder_backend_error"
    return result, ""


async def run_focused_coder(
    backend: CoderBackend,
    *,
    handoff: LocatorHandoff,
    ac_text: str,
    ticket_key: str,
    ticket_summary: str,
    candidate_expander: CandidateExpander | None = None,
    model: str | None = None,
    timeout_s: float = CODER_TIMEOUT_SECONDS,
    max_insufficient_retries: int = CODER_MAX_INSUFFICIENT_CONTEXT_RETRIES,
) -> CoderOutcome:
    """Run the focused coder on a locator handoff (AC #3 + error catalog).

    Flow:
      1. Invoke Sonnet with handoff + AC. Single round-trip; the
         backend's internal tool-use loop runs to ``end_turn``.
      2. If the response contains the insufficient-context sentinel,
         call ``candidate_expander`` (if provided) and retry once.
      3. If the second attempt *also* rejects, return
         ``status="fallback_monolithic_required"`` so the runner can
         escalate to ``run_monolithic_coder``.
    """
    chosen_model = model or resolve_coder_model()
    system = build_coder_system_prompt()
    history: list[str] = []
    current_handoff = handoff

    for attempt in range(max_insufficient_retries + 1):
        prompt = build_focused_coder_prompt(
            handoff=current_handoff,
            ac_text=ac_text,
            ticket_key=ticket_key,
            ticket_summary=ticket_summary,
        )
        result, failure_code = await _run_once(
            backend,
            prompt=prompt,
            system=system,
            tools=CODER_TOOLS,
            model=chosen_model,
            timeout_s=timeout_s,
        )
        if result is None:
            history.append(failure_code)
            status: CoderStatus = (
                "timeout" if failure_code == "coder_timeout" else "backend_error"
            )
            return CoderOutcome(
                status=status,
                fallback_reason=failure_code,
                retries_used=attempt,
                history=tuple(history),
            )

        if not detect_insufficient_context(result.final_text):
            return CoderOutcome(
                status="ok",
                final_text=result.final_text,
                tool_calls_made=result.tool_calls_made,
                retries_used=attempt,
                history=tuple(history),
            )

        # Coder rejected. Try once to expand the candidate set, then
        # surrender to monolithic.
        history.append("coder_rejects_locator_input")
        if attempt >= max_insufficient_retries:
            return CoderOutcome(
                status="fallback_monolithic_required",
                final_text=result.final_text,
                tool_calls_made=result.tool_calls_made,
                retries_used=attempt + 1,
                fallback_reason="coder_rejects_locator_input",
                history=tuple(history),
            )

        if candidate_expander is None:
            # Same handoff would produce the same rejection — skip
            # the retry round-trip and surrender.
            return CoderOutcome(
                status="fallback_monolithic_required",
                final_text=result.final_text,
                tool_calls_made=result.tool_calls_made,
                retries_used=attempt + 1,
                fallback_reason="coder_rejects_locator_input_no_expander",
                history=tuple(history),
            )

        try:
            current_handoff = await _maybe_await(
                candidate_expander(current_handoff)
            )
        except Exception as exc:  # noqa: BLE001 — expander failure ≠ blocker
            log.warning(
                "candidate expander raised %s — surrendering to monolithic",
                type(exc).__name__,
            )
            return CoderOutcome(
                status="fallback_monolithic_required",
                final_text=result.final_text,
                tool_calls_made=result.tool_calls_made,
                retries_used=attempt + 1,
                fallback_reason=f"expander_error:{type(exc).__name__}",
                history=tuple(history),
            )

    # Unreachable: the for-loop covers every (attempt, outcome) path.
    raise RuntimeError("run_focused_coder exhausted loop unexpectedly")


async def run_monolithic_coder(
    backend: CoderBackend,
    *,
    ac_text: str,
    ticket_key: str,
    ticket_summary: str,
    fallback_reason: str,
    model: str | None = None,
    timeout_s: float = CODER_TIMEOUT_SECONDS,
) -> CoderOutcome:
    """Run the full-repo Sonnet coder (AC #5/#7 fallback path).

    Triggered when the locator returned 0 candidates or timed out.
    Same backend, same system prompt, but no locator handoff — the
    model uses Grep/Glob to find files itself. Logged WARNING at the
    call site.
    """
    chosen_model = model or resolve_coder_model()
    system = build_coder_system_prompt()
    prompt = build_monolithic_coder_prompt(
        ac_text=ac_text,
        ticket_key=ticket_key,
        ticket_summary=ticket_summary,
        fallback_reason=fallback_reason,
    )
    log.warning(
        "monolithic coder fallback for %s (reason=%s)",
        ticket_key, fallback_reason,
    )
    result, failure_code = await _run_once(
        backend,
        prompt=prompt,
        system=system,
        tools=CODER_TOOLS,
        model=chosen_model,
        timeout_s=timeout_s,
    )
    if result is None:
        status: CoderStatus = (
            "timeout" if failure_code == "coder_timeout" else "backend_error"
        )
        return CoderOutcome(
            status=status,
            fallback_reason=failure_code,
            history=(f"monolithic:{failure_code}",),
        )
    return CoderOutcome(
        status="ok",
        final_text=result.final_text,
        tool_calls_made=result.tool_calls_made,
        history=(f"monolithic:{fallback_reason}",),
    )


# ── Convenience: ensure the focused-coder prompt contains exactly the
# handoff payload and AC, nothing else. The runner's pipeline tests
# pin this so a regression that leaks locator tool-call history is
# caught at unit-test time, not in production.

def coder_prompt_contains_only_handoff_and_ac(
    prompt: str,
    *,
    handoff: LocatorHandoff,
    ac_text: str,
) -> bool:
    """True iff ``prompt`` contains the locator JSON + AC and nothing else.

    "Nothing else" is a soft check — we look for forbidden markers
    that would indicate the locator's tool-call history bled through.
    """
    forbidden_markers = (
        "tool_use_id",
        "<tool_call>",
        "=== Locator tool history ===",
        "locator_transcript",
    )
    if any(marker in prompt for marker in forbidden_markers):
        return False
    if SCHEMA_VERSION not in prompt:
        return False
    if ac_text and ac_text.strip().split("\n")[0] not in prompt:
        return False
    for candidate in handoff.files:
        if candidate.path not in prompt:
            return False
    return True


__all__ = [
    "DEFAULT_CODER_MODEL",
    "CODER_MODEL_ENV_VAR",
    "CODER_TIMEOUT_SECONDS",
    "CODER_TOOLS",
    "CODER_MAX_INSUFFICIENT_CONTEXT_RETRIES",
    "INSUFFICIENT_CONTEXT_SENTINEL",
    "CoderBackend",
    "CoderBackendResult",
    "CoderOutcome",
    "CoderRunRequest",
    "CandidateExpander",
    "CoderStatus",
    "build_coder_system_prompt",
    "build_focused_coder_prompt",
    "build_monolithic_coder_prompt",
    "coder_prompt_contains_only_handoff_and_ac",
    "detect_insufficient_context",
    "resolve_coder_model",
    "run_focused_coder",
    "run_monolithic_coder",
    # Re-export for tests / runner integration.
    "LocatorHandoff",
    "LocatorCandidate",
]
