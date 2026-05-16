"""OP-1141 — strict-match AC evidence grader.

The OP-860 outcomes grader (``backend.agents.outcomes_consumer.grade_outcomes``)
asks an LLM to compare the AC text against the diff and return
``pass`` / ``partial`` / ``fail``. That works for *semantic* drift but
does not catch a class of failures we have observed in production:

* CLI claims AC ✓ but cites a ``file:line`` range that does not exist
  in the diff (hallucinated cite).
* CLI cites a Change-Id that does not match the actual ``HEAD`` commit.
* CLI marks AC ✓ with no concrete cite at all (vague evidence like
  "looks right", "implemented", "done").

This module is **pure logic** — no LLM call. It parses the agent's
``AC verification for <KEY>:`` comment, classifies each ✓ line's
evidence, and verifies the cite against the actual commit diff and
HEAD Change-Id. A single hallucinated or missing cite fails the whole
ticket — that is the strict-match contract from OP-1141 §Code AC.

Public entry points:

* :func:`extract_text_from_adf` — flatten a JIRA ADF comment to plain text.
* :func:`find_ac_verification_comment` — pick the most-recent matching
  comment from a JIRA ``/issue/<key>/comment`` payload.
* :func:`parse_ac_verification` — tokenise the comment body into
  :class:`ACEvidence` rows.
* :func:`parse_diff_touched_ranges` — extract per-file touched-line
  ranges from a unified diff.
* :func:`grade_ac_evidence` — top-level grading function returning
  :class:`StrictGradeResult`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# ── Evidence pattern catalog ──────────────────────────────────────

# Match Change-Id tokens. Gerrit's Change-Id is ``I`` + 40 hex chars,
# but we accept ≥8 hex so a short-form cite (``Idabcd1234``) still
# round-trips.
_CHANGE_ID_RE = re.compile(r"\bI[0-9a-fA-F]{8,40}\b")

# Match ``path/to/file.ext:L<n>[-L?<m>]`` or ``path:nnn-nnn``. The
# extension class is intentionally broad so we catch ``.py``, ``.ts``,
# ``.tsx``, ``.md``, ``.yaml``, ``.go``, ``.rs`` etc. The path must
# contain at least one ``/`` or ``.`` so we don't false-positive on
# bare identifiers like ``foo:42``.
_FILE_LINE_RE = re.compile(
    r"(?P<path>[\w./\-+]+\.[A-Za-z0-9]+):"
    r"L?(?P<start>\d+)"
    r"(?:[-–]L?(?P<end>\d+))?"
)

# Match ``test_*`` function names (pytest convention). Captures the
# full identifier so we can grep for ``def test_X`` in the diff.
_TEST_NAME_RE = re.compile(r"\btest_[A-Za-z0-9_]+\b")

# ✓ / ✗ line prefix tolerant of leading whitespace, optional bullet
# markers (``-`` / ``*``), and surrounding spaces around the mark.
_AC_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<mark>[✓✗])\s+(?P<rest>.+?)\s*$"
)

# Header line ``AC verification for <KEY>:`` — KEY is captured so we
# can verify the comment is about this ticket, not another one the
# agent referenced inline.
_AC_HEADER_RE = re.compile(
    r"^\s*AC verification for\s+(?P<key>[A-Z][A-Z0-9_]+-\d+)\s*:?\s*$",
    re.IGNORECASE,
)

# Unified-diff file header (``+++ b/<path>``). We always read the
# new-file side because that is what was committed.
_DIFF_FILE_HEADER_RE = re.compile(r"^\+\+\+\s+b/(?P<path>.+?)\s*$")

# Hunk header ``@@ -a,b +c,d @@``. ``c`` is the first new-file line,
# ``d`` is the new-file line count (defaults to 1 when omitted).
_DIFF_HUNK_RE = re.compile(
    r"^@@\s+-\d+(?:,\d+)?\s+\+(?P<start>\d+)(?:,(?P<count>\d+))?\s+@@"
)

# ``--- /dev/null`` marker preceding ``+++ b/<path>`` indicates a
# newly-added file. We treat newly-added files as fully-touched so
# a cite to *any* line in them is honoured.
_DEV_NULL_RE = re.compile(r"^---\s+/dev/null\s*$")

# A new test function in the diff: ``+def test_<name>``.
_NEW_TEST_DEF_RE = re.compile(r"^\+\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(")


# ── Data classes ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ACEvidence:
    """One row from the agent's ``AC verification`` comment."""

    mark: str                     # ✓ or ✗
    description: str              # the paraphrased AC text (left of em-dash)
    raw_evidence: str             # raw text after the em-dash separator
    kind: str                     # "file_line" | "test_name" | "change_id" | "vague" | "none"
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    test_name: str | None = None
    change_id: str | None = None


