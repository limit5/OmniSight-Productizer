"""B6 submit-review checklist orchestrator (OP-835).

Couples the feature-list parser (:mod:`backend.agents.feature_list_parser`)
to the runner's submit gate. The runner injects a checklist message before
the model commits, evaluates the per-item answers, and either unlocks
submit or sends the coder back into working state for up to three cycles.

The four public-API steps, in the order the runner calls them:

1. :func:`build_checklist` — detect AC format, parse / up-convert, render
   the user-message payload to inject before the model commits.
2. :func:`evaluate_responses` — validate per-item model responses, reject
   ambiguous answers, and decide whether to unlock submit or return to
   working-state.
3. :func:`run_checklist_cycle` — drive the up-to-3-cycle re-edit loop and
   raise :class:`ChecklistThreeCycleFail` when the model still fails on
   cycle 3.
4. :func:`assert_feature_list_staged` — F20-analogue post-write integrity
   check; raises :class:`FeatureListNotStaged` if the runner committed
   without staging the JSON artifact.

The error catalog (:class:`ChecklistAmbiguousResponse`,
:class:`ChecklistThreeCycleFail`, :class:`FeatureListNotStaged`) and the
result dataclasses (:class:`ChecklistResponse`, :class:`ChecklistDecision`,
:class:`ChecklistPayload`) are part of the public surface — callers in
the runner FSM pattern-match on them.

This module is side-effect-free apart from :func:`assert_feature_list_staged`,
which shells out to ``git status``; everything else operates on in-memory
strings and dataclasses, which keeps the unit tests free of fixtures.

See ``docs/architecture/sdk-runner-sprint-b-error-handling.md`` §B6 for
the FSM and idempotency notes.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from backend.agents.feature_list_parser import (
    ACDetection,
    FeatureItem,
    FeatureListParseError,
    LegacyUpConvertError,
    detect_and_parse,
)


MAX_CYCLES = 3


# ── Errors mapped to the B6 error catalog ─────────────────────────────


class ChecklistAmbiguousResponse(Exception):
    """Model returned a non-boolean ``passed`` field (e.g. "mostly").

    Raised by :func:`evaluate_responses` whenever a row's ``passed`` is not
    a strict JSON ``bool`` — strings like ``"true"`` / ``"mostly"``,
    integers like ``1``, and ``None`` all qualify. Per B6 AC #5 the runner
    must surface this back to the model as a schema reminder rather than
    consume a re-edit cycle.

    Also raised for non-string ``evidence``; both forms keep the schema
    contract symmetric.

    Attributes:
        item_id: The ``FL<n>`` id of the offending row.
        raw_value: The exact value the model returned for ``passed`` (or
            ``evidence``), preserved so the caller can echo it back into
            the schema-reminder prompt.
    """

    def __init__(self, item_id: str, raw_value: object):
        self.item_id = item_id
        self.raw_value = raw_value
        super().__init__(
            f"item {item_id!r} returned ambiguous passed={raw_value!r}; "
            "concrete true/false required"
        )


class ChecklistThreeCycleFail(Exception):
    """Model still has failing items after :data:`MAX_CYCLES` attempts.

    Terminal for the B6 cycle — the runner escalates to operator review
    rather than burning more cycles. The unresolved failures are kept on
    the exception so the operator-facing JIRA comment can quote concrete
    item ids and evidence strings without re-running the model.

    Attributes:
        ticket_key: JIRA key of the ticket that exhausted its cycles
            (e.g. ``"OP-1213"``).
        last_failures: Frozen tuple of the :class:`ChecklistResponse`
            rows that were still ``passed=False`` on the third cycle.
    """

    def __init__(self, ticket_key: str, last_failures: Sequence["ChecklistResponse"]):
        self.ticket_key = ticket_key
        self.last_failures = tuple(last_failures)
        ids = [r.id for r in last_failures]
        super().__init__(
            f"{ticket_key}: 3 cycles of re-edit + checklist still failing; "
            f"unresolved items: {ids}"
        )


class FeatureListNotStaged(Exception):
    """F20-analogue: feature-list JSON written but never ``git add``-ed.

    Raised by :func:`assert_feature_list_staged` when the file at the given
    path is either untracked or modified-unstaged at the time of the
    integrity check. Catching this exception is the runner's last chance
    to abort before the bad commit lands.

    Attributes:
        path: Repository-relative :class:`~pathlib.Path` of the feature-list
            artifact that failed the staging check.
        status_line: First line of ``git status --porcelain=v1`` output for
            ``path``, or a synthetic ``"git exit <rc>: <stderr>"`` string
            when git itself failed. Inspectable in tests to assert which
            failure mode tripped (``"??"`` vs. ``"AM"`` etc.).
    """

    def __init__(self, path: Path, status_line: str):
        self.path = path
        self.status_line = status_line
        super().__init__(
            f"feature-list artifact at {path} is not staged "
            f"(porcelain status: {status_line!r})"
        )


# ── Data classes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChecklistResponse:
    """One row of the model's per-item answer, after schema validation.

    Constructed inside :func:`evaluate_responses` once each row has been
    confirmed to use a strict JSON ``bool`` for ``passed`` and a ``str``
    for ``evidence`` — downstream consumers can treat the fields as
    already-typed.

    Attributes:
        id: The checklist item id (``"FL<n>"``).
        passed: Strict boolean — ``True`` only if the row qualifies for
            the submit gate.
        evidence: Free-text proof from the model (test name, file:line,
            screenshot ref, etc.). Empty string is allowed.
    """

    id: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class ChecklistDecision:
    """Outcome of :func:`evaluate_responses`.

    Attributes:
        all_passed: ``True`` iff every row in ``responses`` has
            ``passed=True``. Equivalent to ``not failures``.
        failures: Frozen tuple of the failing rows, preserving the
            original checklist ordering. Empty when ``all_passed``.
        responses: Frozen tuple of every row, in the order the items were
            declared in the checklist (not the order the model returned
            them).
    """

    all_passed: bool
    failures: tuple[ChecklistResponse, ...]
    responses: tuple[ChecklistResponse, ...]


@dataclass(frozen=True)
class ChecklistPayload:
    """User-message payload to inject before submit.

    Returned by :func:`build_checklist`. The runner sends ``message`` to
    the model and uses ``items`` to evaluate the response against the
    same checklist that was rendered.

    Attributes:
        mode: Detected AC format — ``"feature_list_json"`` when the
            ticket carries a parseable ``## Feature list (JSON)`` block,
            ``"legacy_freeform"`` when up-converted from
            ``## Acceptance criteria`` bullets.
        items: Parsed feature-list rows, normalised to
            :class:`~backend.agents.feature_list_parser.FeatureItem`.
        message: Pre-rendered user-message string, ready to feed the
            model. Includes the response-schema reminder so the model is
            held to strict per-item booleans (B6 AC #3 + #5).
    """

    mode: str
    items: tuple[FeatureItem, ...]
    message: str


# ── Public API ───────────────────────────────────────────────────────


def build_checklist(description: str) -> ChecklistPayload:
    """Detect AC format, parse, and render the injection message.

    Two-format fallback per B6 AC #1 + #6:

    * If the ``## Feature list (JSON)`` block is present and parseable,
      use it directly (``mode="feature_list_json"``).
    * If it is present but malformed, treat the JSON parse failure as
      ``format_unparseable`` and silently fall back to up-converting the
      ``## Acceptance criteria`` bullets (``mode="legacy_freeform"``).
    * If neither path yields items, surface
      :class:`LegacyUpConvertError` so the caller can block submit and
      require operator review.

    Args:
        description: Raw JIRA description for the ticket under review.
            Whitespace-stripping and section parsing are handled by
            :mod:`backend.agents.feature_list_parser`.

    Returns:
        A :class:`ChecklistPayload` carrying the detected mode, parsed
        items, and the rendered user-message string ready for injection.

    Raises:
        LegacyUpConvertError: ``legacy_up_convert_fail`` — neither a
            parseable JSON block nor any AC bullets were found.
    """
    try:
        detection = detect_and_parse(description)
    except FeatureListParseError:
        # B6 AC #1: malformed JSON → fall back to legacy mode.
        items = _up_convert_or_raise(description)
        detection = ACDetection(mode="legacy_freeform", items=items)
    return ChecklistPayload(
        mode=detection.mode,
        items=detection.items,
        message=render_checklist_message(detection.items),
    )


def _up_convert_or_raise(description: str) -> tuple[FeatureItem, ...]:
    """Legacy-mode fallback shared by :func:`build_checklist` and the parser.

    Thin wrapper around
    :func:`backend.agents.feature_list_parser.up_convert_legacy`. Kept as
    a function (rather than a bare import) so the JSON-failure path in
    :func:`build_checklist` and the parser's own dispatch route through
    one place — easier to monkeypatch in tests, easier to grep.

    Raises:
        LegacyUpConvertError: No bullets were found under
            ``## Acceptance criteria``.
    """
    from backend.agents.feature_list_parser import up_convert_legacy
    return up_convert_legacy(description)


def render_checklist_message(items: Iterable[FeatureItem]) -> str:
    """Build the user-message payload injected before submit.

    Format keeps the response schema explicit so the model can be held
    to strict per-item ``passed: bool`` answers (B6 AC #3 + #5). The
    rendered text contains a literal ``strict JSON boolean`` clause that
    the test suite asserts on as a smoke check.

    Args:
        items: Parsed feature-list rows. Consumed once — iterators are
            fine; the function materialises them internally.

    Returns:
        A newline-joined string ready to send as a user message. Empty
        item lists still produce a well-formed message with no bullet
        rows (the schema preamble is unconditional).
    """
    items = list(items)
    lines = [
        "Submit-review checklist. Respond with a single JSON array; one",
        "object per item below. Each object MUST have the shape:",
        "",
        '    {"id": "FL<n>", "passed": true|false, "evidence": "<concrete proof>"}',
        "",
        "`passed` MUST be a strict JSON boolean. Strings like \"mostly\"",
        "or \"yes\" will be rejected and the submit gate kept closed.",
        "",
        "Checklist:",
    ]
    for it in items:
        lines.append(
            f"- {it.id}: {it.description}  (verify: {it.verify})"
        )
    return "\n".join(lines)


def evaluate_responses(
    items: Sequence[FeatureItem],
    raw_responses: object,
) -> ChecklistDecision:
    """Validate model output and bucket items into pass / fail.

    Strict semantics — the model is held to the schema documented in the
    injected message. Boolean checks reject ``"true"``, ``1``, and other
    truthy-but-not-bool values (B6 AC #5). The ordering of returned rows
    follows ``items`` rather than the model's response, so downstream
    consumers can rely on positional stability.

    Args:
        items: Checklist rows the model was asked to answer. Treated as
            the source of truth for expected ids.
        raw_responses: Either a JSON-encoded ``str`` or an already-parsed
            ``list[dict]``. Each dict must carry ``id``, ``passed``, and
            ``evidence``.

    Returns:
        A :class:`ChecklistDecision` with the per-item answers and a
        ``failures`` tuple ready for the re-edit hook.

    Raises:
        ChecklistAmbiguousResponse: A row's ``passed`` is not a strict
            ``bool``, or its ``evidence`` is not a ``str``. The caller
            should bounce the model with a schema reminder rather than
            consuming a re-edit cycle.
        ValueError: Response payload could not be coerced (bad JSON, not
            an array, missing ``id``) or the set of ids did not match
            ``items``. The caller maps this to a retry with a schema
            reminder.
    """
    parsed: list[dict] = _coerce_response_array(raw_responses)
    expected_ids = [it.id for it in items]
    got_ids = [r.get("id") for r in parsed]
    if sorted(got_ids) != sorted(expected_ids):
        raise ValueError(
            f"response ids {got_ids!r} do not match checklist ids {expected_ids!r}"
        )
    by_id: dict[str, ChecklistResponse] = {}
    for row in parsed:
        item_id = row["id"]
        passed = row.get("passed")
        # Reject anything that isn't a strict JSON bool (rules out "true",
        # 1, "mostly", None, etc.). The bool check is order-sensitive
        # because Python treats bool as a subclass of int.
        if not isinstance(passed, bool):
            raise ChecklistAmbiguousResponse(item_id, passed)
        evidence = row.get("evidence", "")
        if not isinstance(evidence, str):
            raise ChecklistAmbiguousResponse(item_id, evidence)
        by_id[item_id] = ChecklistResponse(
            id=item_id, passed=passed, evidence=evidence
        )
    responses = tuple(by_id[i] for i in expected_ids)
    failures = tuple(r for r in responses if not r.passed)
    return ChecklistDecision(
        all_passed=not failures,
        failures=failures,
        responses=responses,
    )


def _coerce_response_array(raw: object) -> list[dict]:
    """Normalise model output into a ``list[dict]`` shape.

    Accepts either a JSON-encoded string or an already-parsed Python
    list; both shapes are common because the model SDK sometimes returns
    pre-deserialised objects and sometimes raw text. Each entry must be
    a ``dict`` with an ``id`` key — deeper validation is left to
    :func:`evaluate_responses`.

    Raises:
        ValueError: ``raw`` could not be JSON-decoded, is not a list, an
            entry is not a dict, or an entry is missing ``id``.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"response must be a JSON array, got {type(raw).__name__}")
    for idx, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValueError(f"response[{idx}] is not a JSON object")
        if "id" not in row:
            raise ValueError(f"response[{idx}] is missing 'id'")
    return raw


def run_checklist_cycle(
    ticket_key: str,
    items: Sequence[FeatureItem],
    *,
    model_responder: Callable[[Sequence[FeatureItem], int], object],
    re_edit_hook: Callable[[Sequence[ChecklistResponse], int], None] | None = None,
) -> ChecklistDecision:
    """Drive up to :data:`MAX_CYCLES` checklist cycles per B6 AC #4.

    The loop is:

    1. Call ``model_responder(items, cycle)`` — the model produces a
       checklist answer.
    2. Validate via :func:`evaluate_responses`.
    3. If every row passed, return the decision immediately.
    4. If this was the last allowed cycle, raise
       :class:`ChecklistThreeCycleFail`.
    5. Otherwise call ``re_edit_hook(failures, cycle)`` (if provided) to
       drop the coder back into working state, then loop.

    Ambiguous answers (non-bool ``passed``) bubble out unchanged — they
    do *not* count against the cycle budget, because the spec demands
    the model re-issue a properly-typed response first.

    Args:
        ticket_key: JIRA key used only for error messages on terminal
            failure.
        items: The validated checklist rows to answer.
        model_responder: Callable invoked once per cycle with
            ``(items, cycle_index_0based)``. Must return raw checklist
            output — either a parsed ``list[dict]`` or a JSON-encoded
            ``str``.
        re_edit_hook: Optional callback fired between cycles when at
            least one row failed. Receives the failing
            :class:`ChecklistResponse` rows and the just-finished cycle
            index. Not called after the final cycle, since the failure
            is terminal at that point.

    Returns:
        The :class:`ChecklistDecision` from the first all-passed cycle.

    Raises:
        ChecklistThreeCycleFail: All ``MAX_CYCLES`` cycles ran and at
            least one row still failed.
        ChecklistAmbiguousResponse: Propagated unchanged from
            :func:`evaluate_responses` — the model returned a malformed
            payload and must retry with corrected types.
        ValueError: Propagated when the model returned the wrong id set
            or a non-JSON payload.
    """
    for cycle in range(MAX_CYCLES):
        raw = model_responder(items, cycle)
        decision = evaluate_responses(items, raw)
        if decision.all_passed:
            return decision
        if cycle == MAX_CYCLES - 1:
            raise ChecklistThreeCycleFail(ticket_key, decision.failures)
        if re_edit_hook is not None:
            re_edit_hook(decision.failures, cycle)
    # Unreachable — MAX_CYCLES >= 1 and the loop either returns or raises.
    raise AssertionError("run_checklist_cycle exited without decision")


# ── F20-analogue staging guard ──────────────────────────────────────


def assert_feature_list_staged(repo_root: Path, rel_path: Path | str) -> None:
    """Raise :class:`FeatureListNotStaged` if ``rel_path`` is not in the index.

    Implements the F20-analogue post-write integrity check from B6: the
    runner writes the feature-list JSON, ``git add``-s it, and calls
    this just before the commit to confirm the index entry is intact.
    Uses ``git status --porcelain=v1 -- <path>`` so the parsing is
    stable across git versions.

    Acceptable states (no exception raised):
        - clean — no porcelain output, meaning the file is tracked and
          matches HEAD. The commit will pick up the right blob.
        - staged-only — ``X in {A, M, R, C, D}`` and ``Y == " "``.

    Rejected states:
        - untracked (``??``) — the file exists in the worktree but git
          has no record of it.
        - unstaged modifications (``Y != " "``) — the JSON was edited
          after staging and lost the index entry the commit will use.
        - git itself exited non-zero (wrapped in the porcelain status).

    Args:
        repo_root: Absolute path to the git working tree to query.
        rel_path: Path of the feature-list artifact, relative to
            ``repo_root``. Accepts either a :class:`~pathlib.Path` or a
            plain ``str``.

    Raises:
        FeatureListNotStaged: The path is untracked, unstaged-modified,
            or the ``git status`` invocation failed. The exception's
            ``status_line`` attribute carries the raw porcelain line so
            tests can assert on the specific failure mode.
    """
    rel = Path(rel_path)
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--", str(rel)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise FeatureListNotStaged(rel, f"git exit {result.returncode}: {result.stderr.strip()}")
    out = result.stdout.rstrip("\n")
    if not out:
        # File is tracked AND has no pending diff — it's been committed
        # already (or is identical to HEAD). Either way the commit will
        # carry the right blob, so the staging contract is satisfied.
        return
    # Porcelain v1: two status chars + space + path. Multiple files possible
    # when rel is a directory, but we only ever check single-file paths here.
    line = out.splitlines()[0]
    index_char = line[0] if len(line) >= 1 else " "
    worktree_char = line[1] if len(line) >= 2 else " "
    if index_char == "?" or worktree_char != " ":
        raise FeatureListNotStaged(rel, line)
    # index_char in {A, M, R, C, D} with clean worktree → staged.
