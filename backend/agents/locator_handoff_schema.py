"""B7 (OP-839) — Frozen locator → coder handoff schema.

The locator agent (claude-haiku-4-5) returns a single JSON envelope
that the coder agent (claude-sonnet-4-6) consumes. The schema is
FROZEN at filing time per master-plan §2.9 AC #2: any change requires
a follow-up ticket + retro entry. The coder receives ONLY this JSON
plus the ticket AC and the system prompt — *not* the locator's
tool-call history (AC #3).

Schema:

    {
      "files": [
        {"path": "<repo-relative>",
         "line_ranges": [[start, end], ...],   # 1-indexed inclusive
         "why_relevant": "<short rationale>"},
        ...
      ],
      "hypotheses": ["<bullet>", ...],
      "confidence": <float 0.0..1.0>,
      "summary": "<<=200 words>"
    }

Failure classes raised by :func:`parse_locator_handoff`:

* :class:`LocatorMalformedHandoff` — JSON schema mismatch / missing
  required fields / wrong types. Caller (locator_agent) retries with a
  strict-format reminder once (AC #4).
* :class:`LocatorHandoffPollution` — verbose envelope (>2k tokens by
  the 4-chars-per-token heuristic). Caller enforces schema; reject
  without retry (AC #4 — pollution is a different bug class from a
  malformed JSON envelope).

The 200-word summary cap is enforced even when the rest of the
envelope parses; verbose summaries are the most common pollution
vector.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# AC #2 — schema-level constants. Tests pin these to ensure the
# frozen format does not silently drift.
SCHEMA_VERSION = "B7.v1"

# AC #4 pollution gate. 2000 tokens × ~4 chars/token = 8000 chars,
# matching the master-plan threshold without depending on a tokenizer
# at runtime.
POLLUTION_CHAR_BUDGET = 8000

# AC #2 summary cap. 200 words by whitespace split.
SUMMARY_WORD_CAP = 200

# AC #5/#6 candidate-count thresholds.
ZERO_CANDIDATES = 0
TOO_MANY_CANDIDATES = 50


@dataclass(frozen=True)
class LocatorCandidate:
    """One file the locator believes the coder needs to read/edit."""

    path: str
    line_ranges: tuple[tuple[int, int], ...]
    why_relevant: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line_ranges": [list(r) for r in self.line_ranges],
            "why_relevant": self.why_relevant,
        }


@dataclass(frozen=True)
class LocatorHandoff:
    """Parsed + validated locator output. Coder consumes this only."""

    files: tuple[LocatorCandidate, ...]
    hypotheses: tuple[str, ...]
    confidence: float
    summary: str
    raw_json: str = field(default="", repr=False)

    @property
    def candidate_count(self) -> int:
        return len(self.files)

    @property
    def is_zero_candidates(self) -> bool:
        return self.candidate_count == ZERO_CANDIDATES

    @property
    def is_too_many_candidates(self) -> bool:
        return self.candidate_count > TOO_MANY_CANDIDATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [c.to_dict() for c in self.files],
            "hypotheses": list(self.hypotheses),
            "confidence": self.confidence,
            "summary": self.summary,
        }

    def to_coder_prompt(self, ac_text: str) -> str:
        """Serialise the handoff for the coder agent (AC #3).

        Order: schema-version banner → JSON envelope → AC. The coder
        system prompt is appended separately by the caller.
        """
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        return (
            f"=== Locator handoff ({SCHEMA_VERSION}) ===\n"
            f"{payload}\n\n"
            f"=== Ticket Acceptance Criteria ===\n{ac_text}\n"
        )


class LocatorMalformedHandoff(ValueError):
    """JSON missing required keys / wrong types — retry locator once."""


class LocatorHandoffPollution(ValueError):
    """Envelope larger than POLLUTION_CHAR_BUDGET — reject, no retry."""


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> str:
    """Best-effort JSON-object extraction from a raw model response.

    The locator is prompted to emit one JSON envelope, but Haiku
    occasionally prefixes a "Here is the handoff:" preamble. We strip
    that without being permissive about embedded JSON (no recursive
    bracket counting — fence to the outermost {...}).
    """
    text = (text or "").strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        raise LocatorMalformedHandoff(
            "no JSON object found in locator response"
        )
    return match.group(0)


def _validate_files(raw_files: Any) -> tuple[LocatorCandidate, ...]:
    if not isinstance(raw_files, list):
        raise LocatorMalformedHandoff(
            f"'files' must be a list, got {type(raw_files).__name__}"
        )
    out: list[LocatorCandidate] = []
    for idx, entry in enumerate(raw_files):
        if not isinstance(entry, dict):
            raise LocatorMalformedHandoff(
                f"files[{idx}] must be an object, got {type(entry).__name__}"
            )
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            raise LocatorMalformedHandoff(
                f"files[{idx}].path missing or empty"
            )
        ranges_raw = entry.get("line_ranges", [])
        if not isinstance(ranges_raw, list):
            raise LocatorMalformedHandoff(
                f"files[{idx}].line_ranges must be a list"
            )
        ranges: list[tuple[int, int]] = []
        for r_idx, pair in enumerate(ranges_raw):
            if (
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or not all(isinstance(x, int) for x in pair)
            ):
                raise LocatorMalformedHandoff(
                    f"files[{idx}].line_ranges[{r_idx}] must be [start,end] ints"
                )
            start, end = int(pair[0]), int(pair[1])
            if start < 1 or end < start:
                raise LocatorMalformedHandoff(
                    f"files[{idx}].line_ranges[{r_idx}] invalid "
                    f"[{start},{end}] — start must be >=1 and end>=start"
                )
            ranges.append((start, end))
        why = entry.get("why_relevant", "")
        if not isinstance(why, str):
            raise LocatorMalformedHandoff(
                f"files[{idx}].why_relevant must be a string"
            )
        out.append(
            LocatorCandidate(
                path=path.strip(),
                line_ranges=tuple(ranges),
                why_relevant=why.strip(),
            )
        )
    return tuple(out)


def _validate_hypotheses(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LocatorMalformedHandoff(
            f"'hypotheses' must be a list, got {type(raw).__name__}"
        )
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            raise LocatorMalformedHandoff(
                f"hypotheses[{idx}] must be a string"
            )
    return tuple(s.strip() for s in raw if s.strip())


def _validate_confidence(raw: Any) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise LocatorMalformedHandoff(
            f"'confidence' must be a number 0..1, got {type(raw).__name__}"
        )
    val = float(raw)
    if val < 0.0 or val > 1.0:
        raise LocatorMalformedHandoff(
            f"'confidence' out of range [0,1]: {val}"
        )
    return val


def _validate_summary(raw: Any) -> str:
    if not isinstance(raw, str):
        raise LocatorMalformedHandoff(
            f"'summary' must be a string, got {type(raw).__name__}"
        )
    text = raw.strip()
    word_count = len([w for w in re.split(r"\s+", text) if w])
    if word_count > SUMMARY_WORD_CAP:
        raise LocatorMalformedHandoff(
            f"'summary' exceeds {SUMMARY_WORD_CAP}-word cap "
            f"(got {word_count} words)"
        )
    return text


def parse_locator_handoff(text: str) -> LocatorHandoff:
    """Validate raw locator output against the frozen schema.

    Raises :class:`LocatorHandoffPollution` first if the raw envelope
    is larger than :data:`POLLUTION_CHAR_BUDGET` chars — that is a
    distinct error class from a malformed envelope (AC #4 separates
    "verbose" from "structurally wrong"). Otherwise raises
    :class:`LocatorMalformedHandoff` for any schema mismatch.
    """
    raw = text or ""
    if len(raw) > POLLUTION_CHAR_BUDGET:
        raise LocatorHandoffPollution(
            f"locator output {len(raw)} chars exceeds "
            f"{POLLUTION_CHAR_BUDGET}-char budget (>2k tokens approx)"
        )

    payload_text = _extract_json_object(raw)
    try:
        obj = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise LocatorMalformedHandoff(f"json decode error: {exc}") from None

    if not isinstance(obj, dict):
        raise LocatorMalformedHandoff(
            f"top-level payload must be an object, got {type(obj).__name__}"
        )

    missing = [k for k in ("files", "confidence", "summary") if k not in obj]
    if missing:
        raise LocatorMalformedHandoff(
            f"missing required fields: {missing}"
        )

    return LocatorHandoff(
        files=_validate_files(obj["files"]),
        hypotheses=_validate_hypotheses(obj.get("hypotheses")),
        confidence=_validate_confidence(obj["confidence"]),
        summary=_validate_summary(obj["summary"]),
        raw_json=payload_text,
    )


def schema_reminder_for_retry() -> str:
    """Reminder text appended to the locator's prompt on retry (AC #4).

    Used by ``locator_agent`` when the first attempt produces a
    malformed envelope. Spells out the schema in skeleton form so
    Haiku can re-emit without referring back to its own (possibly
    contaminated) prior output.
    """
    return (
        "STRICT FORMAT REMINDER — your previous response did not parse. "
        "Respond with EXACTLY one JSON object, no prose before or after, "
        "matching this schema:\n"
        '{"files": [{"path": "<repo-relative>", '
        '"line_ranges": [[<start_int>, <end_int>]], '
        '"why_relevant": "<short>"}],\n'
        ' "hypotheses": ["<bullet>"],\n'
        ' "confidence": <float 0.0..1.0>,\n'
        f' "summary": "<<= {SUMMARY_WORD_CAP} words>"}}\n'
        "Do not emit Markdown code fences. Do not add commentary."
    )
