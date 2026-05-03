"""W15.3 #XXX — System-prompt template that quotes the most recent
Vite build error back to the agent.

W15.1 shipped the wire shape + per-workspace ring buffer + the
``POST /web-sandbox/preview/{workspace_id}/error`` ingestor.  W15.2
landed the ``backend/web/vite_error_relay.py`` projection that turns
each :class:`backend.web_sandbox_vite_errors.ViteBuildError` sitting in
the W15.1 buffer into a single-line ``state.error_history`` entry shaped::

    vite[<phase>] <file>:<line>: <kind>: <message>

W15.3 is the **agent-facing** consumer of those entries.  It walks the
LangGraph ``state.error_history`` newest-to-oldest, picks the most
recent entry whose prefix matches
:data:`backend.web.vite_error_relay.VITE_ERROR_HISTORY_KEY_PREFIX`,
parses out ``file`` / ``line`` / ``message``, and renders the banner::

    上次 build 有 error: [<file>:<line>] [<message>]

The banner is then injected into the assembled system prompt by
:func:`backend.prompt_loader.build_system_prompt` via the new
``last_vite_error_banner`` keyword argument; the W15.3 specialist-node
wiring populates that argument from
``build_last_vite_error_banner(state.error_history)`` so every LLM turn
opens with a visible, structured reminder of the last failure the
plugin reported — without waiting for a tool-error round trip.

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
                  backend.web.vite_error_prompt                           ← W15.3 (this row)
                                              ↓
                  build_system_prompt(..., last_vite_error_banner=...)    ← W15.3 (this row)
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

W15.3 owns:

  * The banner template literal :data:`VITE_ERROR_BANNER_TEMPLATE`
    (frozen Chinese-localised "上次 build 有 error: [file:line]
    [message]") and the section header
    :data:`VITE_ERROR_BANNER_SECTION_HEADER`.
  * :func:`extract_last_vite_error_from_history` — parser that lifts
    ``(file, line, message)`` out of a single W15.2-formatted entry.
  * :func:`format_vite_error_banner` — renderer that fills the template
    + applies the byte-cap defensively.
  * :func:`build_last_vite_error_banner` — top-level helper that walks
    a ``state.error_history`` newest-to-oldest, finds the most recent
    Vite-source entry, and returns the rendered banner (empty string
    when there is no Vite error to surface so callers may pass the
    result through ``build_system_prompt(...)`` unconditionally).
  * The new ``last_vite_error_banner`` keyword on
    :func:`backend.prompt_loader.build_system_prompt` (defined in
    that module; this row only consumes it).
  * The drift-guard tests that pin the template literal, the parser
    behaviour on every degraded shape, and the specialist-node
    invocation kwarg.

W15.3 explicitly does NOT own:

  * The W15.2 history entry format itself — frozen in
    :func:`backend.web.vite_error_relay.format_vite_error_for_history`,
    parsed here.  A change to that format MUST update both rows
    in lock-step (the parser unit tests cover the contract).
  * The 3-strike auto-retry budget escalation (W15.4) — this row
    surfaces the most-recent error to the agent; W15.4 decides when
    to give up and page the operator.
  * The ``vite.config`` scaffold injection that wires the plugin
    into W6/W7/W8 templates — that lives in W15.5.
  * The syntax / undefined-symbol / import-path-typo three-class
    self-fix tests — those live in W15.6.

Module-global state audit (SOP §1)
----------------------------------

This module ships **zero mutable module-level state** — only frozen
string constants (:data:`VITE_ERROR_BANNER_TEMPLATE`,
:data:`VITE_ERROR_BANNER_SECTION_HEADER`,
:data:`VITE_ERROR_BANNER_NO_LINE_TOKEN`,
:data:`VITE_ERROR_BANNER_UNKNOWN_TOKEN`) and an int cap
(:data:`MAX_VITE_ERROR_BANNER_BYTES`).

The banner is computed per LangGraph turn from the per-state
``error_history`` field so cross-worker visibility is not a concern —
each uvicorn worker sees its own slice of the conversation's history,
which is exactly the slice the W15.2 relay populated for that worker
(SOP answer #3, intentional per-worker independence inherited from
W15.1's buffer).

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure projection from a ``Sequence[str]`` to a single ``str``.
No DB pool migration, no compat→pool conversion, no ``asyncio.gather``
race surface.  The parser is deterministic on its input and the
specialist-node call site reads ``state.error_history`` once per turn
under the LangGraph reducer's REPLACE semantics (see
``backend/agents/state.py:115-121``), so concurrent state updates
cannot interleave entries between the parse and the prompt build.

Compat fingerprint grep (SOP §3)
--------------------------------

Pure stdlib + W15.2 imports, verified clean::

    $ grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]" \\
        backend/web/vite_error_prompt.py
    (empty)

Production Readiness Gate §158
------------------------------

(a) **No new pip dep** — only stdlib (``typing``) plus the W15.2
    :mod:`backend.web.vite_error_relay` constants this row consumes.
(b) **No alembic migration** — pure in-memory projection.
(c) **No new ``OMNISIGHT_*`` env knob** — caps are compile-time
    literals; the banner template is intentionally not operator-
    tunable so the W15.4 retry-budget gate's pattern-detection has
    a stable string to grep for.
(d) **No Dockerfile rebuild required** — banner builder rides the
    backend image rebuild already in progress for W15.1 + W15.2.
(e) **Drift guards locked at literals** —
    :data:`VITE_ERROR_BANNER_TEMPLATE` (the W15.4 pattern detector
    will grep for the leading ``上次 build 有 error:`` substring),
    :data:`VITE_ERROR_BANNER_SECTION_HEADER` (the prompt-loader
    integration test asserts this header lands in the assembled
    prompt), :data:`MAX_VITE_ERROR_BANNER_BYTES` (caps the section
    so a pathological message cannot blow the prompt budget).
"""

