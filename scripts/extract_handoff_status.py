"""FX.7.4 — Extract a machine-readable status manifest from HANDOFF.md.

HANDOFF.md is the human-readable narrative. Every entry ends with two
lines that the SOP (`docs/sop/implement_phase_step.md` "HANDOFF.md 格式
補強") requires:

    **Production status:** <one-of dev-only | deployed-inactive |
                                   deployed-active | deployed-observed>
    **Next gate:** <free-form one-liner pointing at the next flip>

Those two lines were originally read by humans, so over ~230 entries
they drifted into half a dozen formatting variants (`**Production
status: dev-only**`, `**Production status:** dev-only`, `### Production
status` header on its own line followed by `**Production status:**
dev-only`, plus inline parenthetical caveats). That makes them
impossible to query programmatically — you cannot answer "give me every
deployed-active milestone" without manual scrolling.

FX.7.4 extracts the two fields into ``docs/status/handoff_status.yaml``
(the manifest), keyed by a deterministic ``id`` per entry. HANDOFF.md
remains the narrative source of truth; the manifest is generated from
it. A drift guard test re-runs this extractor in ``--check`` mode so
edits to HANDOFF.md without a corresponding manifest regen fail CI.

Usage
-----
    python3 scripts/extract_handoff_status.py --write   # regenerate
    python3 scripts/extract_handoff_status.py --check   # CI mode

Exit codes
----------
0   manifest matches HANDOFF.md (check) / wrote manifest (write)
1   manifest is stale or invalid (check)
2   parser hit unparseable entries that would corrupt the manifest
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
HANDOFF_PATH = REPO_ROOT / "HANDOFF.md"
MANIFEST_PATH = REPO_ROOT / "docs" / "status" / "handoff_status.yaml"

# Canonical Production status values (defined in
# docs/sop/implement_phase_step.md — "TODO.md 狀態分層"). Any other
# token under Production status: is normalised to "unknown" and emitted
# with the original raw text in raw_status for human triage.
CANONICAL_STATUSES: tuple[str, ...] = (
    "dev-only",
    "deployed-inactive",
    "deployed-active",
    "deployed-observed",
)

# Extra status tokens that historically appear in HANDOFF.md and are
# legitimate (e.g. pure documentation rows). Treated as canonical for
# manifest purposes but tracked separately so the drift guard can warn
# if their count grows unexpectedly.
EXTRA_STATUSES: tuple[str, ...] = (
    "planning-only",
    "n/a",
)

ALL_STATUSES: tuple[str, ...] = CANONICAL_STATUSES + EXTRA_STATUSES

# Entry header: `## [Author] YYYY-MM-DD — TITLE`. The em-dash is U+2014.
# Some entries use plain `—` (em-dash); some legacy entries skip the
# separator entirely (`## [Codex/GPT-5.5] 2026-05-02 FS.1.1 完工`). The
# separator is therefore optional; when present it can be em-dash,
# en-dash, or ASCII hyphen.
_ENTRY_HEADER_RE = re.compile(
    r"^##\s+\[(?P<author>[^\]]+)\]\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?:[—–-]\s+)?(?P<title>.+?)\s*$"
)

# Match any markdown formatting around a "Production status" or
# "Next gate" mention. Variants observed in the wild:
#   **Production status:** dev-only
#   **Production status: dev-only**
#   **Production status**: dev-only
#   ### Production status: dev-only
#   ### Production status: **dev-only**
#   ### Production status                 (header-only; value is on a
#                                          later line under it)
#   ### Production status / Next gate     (combined header)
# Group 1 captures everything after the literal phrase, which we then
# strip of trailing/leading markdown punctuation and look for a
# canonical token.
_PROD_STATUS_LINE_RE = re.compile(
    r"(?:^|^[#]{1,6}\s+|^\s*-\s+)?[*_]{0,2}\s*Production\s+status\s*[:*_]*\s*(?P<rest>.*)$",
    re.IGNORECASE,
)

_NEXT_GATE_LINE_RE = re.compile(
    r"(?:^|^[#]{1,6}\s+|^\s*-\s+)?[*_]{0,2}\s*Next\s+gate\s*[:*_]*\s*(?P<rest>.*)$",
    re.IGNORECASE,
)

# Task-ID extraction from the entry title. Order matters — most
# specific patterns first so e.g. `FX.7.3` is preferred over a bare
# `7.3`. Patterns deliberately greedy on the right (`A.5b` keeps the
# trailing letter that disambiguates a/b splits per SOP §Step 2).
_TASK_ID_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\b(FX\.\d+\.\d+(?:[a-z])?)\b",
        r"\b(MS\.\d+(?:[a-z])?)\b",
        r"\b(BP\.[A-Z]\.\d+(?:\.[a-z]|[a-z])?)\b",
        r"\b(SP\.\d+\.\d+(?:[a-z])?)\b",
        r"\b(W\d+\.\d+(?:[a-z])?)\b",
        r"\b(W\d+)\b",
        r"\b(Z\.\d+\.\d+(?:[a-z])?)\b",
        r"\b(Z\.\d+)\b",
        r"\b([A-Z]{1,3}\.\d+\.\d+(?:[a-z])?)\b",
        r"\b([A-Z]\d+(?:\.\d+)?)\b",
    )
)


@dataclass
class Entry:
    """One HANDOFF.md row's manifest record."""

    header_line: int
    date: str
    author: str
    title: str
    task_id: str | None = None
    production_status: str = "unknown"
    raw_status: str | None = None  # set when production_status == "unknown"
    next_gate: str | None = None
    # Stable id: <date>--<task-id-or-slug>. Disambiguated with -N
    # suffix when the same id repeats in HANDOFF.md (rare but does
    # happen for re-work rows on the same day).
    id: str = field(default="")


