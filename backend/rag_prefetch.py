"""Phase 67-D — RAG pre-fetch on step error.

When a sandbox step exits non-zero, the agent's next turn would
normally spend 10–15s calling a search tool to look up the error.
This module short-circuits that: the error log is handed to the L3
episodic-memory FTS5 search the moment `rc != 0`, the top matches
are folded into a `<related_past_solutions>` block, and that block
is ready for inclusion in the next prompt (as a CACHEABLE static_kb
segment via `prompt_cache.CachedPromptBuilder`).

Design rules (all from HANDOFF Phase 67-D):

  * Confidence floor = 0.5 in v1. Phase 63-E memory decay raises
    this to 0.7 once poisoned rows decay below the floor.
  * Top-K = 3. More is noise; input-token inflation would offset
    the TTFT savings.
  * Cacheable marker. The injected block is static-for-this-turn
    — we want Anthropic / OpenAI prefix cache to retain it across
    the agent's retry loop.
  * NEVER inject on rc == 0. The whole point is error-triggered
    pre-fetch.

The module is pure async functions — no global state, no singletons.
Callers (workflow step error path, invoke.py error_check_node) import
and call `prefetch_for_error(error_log) -> str | None`.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables (env-backed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _min_confidence() -> float:
    """v1 default 0.5; operator overrides via env."""
    raw = (os.environ.get("OMNISIGHT_RAG_MIN_CONFIDENCE") or "0.5").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.5


def _top_k() -> int:
    raw = (os.environ.get("OMNISIGHT_RAG_TOP_K") or "3").strip()
    try:
        return max(1, min(10, int(raw)))
    except ValueError:
        return 3


# Phase 67-E (docs/design/dag-pre-fetching.md) — stricter Tier-1 sandbox
# gate. The generic `_min_confidence` above stays (0.5 default, suits
# the agent-side L3 hint path). The *sandbox* injection path routes
# through `prefetch_for_sandbox_error` and wants a higher bar so a
# borderline match can't "poison" the compile-fix loop.
def _min_cosine() -> float:
    """Sandbox-path similarity floor. Design doc spec: cosine > 0.85.
    Caveat: the DB layer today uses FTS5 rank + a curated quality_score
    rather than a real dense-embedding cosine. We use quality_score
    as a proxy for this gate — when a true embedding column lands
    (Phase 67-F), this function's call site is the one place to swap."""
    raw = (os.environ.get("OMNISIGHT_RAG_MIN_COSINE") or "0.85").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.85


def _max_block_tokens() -> int:
    """Total token budget for the injected `<system_auto_prefetch>`
    block. Spec: 1000 max. Approximated as chars/4 for v1 — every
    production tokenizer (tiktoken, claude's) agrees within ±20% on
    English prose, which is within the safety margin."""
    raw = (os.environ.get("OMNISIGHT_RAG_MAX_BLOCK_TOKENS") or "1000").strip()
    try:
        return max(100, min(8000, int(raw)))
    except ValueError:
        return 1000


def _approx_tokens(text: str) -> int:
    """Char/4 approximation. Good enough for a budget gate — the cost
    of an over-budget block is a wasted prompt-cache slot, not
    correctness."""
    return (len(text) + 3) // 4


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Error signature extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Patterns that tend to carry the real "what broke" signal, scored by
# how specific they are. The first hit wins — we don't concatenate
# everything (FTS5 phrase match degrades fast).
_SIGNATURE_PATTERNS: list[re.Pattern[str]] = [
    # Specific signatures FIRST — they carry more signal than a
    # generic "error:" line. The first hit wins.
    re.compile(r"\b(Segmentation fault)\b", re.IGNORECASE),
    re.compile(r"\b(undefined reference to)\b"),
    re.compile(r"\b(Invalid (?:read|write) of size \d+)"),
    re.compile(r"\b(Conditional jump or move depends on uninitialised)"),
    # Python-ish tracebacks: the XxxError type alone is a great FTS key.
    re.compile(r"\b([A-Z][A-Za-z]+Error)\b"),
    # gcc / clang `file:line:col: error: message` — capture the message.
    re.compile(r"[^\s:]+:\d+:\d+:\s*(?:fatal )?error:\s*([^\n]{4,200})"),
    # Generic fallbacks — last.
    re.compile(r"\bfatal[:\s-]+([^\n]{8,200})", re.IGNORECASE),
    re.compile(r"\berror[:\s-]+([^\n]{8,200})", re.IGNORECASE),
]


def extract_signature(error_log: str, *, max_len: int = 200) -> str:
    """Pick one salient line from an error log for FTS5 query.

    Returns '' when nothing matches — the caller must treat that as
    "no prefetch this turn" rather than "query the whole log"
    (FTS5 on 5MB of gcc output is pathological)."""
    if not error_log:
        return ""
    sample = error_log[-8000:]  # last 8KB — failure tail is most specific
    for pat in _SIGNATURE_PATTERNS:
        m = pat.search(sample)
        if not m:
            continue
        # Prefer the captured group (tighter) over the full match.
        text = m.group(1) if m.groups() else m.group(0)
        text = text.strip()[:max_len]
        if text:
            return text
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Prefetch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass(frozen=True)
class PrefetchHit:
    memory_id: str
    error_signature: str
    solution: str
    quality_score: float
    soc_vendor: str
    sdk_version: str