from __future__ import annotations

from typing import Sequence

from backend.web.vite_error_relay import (
    VITE_ERROR_HISTORY_KEY_PREFIX,
    VITE_ERROR_HISTORY_NO_FILE_TOKEN,
    VITE_ERROR_HISTORY_UNKNOWN_TOKEN,
)


__all__ = [
    "MAX_VITE_ERROR_BANNER_BYTES",
    "VITE_ERROR_BANNER_NO_LINE_TOKEN",
    "VITE_ERROR_BANNER_SECTION_HEADER",
    "VITE_ERROR_BANNER_TEMPLATE",
    "VITE_ERROR_BANNER_UNKNOWN_TOKEN",
    "build_last_vite_error_banner",
    "extract_last_vite_error_from_history",
    "format_vite_error_banner",
]


#: The Chinese-localised banner template the W15.3 row spec calls out
#: verbatim ("Error 進 system prompt 模板：「上次 build 有 error:
#: [file:line] [message]」").  Frozen so the W15.4 3-strike retry-budget
#: gate's pattern detector can grep for the leading literal without
#: risking a translation drift.  ``{file}`` / ``{line}`` / ``{message}``
#: substitutions use :py:meth:`str.format`.
VITE_ERROR_BANNER_TEMPLATE: str = "上次 build 有 error: [{file}:{line}] [{message}]"

#: Section header the prompt-loader uses when injecting the banner into
#: the assembled system prompt.  Stable so the W11.10 / B15 prompt
#: snapshot capture diff (``backend/prompt_registry.py``) treats this
#: section as a stable named block rather than churning every turn.
VITE_ERROR_BANNER_SECTION_HEADER: str = "# Recent Build Error (Vite)"

#: Substitution for the line slot when the W15.2 entry recorded ``?``
#: (the JS plugin could not extract a line — runtime errors from a
#: minified vendor chunk where the sourcemap is missing).  Stable so
#: the rendered banner stays parseable by W15.4's pattern bucketer.
VITE_ERROR_BANNER_NO_LINE_TOKEN: str = "?"

#: Substitution for the message slot when the W15.2 entry's message
#: field is empty (or normalises to empty after collapsing whitespace).
#: Lock-step with
#: :data:`backend.web.vite_error_relay.VITE_ERROR_HISTORY_UNKNOWN_TOKEN`
#: so the two surfaces use the same opaque "we know nothing" marker.
VITE_ERROR_BANNER_UNKNOWN_TOKEN: str = VITE_ERROR_HISTORY_UNKNOWN_TOKEN

#: Hard byte cap on the rendered banner.  Sized so the section
#: (header + blank line + banner) stays comfortably under the
#: prompt-loader's 4 KiB-ish per-section informal budget while still
#: giving the agent room for a 200-byte message body — the W15.2
#: per-line cap is 280 bytes and the W15.1 wire-shape ``message`` cap
#: is 4 KiB, so a pathological producer cannot bust this cap unless
#: the file path itself is enormous (which the W15.1 byte cap on
#: ``file`` already rejects upstream).
MAX_VITE_ERROR_BANNER_BYTES: int = 320


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Return ``value`` truncated so its UTF-8 byte length does not
    exceed ``max_bytes``.

    Walks back from the cut so a multi-byte codepoint is never split.
    Mirrors the helper in :mod:`backend.web.vite_error_relay` —
    duplicated rather than re-exported because importing the helper
    out of a sibling module's private API would couple W15.2 and W15.3
    in a way that complicates W15.4's later refactor of the truncation
    surface (W15.4 may want to byte-cap the W15.4 escalation message
    on a different boundary).
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


