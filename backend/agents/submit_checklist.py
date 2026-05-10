"""B6 submit-review checklist orchestrator (OP-835).

Couples the feature-list parser to the runner's submit gate:

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
    """Model returned a non-boolean ``passed`` field (e.g. "mostly")."""

    def __init__(self, item_id: str, raw_value: object):
        self.item_id = item_id
        self.raw_value = raw_value
        super().__init__(
            f"item {item_id!r} returned ambiguous passed={raw_value!r}; "
            "concrete true/false required"
        )


class ChecklistThreeCycleFail(Exception):
    """Model still has failing items after :data:`MAX_CYCLES` attempts."""

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
    integrity check.
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
    """One row of the model's per-item answer."""

    id: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class ChecklistDecision:
    """Outcome of :func:`evaluate_responses`."""

    all_passed: bool
    failures: tuple[ChecklistResponse, ...]
    responses: tuple[ChecklistResponse, ...]


@dataclass(frozen=True)
class ChecklistPayload:
    """User-message payload to inject before submit.

    ``items`` is the parsed feature list. ``mode`` is the detected AC
    format. ``message`` is the rendered string to feed the model.
    """

    mode: str
    items: tuple[FeatureItem, ...]
    message: str


# ── Public API ───────────────────────────────────────────────────────


def build_checklist(description: str) -> ChecklistPayload:
    """Detect AC format, parse, and render the injection message.

    Falls back from JSON-parse failure to legacy mode per B6 AC #1 + #6
    (`format_unparseable` → legacy). Legacy with no bullets propagates
    :class:`LegacyUpConvertError` (``legacy_up_convert_fail``) so the
    caller can block submit and require operator review.
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
    """Legacy fallback used both by detect_and_parse and the JSON-failure path."""
    from backend.agents.feature_list_parser import up_convert_legacy
    return up_convert_legacy(description)


def render_checklist_message(items: Iterable[FeatureItem]) -> str:
    """Build the user-message payload injected before submit.

    Format keeps the response schema explicit so the model can be held
    to strict per-item ``passed: bool`` answers (B6 AC #3 + #5).
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

    Strict semantics — any non-bool ``passed`` raises
    :class:`ChecklistAmbiguousResponse` (B6 AC #5). Missing or extra ids
    raise :class:`ValueError`; the caller maps that to a retry with a
    schema reminder. The returned decision is consumed by
    :func:`run_checklist_cycle`.
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
    """Accept either a JSON string or a pre-parsed list/dict."""
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

    ``model_responder`` is invoked with (items, cycle_index_0based) and
    must return the model's raw checklist answer (parsed list or JSON
    string). Ambiguous answers raise immediately — they do not consume a
    cycle since the spec demands rejection back to the model.

    ``re_edit_hook`` is called between cycles when ``passed=false`` rows
    exist; the runner uses it to drop the coder back into working state.
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

    Uses ``git status --porcelain=v1 -- <path>`` so the result is stable
    across git versions. ``rel_path`` MUST be relative to ``repo_root``.

    Acceptable states:
        - clean (no output)
        - staged-only (``X = A|M|R|C`` and ``Y = " "``)

    Rejected:
        - untracked (``??``)
        - unstaged modifications (``Y != " "``) — the JSON was edited
          after staging and lost the index entry the commit will use.
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
