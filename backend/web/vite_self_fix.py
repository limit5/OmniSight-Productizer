"""W15.6 #XXX — Three-class auto-fix classifier for the Vite self-healing loop.

W15.1 ships the wire shape + per-workspace ring buffer + the
``POST /web-sandbox/preview/{workspace_id}/error`` ingestor.  W15.2
projects each :class:`backend.web_sandbox_vite_errors.ViteBuildError`
into a single-line ``state.error_history`` entry shaped::

    vite[<phase>] <file>:<line>: <kind>: <message>

W15.3 quotes the most recent such entry back to the agent on every LLM
turn via the Chinese-localised system-prompt banner.  W15.4 closes the
loop's escape hatch by escalating to the operator after the same
W15.2 head-only signature recurs 3 times in a row.  W15.5 ships the
scaffold-side closer that wires ``@omnisight/vite-plugin`` into
W6/W7/W8 generated projects so every freshly-rendered project starts
emitting errors into the loop the moment the operator runs ``pnpm
dev`` inside the W14.1 sidecar.

W15.6 (this row) closes the W15 epic with the **three-class
self-fix classifier** that pins which Vite build errors the
self-healing loop is contractually expected to recover from
*automatically* — without paging the operator — within
:data:`backend.web.vite_retry_budget.VITE_RETRY_BUDGET_THRESHOLD`
strikes.

The three classes — frozen for the row-spec literal "syntax error /
undefined symbol / import path typo 三類自動修" — are:

  1. **Syntax error** (compile-time parse failure):
     :data:`VITE_SELF_FIX_CLASS_SYNTAX_ERROR`.  The agent rewrites the
     malformed source and the Vite parser accepts the next build.
     Examples: ``Failed to parse module``, ``Unexpected token``,
     ``ParseError: Unexpected character "}"``.

  2. **Undefined symbol** (compile-time *or* runtime):
     :data:`VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL`.  The agent
     either declares the missing identifier or imports it from the
     correct module.  Examples: ``foo is not defined``,
     ``ReferenceError: bar is not defined``,
     ``Cannot find name 'baz'`` (TypeScript), ``no-undef``.

  3. **Import path typo** (compile-time module resolution):
     :data:`VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO`.  The agent fixes
     the import specifier (case, extension, relative-path direction).
     Examples: ``Failed to resolve import "./Header"``,
     ``Cannot find module './header.tsx'``, ``Module not found``.

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
                  backend.web.vite_retry_budget (3-strike gate)           ← W15.4
                                              ↓
              W15.5 vite.config scaffold ships @omnisight/vite-plugin     ← W15.5
                                              ↓
                  backend.web.vite_self_fix (this row)                    ← W15.6
                                              ↓
              classify_vite_error_for_self_fix(entry) → class | None      ← W15.6
                                              ↓
                  Tests pin per-class round-trip through W15.1 → W15.4    ← W15.6

Row boundary
------------

W15.6 owns:

  * The frozen class identifiers
    (:data:`VITE_SELF_FIX_CLASS_SYNTAX_ERROR`,
    :data:`VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL`,
    :data:`VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO`) and the
    :data:`VITE_SELF_FIX_CLASSES` ordered tuple.
  * The frozen pattern tuples
    (:data:`VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS`, …) — substring +
    regex tokens that match the canonical Vite / Rollup / esbuild
    error message shapes for each class.  Substring tokens are kept
    distinct from the regex tokens so the W15.6 classifier does not
    accidentally compile a substring as a regex (e.g. ``Failed to
    parse module`` would otherwise need backslash-escaping for a
    literal match).
  * :func:`classify_vite_error_for_self_fix` — top-level classifier.
    Accepts a W15.2-formatted history entry (the
    ``vite[<phase>] <file>:<line>: <kind>: <message>`` shape) and
    returns the class identifier or ``None`` when the message body
    does not match any of the three classes (operator-escalation
    territory).
  * :func:`classify_vite_history_for_self_fix` — convenience that
    walks a ``Sequence[str]`` (the LangGraph
    ``state.error_history``) and returns a list of
    :class:`ViteSelfFixClassification` records; non-Vite entries are
    skipped so the caller may pass the entire history without
    pre-filtering.
  * :func:`is_vite_history_entry` — predicate used by the classifier
    and re-exported so test fixtures can assert on the same gate.
  * :func:`summarise_self_fix_classes` — :class:`collections.Counter`
    over the three classes (plus an ``unclassified`` bucket) suitable
    for a one-line debug log or an operator-facing dashboard tile.
  * :class:`ViteSelfFixClassification` — frozen dataclass carrying
    ``(entry, vite_class)`` for the test suite's per-class
    round-trip assertions.
  * The drift-guard tests that pin the class identifiers, the pattern
    contracts, and the per-class round-trip through W15.1 / W15.2 /
    W15.3 / W15.4.

W15.6 explicitly does NOT own:

  * The W15.2 history entry format itself — frozen in
    :func:`backend.web.vite_error_relay.format_vite_error_for_history`,
    consumed here.
  * The W15.4 escalation gate — the classifier returns the class
    name; whether to escalate is W15.4's threshold decision.  W15.6
    tests verify that a same-class trail of
    :data:`backend.web.vite_retry_budget.VITE_RETRY_BUDGET_THRESHOLD`
    consecutive entries fires the gate.
  * Any actual *fix* logic — the agent (LangGraph specialist node)
    is responsible for proposing the diff that resolves the error.
    W15.6 only pins which error classes the agent is contractually
    expected to be able to fix without operator help.
  * The ``vite.config`` scaffold injection — that lives in W15.5.

Module-global state audit (SOP §1)
----------------------------------

This module ships **zero mutable module-level state** — only frozen
string constants
(:data:`VITE_SELF_FIX_CLASS_SYNTAX_ERROR`,
:data:`VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL`,
:data:`VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO`,
:data:`VITE_SELF_FIX_UNCLASSIFIED_TOKEN`), frozen tuples of substrings
and pre-compiled :class:`re.Pattern` objects (see :func:`_compile`),
and a frozen :class:`ViteSelfFixClassification` dataclass.

**Answer #1** — every uvicorn worker reads the same constants from the
same git checkout; classification is computed per-LangGraph-turn from
the per-state ``error_history`` (no shared singleton).  Cross-worker
visibility inherits W15.1's intentional per-worker independence (the
buffer's posture).

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure projection from a ``Sequence[str]`` to a list of
:class:`ViteSelfFixClassification` records.  No DB pool migration, no
compat→pool conversion, no ``asyncio.gather`` race surface.  Pattern
matching is deterministic on its input.

Compat fingerprint grep (SOP §3)
--------------------------------

Pure stdlib + W15.2 imports, verified clean::

    $ grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]" \\
        backend/web/vite_self_fix.py
    (empty)

Production Readiness Gate §158
------------------------------

(a) **No new pip dep** — only stdlib (``collections`` / ``dataclasses``
    / ``re`` / ``typing``) plus the W15.2
    :mod:`backend.web.vite_error_relay` constants this row consumes.
(b) **No alembic migration** — pure in-memory classification.
(c) **No new ``OMNISIGHT_*`` env knob** — class identifiers and
    pattern tuples are compile-time literals so the W15.6 contract
    tests and the W15.4 retry-budget gate stay aligned without an
    operator-tunable surface.
(d) **No Dockerfile rebuild required** — classifier rides the
    backend image rebuild already in progress for W15.1 + W15.2 +
    W15.3 + W15.4 + W15.5.
(e) **Drift guards locked at literals** —
    :data:`VITE_SELF_FIX_CLASS_SYNTAX_ERROR`,
    :data:`VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL`,
    :data:`VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO`,
    :data:`VITE_SELF_FIX_CLASSES`,
    :data:`VITE_SELF_FIX_UNCLASSIFIED_TOKEN`.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from backend.web.vite_error_relay import VITE_ERROR_HISTORY_KEY_PREFIX


__all__ = [
    "VITE_SELF_FIX_CLASSES",
    "VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO",
    "VITE_SELF_FIX_CLASS_SYNTAX_ERROR",
    "VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL",
    "VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS",
    "VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS",
    "VITE_SELF_FIX_UNCLASSIFIED_TOKEN",
    "VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS",
    "ViteSelfFixClassification",
    "classify_vite_error_for_self_fix",
    "classify_vite_history_for_self_fix",
    "is_vite_history_entry",
    "summarise_self_fix_classes",
]


#: Stable class identifier for compile-time parse failures.  Matches
#: the row-spec literal "syntax error" — pinned so the W15.6 contract
#: tests, the W15.4 retry-budget pattern detector, and any future
#: operator dashboard share one bucket key.
VITE_SELF_FIX_CLASS_SYNTAX_ERROR: str = "syntax_error"

#: Stable class identifier for undefined-symbol errors (compile-time
#: ``Cannot find name`` / ``no-undef`` and runtime ``ReferenceError``
#: alike — the agent's fix surface is the same: declare or import the
#: missing identifier).
VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL: str = "undefined_symbol"

#: Stable class identifier for import path typos (compile-time module
#: resolution failure — wrong case, missing extension, wrong relative
#: direction, etc.).
VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO: str = "import_path_typo"

#: Ordered tuple of all three self-fix class identifiers.  Order is
#: row-spec verbatim so a debug summary lists them in the same order
#: the row's checkbox reads.  Frozen tuple so callers may rely on
#: index-based iteration without defensive copies.
VITE_SELF_FIX_CLASSES: tuple[str, ...] = (
    VITE_SELF_FIX_CLASS_SYNTAX_ERROR,
    VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL,
    VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO,
)

#: Token used by :func:`summarise_self_fix_classes` for entries that
#: did not match any of the three known classes (typically a runtime
#: error from a vendor chunk or a Vite plugin error the agent cannot
#: fix without operator context).  Stable so the dashboard tile keys
#: on the same string across rows.
VITE_SELF_FIX_UNCLASSIFIED_TOKEN: str = "unclassified"


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    """Pre-compile a tuple of regex patterns.  Used at module-import
    time so the classifier hot-path is a tight ``any(p.search(msg)
    for p in patterns)`` without a per-call compile.
    """

    return tuple(re.compile(pat, re.IGNORECASE) for pat in patterns)


#: Patterns that classify an entry as :data:`VITE_SELF_FIX_CLASS_SYNTAX_ERROR`.
#:
#: Source: Rollup parser (``"Failed to parse module"`` / ``"Unexpected
#: token"`` / ``"Expected"``), esbuild (``"Expected"`` / ``"Syntax
#: error"``), Vite's own SyntaxError surfacing.  Each entry must be
#: unique enough that a non-syntax error message does not match by
#: accident — the W15.6 contract tests pin both positive and negative
#: cases.
VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = _compile(
    r"failed to parse module",
    r"\bunexpected token\b",
    r"\bunexpected character\b",
    r"\bunexpected end of (?:input|file)\b",
    r"\bexpected\b.*\bbut (?:found|got)\b",
    r"\bsyntax\s*error\b",
    r"\bparse\s*error\b",
    r"\bparseerror\b",
)

#: Patterns that classify an entry as
#: :data:`VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL`.
#:
#: Source: V8 / SpiderMonkey ``ReferenceError: <name> is not defined``,
#: esbuild ``"<name>" is not defined``, TypeScript ``Cannot find name``,
#: ESLint ``no-undef``.  ``\bis not defined\b`` covers the runtime
#: shape; ``cannot find name`` covers the TS shape; ``no-undef`` covers
#: the lint shape.
VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS: tuple[re.Pattern[str], ...] = _compile(
    r"\bis not defined\b",
    r"\bcannot find name\b",
    r"\bno-undef\b",
    r"\breferenceerror\b",
)

#: Patterns that classify an entry as
#: :data:`VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO`.
#:
#: Source: Vite's own ``Failed to resolve import`` (most common —
#: matches every wrong-case / missing-extension / wrong-direction
#: import in the W14.1 sandboxed projects), Node's ``Cannot find
#: module``, esbuild's ``Could not resolve``.  ``\bmodule not found\b``
#: covers the older webpack-style surfacing some plugins still use.
VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS: tuple[re.Pattern[str], ...] = _compile(
    r"failed to resolve import",
    r"cannot find module",
    r"could not resolve",
    r"\bmodule not found\b",
    r"\bunresolved import\b",
)


@dataclass(frozen=True)
class ViteSelfFixClassification:
    """Frozen value object describing one classified W15.2 history
    entry.

    Fields:

      * ``entry``       — the original W15.2-formatted history entry
                          (verbatim — no normalisation) so the test
                          suite can assert exactly which line produced
                          the classification.
      * ``vite_class``  — the matched class identifier (one of
                          :data:`VITE_SELF_FIX_CLASSES`) or ``None``
                          when no class matched.  ``None`` rather
                          than the unclassified token because callers
                          typically want a typed sentinel; the
                          unclassified token only surfaces in
                          :func:`summarise_self_fix_classes` where
                          a string key is required for the
                          :class:`Counter`.
    """

    entry: str
    vite_class: str | None


def is_vite_history_entry(entry: object) -> bool:
    """Return ``True`` when ``entry`` is a string starting with the
    W15.2 history-entry prefix
    (:data:`backend.web.vite_error_relay.VITE_ERROR_HISTORY_KEY_PREFIX`).

    Used as the gate inside :func:`classify_vite_error_for_self_fix`
    and :func:`classify_vite_history_for_self_fix` so a non-Vite
    history entry (tool-error key from the existing self-healing
    loop) is skipped without raising.  Re-exported so test fixtures
    can assert on the same gate the classifier uses.
    """

    return isinstance(entry, str) and entry.startswith(
        VITE_ERROR_HISTORY_KEY_PREFIX
    )


def _extract_message_body(entry: str) -> str | None:
    """Lift the message body out of a W15.2-formatted entry.

    The W15.2 format is::

        vite[<phase>] <file>:<line>: <kind>: <message>

    so the message body sits after the third ``":"`` in the part that
    follows the ``"] "`` separator.  Returns ``None`` for degraded
    entries (entry that matches the prefix but does not have the
    expected colon shape — e.g. a pathological filename that
    exhausted the byte cap before the body) so the caller can skip
    them without misclassifying.

    The split limit of ``4`` mirrors
    :func:`backend.web.vite_error_prompt.extract_last_vite_error_from_history`
    so message-body colons (``"ParseError: Unexpected token"``)
    survive intact.
    """

    if not is_vite_history_entry(entry):
        return None
    try:
        after_phase = entry.split("] ", 1)[1]
    except IndexError:
        return None
    parts = after_phase.split(":", 4)
    if len(parts) < 4:
        return None
    body = ":".join(parts[3:]).strip()
    return body or None


def classify_vite_error_for_self_fix(entry: str) -> str | None:
    """Classify a single W15.2-formatted history entry.

    Returns one of :data:`VITE_SELF_FIX_CLASSES` when the message
    body matches a known class pattern, ``None`` otherwise (which the
    operator-escalation path treats as "we cannot auto-fix this — page
    a human").

    Classification order mirrors :data:`VITE_SELF_FIX_CLASSES` so a
    message that matches multiple classes is bucketed under the
    earliest matching class.  In practice this only happens for
    pathological constructed messages — the canonical Vite / Rollup /
    esbuild messages each match exactly one class (the W15.6 contract
    tests pin both directions to defend against pattern drift).

    Non-Vite entries (entries that do not start with
    :data:`backend.web.vite_error_relay.VITE_ERROR_HISTORY_KEY_PREFIX`)
    return ``None`` without raising so the caller may pass arbitrary
    history entries.
    """

    body = _extract_message_body(entry)
    if body is None:
        return None
    if any(pat.search(body) for pat in VITE_SELF_FIX_SYNTAX_ERROR_PATTERNS):
        return VITE_SELF_FIX_CLASS_SYNTAX_ERROR
    if any(pat.search(body) for pat in VITE_SELF_FIX_UNDEFINED_SYMBOL_PATTERNS):
        return VITE_SELF_FIX_CLASS_UNDEFINED_SYMBOL
    if any(pat.search(body) for pat in VITE_SELF_FIX_IMPORT_PATH_TYPO_PATTERNS):
        return VITE_SELF_FIX_CLASS_IMPORT_PATH_TYPO
    return None


def classify_vite_history_for_self_fix(
    error_history: Sequence[str],
) -> list[ViteSelfFixClassification]:
    """Walk ``error_history`` oldest-to-newest and classify every Vite
    entry.

    Non-Vite entries are skipped (rather than emitted with
    ``vite_class=None``) so the caller may pass the raw
    ``state.error_history`` without pre-filtering.  Returns a fresh
    :class:`list` of :class:`ViteSelfFixClassification` records — never
    mutates the input.

    Order is preserved (oldest first) so consumers may zip the result
    with the original Vite-only slice for index-aligned reporting.
    """

    out: list[ViteSelfFixClassification] = []
    for entry in error_history:
        if not is_vite_history_entry(entry):
            continue
        cls = classify_vite_error_for_self_fix(entry)
        out.append(ViteSelfFixClassification(entry=entry, vite_class=cls))
    return out


def summarise_self_fix_classes(
    error_history: Sequence[str],
) -> Counter[str]:
    """Count how many entries in ``error_history`` fall in each
    self-fix class.

    Returns a :class:`collections.Counter` keyed on the class
    identifiers in :data:`VITE_SELF_FIX_CLASSES` plus
    :data:`VITE_SELF_FIX_UNCLASSIFIED_TOKEN` for entries that matched
    no class.  Non-Vite entries are skipped (they do not pollute the
    ``unclassified`` bucket — the bucket is reserved for Vite errors
    whose message body is not in any of the three known classes).

    Suitable for a one-line debug log or a dashboard tile
    ("syntax_error: 4 / undefined_symbol: 1 / import_path_typo: 0 /
    unclassified: 0").
    """

    counter: Counter[str] = Counter()
    for record in classify_vite_history_for_self_fix(error_history):
        if record.vite_class is None:
            counter[VITE_SELF_FIX_UNCLASSIFIED_TOKEN] += 1
        else:
            counter[record.vite_class] += 1
    return counter