def _strip_markdown(s: str) -> str:
    """Remove `**`, trailing `**`, leading `**`, and stray underscores."""
    s = s.strip()
    # Strip leading/trailing bold/italic markers conservatively (only at
    # the boundaries; intra-text ** is left alone).
    while s.startswith(("**", "__")):
        s = s[2:].lstrip()
    while s.endswith(("**", "__")):
        s = s[:-2].rstrip()
    while s.startswith(("*", "_")):
        s = s[1:].lstrip()
    while s.endswith(("*", "_")):
        s = s[:-1].rstrip()
    # Strip leading colon/whitespace runs sometimes left by the regex
    # capture (the line `### Production status:` after stripping the
    # heading marker leaves just `:`).
    s = s.lstrip(": \t")
    return s.strip()


def _normalise_status(raw: str) -> tuple[str, str | None]:
    """Return (canonical_status, raw_text_if_unknown).

    ``raw`` is the text after the "Production status" prefix. We strip
    markdown chrome, then look for the first canonical token. The
    remainder (parenthetical caveats, dash-prefixed notes) is dropped
    on the floor — that detail belongs in HANDOFF.md, not the manifest.
    """
    cleaned = _strip_markdown(raw)
    if not cleaned:
        return ("unknown", "")
    # Some entries put the status inside a backtick or in parentheses.
    cleaned_for_match = cleaned.replace("`", "").replace("（", " ").replace("(", " ")
    # Find the FIRST canonical token (longest first to avoid `dev-only`
    # eating a `deployed-inactive` prefix — but they share no prefix so
    # order is fine; sort defensively anyway).
    for token in sorted(ALL_STATUSES, key=len, reverse=True):
        # word boundary must work even when the token contains hyphens;
        # use a simple substring match anchored to non-alphanumeric
        # neighbours.
        m = re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", cleaned_for_match)
        if m:
            return (token, None)
    return ("unknown", cleaned)


def _slugify(text: str) -> str:
    """Lowercase ASCII slug for falling back when no task ID is found."""
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:60] or "untitled"


def _extract_task_id(title: str) -> str | None:
    for pat in _TASK_ID_PATTERNS:
        m = pat.search(title)
        if m:
            return m.group(1)
    return None