async def prefetch_for_error(
    error_log: str, *,
    rc: int = 1,
    soc_vendor: str = "",
    sdk_version: str = "",
) -> Optional[str]:
    """End-to-end pre-fetch: rc != 0 → extract signature → search L3 →
    filter by confidence → format as a <related_past_solutions> block.

    Returns None when:
      * rc == 0 (no error → nothing to pre-fetch)
      * signature extraction returned ''
      * no search hit clears the confidence floor

    Never raises — DB failures are logged and we return None so the
    caller's error path stays on its fallback (ask the agent to search
    itself).
    """
    if rc == 0:
        return None
    sig = extract_signature(error_log)
    if not sig:
        return None

    try:
        from backend import db
        hits_raw = await db.search_episodic_memory(
            sig, soc_vendor=soc_vendor, sdk_version=sdk_version,
            limit=_top_k() * 2,  # over-fetch, then apply confidence filter
        )
    except Exception as exc:
        logger.warning("rag_prefetch: episodic search failed: %s", exc)
        _bump("search_error")
        return None

    min_c = _min_confidence()
    hits: list[PrefetchHit] = []
    for r in hits_raw:
        try:
            q = float(r.get("quality_score") or 0.0)
        except (TypeError, ValueError):
            q = 0.0
        if q < min_c:
            continue
        hits.append(PrefetchHit(
            memory_id=r.get("id") or "",
            error_signature=r.get("error_signature") or "",
            solution=r.get("solution") or "",
            quality_score=q,
            soc_vendor=r.get("soc_vendor") or "",
            sdk_version=r.get("sdk_version") or "",
        ))
        if len(hits) >= _top_k():
            break

    if not hits:
        _bump("below_confidence" if hits_raw else "no_hit")
        return None

    _bump("injected")
    await _touch_hits(hits)
    return format_block(hits)


def format_block(hits: list[PrefetchHit], *, max_solution_chars: int = 800) -> str:
    """Render hits as a deterministic `<related_past_solutions>` block.

    Sort order: quality_score desc, then memory_id asc for tie
    stability — the prefix must be byte-identical across retries for
    the cache to actually hit."""
    sorted_hits = sorted(
        hits, key=lambda h: (-h.quality_score, h.memory_id),
    )
    lines = ["<related_past_solutions>"]
    for h in sorted_hits:
        lines.append(
            f"  <solution id={h.memory_id!r} quality={h.quality_score:.2f}"
            + (f" soc={h.soc_vendor!r}" if h.soc_vendor else "")
            + (f" sdk={h.sdk_version!r}" if h.sdk_version else "")
            + ">"
        )
        lines.append(f"    signature: {h.error_signature}")
        sol = (h.solution or "").strip()
        if len(sol) > max_solution_chars:
            sol = sol[:max_solution_chars] + "…[truncated]"
        lines.append("    solution: |")
        for sline in sol.splitlines():
            lines.append(f"      {sline}")
        lines.append("  </solution>")
    lines.append("</related_past_solutions>")
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 67-E — Tier-1 sandbox-error prefetch (strict guards)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Same underlying L3 search as `prefetch_for_error`, but applies the
# three guardrails the design doc calls out explicitly:
#
#   1. **Cosine (proxy) > 0.85**. Borderline quality_score matches
#      are rejected outright; they're exactly the "agent degradation"
#      poison the doc warns about.
#   2. **SDK version hard-lock**. When `sdk_version` is supplied and
#      the hit's sdk_version is non-empty and differs, the hit is
#      DROPPED even at confidence 0.99. Older APIs are a top cause of
#      agent confusion — we'd rather the agent reason from scratch
#      than parrot a deprecated fix.
#   3. **1000-token block budget**. Hits are emitted in quality order
#      and we stop as soon as adding the next one would blow the
#      budget. The block gets a `truncated="true"` attribute so the
#      agent knows there might be more.

def _version_hard_lock_rejects(hit_sdk: str, env_sdk: str) -> bool:
    """Return True iff the hit must be dropped on version mismatch.

    Rules:
      * env_sdk unknown ('') → accept anything (nothing to compare).
      * hit_sdk unknown ('') → accept (legacy rows have no tag).
      * both set and unequal → reject.
    """
    if not env_sdk or not hit_sdk:
        return False
    return hit_sdk.strip() != env_sdk.strip()


