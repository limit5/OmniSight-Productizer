"""B7 (OP-839) — Locator (Haiku) phase of the locator → coder pipeline.

The locator agent runs FIRST, before any coder write. Its job is to
turn a JIRA ticket (title + AC + optional Files Touched hints) into a
narrow, structured candidate set the coder agent can act on, so the
Sonnet coder never has to scan the full repo.

Critical invariants:

* **Read-only**. The locator's only tools are ``text_editor.view``,
  ``Grep``, ``Glob`` and (when available) the B8 repo-map preamble.
  No write paths are exposed (AC #1).
* **Schema-frozen handoff** — see ``locator_handoff_schema`` (AC #2/#3).
  The coder receives ONLY the parsed JSON, not the locator's tool-call
  history.
* **60s budget** — beyond that we fall back to monolithic Sonnet
  (AC #7). The locator must never block the pipeline indefinitely.
* **Cross-ticket peer detection** runs *before* the Haiku call, not
  after, so a peer-conflict short-circuits without burning model
  tokens (AC #8 / F6 / F7 / F12).
* **Prior-PS rebase detection** runs *before* the Haiku call too
  (AC #9 / F11) — the rebase is a worktree-side operation owned by
  the caller; we surface the requirement.

Outcome surface — :class:`LocatorOutcome` is a tagged union of:

* ``status="ok"`` + ``handoff``: pass to coder.
* ``status="fallback_monolithic"``: 0 candidates or timeout — caller
  invokes ``monolithic_coder`` (full-repo Sonnet) and logs a warning.
* ``status="needs_refinement"``: >50 candidates — escalate ticket.
* ``status="peer_conflict"``: another in-flight ticket touches one of
  the candidate files — escalate.
* ``status="prior_ps_exists"``: a prior PS exists for the same
  Change-Id — caller rebases before re-invoking the locator.

The locator NEVER raises into the caller; every failure path maps to
an outcome the runner can route on.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from backend.agents.locator_handoff_schema import (
    LocatorHandoff,
    LocatorHandoffPollution,
    LocatorMalformedHandoff,
    parse_locator_handoff,
    schema_reminder_for_retry,
)

log = logging.getLogger(__name__)

DEFAULT_LOCATOR_MODEL = "claude-haiku-4-5"
LOCATOR_MODEL_ENV_VAR = "OMNISIGHT_LOCATOR_MODEL"
LOCATOR_TIMEOUT_SECONDS = 60.0
LOCATOR_MAX_RETRIES = 1

# Locator tools per AC #1. Hard-coded so a config drift can't widen
# them to a write tool by accident. The names mirror the runner's tool
# dispatcher keys (text_editor view = read-only).
LOCATOR_TOOLS: tuple[str, ...] = ("text_editor.view", "Grep", "Glob")

LocatorStatus = Literal[
    "ok",
    "fallback_monolithic",
    "needs_refinement",
    "peer_conflict",
    "prior_ps_exists",
    "malformed_handoff",
    "handoff_pollution",
]


@dataclass(frozen=True)
class PeerConflict:
    """A peer in-flight Gerrit change touches one of our candidate files."""

    file_path: str
    peer_ticket: str
    peer_change_number: int
    peer_change_url: str = ""


@dataclass(frozen=True)
class PriorPS:
    """A prior PS for this ticket's Change-Id is open on Gerrit."""

    change_id: str
    change_number: int
    change_url: str = ""


@dataclass(frozen=True)
class LocatorOutcome:
    """Tagged-union result the runner routes on (see module docstring)."""

    status: LocatorStatus
    handoff: LocatorHandoff | None = None
    fallback_reason: str = ""
    peer_conflicts: tuple[PeerConflict, ...] = ()
    prior_ps: PriorPS | None = None
    history: tuple[str, ...] = field(default_factory=tuple)
    """Per-attempt failure codes (e.g. ('locator_malformed_handoff',))."""

    @property
    def is_terminal_for_coder(self) -> bool:
        """True when the caller must NOT proceed to the Sonnet coder.

        Terminal classes route to escalation or to a different agent;
        only ``status="ok"`` feeds the coder, and only
        ``status="fallback_monolithic"`` feeds the monolithic coder.
        """
        return self.status in (
            "needs_refinement",
            "peer_conflict",
            "prior_ps_exists",
        )