@dataclass(frozen=True)
class EvidenceVerdict:
    """Result of verifying one :class:`ACEvidence` against the diff."""

    evidence: ACEvidence
    ok: bool
    reason: str


@dataclass(frozen=True)
class StrictGradeResult:
    """Final strict-match verdict for the ``AC verification`` comment."""

    verdict: str                  # "pass" or "fail"
    comment_found: bool
    per_evidence: tuple[EvidenceVerdict, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


@dataclass(frozen=True)
class DiffIndex:
    """Parsed view of a unified diff used for cite verification."""

    # path → sorted list of (start, end) inclusive touched-line ranges
    touched_ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # paths that appeared with ``--- /dev/null`` (newly-added; any line cite OK)
    newly_added: set[str] = field(default_factory=set)
    # set of ``def test_*`` names that the diff introduces (lines starting with ``+``)
    new_test_names: set[str] = field(default_factory=set)

    def file_touched(self, path: str) -> bool:
        return path in self.touched_ranges or path in self.newly_added

    def line_range_touched(self, path: str, start: int, end: int | None) -> bool:
        """Return True if any line in [start, end] is touched in ``path``."""
        if path in self.newly_added:
            return True
        ranges = self.touched_ranges.get(path)
        if not ranges:
            return False
        lo = start
        hi = end if end is not None else start
        if hi < lo:
            lo, hi = hi, lo
        for r_start, r_end in ranges:
            if r_end < lo:
                continue
            if r_start > hi:
                break
            return True
        return False


# ── ADF flattening ────────────────────────────────────────────────


def extract_text_from_adf(node: object) -> str:
    """Flatten a JIRA ADF document into plain text.

    Mirrors ``backend.agents.file_coordinator._adf_to_text`` but lives
    here so the grader can be imported without dragging the file-mutex
    dependency tree.
    """
    chunks: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            node_type = value.get("type")
            if node_type == "text":
                chunks.append(str(value.get("text", "")))
            elif node_type == "hardBreak":
                chunks.append("\n")
            else:
                for child in value.get("content", []) or []:
                    walk(child)
                if node_type in {"paragraph", "codeBlock", "listItem"}:
                    chunks.append("\n")
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return "".join(chunks)


# ── Comment selection ────────────────────────────────────────────


def find_ac_verification_comment(
    comments: Iterable[dict], ticket_key: str
) -> str | None:
    """Return the body of the most-recent ``AC verification for <KEY>`` comment.

    ``comments`` is the raw ``comments`` array from a JIRA
    ``/issue/<key>/comment`` response. Iteration order follows JIRA's
    default (oldest → newest), so we keep the last hit. Returns
    ``None`` when no such comment exists.
    """
    found: str | None = None
    for comment in comments:
        body = comment.get("body") if isinstance(comment, dict) else None
        if body is None:
            continue
        text = extract_text_from_adf(body) if isinstance(body, (dict, list)) else str(body)
        if _comment_is_ac_verification(text, ticket_key):
            found = text
    return found


def _comment_is_ac_verification(text: str, ticket_key: str) -> bool:
    """True iff ``text`` contains a header line for ``ticket_key``."""
    for line in text.splitlines():
        match = _AC_HEADER_RE.match(line)
        if match and match.group("key").upper() == ticket_key.upper():
            return True
    return False


# ── Comment parsing ──────────────────────────────────────────────


def parse_ac_verification(comment_body: str, ticket_key: str) -> list[ACEvidence]:
    """Parse ``AC verification`` lines out of a comment body.

    Only ✓/✗ lines that appear *after* the header for ``ticket_key``
    are included. We stop on a blank line followed by non-mark text so
    a trailing operator note ("Operator: please review...") does not
    leak into the evidence list — but blank lines inside the marks
    block are tolerated (the agent sometimes adds spacing).
    """
    lines = comment_body.splitlines()
    in_block = False
    evidence: list[ACEvidence] = []
    saw_blank = False
    for raw in lines:
        header = _AC_HEADER_RE.match(raw)
        if header:
            if header.group("key").upper() == ticket_key.upper():
                in_block = True
                saw_blank = False
                continue
            in_block = False
            continue
        if not in_block:
            continue
        if not raw.strip():
            saw_blank = True
            continue
        mark = _AC_LINE_RE.match(raw)
        if mark is None:
            # Tolerate continuation lines that are not blank but also
            # not ✓/✗ marks while we are still in the block. If we
            # already saw at least one mark and now we hit a free-form
            # line, treat that as the end of the block (most likely
            # the operator-facing trailing prose).
            if evidence and saw_blank:
                break
            continue
        saw_blank = False
        evidence.append(_classify_evidence(mark.group("mark"), mark.group("rest")))
    return evidence


def _classify_evidence(mark: str, rest: str) -> ACEvidence:
    """Split a mark-line body into ``description — raw_evidence`` and classify."""
    description, raw_evidence = _split_on_dash(rest)
    kind = "none"
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    test_name: str | None = None
    change_id: str | None = None

    if raw_evidence.strip():
        # Priority: file:line > Change-Id > test_ > vague. file:line is
        # the highest-precision cite, and Change-Id can fall out of
        # the body of a file:line description (commit refs).
        file_match = _FILE_LINE_RE.search(raw_evidence)
        change_match = _CHANGE_ID_RE.search(raw_evidence)
        test_match = _TEST_NAME_RE.search(raw_evidence)
        if file_match:
            kind = "file_line"
            file_path = file_match.group("path")
            line_start = int(file_match.group("start"))
            end_str = file_match.group("end")
            line_end = int(end_str) if end_str is not None else line_start
        elif change_match:
            kind = "change_id"
            change_id = change_match.group(0)
        elif test_match:
            kind = "test_name"
            test_name = test_match.group(0)
        else:
            kind = "vague"
    return ACEvidence(
        mark=mark,
        description=description.strip(),
        raw_evidence=raw_evidence.strip(),
        kind=kind,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        test_name=test_name,
        change_id=change_id,
    )


def _split_on_dash(text: str) -> tuple[str, str]:
    """Split ``description — evidence`` on em-dash or `` -- `` / `` - ``.

    Returns ``(text, "")`` when no separator is found — the caller
    classifies that as the ``none`` evidence kind.
    """
    for sep in ("—", "–", " -- ", " - "):
        idx = text.find(sep)
        if idx >= 0:
            return text[:idx], text[idx + len(sep):]
    return text, ""


# ── Diff parsing ─────────────────────────────────────────────────


def parse_diff_touched_ranges(diff_text: str) -> DiffIndex:
    """Parse a unified diff into a :class:`DiffIndex`.

    Resilient to noise: lines that do not match the header / hunk
    grammar are ignored, so the input can be an unfiltered
    ``git diff`` blob.
    """
    touched: dict[str, list[tuple[int, int]]] = {}
    newly_added: set[str] = set()
    new_tests: set[str] = set()
    current_path: str | None = None
    prev_was_dev_null = False

    for line in diff_text.splitlines():
        if _DEV_NULL_RE.match(line):
            prev_was_dev_null = True
            continue
        header = _DIFF_FILE_HEADER_RE.match(line)
        if header:
            path = header.group("path")
            if path == "/dev/null":
                current_path = None
            else:
                current_path = path
                touched.setdefault(path, [])
                if prev_was_dev_null:
                    newly_added.add(path)
            prev_was_dev_null = False
            continue
        prev_was_dev_null = False
        if current_path is None:
            continue
        hunk = _DIFF_HUNK_RE.match(line)
        if hunk:
            start = int(hunk.group("start"))
            count_str = hunk.group("count")
            count = int(count_str) if count_str is not None else 1
            if count <= 0:
                # Pure deletion hunk on the new side — still position
                # the range at ``start`` so callers see a touched line.
                end = start
            else:
                end = start + count - 1
            touched.setdefault(current_path, []).append((start, end))
            continue
        new_test = _NEW_TEST_DEF_RE.match(line)
        if new_test:
            new_tests.add(new_test.group(1))

    # Normalise: sort + merge overlapping/adjacent ranges per file.
    normalised: dict[str, list[tuple[int, int]]] = {}
    for path, ranges in touched.items():
        if not ranges:
            normalised[path] = []
            continue
        ranges_sorted = sorted(ranges)
        merged: list[tuple[int, int]] = [ranges_sorted[0]]
        for start, end in ranges_sorted[1:]:
            last_start, last_end = merged[-1]
            if start <= last_end + 1:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        normalised[path] = merged

    return DiffIndex(
        touched_ranges=normalised,
        newly_added=newly_added,
        new_test_names=new_tests,
    )


# ── Verification ─────────────────────────────────────────────────


def verify_evidence(
    evidence: ACEvidence,
    diff_index: DiffIndex,
    head_change_id: str | None,
) -> EvidenceVerdict:
    """Verify one piece of evidence against the diff + HEAD Change-Id."""
    # ✗ lines are not evidence claims — the agent already conceded the
    # AC could not be verified, so they cannot hallucinate a cite. We
    # do NOT fail the ticket on ✗ lines (an explicit skip is honest);
    # they are passed through with a neutral reason.
    if evidence.mark == "✗":
        return EvidenceVerdict(evidence, True, "skipped by agent (✗)")

    if evidence.kind == "none":
        return EvidenceVerdict(
            evidence, False,
            "no evidence cited after ✓ mark — strict mode requires file:line, "
            "test name, or Change-Id",
        )

    if evidence.kind == "vague":
        return EvidenceVerdict(
            evidence, False,
            f"vague evidence {evidence.raw_evidence!r} — no recognized cite "
            "(file:line, test name, or Change-Id) present",
        )

    if evidence.kind == "change_id":
        if not head_change_id:
            return EvidenceVerdict(
                evidence, False,
                f"cited Change-Id {evidence.change_id} but HEAD has no Change-Id footer",
            )
        if evidence.change_id != head_change_id and not (
            evidence.change_id and head_change_id.startswith(evidence.change_id)
        ):
            return EvidenceVerdict(
                evidence, False,
                f"cited Change-Id {evidence.change_id} does not match "
                f"HEAD Change-Id {head_change_id}",
            )
        return EvidenceVerdict(evidence, True, "Change-Id matches HEAD")

    if evidence.kind == "test_name":
        if evidence.test_name in diff_index.new_test_names:
            return EvidenceVerdict(evidence, True, "test function added in diff")
        return EvidenceVerdict(
            evidence, False,
            f"cited test {evidence.test_name!r} not added by this diff "
            f"(no ``+def {evidence.test_name}(`` line in any hunk)",
        )

    if evidence.kind == "file_line":
        path = evidence.file_path or ""
        if not diff_index.file_touched(path):
            return EvidenceVerdict(
                evidence, False,
                f"cited file {path!r} is not touched by this diff",
            )
        if not diff_index.line_range_touched(
            path, evidence.line_start or 0, evidence.line_end,
        ):
            range_str = (
                f"L{evidence.line_start}"
                if evidence.line_end in (None, evidence.line_start)
                else f"L{evidence.line_start}-L{evidence.line_end}"
            )
            return EvidenceVerdict(
                evidence, False,
                f"cited {path}:{range_str} is in the diff's file list but the "
                f"line range does not overlap any touched hunk",
            )
        return EvidenceVerdict(evidence, True, "file:line range overlaps touched hunk")

    return EvidenceVerdict(evidence, False, f"unknown evidence kind: {evidence.kind!r}")


def grade_ac_evidence(
    *,
    comment_body: str | None,
    diff_text: str,
    head_change_id: str | None,
    ticket_key: str,
) -> StrictGradeResult:
    """Top-level strict-match grade.

    ``comment_body`` is the agent's ``AC verification`` JIRA comment as
    plain text (use :func:`find_ac_verification_comment` to pluck it
    out of a JIRA comments payload first). When ``None`` or the
    comment lacks the canonical header, the verdict is ``fail`` — the
    agent has not produced the evidence the runner contracts on.
    """
    if not comment_body or not _comment_is_ac_verification(comment_body, ticket_key):
        return StrictGradeResult(
            verdict="fail",
            comment_found=False,
            per_evidence=(),
            reasons=(
                f"no ``AC verification for {ticket_key}`` comment found on "
                "ticket — strict mode requires the agent to post evidence "
                "before the runner pushes",
            ),
        )

    evidence_rows = parse_ac_verification(comment_body, ticket_key)
    if not evidence_rows:
        return StrictGradeResult(
            verdict="fail",
            comment_found=True,
            per_evidence=(),
            reasons=(
                f"``AC verification for {ticket_key}`` comment is present but "
                "contains zero ✓/✗ rows",
            ),
        )

    diff_index = parse_diff_touched_ranges(diff_text)
    per_evidence: list[EvidenceVerdict] = [
        verify_evidence(ev, diff_index, head_change_id) for ev in evidence_rows
    ]

    failures = [v for v in per_evidence if not v.ok]
    if failures:
        reasons = tuple(
            f"{v.evidence.mark} {v.evidence.description[:80]!r}: {v.reason}"
            for v in failures
        )
        return StrictGradeResult(
            verdict="fail",
            comment_found=True,
            per_evidence=tuple(per_evidence),
            reasons=reasons,
        )

    return StrictGradeResult(
        verdict="pass",
        comment_found=True,
        per_evidence=tuple(per_evidence),
        reasons=(),
    )


def format_failure_comment(
    ticket_key: str, result: StrictGradeResult
) -> str:
    """Render a diagnostic JIRA comment for a strict-grade FAIL.

    Used by the runner just before §11-reverting the ticket.
    """
    lines = [
        f"[outcomes-grader-strict:fail] AC-evidence strict grader (OP-1141) "
        f"refused {ticket_key} before Gerrit push.",
        "",
    ]
    if not result.comment_found:
        lines.extend([
            "Root cause: the CLI did not post an `AC verification for "
            f"{ticket_key}:` comment, so there is no evidence to check.",
            "",
            "Strict-mode contract: every AC item must be cited with a "
            "concrete piece of evidence (file:line, test name, or Change-Id) "
            "before the runner will push to Gerrit.",
        ])
        return "\n".join(lines)

    lines.append("The following AC ✓ claims failed strict cite verification:")
    lines.append("")
    for verdict in result.per_evidence:
        if verdict.ok:
            continue
        ev = verdict.evidence
        lines.append(
            f"  {ev.mark} {ev.description[:80]} — {ev.raw_evidence[:80]}"
        )
        lines.append(f"      → {verdict.reason}")
    lines.append("")
    lines.append(
        "Reverting to To Do per §11. Fix the AC verification comment "
        "(real file:line, real Change-Id, real test name) and re-pickup."
    )
    return "\n".join(lines)