def _extract_value_after_phrase(line: str, phrase_re: re.Pattern[str]) -> str | None:
    """If ``line`` matches the prefix regex for the phrase, return the
    remainder; else None.

    Note the regexes are intentionally permissive on the prefix to
    catch every formatting variant; we then strip markdown from the
    remainder.
    """
    m = phrase_re.match(line)
    if not m:
        return None
    return m.group("rest")


def _continue_next_gate(rest: str, lines: list[str], start_idx: int) -> str:
    """Glue subsequent continuation lines into the Next gate value.

    Continuation rules (mirroring how humans wrote these in HANDOFF.md):
    - blank line ends the value
    - a line starting with `## ` (next entry) ends the value
    - a line starting with `---` (entry separator) ends the value
    - a line that itself starts a new field (e.g. `**Production
      status`) ends the value
    - otherwise the line is appended with a single space joiner
    """
    parts: list[str] = []
    first = _strip_markdown(rest)
    if first:
        parts.append(first)

    j = start_idx + 1
    while j < len(lines):
        nxt = lines[j].rstrip()
        if not nxt.strip():
            break
        if nxt.startswith("## ") or nxt.startswith("---"):
            break
        # Defensive: if continuation is a fresh "Production status" line,
        # stop — the entry author probably forgot a blank separator.
        if _PROD_STATUS_LINE_RE.match(nxt):
            break
        parts.append(_strip_markdown(nxt))
        j += 1
    return " ".join(p for p in parts if p).strip()


def parse_handoff(text: str) -> tuple[list[Entry], list[str]]:
    """Return (entries, warnings).

    Walks HANDOFF.md and slices it into entries on the `## [Author]
    DATE — TITLE` boundary. Within each entry, finds the first
    Production status line and the first Next gate line.

    Warnings are non-fatal but surfaced so the operator can fix
    formatting drift during the next HANDOFF edit.
    """
    lines = text.splitlines()
    entries: list[Entry] = []
    warnings: list[str] = []

    # First pass: locate entry header line numbers.
    header_indices: list[int] = []
    for i, ln in enumerate(lines):
        if _ENTRY_HEADER_RE.match(ln):
            header_indices.append(i)

    for entry_idx, hdr_i in enumerate(header_indices):
        hdr_match = _ENTRY_HEADER_RE.match(lines[hdr_i])
        assert hdr_match is not None
        end_i = (
            header_indices[entry_idx + 1]
            if entry_idx + 1 < len(header_indices)
            else len(lines)
        )
        body = lines[hdr_i:end_i]

        entry = Entry(
            header_line=hdr_i + 1,  # 1-indexed for human consumption
            date=hdr_match.group("date"),
            author=hdr_match.group("author").strip(),
            title=hdr_match.group("title").strip(),
        )
        entry.task_id = _extract_task_id(entry.title)

        # Within the body, find Production status + Next gate.
        prod_found = False
        gate_found = False
        for k, raw_line in enumerate(body):
            if not prod_found:
                rest = _extract_value_after_phrase(raw_line, _PROD_STATUS_LINE_RE)
                if rest is not None:
                    cleaned = _strip_markdown(rest)
                    # Treat as header-only-needs-peek when:
                    #   - the line was literally just "### Production status"
                    #     (cleaned is empty), OR
                    #   - the captured rest is a combined header marker like
                    #     "/ Next gate" (no canonical status token AND
                    #     doesn't look like a real value line)
                    needs_peek = not cleaned
                    if not needs_peek:
                        probe_status, _ = _normalise_status(rest)
                        if probe_status == "unknown" and (
                            cleaned.startswith("/")
                            or re.match(r"^[/&\\]\s*Next\s+gate", cleaned, re.IGNORECASE)
                        ):
                            needs_peek = True
                    if needs_peek:
                        for peek_k in range(k + 1, min(k + 8, len(body))):
                            peek = body[peek_k].strip()
                            if not peek:
                                continue
                            # A peek line that itself contains
                            # "Production status" is the value-bearing
                            # follow-up.
                            if _PROD_STATUS_LINE_RE.match(peek):
                                inner = _extract_value_after_phrase(
                                    peek, _PROD_STATUS_LINE_RE
                                )
                                if inner and inner.strip():
                                    rest = inner
                                    break
                            else:
                                # If the peek line contains a canonical
                                # token, take it; otherwise stop peeking
                                # (keeps "rest" as the original capture).
                                probe, _ = _normalise_status(peek)
                                if probe != "unknown":
                                    rest = peek
                                    break
                    status, raw_unknown = _normalise_status(rest)
                    entry.production_status = status
                    if status == "unknown":
                        entry.raw_status = raw_unknown or rest
                        warnings.append(
                            f"HANDOFF.md:{hdr_i + k + 1}: could not normalise "
                            f"Production status; raw={raw_unknown!r} title={entry.title!r}"
                        )
                    prod_found = True
                    continue
            if not gate_found:
                rest = _extract_value_after_phrase(raw_line, _NEXT_GATE_LINE_RE)
                if rest is not None:
                    cleaned = _strip_markdown(rest)
                    if not cleaned:
                        # header-only line — pull the next non-blank line
                        for peek_k in range(k + 1, min(k + 6, len(body))):
                            peek = body[peek_k].strip()
                            if not peek:
                                continue
                            if _NEXT_GATE_LINE_RE.match(peek):
                                inner = _extract_value_after_phrase(
                                    peek, _NEXT_GATE_LINE_RE
                                )
                                if inner:
                                    rest = inner
                                    k = peek_k
                                    break
                            else:
                                rest = peek
                                k = peek_k
                                break
                    value = _continue_next_gate(rest, body, k)
                    entry.next_gate = value or None
                    gate_found = True
                    continue
            if prod_found and gate_found:
                break

        if not prod_found:
            warnings.append(
                f"HANDOFF.md:{hdr_i + 1}: entry has no Production status line "
                f"(title={entry.title!r})"
            )
        entries.append(entry)

    # Assign stable ids with deduplication.
    used: dict[str, int] = {}
    for e in entries:
        base_id = e.task_id or _slugify(e.title)
        candidate = f"{e.date}--{base_id}"
        n = used.get(candidate, 0)
        used[candidate] = n + 1
        e.id = candidate if n == 0 else f"{candidate}-{n + 1}"

    return entries, warnings


