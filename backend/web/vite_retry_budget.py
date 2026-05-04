"""W15.4 #XXX — Auto-retry budget for the Vite self-healing loop.

W15.1 ships the wire shape + per-workspace ring buffer + the
``POST /web-sandbox/preview/{workspace_id}/error`` endpoint.  W15.2
projects each :class:`backend.web_sandbox_vite_errors.ViteBuildError`
into a single-line ``state.error_history`` entry shaped::

    vite[<phase>] <file>:<line>: <kind>: <message>

W15.3 quotes the most recent such entry back to the agent on every LLM
turn via the Chinese-localised system-prompt banner.  W15.4 (this row)
closes the self-healing loop: when the agent retries the same Vite
build error 3 times in a row without progress, the runtime escalates
to the operator instead of letting the loop spin indefinitely.

Where this slots into the W15 pipeline
--------------------------------------

::

    W14.1 sidecar → omnisight-vite-plugin → POST /preview/{ws}/error      ← W15.1
                                              ↓
                                ViteErrorBuffer (per-worker)              ← W15.1
                                              ↓
                          backend.web.vite_error_relay                    ← W15.2
                                              ↓
                            GraphState.error_history (list[str])          ← W15.2
                                              ↓
                  backend.web.vite_error_prompt (banner)                  ← W15.3
                                              ↓
                  backend.web.vite_retry_budget                           ← W15.4 (this row)
                                              ↓
              same head 3× consecutively → emit_debug_finding +
              emit_pipeline_phase → operator UI alert                     ← W15.4 (this row)
                                              ↓
              W15.5 vite.config scaffold ships @omnisight/vite-plugin     ← W15.5
                                              ↓
                  W15.6 syntax / undefined / import-typo tests            ← W15.6

Row boundary
------------

W15.4 owns:

  * The 3-strike threshold literal :data:`VITE_RETRY_BUDGET_THRESHOLD`
    (intentionally compile-time so the W15.6 self-fix tests have a
    stable target — the operator-tunable knob is filed as a follow-up
    if real-world tuning demands it).
  * The pattern detector
    :func:`count_trailing_same_vite_signature` — walks
    ``state.error_history`` newest-to-oldest and counts how many
    trailing Vite-source entries share the most recent W15.2 signature
    (head only — see
    :func:`backend.web.vite_error_relay.vite_error_history_signature`).
  * The escalation predicate :func:`should_escalate_vite_pattern` —
    returns a :class:`ViteRetryBudgetEscalation` decision when the
    trailing-same-signature count crosses the threshold AND the
    signature has not already been escalated this graph run.
  * The operator-facing banner template
    :data:`VITE_ESCALATION_BANNER_TEMPLATE` and the renderer
    :func:`format_vite_escalation_banner`.
  * The emission helper :func:`emit_vite_pattern_escalation` —
    publishes one ``vite_retry_budget_exhausted`` debug finding +
    one ``vite_retry_budget_exhausted`` pipeline phase per
    (graph run, signature) tuple.
  * The new ``vite_escalated_signatures`` list field on
    :class:`backend.agents.state.GraphState` (defined in that module;
    this row only consumes it for idempotency gating).

W15.4 explicitly does NOT own:

  * The W15.2 history entry format itself — frozen in
    :func:`backend.web.vite_error_relay.format_vite_error_for_history`,
    consumed here.  A change to that format MUST update both rows in
    lock-step (the drift-guard tests pin the contract).
  * The W15.3 system-prompt banner — quoted by the agent on every
    LLM turn, independent of the W15.4 escalation decision.  W15.3's
    banner stays even after escalation so the operator can read what
    the agent saw.
  * Hard-freezing the agent (``AgentAction(type="update_status",
    status="awaiting_confirmation")`` in the existing
    :func:`backend.agents.nodes.error_check_node` retry-exhausted
    path).  W15.4 raises the operator alert; whether to also freeze
    the agent is a downstream policy decision tracked under
    :data:`VITE_RETRY_BUDGET_FOLLOWUP_FREEZE_HINT` (see comment).
  * The ``vite.config`` scaffold injection (W15.5) and the
    syntax / undefined-symbol / import-path-typo three-class
    self-fix tests (W15.6).

Module-global state audit (SOP §1)
----------------------------------

This module ships **zero mutable module-level state** — only frozen
string constants (:data:`VITE_ESCALATION_FINDING_TYPE`,
:data:`VITE_ESCALATION_PIPELINE_PHASE`,
:data:`VITE_ESCALATION_BANNER_TEMPLATE`), int caps
(:data:`VITE_RETRY_BUDGET_THRESHOLD`,
:data:`MAX_VITE_ESCALATION_BANNER_BYTES`), and a frozen dataclass
(:class:`ViteRetryBudgetEscalation`).

The escalation idempotency gate is per-LangGraph-state — not
per-worker — so the SOP §1 cross-worker concern does not apply.  Each
uvicorn worker reads the same constants from the same git checkout
(SOP answer #1) and computes the decision per LangGraph turn from the
per-state ``error_history`` + ``vite_escalated_signatures`` (which
LangGraph reducers serialise on the framework's side via REPLACE
semantics — see ``backend/agents/state.py:115-121``).  Cross-worker
visibility inherits W15.1's intentional per-worker independence (the
buffer's posture).

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure projection from a ``Sequence[str]`` (the merged
``error_history``) plus a ``Sequence[str]`` of already-escalated
signatures into an :class:`Optional` :class:`ViteRetryBudgetEscalation`.
No DB pool migration, no compat→pool conversion, no
``asyncio.gather`` race surface.  ``emit_vite_pattern_escalation``
delegates to :func:`backend.events.emit_debug_finding` /
:func:`backend.events.emit_pipeline_phase`, which already carry their
own DB-persistence semantics (the debug-finding write is a fire-and-
forget ``asyncio.create_task`` — see ``backend/events.py:836-846``).

Compat fingerprint grep (SOP §3)
--------------------------------

Pure stdlib + W15.2 imports, verified clean::

    $ grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]" \\
        backend/web/vite_retry_budget.py
    (empty)

Production Readiness Gate §158
------------------------------

(a) **No new pip dep** — only stdlib (``dataclasses`` / ``typing``)
    plus the W15.2 :mod:`backend.web.vite_error_relay` constants this
    row consumes.
(b) **No alembic migration** — pure in-memory detection.  The
    debug-finding emission rides the existing
    :func:`backend.events._persist_debug_finding` write path (already
    DB-backed via the asyncpg pool).
(c) **No new ``OMNISIGHT_*`` env knob** — :data:`VITE_RETRY_BUDGET_THRESHOLD`
    is a compile-time literal so the W15.6 self-fix tests can pin it.
    A future operator-tunable knob can be added via a wrapper that
    reads ``OMNISIGHT_VITE_RETRY_BUDGET_THRESHOLD`` and falls back to
    the literal — filed as a W15.4 follow-up if real-world tuning
    demands it.
(d) **No Dockerfile rebuild required** — escalation rides the
    backend image rebuild already in progress for W15.1 + W15.2 +
    W15.3.
(e) **Drift guards locked at literals** —
    :data:`VITE_RETRY_BUDGET_THRESHOLD` (W15.6 grep target),
    :data:`VITE_ESCALATION_FINDING_TYPE` (the operator UI's debug-
    finding filter pins this string),
    :data:`VITE_ESCALATION_PIPELINE_PHASE` (the SSE consumer's phase
    name discriminator),
    :data:`VITE_ESCALATION_BANNER_TEMPLATE` (the operator-facing
    message stays byte-stable across rows),
    :data:`MAX_VITE_ESCALATION_BANNER_BYTES` (caps the banner so a
    pathological signature cannot blow the SSE event budget).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from backend.web.vite_error_relay import (
    VITE_ERROR_HISTORY_KEY_PREFIX,
    vite_error_history_signature,
)


__all__ = [
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


#: Number of consecutive same-signature Vite errors that triggers an
#: operator escalation.  Three strikes (matching the W15.4 row spec
#: literal "連 3 次失敗") — the agent gets two retries with the W15.3
#: banner in context before the operator is paged.  Compile-time so
#: the W15.6 self-fix tests have a stable grep target; if real-world
#: tuning demands an operator knob, wrap this constant in a
#: ``resolve_vite_retry_budget_threshold()`` helper that reads
#: ``OMNISIGHT_VITE_RETRY_BUDGET_THRESHOLD`` with this literal as
#: the default — adding the knob without changing the literal keeps
#: the drift guard.
VITE_RETRY_BUDGET_THRESHOLD: int = 3

#: Stable ``finding_type`` string the operator UI filters on when
#: surfacing W15.4 escalations in the debug-finding feed.  Lock-step
#: with the existing tool-error escalation finding types (``stuck_loop``
#: / ``retries_exhausted`` / ``verification_exhausted`` — see
#: ``backend/agents/nodes.py:867-987``) so the operator can apply a
#: prefix filter ``WHERE finding_type LIKE '%exhausted'`` and catch
#: every escalation channel.
VITE_ESCALATION_FINDING_TYPE: str = "vite_retry_budget_exhausted"

#: Stable pipeline-phase string the SSE event consumer keys on when
#: highlighting W15.4 escalations in the pipeline timeline.  Matches
#: the existing pattern of ``<bucket>_exhausted`` / ``<bucket>_failed``
#: phase names so the timeline UI's colour map stays consistent.
VITE_ESCALATION_PIPELINE_PHASE: str = "vite_retry_budget_exhausted"

#: Operator-facing banner template the W15.4 escalation renders into
#: the debug-finding ``message`` field and the pipeline-phase ``detail``
#: field.  Frozen so the operator UI's snapshot capture stays byte-
#: stable across rows and the W15.6 self-fix tests can pin the
#: substring they expect to see in the SSE payload.
#:
#: ``{count}`` and ``{threshold}`` are integers; ``{pattern}`` is the
#: W15.2 head-only signature (e.g. ``vite[transform] src/App.tsx:42:
#: compile:``).
VITE_ESCALATION_BANNER_TEMPLATE: str = (
    "Vite build error pattern repeated {count}× "
    "(threshold {threshold}) — escalating to operator: {pattern}"
)

#: Hard byte cap on the rendered escalation banner.  Sized so the SSE
#: event payload (event header + finding-type + this banner + W15.2
#: signature) stays under 1 KiB even with a pathological signature
#: (the W15.2 per-line cap is 280 bytes, so the head-only signature
#: which is a strict prefix of that line cannot exceed 280 bytes
#: either; 512 gives ~230 bytes of headroom for the surrounding
#: template literal and bookkeeping fields).
MAX_VITE_ESCALATION_BANNER_BYTES: int = 512


@dataclass(frozen=True)
class ViteRetryBudgetEscalation:
    """Frozen value object describing a 3-strike Vite escalation
    decision.  Returned by :func:`should_escalate_vite_pattern`; the
    caller passes it to :func:`emit_vite_pattern_escalation` and
    appends ``pattern`` to ``state.vite_escalated_signatures`` to
    prevent re-emission on subsequent LLM turns that observe the
    same trailing signature.

    Fields:

      * ``pattern``    — the W15.2 head-only signature (the value the
                        idempotency gate reads).  Stable string keyed
                        on file/line/phase/kind only — message body
                        is dropped so two errors with slightly
                        different wording bucket together.
      * ``count``      — number of consecutive trailing Vite entries
                        sharing this signature in the observed history
                        snapshot.  Always ``>= threshold`` when this
                        dataclass is returned by
                        :func:`should_escalate_vite_pattern`.
      * ``threshold``  — value of :data:`VITE_RETRY_BUDGET_THRESHOLD`
                        that the count crossed.  Echoed so the
                        operator-facing message can quote the gate
                        verbatim (W15.6 self-fix tests pin the
                        substring).
    """

    pattern: str
    count: int
    threshold: int


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Return ``value`` truncated so its UTF-8 byte length does not
    exceed ``max_bytes``.

    Walks back from the cut so a multi-byte codepoint is never split.
    Mirrors the helper in :mod:`backend.web.vite_error_relay` and
    :mod:`backend.web.vite_error_prompt` — duplicated rather than
    re-exported so the W15.4 escalation surface stays decoupled from
    the W15.2/W15.3 truncation policy (W15.4 may want to byte-cap on
    a different boundary in a follow-up).
    """

    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    cut = max_bytes
    while cut > 0 and (encoded[cut] & 0b1100_0000) == 0b1000_0000:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore")