async def prefetch_for_sandbox_error(
    error_log: str, *,
    rc: int = 1,
    soc_vendor: str = "",
    sdk_version: str = "",
) -> Optional[str]:
    """Tier-1 sandbox-specific pre-fetch with the strict guardrails
    from docs/design/dag-pre-fetching.md. Returns a `<system_auto_prefetch>`
    XML block ready for injection, or None if no hit clears the bars.

    Never raises; DB / metric failures degrade to None."""
    if rc == 0:
        return None
    sig = extract_signature(error_log)
    if not sig:
        return None

    min_cos = _min_cosine()
    try:
        from backend import db
        hits_raw = await db.search_episodic_memory(
            sig, soc_vendor=soc_vendor, sdk_version=sdk_version,
            limit=_top_k() * 2, min_quality=min_cos,
        )
    except Exception as exc:
        logger.warning("rag_prefetch(sandbox): episodic search failed: %s", exc)
        _bump("search_error")
        return None

    hits: list[PrefetchHit] = []
    rejected_version = 0
    for r in hits_raw:
        try:
            q = float(r.get("quality_score") or 0.0)
        except (TypeError, ValueError):
            q = 0.0
        # SQL already filtered but the param is optional; belt-and-braces.
        if q < min_cos:
            continue
        hit_sdk = (r.get("sdk_version") or "").strip()
        if _version_hard_lock_rejects(hit_sdk, sdk_version):
            rejected_version += 1
            continue
        hits.append(PrefetchHit(
            memory_id=r.get("id") or "",
            error_signature=r.get("error_signature") or "",
            solution=r.get("solution") or "",
            quality_score=q,
            soc_vendor=r.get("soc_vendor") or "",
            sdk_version=hit_sdk,
        ))
        if len(hits) >= _top_k():
            break

    if rejected_version:
        _bump("version_mismatch")

    if not hits:
        _bump("below_cosine" if hits_raw else "no_hit")
        return None

    _bump("injected")
    await _touch_hits(hits)
    return format_sandbox_block(hits, max_tokens=_max_block_tokens())


def format_sandbox_block(
    hits: list[PrefetchHit], *,
    max_tokens: int = 1000,
    max_solution_chars: int = 800,
) -> str:
    """Render hits per docs/design/dag-pre-fetching.md §三 template:

        <system_auto_prefetch>
        💡 ...
          <past_solution>
            <bug_context>...</bug_context>
            <working_fix>...</working_fix>
          </past_solution>
        </system_auto_prefetch>

    Budget logic: greedy, quality-first. Stop adding solutions once
    the accumulated char count would exceed `max_tokens * 4` (the
    inverse of `_approx_tokens`). The block header + `💡` intro are
    always emitted even if only one solution fits."""
    HEADER = (
        "<system_auto_prefetch>\n"
        "💡 系統在 L3 經驗記憶庫中發現了與此錯誤高度相似的歷史解法，"
        "請優先參考以下經驗進行除錯：\n"
    )
    FOOTER = "</system_auto_prefetch>"
    char_budget = max_tokens * 4
    used = len(HEADER) + len(FOOTER)
    truncated = False

    sorted_hits = sorted(hits, key=lambda h: (-h.quality_score, h.memory_id))
    body_parts: list[str] = []
    for h in sorted_hits:
        bug_ctx = (h.error_signature or "(no signature)").strip()
        fix = (h.solution or "").strip()
        if len(fix) > max_solution_chars:
            fix = fix[:max_solution_chars] + "…[truncated]"
        # Attrs only if informative; stable order so the prompt cache
        # prefix is byte-identical across retries.
        attrs = f' id="{h.memory_id}" quality="{h.quality_score:.2f}"'
        if h.soc_vendor:
            attrs += f' soc="{h.soc_vendor}"'
        if h.sdk_version:
            attrs += f' sdk="{h.sdk_version}"'
        part = (
            f"  <past_solution{attrs}>\n"
            f"    <bug_context>{bug_ctx}</bug_context>\n"
            f"    <working_fix>{fix}</working_fix>\n"
            f"  </past_solution>\n"
        )
        # Over-budget check — but always include the first hit so the
        # caller never gets an empty block from a too-tight budget.
        if body_parts and used + len(part) > char_budget:
            truncated = True
            break
        body_parts.append(part)
        used += len(part)

    if truncated:
        opening = HEADER.replace(
            "<system_auto_prefetch>",
            '<system_auto_prefetch truncated="true">',
        )
        return opening + "".join(body_parts) + FOOTER
    return HEADER + "".join(body_parts) + FOOTER


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  prompt_cache integration helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def inject_into_builder(builder, block: str) -> None:
    """Append a prefetch block to a `CachedPromptBuilder` as a
    static_kb segment (cacheable). No-op when `block` is falsy."""
    if block:
        builder.add_static_kb(block)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Memory decay integration (Phase 67-E S4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _touch_hits(hits: list[PrefetchHit]) -> None:
    """Reset Phase 63-E decay clock for every solution that's about
    to be injected. Best-effort — decay is a background process; a
    failed touch just means the row will decay on its normal schedule
    instead of getting a fresh 90-day lease."""
    try:
        from backend import memory_decay as _md
    except Exception:
        return
    for h in hits:
        if not h.memory_id:
            continue
        try:
            await _md.touch(h.memory_id)
        except Exception as exc:
            logger.debug("memory_decay.touch(%s) failed: %s", h.memory_id, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _bump(result: str) -> None:
    try:
        from backend import metrics as _m
        _m.rag_prefetch_total.labels(result=result).inc()
    except Exception:
        pass