def build_manifest(entries: list[Entry]) -> dict:
    counts: dict[str, int] = {}
    for s in ALL_STATUSES + ("unknown",):
        counts[s] = 0
    for e in entries:
        counts[e.production_status] = counts.get(e.production_status, 0) + 1

    manifest = {
        "schema_version": 1,
        "generated_from": "HANDOFF.md",
        "generator": "scripts/extract_handoff_status.py",
        "drift_guard": "backend/tests/test_handoff_status_manifest_drift_guard.py",
        "entry_count": len(entries),
        "status_counts": counts,
        "canonical_statuses": list(CANONICAL_STATUSES) + list(EXTRA_STATUSES),
        "entries": [
            {
                "id": e.id,
                "header_line": e.header_line,
                "date": e.date,
                "author": e.author,
                "task_id": e.task_id,
                "title": e.title,
                "production_status": e.production_status,
                **({"raw_status": e.raw_status} if e.raw_status is not None else {}),
                "next_gate": e.next_gate,
            }
            for e in entries
        ],
    }
    return manifest


_HEADER_COMMENT = """\
# AUTO-GENERATED FROM HANDOFF.md — DO NOT EDIT BY HAND.
#
# Source:        HANDOFF.md (## [...] entry blocks)
# Generator:     scripts/extract_handoff_status.py
# Drift guard:   backend/tests/test_handoff_status_manifest_drift_guard.py
#
# To regenerate after editing HANDOFF.md:
#     python3 scripts/extract_handoff_status.py --write
#
# To verify (run by CI / drift guard test):
#     python3 scripts/extract_handoff_status.py --check
#
# Schema (v1):
#   schema_version    int
#   generated_from    str  — always "HANDOFF.md"
#   entry_count       int  — number of `## [Author] DATE — TITLE` rows
#   status_counts     map<status, count>
#   canonical_statuses list<str>  — allowed Production status values
#   entries           list of:
#     id                  str  — `<date>--<task-id-or-slug>` (stable, unique)
#     header_line         int  — 1-indexed line in HANDOFF.md
#     date                str  — YYYY-MM-DD
#     author              str  — e.g. "Claude/Opus", "Claude/Sonnet"
#     task_id             str|null — extracted from title (e.g. FX.7.3)
#     title               str
#     production_status   str  — one of canonical_statuses or "unknown"
#     raw_status          str  — present only when production_status == "unknown"
#     next_gate           str|null — one-line summary of the next gate
#
# Background: see docs/sop/implement_phase_step.md "HANDOFF.md 格式補強"
# and the FX.7.4 HANDOFF entry.
"""


