"""W15.2 #XXX — Vite plugin error → LangGraph ``state.error_history`` relay.

W15.1 landed the wire shape, the per-workspace ring buffer, and the
``POST /web-sandbox/preview/{workspace_id}/error`` endpoint that
``packages/omnisight-vite-plugin`` POSTs every compile-time and runtime
error to.  W15.2 is the consumer-side bridge that turns each
:class:`backend.web_sandbox_vite_errors.ViteBuildError` sitting in the
buffer into a single-line ``state.error_history`` entry shaped to slot
into the existing self-healing loop in
:func:`backend.agents.nodes.error_check_node`.

Where it slots into the W15 pipeline
------------------------------------

::

    W14.1 sidecar → omnisight-vite-plugin → POST /preview/{ws}/error      ← W15.1
                                              ↓
                                ViteErrorBuffer (per-worker)              ← W15.1
                                              ↓
                          backend.web.vite_error_relay                    ← W15.2 (this row)
                                              ↓
                            GraphState.error_history (list[str])          ← W15.3 wires this
                                              ↓
                   build_system_prompt(..., last_vite_error=...)          ← W15.3
                                              ↓
                          error_check_node loop detection                 ← existing
                                              ↓
                      same key 3× → W15.4 escalate operator               ← W15.4
                                              ↓
              W15.5 vite.config scaffold ships @omnisight/vite-plugin     ← W15.5
                                              ↓
                  W15.6 syntax / undefined / import-typo tests            ← W15.6

Row boundary
------------

W15.2 owns:

  * The deterministic single-line projection of a ``ViteBuildError``
    into a string entry suitable for ``GraphState.error_history``.
  * The buffer-drain helper that turns the W15.1 ring buffer's
    ``recent()`` output into a list of those entries.
  * The merge helper that appends Vite-source entries onto the
    existing ``state.error_history`` while honouring the same 50-entry
    cap that :func:`backend.agents.nodes.error_check_node` enforces
    (``_ERROR_HISTORY_MAX = 50`` — locked here as
    :data:`VITE_ERROR_HISTORY_MAX`).
  * The partial-state patch builder a future W15.3 hook can return
    from a LangGraph node to fold Vite errors into the active
    graph run without crossing the relay/state boundary.
  * The pattern-signature helper W15.4's 3-strike retry-budget gate
    will feed into its escalation predicate.

W15.2 explicitly does NOT own:

  * The system-prompt template that quotes the last Vite error back
    to the agent — that lives in W15.3.
  * The 3-strike auto-retry budget escalation — that lives in W15.4.
  * The ``vite.config`` scaffold injection (``@omnisight/vite-plugin``
    must be ``import``ed by the W6/W7/W8 templates) — that lives in
    W15.5.
  * The syntax / undefined-symbol / import-path-typo three-class
    self-fix tests — those live in W15.6.
  * The W14.5 idle reaper hook that calls
    :meth:`backend.web_sandbox_vite_errors.ViteErrorBuffer.drop` when
    a sandbox is collected — that hook is filed under W15.4 follow-up
    in HANDOFF.md.

Module-global state audit (SOP §1)
----------------------------------

This module ships **zero mutable module-level state** — only frozen
string constants
(:data:`VITE_ERROR_HISTORY_KEY_PREFIX`, :data:`VITE_ERROR_HISTORY_UNKNOWN_TOKEN`,
…), int caps (:data:`VITE_ERROR_HISTORY_MAX`,
:data:`MAX_VITE_ERROR_HISTORY_ENTRIES`,
:data:`MAX_VITE_ERROR_HISTORY_LINE_BYTES`), and a stdlib ``logger``
(Python's ``logging`` module owns the thread-safe singleton — SOP
answer #1).

The :class:`backend.web_sandbox_vite_errors.ViteErrorBuffer` instance
the relay reads from is owned by W15.1 and is intentionally per-worker
(answer **#3** — see W15.1's audit at
``backend/web_sandbox_vite_errors.py:45``).  W15.2 inherits that
posture: every uvicorn worker drains its own buffer into its own
LangGraph state instance; cross-worker visibility is not required
because every error POST lands on the worker that owns the agent
graph run that triggered the W14.1 sidecar (LB hash by sandbox
hostname; W15.3/W15.4 run on the same request thread).

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure read from
:meth:`backend.web_sandbox_vite_errors.ViteErrorBuffer.recent`
(``RLock``-serialised on the W15.1 side) followed by a pure projection
to ``list[str]``.  No DB pool migration, no compat→pool conversion,
no asyncio.gather race surface.  LangGraph node returns are merged
deterministically by the framework's reducers (``error_history``
explicitly uses REPLACE semantics — see
``backend/agents/state.py:115-121``), so concurrent state updates
cannot interleave entries from this relay.

Compat fingerprint grep (SOP §3)
--------------------------------

Pure stdlib + dataclass imports, verified clean::

    $ grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]" \\
        backend/web/vite_error_relay.py
    (empty)

Production Readiness Gate §158
------------------------------

(a) **No new pip dep** — only stdlib (``logging`` / ``typing``) plus
    the W15.1 :mod:`backend.web_sandbox_vite_errors` module already
    shipped in the previous row.
(b) **No alembic migration** — relay drains the in-memory ring
    buffer; no durable store is touched.
(c) **No new ``OMNISIGHT_*`` env knob** — caps are compile-time
    literals; W15.4 will introduce the operator-tunable retry-budget
    knob, not this row.
(d) **No Dockerfile rebuild required** — the relay rides the
    backend image rebuild already in progress for W15.1.
(e) **Drift guards locked at literals** —
    :data:`VITE_ERROR_HISTORY_MAX` (lock-step with
    ``backend/agents/nodes.py::_ERROR_HISTORY_MAX = 50``),
    :data:`VITE_ERROR_HISTORY_KEY_PREFIX` (the W15.3 system-prompt
    template grep target),
    :data:`MAX_VITE_ERROR_HISTORY_LINE_BYTES`,
    :data:`MAX_VITE_ERROR_HISTORY_ENTRIES`,
    :data:`VITE_ERROR_HISTORY_UNKNOWN_TOKEN`,
    :data:`VITE_ERROR_HISTORY_NO_FILE_TOKEN`.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from backend.web_sandbox_vite_errors import (
    ViteBuildError,
    ViteErrorBuffer,
    get_default_buffer,
)

logger = logging.getLogger(__name__)


__all__ = [
    "MAX_VITE_ERROR_HISTORY_ENTRIES",
    "MAX_VITE_ERROR_HISTORY_LINE_BYTES",
    "VITE_ERROR_HISTORY_KEY_PREFIX",
    "VITE_ERROR_HISTORY_MAX",
    "VITE_ERROR_HISTORY_NO_FILE_TOKEN",
    "VITE_ERROR_HISTORY_UNKNOWN_TOKEN",
    "build_vite_error_state_patch",
    "format_vite_error_for_history",
    "merge_vite_errors_into_history",
    "vite_error_history_signature",
    "vite_errors_for_history",
]


#: Maximum number of entries the merged ``error_history`` may hold.
#: Lock-step with ``_ERROR_HISTORY_MAX`` in ``backend/agents/nodes.py``
#: line 940 — the same window the existing self-healing loop uses for
#: tool-error keys.  W15.2 reuses the same cap so Vite-source entries
#: and tool-error entries share one bounded list (``error_history``
#: uses LangGraph REPLACE reducer — see
#: ``backend/agents/state.py:115-121``).
VITE_ERROR_HISTORY_MAX: int = 50

#: Default upper bound on how many *Vite* errors per
#: :func:`vite_errors_for_history` call.  W15.4 will likely tune this
#: down once the 3-strike retry budget gate lands; until then 10
#: gives the agent enough recent-window context (one HMR retry storm
#: easily produces 5-10 errors before the operator notices) without
#: blowing the merged-history cap.
MAX_VITE_ERROR_HISTORY_ENTRIES: int = 10

#: Per-line byte cap on a formatted Vite history entry.  Sized so the
#: merged 50-entry history stays comfortably under the LLM context
#: budget when paired with the existing tool-error entries (which
#: ``_extract_error_key`` already truncates to ~50 bytes).  The W15.1
#: wire shape already byte-caps ``message`` at 4 KiB; this is a
#: secondary cap applied at projection time so the formatted line is
#: bounded independently of the source ``ViteBuildError``.
MAX_VITE_ERROR_HISTORY_LINE_BYTES: int = 280

#: Stable prefix on every formatted history entry.  W15.3's
#: system-prompt template will grep for this prefix to lift the
#: most recent Vite error onto the agent's "上次 build 有 error: ..."
#: banner without confusing it with tool-error entries.  The prefix
#: doubles as the W15.4 retry-budget pattern key — same prefix means
#: same source channel.
VITE_ERROR_HISTORY_KEY_PREFIX: str = "vite["

#: Token substituted for ``ViteBuildError.file`` when the JS plugin
#: could not extract a source location (e.g. a runtime error from a
#: minified vendor chunk where the sourcemap is missing).  Stable
#: so the W15.4 pattern signature treats two no-location errors with
#: the same message as the same pattern.
VITE_ERROR_HISTORY_NO_FILE_TOKEN: str = "<no-file>"

#: Token substituted for the trimmed message when it normalises to
#: empty (e.g. a runtime error whose ``message`` was nothing but
#: whitespace).  Stable so the W15.4 retry-budget signature does not
#: collapse two truly distinct unknown-message errors into one
#: bucket via the empty string.
VITE_ERROR_HISTORY_UNKNOWN_TOKEN: str = "<unknown>"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Return ``value`` truncated so the UTF-8 byte length does not
    exceed ``max_bytes``.

    Walks back from the cut so a multi-byte codepoint is never split.
    Mirrors the helper in :mod:`backend.web_sandbox_vite_errors` —
    duplicated rather than re-exported because importing the helper
    out of a sibling module's private API would couple the two rows
    in a way W15.4 will then have to unpick.
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


def _normalise_one_line(value: str) -> str:
    """Collapse newlines and tabs to single spaces so the formatted
    line stays single-line.

    Multi-line stack frames in ``ViteBuildError.stack`` are not the
    target of the formatter (the relay only renders ``message`` +
    location), but multi-line ``message`` payloads do exist
    (Rollup's parse error sometimes embeds a code excerpt) and the
    history format is contractually one entry per line.
    """

    if not value:
        return ""
    out: list[str] = []
    last_was_space = False
    for ch in value:
        if ch in ("\n", "\r", "\t"):
            if not last_was_space:
                out.append(" ")
                last_was_space = True
            continue
        out.append(ch)
        last_was_space = ch == " "
    return "".join(out).strip()


def format_vite_error_for_history(error: ViteBuildError) -> str:
    """Project a :class:`ViteBuildError` into the single-line entry
    shape used by ``GraphState.error_history``.

    Format::

        vite[<phase>] <file>:<line>: <kind>: <message>

    Tokens:

      * ``<phase>``  — :data:`backend.web_sandbox_vite_errors.VITE_ERROR_ALLOWED_PHASES`
        member (``config`` / ``buildStart`` / ``load`` / ``transform`` /
        ``hmr`` / ``client``); never empty because W15.1 already
        rejects the wire-shape POST when missing.
      * ``<file>``   — :attr:`ViteBuildError.file` if non-empty, else
        :data:`VITE_ERROR_HISTORY_NO_FILE_TOKEN`.
      * ``<line>``   — :attr:`ViteBuildError.line` if non-null, else
        ``?``.  Column is dropped intentionally — agents care about
        file+line for retry diff, column adds noise.
      * ``<kind>``   — :data:`backend.web_sandbox_vite_errors.VITE_ERROR_ALLOWED_KINDS`
        member (``compile`` / ``runtime``); the kind discriminator
        feeds the W15.4 retry budget so a runtime error in HMR can
        be budgeted differently from a compile error in transform.
      * ``<message>`` — :attr:`ViteBuildError.message` collapsed to
        a single line, then truncated to fit
        :data:`MAX_VITE_ERROR_HISTORY_LINE_BYTES` overall.  Empty
        messages (whitespace-only after collapse) substitute
        :data:`VITE_ERROR_HISTORY_UNKNOWN_TOKEN`.

    Examples::

        vite[transform] src/App.tsx:42: compile: Failed to parse module
        vite[hmr] src/components/Card.tsx:?: runtime: Identifier expected
        vite[client] <no-file>:?: runtime: <unknown>
    """

    if not isinstance(error, ViteBuildError):
        raise TypeError(
            f"error must be a ViteBuildError, got {type(error).__name__}"
        )

    file_token = error.file if (error.file and error.file.strip()) else VITE_ERROR_HISTORY_NO_FILE_TOKEN
    line_token = str(error.line) if error.line is not None else "?"
    message = _normalise_one_line(error.message or "")
    if not message:
        message = VITE_ERROR_HISTORY_UNKNOWN_TOKEN

    head = (
        f"{VITE_ERROR_HISTORY_KEY_PREFIX}{error.phase}] "
        f"{file_token}:{line_token}: {error.kind}: "
    )
    head_bytes = len(head.encode("utf-8"))
    if head_bytes >= MAX_VITE_ERROR_HISTORY_LINE_BYTES:
        # Pathological ``file`` that already busts the cap — fall
        # back to a stable degraded form so the line is still
        # parseable by W15.3's prefix grep.  This branch only fires
        # if the JS plugin sent a 256-byte filename; the W15.1
        # wire-shape validation byte-caps ``file`` at 2 KiB so
        # degraded form is reachable in pathological tests but
        # essentially never in production.
        return _truncate_utf8(head, MAX_VITE_ERROR_HISTORY_LINE_BYTES).rstrip()

    remaining = MAX_VITE_ERROR_HISTORY_LINE_BYTES - head_bytes
    message_truncated = _truncate_utf8(message, remaining)
    return head + message_truncated


def vite_errors_for_history(
    buffer: ViteErrorBuffer | None,
    workspace_id: str,
    *,
    limit: int | None = None,
) -> list[str]:
    """Drain up to ``limit`` most-recent Vite errors for
    ``workspace_id`` into the formatted ``error_history`` entry shape.

    Always returns a fresh list (the W15.1 buffer's ``recent`` already
    returns a fresh list — we project it into a new list of strings
    so callers may mutate without aliasing).  Returns an empty list
    when:

      * ``buffer`` is ``None`` (callers may pass ``None`` to mean
        "use the per-worker default"; that fallback is handled
        explicitly via :func:`get_default_buffer` so the test
        injection point in :mod:`backend.web_sandbox_vite_errors`
        keeps working);
      * ``workspace_id`` is unknown to the buffer;
      * ``limit`` is ``0`` (callers wanting "all" pass ``None``).

    ``limit=None`` defaults to :data:`MAX_VITE_ERROR_HISTORY_ENTRIES`
    rather than "drain everything" so a runaway HMR loop cannot
    explode the merged history past the 50-entry cap on a single
    fold.  Callers who genuinely want the entire buffer pass an
    explicit large ``limit`` (or call
    :meth:`backend.web_sandbox_vite_errors.ViteErrorBuffer.recent`
    directly).
    """

    if not isinstance(workspace_id, str) or not workspace_id:
        raise ValueError("workspace_id must be a non-empty string")
    if limit is not None and not isinstance(limit, int):
        raise TypeError(f"limit must be an int or None, got {type(limit).__name__}")
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")

    target_buffer = buffer if buffer is not None else get_default_buffer()
    effective_limit = MAX_VITE_ERROR_HISTORY_ENTRIES if limit is None else limit
    if effective_limit == 0:
        return []
    raw = target_buffer.recent(workspace_id, limit=effective_limit)
    return [format_vite_error_for_history(err) for err in raw]


def merge_vite_errors_into_history(
    existing: Sequence[str],
    formatted: Iterable[str],
    *,
    max_entries: int = VITE_ERROR_HISTORY_MAX,
) -> list[str]:
    """Append ``formatted`` Vite entries onto ``existing`` history,
    capping the result at ``max_entries`` (default
    :data:`VITE_ERROR_HISTORY_MAX`).

    Behaviour mirrors :func:`backend.agents.nodes.error_check_node`'s
    inline cap (``[-_ERROR_HISTORY_MAX:]``) so the two histories
    coexist on the same bounded list without one starving the other.
    Order is preserved (oldest existing first, newest Vite last) so
    the loop-detection comparison
    (``updated_history[-1] == updated_history[-2]``) sees a stable
    most-recent entry.

    Returns a fresh ``list[str]`` — never mutates the inputs.
    """

    if max_entries < 0:
        raise ValueError(f"max_entries must be >= 0, got {max_entries}")
    if max_entries == 0:
        return []
    merged = list(existing)
    for entry in formatted:
        if not isinstance(entry, str):
            raise TypeError(
                f"formatted entries must be str, got {type(entry).__name__}"
            )
        merged.append(entry)
    if len(merged) <= max_entries:
        return merged
    return merged[-max_entries:]


def build_vite_error_state_patch(
    state: Any,
    *,
    buffer: ViteErrorBuffer | None,
    workspace_id: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a partial LangGraph state patch dict that folds the
    most-recent Vite errors for ``workspace_id`` into
    ``state.error_history`` without mutating the input ``state``.

    Returns a dict shaped for direct return from a LangGraph node
    (``error_history`` keyed at top level — REPLACE reducer).  The
    dict is **empty** when there is nothing to fold (no buffer
    entries for the workspace), so a node may unconditionally
    spread it into its return without polluting the state.

    ``state`` is duck-typed so callers may pass either a
    :class:`backend.agents.state.GraphState` (the production path)
    or a test fake exposing ``error_history`` (a sequence of
    strings).  Missing or non-iterable ``error_history`` is treated
    as an empty list rather than raising — the goal is best-effort
    folding from a relay perspective; the caller's node decides
    whether to enforce schema strictness.
    """

    if not isinstance(workspace_id, str) or not workspace_id:
        raise ValueError("workspace_id must be a non-empty string")

    formatted = vite_errors_for_history(buffer, workspace_id, limit=limit)
    if not formatted:
        return {}

    raw_existing = getattr(state, "error_history", None)
    if raw_existing is None:
        existing: list[str] = []
    else:
        try:
            existing = [str(item) for item in raw_existing]
        except TypeError:
            logger.warning(
                "build_vite_error_state_patch: state.error_history "
                "is not iterable (type=%s); treating as empty",
                type(raw_existing).__name__,
            )
            existing = []
    merged = merge_vite_errors_into_history(existing, formatted)
    return {"error_history": merged}


def vite_error_history_signature(formatted: Sequence[str]) -> tuple[str, ...]:
    """Stable signature of a list of formatted Vite history entries
    suitable for the W15.4 3-strike retry-budget pattern detector.

    The signature is the tuple of the leading
    ``vite[<phase>] <file>:<line>: <kind>:`` heads — the message
    body is dropped so a single error pattern that recurs with
    slightly different wording (e.g. ``"foo is not defined"`` and
    ``"'foo' is not defined"`` both reduce to the same head if the
    file/line/phase/kind match) collapses to one bucket.

    W15.4 will compare the most-recent N signatures and escalate to
    operator when the same head appears 3+ times consecutively.
    Returning ``tuple`` (not ``list``) so the result is hashable
    and can sit directly in W15.4's ``Counter``-based budget
    accumulator without an extra ``tuple()`` cast.
    """

    out: list[str] = []
    for entry in formatted:
        if not isinstance(entry, str):
            raise TypeError(
                f"formatted entries must be str, got {type(entry).__name__}"
            )
        # Head ends after the second ":" because the format is
        # ``vite[<phase>] <file>:<line>: <kind>: <message>``.
        # Walk through the colons preserving the bracketed phase
        # (which itself contains no colons by construction —
        # ``VITE_ERROR_ALLOWED_PHASES`` are alphanumeric).
        head_end = -1
        colons_seen = 0
        for idx, ch in enumerate(entry):
            if ch == ":":
                colons_seen += 1
                if colons_seen == 3:
                    head_end = idx + 1
                    break
        if head_end <= 0:
            # Degraded entry that does not match the W15.2 format
            # (e.g. a pathological filename that exhausted the byte
            # cap before the body).  Use the entire entry as the
            # signature so two such entries still bucket together
            # when identical.
            out.append(entry)
        else:
            out.append(entry[:head_end])
    return tuple(out)