def extract_last_vite_error_from_history(
    error_history: Sequence[str],
) -> tuple[str, str, str] | None:
    """Walk ``error_history`` newest-to-oldest, find the most recent
    entry whose prefix matches
    :data:`backend.web.vite_error_relay.VITE_ERROR_HISTORY_KEY_PREFIX`,
    and parse out ``(file, line, message)``.

    Returns ``None`` when no Vite-source entry is present (so the
    caller can short-circuit to "no banner").

    The W15.2 entry format is::

        vite[<phase>] <file>:<line>: <kind>: <message>

    Parsing strategy:

      1. Skip entries that don't start with the prefix (tool-error
         entries from the existing self-healing loop or anything else
         the prompt-loader history accumulates).
      2. Strip ``vite[<phase>] `` (split on ``"] "`` once).
      3. Split the remainder on ``":"`` *limited to 4 parts* so the
         message body — which may legitimately contain colons (e.g.
         ``ParseError: Unexpected token ":"``) — stays intact in the
         tail.  ``parts == [file, line, " kind", " message ..."]``.
      4. Rejoin parts[3:] with ``":"`` to recover the message verbatim
         minus the leading space.
      5. Substitute the no-file / no-line / unknown-message tokens
         when the parsed slot is empty (stable so the rendered banner
         is still well-formed).

    Degraded entries (entry that matches the prefix but does not have
    the expected colon shape — e.g. a pathological filename that
    exhausted the byte cap before the body) are skipped and the walk
    continues looking for an older well-shaped entry.  This matches
    the W15.2 ``vite_error_history_signature`` policy of treating
    degraded entries as their own bucket without poisoning the
    well-shaped data.
    """

    if not error_history:
        return None
    # Materialise once so reversed() works on Sequence (which doesn't
    # always support efficient reverse iteration on arbitrary types).
    materialised = list(error_history)
    for entry in reversed(materialised):
        if not isinstance(entry, str):
            continue
        if not entry.startswith(VITE_ERROR_HISTORY_KEY_PREFIX):
            continue
        # Strip the bracketed phase: ``vite[<phase>] `` -> ``<phase>] ``
        # then the remainder after ``"] "``.  The W15.2 format always
        # emits a literal ``"] "`` separator after the phase bracket;
        # an entry missing that separator is degraded.
        try:
            after_phase = entry.split("] ", 1)[1]
        except IndexError:
            continue
        # ``after_phase`` shape: ``<file>:<line>: <kind>: <message>``
        # Limit splits to 4 parts so message-body colons survive.
        parts = after_phase.split(":", 4)
        if len(parts) < 4:
            # Degraded entry — keep walking.
            continue
        file_token = parts[0].strip() or VITE_ERROR_HISTORY_NO_FILE_TOKEN
        line_token = parts[1].strip() or VITE_ERROR_BANNER_NO_LINE_TOKEN
        # ``parts[2]`` is the kind (e.g. " compile" / " runtime"); the
        # banner intentionally drops it because the row spec is
        # ``[file:line] [message]`` only — operators reading the
        # banner care about *what* failed, not whether the failure was
        # compile-time vs runtime (the agent can re-derive that from
        # the message body).  W15.4's pattern bucketer reads the kind
        # from the original W15.2 entry, not the banner.
        message_tail = ":".join(parts[3:]).strip()
        message_token = message_tail or VITE_ERROR_BANNER_UNKNOWN_TOKEN
        return file_token, line_token, message_token
    return None


def format_vite_error_banner(file: str, line: str, message: str) -> str:
    """Render the banner template with the given file / line / message,
    then truncate to :data:`MAX_VITE_ERROR_BANNER_BYTES`.

    The template literal is frozen at
    :data:`VITE_ERROR_BANNER_TEMPLATE` so this function is a thin
    formatter; callers that need the raw template string should
    reference the constant directly.
    """

    rendered = VITE_ERROR_BANNER_TEMPLATE.format(
        file=file, line=line, message=message,
    )
    return _truncate_utf8(rendered, MAX_VITE_ERROR_BANNER_BYTES)


def build_last_vite_error_banner(error_history: Sequence[str]) -> str:
    """Top-level helper: extract the most recent Vite error from
    ``error_history`` and render the system-prompt banner.

    Returns ``""`` when there is no Vite-source entry to surface so
    the caller may pass the result through
    ``build_system_prompt(last_vite_error_banner=...)`` unconditionally
    — empty banner is a no-op there.

    The specialist-node integration in
    :mod:`backend.agents.nodes._specialist_node_factory` calls this
    function with ``state.error_history`` on every LLM turn so the
    most-recent Vite error sits at the top of the agent's context
    without polluting the per-turn ``last_error`` slot (which the
    self-healing loop reserves for tool-execution errors).
    """

    parts = extract_last_vite_error_from_history(error_history)
    if parts is None:
        return ""
    return format_vite_error_banner(*parts)