def count_trailing_same_vite_signature(
    error_history: Sequence[str],
) -> tuple[int, str | None]:
    """Walk ``error_history`` newest-to-oldest, filter to entries
    matching :data:`backend.web.vite_error_relay.VITE_ERROR_HISTORY_KEY_PREFIX`,
    and count how many trailing entries share the most recent W15.2
    signature.

    Returns ``(count, signature)`` where ``signature`` is the W15.2
    head-only string (e.g. ``vite[transform] src/App.tsx:42: compile:``)
    or ``(0, None)`` when no Vite-source entry is present in the
    history.

    Non-Vite entries (tool-error keys from the existing self-healing
    loop, anything else the prompt-loader history accumulates) are
    skipped — the count walks only across Vite-prefixed entries so a
    tool error wedged between two same-signature Vite entries does not
    reset the budget.  This matches the row-spec semantics of "同
    error pattern 連 3 次失敗" — the pattern is the *Vite* pattern,
    independent of unrelated tool-channel noise.

    Implementation detail: the signature comparison delegates to
    :func:`backend.web.vite_error_relay.vite_error_history_signature`
    so any change to the head-only projection (e.g. dropping the kind
    discriminator from the head) automatically propagates to the W15.4
    bucket — no parallel parsing here.
    """

    if not error_history:
        return 0, None
    vite_entries = [
        entry
        for entry in error_history
        if isinstance(entry, str)
        and entry.startswith(VITE_ERROR_HISTORY_KEY_PREFIX)
    ]
    if not vite_entries:
        return 0, None
    sigs = vite_error_history_signature(vite_entries)
    if not sigs:
        return 0, None
    last_sig = sigs[-1]
    count = 0
    for sig in reversed(sigs):
        if sig == last_sig:
            count += 1
        else:
            break
    return count, last_sig