def serialise_manifest(manifest: dict) -> str:
    body = yaml.safe_dump(
        manifest,
        sort_keys=False,
        allow_unicode=True,
        width=120,
        default_flow_style=False,
    )
    return _HEADER_COMMENT + body


def load_existing_manifest() -> str | None:
    if not MANIFEST_PATH.exists():
        return None
    return MANIFEST_PATH.read_text(encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--write",
        action="store_true",
        help="Regenerate docs/status/handoff_status.yaml from HANDOFF.md.",
    )
    group.add_argument(
        "--check",
        action="store_true",
        help="Fail if the manifest is stale relative to HANDOFF.md.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress non-fatal warnings (still printed to stderr if --check fails).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not HANDOFF_PATH.exists():
        print(f"FATAL: {HANDOFF_PATH} not found", file=sys.stderr)
        return 2

    text = HANDOFF_PATH.read_text(encoding="utf-8")
    entries, warnings = parse_handoff(text)

    if not entries:
        print(
            "FATAL: parser found 0 entries — HANDOFF.md format may have changed",
            file=sys.stderr,
        )
        return 2

    manifest = build_manifest(entries)
    serialised = serialise_manifest(manifest)

    if not args.quiet:
        for w in warnings:
            print(f"WARN: {w}", file=sys.stderr)

    if args.write:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(serialised, encoding="utf-8")
        print(
            f"Wrote {MANIFEST_PATH.relative_to(REPO_ROOT)} — "
            f"{len(entries)} entries, {len(warnings)} warning(s)."
        )
        return 0

    # --check mode
    existing = load_existing_manifest()
    if existing is None:
        print(
            f"FATAL: {MANIFEST_PATH.relative_to(REPO_ROOT)} does not exist. "
            f"Run: python3 scripts/extract_handoff_status.py --write",
            file=sys.stderr,
        )
        return 1
    if existing != serialised:
        print(
            f"FATAL: {MANIFEST_PATH.relative_to(REPO_ROOT)} is stale relative "
            f"to HANDOFF.md. Run: python3 scripts/extract_handoff_status.py --write",
            file=sys.stderr,
        )
        # Surface a small diff hint — first ~10 differing lines.
        ex_lines = existing.splitlines()
        new_lines = serialised.splitlines()
        diffs = 0
        for i, (a, b) in enumerate(zip(ex_lines, new_lines)):
            if a != b:
                print(f"  line {i + 1}:", file=sys.stderr)
                print(f"    on disk: {a[:160]}", file=sys.stderr)
                print(f"    expected: {b[:160]}", file=sys.stderr)
                diffs += 1
                if diffs >= 10:
                    break
        if len(ex_lines) != len(new_lines):
            print(
                f"  (file lengths differ: on disk {len(ex_lines)} lines, "
                f"expected {len(new_lines)} lines)",
                file=sys.stderr,
            )
        return 1

    print(
        f"OK: manifest matches HANDOFF.md "
        f"({len(entries)} entries, {len(warnings)} warning(s))."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
