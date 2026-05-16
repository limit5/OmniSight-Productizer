"""RPG.W5.2 -- Distilled skill summary for ``lessons_learned`` writes.

When a new row is inserted into ``episodic_memory`` (the L3
"lessons_learned" knowledge base surfaced by
:func:`backend.project_report._lessons_learned`), this module computes a
compact, ``<= 200`` token markdown summary intended for the L2 distilled-
skills layer described in ADR-0008 ("Agent RPG Class & Skill Leveling"),
section *Memory hierarchy*.

The summary is a pure function of the lesson payload -- the module
performs no DB writes of its own.  Callers (``insert_episodic_memory``
in ``backend/db.py``) invoke :func:`on_lesson_written` as a best-effort
post-insert hook; the returned summary is suitable for emitting to
audit telemetry or for handing to the BP.M dim-memory writer once the
L2 backend lands (RPG.W5.1).

Module-global / cross-worker state audit
----------------------------------------
Pure-function module.  Only immutable constants live at module scope;
the scrubbing rules are imported from :mod:`backend.skills_scrubber`,
which itself uses compiled regexes only.  No mutable cache participates
in the summary value, so every worker that distils the same lesson
produces the same string.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from backend.skills_scrubber import scrub

logger = logging.getLogger(__name__)

# Anthropic's published rule of thumb: ~4 characters per token.  Mirrors
# :func:`backend.agents.stale_refresh_strategy.estimate_tokens` so the
# repo shares one token-counting convention.
_CHARS_PER_TOKEN = 4

# ADR-0008 RPG.W5.2 -- distilled skill summary cap.  Tight enough that
# Layer 2 top-K retrieval can return several hits under the 2 KB pre-
# task injection budget described in ADR-0008 *Memory hierarchy*.
MAX_SUMMARY_TOKENS = 200

_TRUNCATION_MARKER = "..."


def estimate_tokens(text: str) -> int:
    """Approximate token count using the ~4 chars/token heuristic."""

    if not text:
        return 0
    return len(text) // _CHARS_PER_TOKEN + 1


def _truncate_to_token_budget(text: str, budget: int) -> str:
    if estimate_tokens(text) <= budget:
        return text
    # ``estimate_tokens`` is ``len // 4 + 1``, so a length of ``budget * 4
    # - 1`` is the largest string whose estimate still equals ``budget``.
    # Reserve the tail for the truncation marker.
    max_chars = budget * _CHARS_PER_TOKEN - 1
    char_budget = max(0, max_chars - len(_TRUNCATION_MARKER))
    return text[:char_budget].rstrip() + _TRUNCATION_MARKER


def _coerce_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(t) for t in raw if t is not None and str(t).strip()]
    if isinstance(raw, str):
        # ``episodic_memory.tags`` is JSON-encoded in the DB.  Accept
        # both the decoded list and the raw JSON string so callers do
        # not have to pre-parse.
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return [raw.strip()] if raw.strip() else []
        if isinstance(decoded, list):
            return [str(t) for t in decoded if t is not None and str(t).strip()]
        return []
    return []


def distill_lesson_summary(lesson: Mapping[str, Any]) -> str:
    """Return a ``<= MAX_SUMMARY_TOKENS`` markdown summary for a lesson.

    Accepts the same dict shape :func:`backend.db.insert_episodic_memory`
    consumes: ``error_signature`` and ``solution`` are load-bearing; the
    other fields (``soc_vendor``, ``sdk_version``, ``hardware_rev``,
    ``gerrit_change_id``, ``tags``) shape the summary header.

    Secrets / PII in either field are redacted via
    :func:`backend.skills_scrubber.scrub` before the budget check so the
    final string is safe to hand off to the dim-memory writer.
    """

    error_signature = str(lesson.get("error_signature") or "").strip()
    solution = str(lesson.get("solution") or "").strip()
    soc_vendor = str(lesson.get("soc_vendor") or "").strip()
    sdk_version = str(lesson.get("sdk_version") or "").strip()
    hardware_rev = str(lesson.get("hardware_rev") or "").strip()
    gerrit = str(lesson.get("gerrit_change_id") or "").strip()
    tags = _coerce_tags(lesson.get("tags"))

    head_bits: list[str] = []
    if soc_vendor:
        head_bits.append(f"soc:{soc_vendor}")
    if sdk_version:
        head_bits.append(f"sdk:{sdk_version}")
    if hardware_rev:
        head_bits.append(f"hw:{hardware_rev}")
    if gerrit:
        head_bits.append(f"change:{gerrit}")

    parts: list[str] = ["## Lesson"]
    if head_bits:
        parts.append("_" + " ".join(head_bits) + "_")
    if error_signature:
        parts.append("**Signature:** " + error_signature)
    if solution:
        parts.append("**Fix:** " + solution)
    if tags:
        parts.append("**Tags:** " + ", ".join("`" + t + "`" for t in tags[:8]))

    raw = "\n\n".join(parts) + "\n"
    scrubbed, _hits = scrub(raw)
    return _truncate_to_token_budget(scrubbed, MAX_SUMMARY_TOKENS)


async def on_lesson_written(lesson: Mapping[str, Any]) -> str | None:
    """Best-effort post-insert hook fired from ``insert_episodic_memory``.

    Returns the distilled summary so callers and contract tests can
    inspect it.  Swallows exceptions because lesson recording is the
    load-bearing operation -- distillation failure must never poison the
    write that triggered it.
    """

    try:
        summary = distill_lesson_summary(lesson)
    except Exception as exc:  # pragma: no cover -- defensive only
        logger.debug("distill_lesson_summary failed: %s", exc)
        return None

    tokens = estimate_tokens(summary)
    if tokens > MAX_SUMMARY_TOKENS:  # pragma: no cover -- invariant
        # Truncation is bounded by ``MAX_SUMMARY_TOKENS`` so this branch
        # is unreachable absent a chars/token-heuristic regression.  Log
        # loudly if the invariant ever breaks.
        logger.warning(
            "distilled lesson summary exceeded %d-token budget (was %d)",
            MAX_SUMMARY_TOKENS,
            tokens,
        )

    logger.debug(
        "lessons_distiller: summarised lesson id=%s tokens=%d",
        lesson.get("id"),
        tokens,
    )
    return summary


__all__ = [
    "MAX_SUMMARY_TOKENS",
    "distill_lesson_summary",
    "estimate_tokens",
    "on_lesson_written",
]