def should_escalate_vite_pattern(
    error_history: Sequence[str],
    *,
    threshold: int = VITE_RETRY_BUDGET_THRESHOLD,
    already_escalated: Sequence[str] = (),
) -> ViteRetryBudgetEscalation | None:
    """Decide whether a 3-strike Vite escalation should fire for the
    given ``error_history`` snapshot.

    Returns a :class:`ViteRetryBudgetEscalation` when:

      1. The trailing-same-signature count from
         :func:`count_trailing_same_vite_signature` is ``>= threshold``
         (default :data:`VITE_RETRY_BUDGET_THRESHOLD`).
      2. The signature has not already been escalated this graph run
         (i.e. it is not present in ``already_escalated``).

    Returns ``None`` otherwise.  The caller is expected to:

      * Pass ``state.vite_escalated_signatures`` as ``already_escalated``
        so the gate is per-graph-run idempotent.
      * On a non-``None`` return, call
        :func:`emit_vite_pattern_escalation` and append
        ``decision.pattern`` to its return-dict's
        ``vite_escalated_signatures`` field so the next LLM turn's
        ``state.vite_escalated_signatures`` reflects the emission.

    Threshold validation: ``threshold`` must be ``>= 1``; otherwise
    the gate is meaningless (count starts at 1 the moment a single
    Vite error appears).  Raises :class:`ValueError` on invalid input
    so a misuse fails loudly rather than escalating on every entry.
    """

    if not isinstance(threshold, int):
        raise TypeError(
            f"threshold must be an int, got {type(threshold).__name__}"
        )
    if threshold < 1:
        raise ValueError(f"threshold must be >= 1, got {threshold}")
    count, sig = count_trailing_same_vite_signature(error_history)
    if sig is None or count < threshold:
        return None
    if sig in already_escalated:
        return None
    return ViteRetryBudgetEscalation(
        pattern=sig, count=count, threshold=threshold,
    )


