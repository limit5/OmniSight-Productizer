"""OP-713 / OP-735 / OP-756 -- AI Reviewer for Gerrit patchsets.

This module owns two related but separate responsibilities:

1. **OP-713 Phase 2** (this layer): tiered model routing + LLM review +
   Code-Review +1 / 0 vote on every new patchset, plus the
   ``(change_id, revision)`` idempotency throttle. Public API:
   :func:`route_model`, :func:`is_too_large`, :func:`review_patchset`,
   :func:`should_skip_recent`, :func:`mark_reviewed`,
   :func:`too_large_message`, :class:`ReviewResult`.

2. **OP-713 Phase 2+ / OP-735 / OP-756**: trust-tier-driven action
   selection on top of (1). For each patchset that clears the LLM
   review and yields ``Severity.APPROVE``, the trust tier for the
   ``(bot, file_class)`` pair (see ``trust_scoring.trust_tier``) plus
   the OP-735 hard gates (mergeable, size <= 200, no safety-critical
   paths) determine which of four actions the reviewer takes:

     - AUTO_PLUS_ONE   — +1 + ``runner-batch-merge-candidate`` hashtag
     - GLANCE_PLUS_ONE — +1 + ``runner-glance-required`` hashtag
     - COMMENT_ONLY    — score 0, no hashtag (operator does normal +2)
     - SKIP            — AI Reviewer disabled for this pair

   See :func:`resolve_reviewer_action`. The +2 (Submit-blessing) is
   *never* given by the AI -- the operator still casts +2 from
   ``/admin/batch-merge``. CLAUDE.md L1 ("AI reviewer max +1, human
   +2") is preserved.

Cost-over-correctness (sora 2026-05-07)
---------------------------------------
The AI +1 is signal-only because the human +2 is the absolute hard
gate. That justifies aggressive cost optimisation on the routing tier:
default to haiku, escalate to sonnet for general backend, opus only on
high-risk paths. A noisy haiku review is fine; a runaway opus bill on
every doc-typo patchset is not.

Pure-data / I/O-injectable design
---------------------------------
``can_auto_plus_one``, :func:`route_model`, and :func:`is_too_large`
are pure functions. :func:`review_patchset` accepts injectable
``invoke`` and ``pricing`` callables so unit tests can stub the LLM
without touching the network. The webhook handler in
``backend/routers/webhooks.py`` is the only place that wires real I/O.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    """AI Reviewer Phase 1 verdict severity.

    APPROVE  -- no findings; eligible for auto-+1 if other gates pass.
    NITPICK  -- style-only; AI Reviewer still posts +1, but NOT auto-batch.
    NEEDS_WORK -- substantive concerns; AI Reviewer posts -1.
    REJECT   -- blocking issues; AI Reviewer posts -1 + comments.
    """

    APPROVE = "APPROVE"
    NITPICK = "NITPICK"
    NEEDS_WORK = "NEEDS_WORK"
    REJECT = "REJECT"


@dataclass(frozen=True)
class AIReviewVerdict:
    severity: Severity
    summary: str = ""
    finding_count: int = 0


@dataclass(frozen=True)
class ChangeFile:
    file: str
    insertions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class Change:
    """Snapshot of a Gerrit change + current patch set used by the gate.

    Field names mirror the spec in OP-735 / Gerrit REST so test fixtures
    line up with what the webhook handler actually has at hand.
    """

    id: str
    project: str = ""
    bot: str = ""
    mergeable: bool = False
    size_insertions: int = 0
    size_deletions: int = 0
    files: tuple[ChangeFile, ...] = field(default_factory=tuple)


# Spec lists alembic migrations + security configs + secret-handling
# files as off-limits for auto-+1. Prefixes are matched with
# ``str.startswith`` so any nested file under these roots is captured.
# Out-of-area domains for OP-735 (devops/db/security/embedded/tooling)
# all map cleanly to one of these prefixes -- the ticket's area gate
# and the safety gate share the same intent.
SAFETY_CRITICAL_PATHS: tuple[str, ...] = (
    "backend/alembic/",
    "backend/security/",
    "backend/secrets",
    "backend/auth",
    "deploy/",
    "configs/",
    ".gerrit/",
    "Dockerfile",
    "docker-compose",
)


_AUTO_PLUS_ONE_DIFF_LIMIT = 200


def _safety_critical_files(files: Iterable[ChangeFile]) -> list[str]:
    """Return the subset of ``files`` whose paths sit under a
    SAFETY_CRITICAL_PATHS prefix. Skips Gerrit's synthetic /COMMIT_MSG
    pseudo-file which is never part of the diff."""
    hits: list[str] = []
    for f in files:
        if f.file == "/COMMIT_MSG":
            continue
        if any(f.file.startswith(p) for p in SAFETY_CRITICAL_PATHS):
            hits.append(f.file)
    return hits


def can_auto_plus_one(
    change: Change,
    ai_verdict: AIReviewVerdict,
    *,
    diff_limit: int = _AUTO_PLUS_ONE_DIFF_LIMIT,
    trust_ok: bool = True,
) -> tuple[bool, str]:
    """Decide whether a patchset clears all 5 auto-+1 gates.

    Returns ``(ok, reason)``. On rejection ``ok=False`` and ``reason``
    is a short human-readable phrase suitable for the AI Reviewer
    comment (e.g. ``mergeable=false``, ``diff size 312 > 200``). On
    accept ``ok=True`` and ``reason="all-green"``.

    ``trust_ok`` is injected by the caller via ``trust_score_ok`` so
    this function stays free of I/O. When trust is degraded for the
    relevant ``(bot, file_class)`` tuple, callers pass ``trust_ok=False``
    and we reject with reason ``trust-degraded`` -- the patch falls
    back to AI Reviewer Phase-1 behaviour (no auto +1, just review).
    """
    if ai_verdict.severity != Severity.APPROVE:
        return False, f"AI verdict={ai_verdict.severity.value}"

    if not change.mergeable:
        return False, "mergeable=false"

    diff_total = change.size_insertions + change.size_deletions
    if diff_total > diff_limit:
        return False, f"diff size {diff_total} > {diff_limit}"

    safety_hits = _safety_critical_files(change.files)
    if safety_hits:
        # Truncate the listed files: the comment goes back to Gerrit
        # and humans only need a sample to grep for the prefix.
        return False, f"safety-critical files: {safety_hits[:2]}"

    if not trust_ok:
        return False, "trust-degraded"

    return True, "all-green"


# ── Phase 2+ post-decision tag/comment helpers ───────────────────────


BATCH_MERGE_HASHTAG = "runner-batch-merge-candidate"


def auto_plus_one_message(reason: str = "all-green") -> str:
    """Standard AI Reviewer comment posted with the auto-+1 vote.

    Kept short on purpose -- the dashboard surfaces the full audit
    trail; the Gerrit comment just needs to be greppable. The
    ``[AUTO-+1]`` prefix is what the dashboard's PG query filters on
    to enumerate batch candidates.
    """
    return (
        f"[AUTO-+1] AI Reviewer auto-approved this change ({reason}). "
        f"Tagged ``{BATCH_MERGE_HASHTAG}`` for operator batch +2 review. "
        "Human +2 still required for submit (CLAUDE.md L1)."
    )


def auto_plus_one_skip_message(reason: str) -> str:
    """Comment when AI Reviewer reaches APPROVE but a non-verdict gate
    rejected auto-+1 (size, safety path, trust). The patchset still
    gets the normal Phase-1 +1, but is NOT batch-tagged; the comment
    explains why so the operator knows where to look."""
    return (
        f"[AUTO-+1 skipped: {reason}] AI Reviewer approves this change "
        "but auto-batch is gated; please review and +2 manually."
    )


# ── OP-756 graceful-degradation tier resolver ────────────────────────


GLANCE_REQUIRED_HASHTAG = "runner-glance-required"


class ReviewerAction(str, Enum):
    """OP-756 outcome of the AI Reviewer's action selection.

    AUTO_PLUS_ONE   — post +1 + ``runner-batch-merge-candidate`` hashtag.
    GLANCE_PLUS_ONE — post +1 + ``runner-glance-required`` hashtag;
                      operator confirms with one-click bulk +2.
    COMMENT_ONLY    — post comment with score 0; operator does normal +2.
    SKIP            — AI Reviewer is muted for this (bot, file_class).
    """

    AUTO_PLUS_ONE = "auto_plus_one"
    GLANCE_PLUS_ONE = "glance_plus_one"
    COMMENT_ONLY = "comment_only"
    SKIP = "skip"


@dataclass(frozen=True)
class ReviewerDecision:
    """Bundle of (action, tier, reason, hashtag) returned by
    :func:`resolve_reviewer_action`. ``hashtag`` is empty for
    COMMENT_ONLY / SKIP.
    """

    action: ReviewerAction
    tier: str
    reason: str
    hashtag: str = ""


def resolve_reviewer_action(
    change: Change,
    ai_verdict: AIReviewVerdict,
    tier: Any,
    *,
    diff_limit: int = _AUTO_PLUS_ONE_DIFF_LIMIT,
) -> ReviewerDecision:
    """OP-756: pick the reviewer action for a patchset given its tier.

    Trust tier (from ``trust_scoring.trust_tier``) sets the *ceiling*
    on the action; the OP-735 hard gates (mergeable / diff size /
    safety paths / verdict) can downgrade further to COMMENT_ONLY.

    Tier ladder:

      DISABLED  → SKIP (no review, no comment).
      COMMENT   → COMMENT_ONLY (review + comment, score 0).
      GLANCE    → GLANCE_PLUS_ONE if hard gates pass, else COMMENT_ONLY.
      AUTO      → AUTO_PLUS_ONE  if hard gates pass, else COMMENT_ONLY.

    Pure function so the webhook handler can call it without I/O.
    ``tier`` is typed ``Any`` to avoid an import cycle with
    ``backend.agents.trust_scoring``; in practice the caller passes a
    ``TrustTier`` enum. Comparison is on the ``.value`` string.
    """
    tier_value = getattr(tier, "value", tier)

    if tier_value == "disabled":
        return ReviewerDecision(
            action=ReviewerAction.SKIP,
            tier=tier_value,
            reason="tier=disabled",
        )

    if ai_verdict.severity != Severity.APPROVE:
        # Even GLANCE / AUTO tier can't auto-vote when the LLM didn't
        # approve. Drop to comment-only so the operator drives.
        return ReviewerDecision(
            action=ReviewerAction.COMMENT_ONLY,
            tier=tier_value,
            reason=f"AI verdict={ai_verdict.severity.value}",
        )

    if tier_value == "comment":
        return ReviewerDecision(
            action=ReviewerAction.COMMENT_ONLY,
            tier=tier_value,
            reason="tier=comment",
        )

    # AUTO and GLANCE share the OP-735 hard gates.
    ok, gate_reason = can_auto_plus_one(
        change, ai_verdict, diff_limit=diff_limit, trust_ok=True,
    )
    if not ok:
        return ReviewerDecision(
            action=ReviewerAction.COMMENT_ONLY,
            tier=tier_value,
            reason=gate_reason,
        )

    if tier_value == "glance":
        return ReviewerDecision(
            action=ReviewerAction.GLANCE_PLUS_ONE,
            tier=tier_value,
            reason=gate_reason,
            hashtag=GLANCE_REQUIRED_HASHTAG,
        )
    return ReviewerDecision(
        action=ReviewerAction.AUTO_PLUS_ONE,
        tier=tier_value,
        reason=gate_reason,
        hashtag=BATCH_MERGE_HASHTAG,
    )


def glance_plus_one_message(reason: str = "all-green") -> str:
    """Standard AI Reviewer comment posted with the GLANCE-tier +1 vote.

    Mirrors :func:`auto_plus_one_message` but points at the
    glance-required hashtag so the dashboard can render glance-tier
    candidates in a separate column with a one-click bulk-+2 affordance.
    """
    return (
        f"[GLANCE-+1] AI Reviewer auto-approved this change ({reason}). "
        f"Tagged ``{GLANCE_REQUIRED_HASHTAG}`` for operator one-click "
        "confirmation. Human +2 still required for submit (CLAUDE.md L1)."
    )


def comment_only_message(reason: str = "comment-only") -> str:
    """Comment posted when the trust tier (or a hard-gate downgrade)
    puts this change in COMMENT-only mode -- AI offers a verdict but
    casts no +1, so operator runs a normal +2 review.
    """
    return (
        f"[COMMENT-ONLY] AI Reviewer is in comment-only mode for this "
        f"(bot, file_class) tier ({reason}). Operator should run a normal "
        "+2 review; no auto-vote was cast."
    )


# ── OP-713 Phase 2: tiered model routing + LLM review + throttle ─────


# Model tier IDs match the Anthropic provider naming used elsewhere in
# the backend (see ``backend/llm_adapter.py``). Keeping them as module
# constants makes the AC verification step ("footer shows
# model=claude-haiku-4-5") textually checkable in test output.
MODEL_HAIKU = "claude-haiku-4-5"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS = "claude-opus-4-7"


# Spec-listed HIGH-RISK paths that *always* route to opus regardless of
# diff size or other heuristics. Mix of prefixes (``alembic/versions/``)
# and exact file names (``CLAUDE.md``); the ``_path_is_high_risk``
# helper handles both. Adding a path here is a one-line change; please
# also extend ``docs/ops/ai_reviewer.md`` so the runbook stays in sync.
HIGH_RISK_PATH_PREFIXES: tuple[str, ...] = (
    "alembic/versions/",
    "backend/alembic/versions/",
    "security/",
    "backend/security/",
    "deploy/",
    ".gerrit/",
    "config/",
    "configs/",
)

HIGH_RISK_FILES: tuple[str, ...] = (
    "backend/submit_rule.py",
    "backend/merger_agent.py",
    "backend/merge_arbiter.py",
    "CLAUDE.md",
)

HIGH_RISK_SUFFIXES: tuple[str, ...] = (
    ".sql",
)


# Path prefixes that route to sonnet (mid-risk). Anything not matching
# high-risk *or* sonnet falls back to haiku (default / docs / scripts).
SONNET_PATH_PREFIXES: tuple[str, ...] = (
    "backend/",
    "frontend/",
    "lib/",
    "components/",
    "app/",
)


# Diff size cap (added + removed lines). Above this we skip the LLM
# review entirely and post a "too large" comment with score 0 — humans
# must deep-review oversized patches anyway, and the LLM's signal
# degrades sharply past ~1k LOC of diff.
DEFAULT_REVIEW_DIFF_LIMIT_LOC = 1500


def _path_is_high_risk(path: str) -> bool:
    """True if a file path matches any HIGH-RISK rule (prefix, exact, suffix)."""
    if not path or path == "/COMMIT_MSG":
        return False
    if path in HIGH_RISK_FILES:
        return True
    for prefix in HIGH_RISK_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    for suffix in HIGH_RISK_SUFFIXES:
        if path.endswith(suffix):
            return True
    return False


def _path_is_sonnet(path: str) -> bool:
    """True if a file path falls into the sonnet (mid-risk) tier."""
    if not path or path == "/COMMIT_MSG":
        return False
    return any(path.startswith(p) for p in SONNET_PATH_PREFIXES)


def route_model(
    diff: str = "",
    *,
    files: Iterable[str] = (),
) -> str:
    """Pick the model tier (haiku / sonnet / opus) for a patchset.

    The decision is path-based:

      * If *any* file matches HIGH-RISK rules → opus.
      * Else if *any* file is under a sonnet prefix → sonnet.
      * Else → haiku (the cheap baseline; docs, scripts, configs that
        don't sit under a high-risk root, etc.).

    *diff* is currently unused for routing but is part of the public
    signature per the OP-713 spec — future heuristics (e.g. heuristic
    LOC-based opus escalation) can drop in without breaking callers.
    The unused-arg pattern matches the spec:
    ``route_model(diff)`` from the ticket description.
    """
    file_list = [f for f in files if f and f != "/COMMIT_MSG"]
    if any(_path_is_high_risk(f) for f in file_list):
        return MODEL_OPUS
    if any(_path_is_sonnet(f) for f in file_list):
        return MODEL_SONNET
    return MODEL_HAIKU


def diff_loc(diff: str) -> int:
    """Count added + removed lines in a unified diff string.

    Counts lines beginning with ``+`` or ``-`` but not the ``+++ ``/
    ``--- `` file headers. Empty / non-diff inputs return 0.
    """
    if not diff:
        return 0
    n = 0
    for line in diff.splitlines():
        if not line:
            continue
        first = line[0]
        if first not in ("+", "-"):
            continue
        # Skip the file headers (``+++ b/foo.py`` / ``--- a/foo.py``)
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        n += 1
    return n


def is_too_large(
    *,
    diff: str = "",
    insertions: int | None = None,
    deletions: int | None = None,
    limit: int = DEFAULT_REVIEW_DIFF_LIMIT_LOC,
) -> bool:
    """Return True iff the diff is over the LLM review size cap.

    Caller can pass either a raw ``diff`` string (we count via
    :func:`diff_loc`) OR pre-counted ``insertions`` / ``deletions`` as
    reported by Gerrit's patch-set metadata. When both are supplied the
    pre-counted numbers win — they're cheaper and the Gerrit number is
    authoritative.
    """
    if insertions is not None or deletions is not None:
        total = (insertions or 0) + (deletions or 0)
    else:
        total = diff_loc(diff)
    return total > limit


def too_large_message(
    *,
    insertions: int | None = None,
    deletions: int | None = None,
    diff: str = "",
    limit: int = DEFAULT_REVIEW_DIFF_LIMIT_LOC,
    model_id: str = "",
) -> str:
    """Message posted when a patchset trips the size cap.

    Phrasing pinned by AC #4: ``too large for AI review, please ensure
    human deep-review``. The trailing footer convention keeps the comment
    grep-able by the cost dashboard."""
    if insertions is not None or deletions is not None:
        total = (insertions or 0) + (deletions or 0)
    else:
        total = diff_loc(diff)
    base = (
        "too large for AI review, please ensure human deep-review "
        f"(diff size {total} > {limit} LOC limit)."
    )
    return _with_footer(base, model_id=model_id, cost_usd=0.0)


# ── Throttle / idempotency ───────────────────────────────────────────


# Per-worker in-memory cache. Keyed by ``(change_id, revision)`` →
# epoch-seconds of last review. Multi-worker dedup is best-effort: the
# webhook plugin retries are usually re-delivered to the same worker
# inside the throttle window, and Gerrit itself merges duplicate label
# scores on the same revision (so worst-case duplicate posts are a
# benign double-comment, not a double-score). Move to Redis if the dup
# rate becomes operator-visible.
_THROTTLE: dict[tuple[str, str], float] = {}
DEFAULT_THROTTLE_TTL_S = 24 * 3600


def should_skip_recent(
    change_id: str,
    revision: str,
    *,
    now: float | None = None,
    ttl_s: float = DEFAULT_THROTTLE_TTL_S,
) -> bool:
    """True iff this ``(change_id, revision)`` was reviewed within ttl_s."""
    if not change_id or not revision:
        return False
    key = (change_id, revision)
    last = _THROTTLE.get(key)
    if last is None:
        return False
    current = now if now is not None else time.time()
    return (current - last) < ttl_s


def mark_reviewed(
    change_id: str,
    revision: str,
    *,
    now: float | None = None,
) -> None:
    """Record a review event in the throttle cache."""
    if not change_id or not revision:
        return
    _THROTTLE[(change_id, revision)] = (
        now if now is not None else time.time()
    )


def reset_throttle() -> None:
    """Clear the throttle cache. Test-only — production has no caller."""
    _THROTTLE.clear()


# ── Review result + LLM invocation ───────────────────────────────────


@dataclass(frozen=True)
class ReviewResult:
    """Outcome of one ``review_patchset`` call.

    ``score`` is the Code-Review label value to post: +1 for approve, 0
    for "uncertain / could not parse / size-cap skip" (the default
    OP-713 skip score). The AI never posts -1 or +2. ``message`` is the
    full Gerrit comment body, footer included. ``model_id`` /
    ``input_tokens`` / ``output_tokens`` / ``cost_usd`` feed the
    billing-event recorder.
    """

    score: int
    message: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped_reason: str = ""


# Footer convention pinned by the OP-713 spec ("every AI review comment
# ends with 'reviewed-by: <model_id> · cost: $X.XX' for observability").
# Kept as a module-level format string so cost-dashboard greps stay
# stable.
_FOOTER_FMT = "reviewed-by: {model_id} · cost: ${cost_usd:.2f}"


def _with_footer(message: str, *, model_id: str, cost_usd: float) -> str:
    if not model_id:
        return message
    footer = _FOOTER_FMT.format(model_id=model_id, cost_usd=cost_usd)
    return f"{message}\n\n{footer}"


def _estimate_tokens(text: str) -> int:
    """Rough char/4 fallback when the provider doesn't surface usage."""
    if not text:
        return 0
    return max(1, len(text) // 4)


_REVIEW_PROMPT_HEADER = (
    "You are an automated AI code reviewer for a Gerrit patchset. The "
    "human reviewer always casts the final +2; your role is signal "
    "only. Be brief: at most 8 lines, no preamble. Comment on bugs, "
    "missing tests, security risks, or contract violations. If the "
    "diff looks fine, reply with a single line approving the patch."
)


def _build_review_prompt(
    diff: str,
    *,
    files: Sequence[str] = (),
    subject: str = "",
) -> str:
    parts = [_REVIEW_PROMPT_HEADER]
    if subject:
        parts.append(f"\nChange subject: {subject}")
    if files:
        joined = ", ".join(files[:20])
        parts.append(f"\nFiles touched ({len(files)}): {joined}")
    if diff:
        parts.append("\nUnified diff:\n" + diff)
    return "\n".join(parts)


def review_patchset(
    diff: str,
    model: str = "",
    *,
    files: Sequence[str] = (),
    subject: str = "",
    insertions: int | None = None,
    deletions: int | None = None,
    diff_limit: int = DEFAULT_REVIEW_DIFF_LIMIT_LOC,
    invoke: Callable[..., str] | None = None,
    pricing: Callable[[str | None, str], tuple[float, float]] | None = None,
) -> ReviewResult:
    """Run one LLM-backed review pass over a diff.

    *diff* is the unified-diff text. *model* is the explicit model tier
    (defaults to :func:`route_model` over *files*). *files* /
    *subject* are wired into the prompt for context. *insertions* /
    *deletions* let the caller use Gerrit's authoritative LOC counts
    when available.

    Returns a :class:`ReviewResult` with score +1 on success or 0 on
    over-cap / empty / parse-failed. The comment body always carries
    the ``reviewed-by: <model> · cost: $X.XX`` footer.

    *invoke* / *pricing* are dependency-injection seams for tests:

      * ``invoke(messages, *, model)`` returning the reply string.
        Default: ``backend.llm_adapter.invoke_chat`` with
        ``provider="anthropic"``.
      * ``pricing(provider, model)`` returning ``(input_per_mtok,
        output_per_mtok)``. Default: ``backend.pricing.get_pricing``.
    """
    chosen_model = (model or route_model(diff, files=files)).strip()

    # Size-cap fast path — no LLM call.
    if is_too_large(
        diff=diff,
        insertions=insertions,
        deletions=deletions,
        limit=diff_limit,
    ):
        message = too_large_message(
            insertions=insertions,
            deletions=deletions,
            diff=diff,
            limit=diff_limit,
            model_id=chosen_model,
        )
        return ReviewResult(
            score=0,
            message=message,
            model_id=chosen_model,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            skipped_reason="too_large",
        )

    prompt = _build_review_prompt(diff, files=tuple(files), subject=subject)

    if invoke is None:
        from backend.llm_adapter import invoke_chat as _invoke_chat
        from langchain_core.messages import HumanMessage

        def _default_invoke(messages: Any, *, model: str) -> str:
            return _invoke_chat(
                [HumanMessage(content=messages)] if isinstance(messages, str)
                else messages,
                provider="anthropic",
                model=model,
            )
        invoke = _default_invoke

    try:
        reply_text = invoke(prompt, model=chosen_model) or ""
    except Exception as exc:
        logger.warning(
            "ai_reviewer.review_patchset: LLM invocation failed: %s",
            exc,
        )
        return ReviewResult(
            score=0,
            message=_with_footer(
                "AI review skipped — LLM provider error. "
                "Please review manually.",
                model_id=chosen_model,
                cost_usd=0.0,
            ),
            model_id=chosen_model,
            skipped_reason="invoke_error",
        )

    reply = (reply_text or "").strip()
    if not reply:
        return ReviewResult(
            score=0,
            message=_with_footer(
                "AI review produced no output. Please review manually.",
                model_id=chosen_model,
                cost_usd=0.0,
            ),
            model_id=chosen_model,
            skipped_reason="empty_reply",
        )

    # Token + cost accounting. The adapter doesn't surface usage so we
    # fall back to a char/4 estimate. Pricing rates come from the
    # central pricing table so a YAML update is reflected here without
    # code changes (matches the rest of the backend's billing path).
    if pricing is None:
        from backend.pricing import get_pricing as _get_pricing
        pricing = _get_pricing
    input_per_mtok, output_per_mtok = pricing("anthropic", chosen_model)
    input_tokens = _estimate_tokens(prompt)
    output_tokens = _estimate_tokens(reply)
    cost_usd = round(
        input_tokens * input_per_mtok / 1_000_000.0
        + output_tokens * output_per_mtok / 1_000_000.0,
        6,
    )

    return ReviewResult(
        score=_score_from_reply(reply),
        message=_with_footer(reply, model_id=chosen_model, cost_usd=cost_usd),
        model_id=chosen_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


_REJECT_RX = re.compile(
    r"\b(REJECT|NEEDS[ _-]?WORK|BLOCKER|do not merge|cannot approve)\b",
    re.IGNORECASE,
)


def _score_from_reply(reply: str) -> int:
    """Map an LLM reply to a Code-Review score.

    Per CLAUDE.md L1 the AI's max signal is +1; we never escalate to
    +2. We return 0 (not -1) when the model's verdict is uncertain or
    we can't parse it — that lets the human reviewer drive the
    decision without an AI veto blocking submission via the No-Veto
    submit-requirement.

    OP-713 is intentionally permissive: cost-over-correctness, +1 is
    signal-only. We only downgrade to 0 if the reply explicitly says
    "REJECT" or equivalent. Future tickets may extend this to a -1
    path if dashboards show we're rubber-stamping bad changes.
    """
    if _REJECT_RX.search(reply):
        return 0
    return 1


__all__ = [
    "AIReviewVerdict",
    "BATCH_MERGE_HASHTAG",
    "Change",
    "ChangeFile",
    "DEFAULT_REVIEW_DIFF_LIMIT_LOC",
    "DEFAULT_THROTTLE_TTL_S",
    "GLANCE_REQUIRED_HASHTAG",
    "HIGH_RISK_FILES",
    "HIGH_RISK_PATH_PREFIXES",
    "HIGH_RISK_SUFFIXES",
    "MODEL_HAIKU",
    "MODEL_OPUS",
    "MODEL_SONNET",
    "ReviewResult",
    "ReviewerAction",
    "ReviewerDecision",
    "SAFETY_CRITICAL_PATHS",
    "Severity",
    "SONNET_PATH_PREFIXES",
    "auto_plus_one_message",
    "auto_plus_one_skip_message",
    "can_auto_plus_one",
    "comment_only_message",
    "diff_loc",
    "glance_plus_one_message",
    "is_too_large",
    "mark_reviewed",
    "reset_throttle",
    "resolve_reviewer_action",
    "review_patchset",
    "route_model",
    "should_skip_recent",
    "too_large_message",
]
