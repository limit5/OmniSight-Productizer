"""Phase 68-D — persist operator's clarification choices into L3.

When the operator resolves a spec conflict (Phase 68-B's
`apply_clarification`), the pair `(conflict_id, option_id)` is a
piece of information future parses can reuse: "last time you saw a
`static_with_runtime_db` conflict on a prompt like this, you
picked ssr_runtime". Storing these in `episodic_memory` with a
tag of `decision/spec-conflict` lets the existing Phase 67-E RAG
pre-fetch machinery surface them unchanged — same L3 table, same
search, same decay.

We intentionally DON'T auto-apply the prior choice. That would
silently steer the spec even when the operator's intent has
shifted. Instead: the clarification proposal gets a "last time you
picked X" hint, the matching option is visually pre-selected, and
clicking still counts as a fresh choice (which gets its own
row — repeated picks reinforce the signal via Phase 63-E's decay
rather than a special counter).

Module is pure IO — no global state, no singletons.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# Tag applied to every spec-conflict clarification row. Phase 67-E
# RAG pre-fetch filters by tag via FTS5 + the post-filter below.
_TAG_PREFIX = "decision/spec-conflict"


@dataclass(frozen=True)
class PriorChoice:
    """Represents a previously-recorded clarification choice that
    matches the current context closely enough to be worth showing
    as a hint."""
    conflict_id: str
    option_id: str
    quality: float   # 0..1 — used to rank multiple competing priors
    memory_id: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Signature — the FTS5 query key
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# The error_signature column is what FTS5 searches on. We need two
# things in it: the conflict_id (so look-ups don't cross-match
# different conflict types) AND a digested fingerprint of the raw
# prompt (so only similar-sounding prompts match). Kept under the
# column's 200-char practical limit.

def _signature(conflict_id: str, raw_text: str) -> str:
    """Build a compact, FTS5-friendly signature. Format:

        spec-conflict:<conflict_id>:<first-80-chars-of-prompt>

    The suffix is the leading 80 chars of the prompt with whitespace
    collapsed — enough for FTS5 to tell 'static Next.js site with
    SQLite' apart from 'build an arm64 firmware driver', without
    creating thousands of unique signatures that never match."""
    snippet = re.sub(r"\s+", " ", (raw_text or "")).strip()[:80]
    return f"spec-conflict:{conflict_id}:{snippet}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Record path — called from /intent/clarify
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def record_clarification_choice(
    *,
    raw_text: str,
    conflict_id: str,
    option_id: str,
    operator_email: str | None = None,
    quality: float = 0.85,
) -> Optional[str]:
    """Persist the operator's pick. Returns the new memory row id on
    success, None on failure (DB error etc). Best-effort — the
    caller's clarification flow must not hard-fail because we
    couldn't write a hint row.

    `quality` defaults to 0.85 — operator's explicit choice is a
    strong signal but we want future prefetch to keep respecting
    Phase 67-E's `min_cosine=0.85` gate, so we sit exactly on it.
    """
    try:
        from backend import db
    except Exception as exc:
        logger.debug("intent_memory: db import failed: %s", exc)
        return None

    memory_id = f"spec-{uuid.uuid4().hex[:12]}"
    sig = _signature(conflict_id, raw_text)
    # `solution` is the structured pick so future parses can restore
    # both sides — which conflict, which option, who picked, when.
    solution_blob = json.dumps({
        "conflict_id": conflict_id,
        "option_id": option_id,
        "operator": operator_email or "",
        "recorded_at": time.time(),
    })
    try:
        await db.insert_episodic_memory({
            "id": memory_id,
            "error_signature": sig,
            "solution": solution_blob,
            "soc_vendor": "", "sdk_version": "", "hardware_rev": "",
            "source_task_id": None,
            "source_agent_id": None,
            "gerrit_change_id": None,
            "tags": [_TAG_PREFIX, f"conflict:{conflict_id}"],
            "quality_score": max(0.0, min(1.0, quality)),
        })
    except Exception as exc:
        logger.warning(
            "intent_memory: record_clarification_choice failed "
            "(conflict=%s option=%s): %s",
            conflict_id, option_id, exc,
        )
        return None

    logger.info(
        "intent_memory: recorded %s → %s as %s",
        conflict_id, option_id, memory_id,
    )
    return memory_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Lookup path — called from /intent/parse response annotation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def lookup_prior_choice(
    *,
    raw_text: str,
    conflict_id: str,
    top_k: int = 3,
    min_quality: float = 0.5,
) -> Optional[PriorChoice]:
    """Look for a prior clarification whose signature matches this
    (raw_text, conflict_id) pair. Returns the highest-quality hit,
    None if none clear the floor.

    `min_quality` here is 0.5 — we use 0.85 on the recording side
    so any row that survives decay will still pass this gate; we
    keep it lower for robustness during the warm-up period before
    a workspace has accumulated many examples.
    """
    try:
        from backend import db
    except Exception as exc:
        logger.debug("intent_memory: db import failed: %s", exc)
        return None

    # FTS5 query uses the signature prefix so we don't match across
    # conflict types. Trim the prompt snippet to the same shape the
    # recorder used so FTS term overlap is meaningful.
    query_sig = _signature(conflict_id, raw_text)
    # Strip the non-searchable prefix for the actual FTS query —
    # FTS5 tokenises on word chars + the colons have become word
    # boundaries.
    query = query_sig

    try:
        rows = await db.search_episodic_memory(
            query, limit=top_k, min_quality=min_quality,
        )
    except Exception as exc:
        logger.debug("intent_memory: search failed: %s", exc)
        return None

    for r in rows:
        sig = (r.get("error_signature") or "")
        if not sig.startswith(f"spec-conflict:{conflict_id}:"):
            continue
        try:
            payload = json.loads(r.get("solution") or "{}")
        except Exception:
            continue
        if payload.get("conflict_id") != conflict_id:
            continue
        opt = payload.get("option_id")
        if not opt:
            continue
        try:
            q = float(r.get("quality_score") or 0.0)
        except (TypeError, ValueError):
            q = 0.0
        return PriorChoice(
            conflict_id=conflict_id, option_id=str(opt),
            quality=q, memory_id=r.get("id") or "",
        )
    return None


async def annotate_conflicts_with_priors(
    raw_text: str,
    conflicts: list[dict],
) -> list[dict]:
    """For each conflict in a ParsedSpec.to_dict()-shaped list, add
    a `prior_choice` field if a historical pick is available. The
    UI uses this to pre-select the matching option + show a "last
    time you picked …" hint.

    Input is the conflicts array from `ParsedSpec.to_dict()` (dicts
    with id / message / fields / options / severity). Output is the
    same list mutated in place — kept simple for the router hot
    path, which isn't thread-sharing the structure.
    """
    for c in conflicts:
        cid = c.get("id") or ""
        if not cid:
            continue
        try:
            prior = await lookup_prior_choice(
                raw_text=raw_text, conflict_id=cid,
            )
        except Exception as exc:
            logger.debug("annotate: lookup failed for %s: %s", cid, exc)
            prior = None
        if prior is not None:
            c["prior_choice"] = {
                "option_id": prior.option_id,
                "quality": round(prior.quality, 2),
                "memory_id": prior.memory_id,
            }
    return conflicts