def format_vite_escalation_banner(
    pattern: str, count: int, threshold: int,
) -> str:
    """Render the operator-facing escalation banner using the frozen
    :data:`VITE_ESCALATION_BANNER_TEMPLATE`, then truncate to
    :data:`MAX_VITE_ESCALATION_BANNER_BYTES`.

    Used by :func:`emit_vite_pattern_escalation` for the
    ``debug_finding.message`` and ``pipeline_phase.detail`` payloads.
    """

    rendered = VITE_ESCALATION_BANNER_TEMPLATE.format(
        count=count, threshold=threshold, pattern=pattern,
    )
    return _truncate_utf8(rendered, MAX_VITE_ESCALATION_BANNER_BYTES)


def emit_vite_pattern_escalation(
    *,
    task_id: str,
    agent_id: str,
    decision: ViteRetryBudgetEscalation,
) -> None:
    """Publish one ``vite_retry_budget_exhausted`` debug finding +
    one ``vite_retry_budget_exhausted`` pipeline phase for the given
    escalation decision.

    The two emissions cover the two operator surfaces:

      * Debug finding — durable record persisted via the existing
        :func:`backend.events.emit_debug_finding` write path
        (asyncpg pool fire-and-forget).  The operator UI's
        debug-feed filter pins :data:`VITE_ESCALATION_FINDING_TYPE`
        as the discriminator.
      * Pipeline phase — transient SSE event the timeline UI
        consumes for live retry-budget status.

    Severity is fixed at ``"error"`` (matching the existing
    ``retries_exhausted`` / ``verification_exhausted`` finding types)
    so the operator UI's "high-priority alert" filter catches it.

    Imports are local so the relay import graph stays lean —
    :mod:`backend.events` pulls in the SSE bus, the debug-finding DB
    write helper, and a chunk of the routing graph that this module
    has no business loading at import time (mirrors the local-import
    pattern in :func:`backend.agents.nodes.error_check_node`).
    """

    from backend.events import emit_debug_finding, emit_pipeline_phase

    banner = format_vite_escalation_banner(
        decision.pattern, decision.count, decision.threshold,
    )
    emit_pipeline_phase(VITE_ESCALATION_PIPELINE_PHASE, banner, broadcast_scope="session")
    emit_debug_finding(
        task_id=task_id or "",
        agent_id=agent_id or "",
        finding_type=VITE_ESCALATION_FINDING_TYPE,
        severity="error",
        message=banner,
        context={
            "pattern": decision.pattern,
            "count": decision.count,
            "threshold": decision.threshold,
        },
        broadcast_scope="session",
    )