class LocatorBackend(Protocol):
    """Minimal async interface the locator needs from the LLM client.

    Production: a thin wrapper over ``AnthropicClient.run_with_tools``
    that exposes the locator-allowed tool subset. Tests pass scripted
    coroutines directly.
    """

    async def invoke(
        self,
        *,
        prompt: str,
        model: str,
        tools: tuple[str, ...],
        timeout_s: float,
    ) -> str: ...


GerritPeerProbe = Callable[
    [str, str],  # (current_ticket, candidate_file_path)
    Awaitable[list[PeerConflict]] | list[PeerConflict],
]
"""Callback that returns peer conflicts for one (ticket, file) pair.

The runner injects a real Gerrit-backed probe in production. Tests
pass a static dict or a callable that returns ``[]``.
"""

GerritPriorPSProbe = Callable[
    [str],  # change_id
    Awaitable[PriorPS | None] | (PriorPS | None),
]
"""Callback that returns the prior PS for a Change-Id, if any."""


def resolve_locator_model() -> str:
    """Return the locator model name, honouring the env-var override."""
    return os.environ.get(LOCATOR_MODEL_ENV_VAR) or DEFAULT_LOCATOR_MODEL


def build_locator_prompt(
    *,
    ticket_key: str,
    ticket_summary: str,
    ac_text: str,
    files_touched_hint: tuple[str, ...] = (),
    repo_map_preamble: str = "",
    retry_reminder: bool = False,
) -> str:
    """Construct the prompt fed to the Haiku locator.

    The prompt is intentionally small: long context inflates the
    pollution risk. We restate the schema in skeleton form on retry so
    a malformed envelope does not pollute the second attempt.
    """
    hint_block = ""
    if files_touched_hint:
        hint_block = "\n=== Ticket Files-Touched hint ===\n" + "\n".join(
            f"- {p}" for p in files_touched_hint
        )
    repo_block = ""
    if repo_map_preamble:
        # Defensive: cap preamble length here too — a giant repo-map
        # could explode the locator prompt and crowd out the AC.
        truncated = repo_map_preamble[:4000]
        repo_block = f"\n=== Repo-map preamble (top-N by PageRank) ===\n{truncated}"

    body = (
        f"You are the LOCATOR phase of a two-phase coding pipeline. Your "
        f"job is to identify the small set of files (and line ranges) the "
        f"coder agent will need to read/edit to satisfy the ticket. You "
        f"have READ-ONLY tools only: text_editor.view, Grep, Glob.\n\n"
        f"Ticket: {ticket_key} — {ticket_summary}\n\n"
        f"=== Acceptance Criteria ===\n{ac_text}\n"
        f"{hint_block}{repo_block}\n\n"
        "Respond with EXACTLY one JSON object on a single line — no "
        "Markdown fences, no preamble. Schema:\n"
        '{"files": [{"path": "<repo-relative>", '
        '"line_ranges": [[<start_int>, <end_int>]], '
        '"why_relevant": "<short>"}],\n'
        ' "hypotheses": ["<bullet>"],\n'
        ' "confidence": <float 0.0..1.0>,\n'
        ' "summary": "<<=200 words>"}\n\n'
        "If the ticket scope appears wider than 50 files, return your "
        "best 50 — the caller will escalate `needs:refinement`. If you "
        "find no relevant files, return an empty `files` list and a "
        "summary explaining why."
    )
    if retry_reminder:
        body = body + "\n\n" + schema_reminder_for_retry()
    return body


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` iff it's a coroutine; otherwise return it as-is."""
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _check_prior_ps(
    probe: GerritPriorPSProbe | None,
    change_id: str | None,
) -> PriorPS | None:
    """Run the F11 prior-PS detector (AC #9) if a Change-Id is known."""
    if probe is None or not change_id:
        return None
    try:
        result = await _maybe_await(probe(change_id))
    except Exception as exc:  # noqa: BLE001 — probe failure must not block
        log.warning(
            "prior-PS probe failed (%s); proceeding without rebase check",
            type(exc).__name__,
        )
        return None
    if isinstance(result, PriorPS):
        return result
    return None


async def _check_peer_conflicts(
    probe: GerritPeerProbe | None,
    ticket_key: str,
    candidates: tuple[str, ...],
) -> tuple[PeerConflict, ...]:
    """Run the F6/F7/F12 peer-conflict detector (AC #8)."""
    if probe is None or not candidates:
        return ()
    conflicts: list[PeerConflict] = []
    for path in candidates:
        try:
            result = await _maybe_await(probe(ticket_key, path))
        except Exception as exc:  # noqa: BLE001 — probe failure ≠ pipeline failure
            log.warning(
                "peer-conflict probe failed for %s (%s); proceeding "
                "without peer-detection on that file",
                path, type(exc).__name__,
            )
            continue
        if not result:
            continue
        for entry in result:
            if isinstance(entry, PeerConflict) and entry.peer_ticket != ticket_key:
                conflicts.append(entry)
    return tuple(conflicts)


async def _invoke_with_retry(
    backend: LocatorBackend,
    *,
    base_prompt_factory: Callable[[bool], str],
    model: str,
    timeout_s: float,
    max_retries: int,
) -> tuple[LocatorHandoff | None, str, tuple[str, ...]]:
    """Invoke the Haiku locator with one-shot retry on malformed handoff.

    Returns ``(handoff, raw_text_of_last_attempt, history)``. ``handoff``
    is ``None`` if every attempt failed; the history tuple captures the
    failure code from each attempt for telemetry.
    """
    history: list[str] = []
    last_text = ""

    for attempt in range(max_retries + 1):
        retry = attempt > 0
        prompt = base_prompt_factory(retry)
        try:
            text = await asyncio.wait_for(
                backend.invoke(
                    prompt=prompt,
                    model=model,
                    tools=LOCATOR_TOOLS,
                    timeout_s=timeout_s,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            history.append("locator_timeout")
            return None, last_text, tuple(history)
        except Exception as exc:  # noqa: BLE001 — backend bug ≠ pipeline blocker
            history.append("locator_backend_error")
            log.warning(
                "locator backend raised %s on attempt %d: %s",
                type(exc).__name__, attempt + 1, exc,
            )
            return None, last_text, tuple(history)

        last_text = text or ""
        try:
            handoff = parse_locator_handoff(last_text)
        except LocatorHandoffPollution as exc:
            history.append("locator_handoff_pollution")
            log.warning("locator handoff pollution: %s", exc)
            # Pollution is NOT a retry class per AC #4 — it indicates
            # the model ignored the schema. Bail.
            return None, last_text, tuple(history)
        except LocatorMalformedHandoff as exc:
            history.append("locator_malformed_handoff")
            log.info(
                "locator handoff malformed on attempt %d: %s",
                attempt + 1, exc,
            )
            if attempt < max_retries:
                continue
            return None, last_text, tuple(history)
        else:
            return handoff, last_text, tuple(history)

    return None, last_text, tuple(history)


async def locate(
    backend: LocatorBackend,
    *,
    ticket_key: str,
    ticket_summary: str,
    ac_text: str,
    files_touched_hint: tuple[str, ...] = (),
    repo_map_preamble: str = "",
    change_id: str | None = None,
    peer_probe: GerritPeerProbe | None = None,
    prior_ps_probe: GerritPriorPSProbe | None = None,
    model: str | None = None,
    timeout_s: float = LOCATOR_TIMEOUT_SECONDS,
    max_retries: int = LOCATOR_MAX_RETRIES,
) -> LocatorOutcome:
    """Run the full B7 locator phase. Never raises into the caller.

    Order of checks:
      1. Prior-PS detector (AC #9 / F11) — if present, return
         ``status="prior_ps_exists"``. Caller rebases and re-invokes.
      2. Haiku locator call with 1× malformed retry (AC #4).
         * Timeout → ``status="fallback_monolithic"``.
         * Pollution → ``status="handoff_pollution"`` (terminal).
         * Malformed twice → ``status="malformed_handoff"`` (terminal).
      3. Candidate-count gates (AC #5/#6):
         * 0 candidates → ``status="fallback_monolithic"``.
         * >50 → ``status="needs_refinement"``.
      4. Peer-conflict detector (AC #8 / F6/F7/F12) over the survivors.
         * Any cross-ticket peer → ``status="peer_conflict"``.
      5. ``status="ok"`` — coder may proceed.
    """
    chosen_model = model or resolve_locator_model()

    # Step 1 — prior-PS detector. Done first because if a prior PS
    # exists, the worktree is on a stale base and *any* locator output
    # could be wrong. The caller must rebase before reinvoking.
    prior_ps = await _check_prior_ps(prior_ps_probe, change_id)
    if prior_ps is not None:
        return LocatorOutcome(
            status="prior_ps_exists",
            prior_ps=prior_ps,
            history=("prior_ps_exists_for_change_id",),
        )

    # Step 2 — Haiku locator call.
    def _factory(retry: bool) -> str:
        return build_locator_prompt(
            ticket_key=ticket_key,
            ticket_summary=ticket_summary,
            ac_text=ac_text,
            files_touched_hint=files_touched_hint,
            repo_map_preamble=repo_map_preamble,
            retry_reminder=retry,
        )

    handoff, _last_text, history = await _invoke_with_retry(
        backend,
        base_prompt_factory=_factory,
        model=chosen_model,
        timeout_s=timeout_s,
        max_retries=max_retries,
    )

    if handoff is None:
        # Map the terminal failure code in `history` to an outcome.
        last_code = history[-1] if history else "locator_backend_error"
        if last_code == "locator_timeout":
            return LocatorOutcome(
                status="fallback_monolithic",
                fallback_reason="locator_timeout",
                history=history,
            )
        if last_code == "locator_handoff_pollution":
            return LocatorOutcome(
                status="handoff_pollution",
                fallback_reason="locator_handoff_pollution",
                history=history,
            )
        # Includes malformed-after-retry and generic backend errors —
        # both surface as ``malformed_handoff`` so the runner can flag
        # the locator as broken for this ticket rather than wasting
        # the monolithic-coder budget on a Haiku bug.
        return LocatorOutcome(
            status="malformed_handoff",
            fallback_reason=last_code,
            history=history,
        )

    # Step 3 — candidate-count gates.
    if handoff.is_zero_candidates:
        log.warning(
            "locator returned 0 candidates for %s — falling back to "
            "monolithic Sonnet coder",
            ticket_key,
        )
        return LocatorOutcome(
            status="fallback_monolithic",
            handoff=handoff,
            fallback_reason="locator_zero_candidates",
            history=history,
        )

    if handoff.is_too_many_candidates:
        return LocatorOutcome(
            status="needs_refinement",
            handoff=handoff,
            fallback_reason="locator_too_many",
            history=history,
        )

    # Step 4 — peer-conflict detector.
    candidate_paths = tuple(c.path for c in handoff.files)
    peer_conflicts = await _check_peer_conflicts(
        peer_probe, ticket_key, candidate_paths,
    )
    if peer_conflicts:
        return LocatorOutcome(
            status="peer_conflict",
            handoff=handoff,
            peer_conflicts=peer_conflicts,
            history=history,
        )

    # Step 5 — success.
    return LocatorOutcome(
        status="ok",
        handoff=handoff,
        history=history,
    )
