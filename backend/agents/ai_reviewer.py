"""OP-713 Phase 2+ / OP-735 -- AI Reviewer auto-+1 gating.

OP-713 Phase 1 ships an AI Reviewer agent that can post Code-Review +1
or -1 on a Gerrit patchset. R5 (this module) adds a *confidence-gated*
auto-+1 path on top of that: a patchset is auto-tagged
``runner-batch-merge-candidate`` and auto-+1'd by the AI Reviewer only
when ALL of the following are green:

  1. ``ai_verdict.severity == APPROVE`` (no findings from Phase 1).
  2. Gerrit reports the change as ``mergeable=True``.
  3. ``insertions + deletions <= 200`` lines.
  4. No file in the patch sits under a ``SAFETY_CRITICAL_PATHS`` prefix
     (alembic migrations, security configs, secret-handling code, …).
  5. Trust score for this ``(bot, file_class)`` tuple is still healthy
     (delegated to ``backend.agents.trust_scoring``).

The +2 (Submit-blessing) is *never* given by the AI -- the operator
still bulk-+2's selected candidates from the ``/admin/batch-merge``
dashboard. CLAUDE.md L1 invariant ("AI reviewer max +1, human +2
required for merge") is preserved.

Pure-data design
----------------
``can_auto_plus_one`` is a pure function over already-resolved
arguments: it does NOT call Gerrit, the DB, or the network. The caller
(webhook handler in ``backend/routers/webhooks.py`` or the dashboard
sweep in ``backend/routers/batch_merge.py``) is responsible for
populating the ``Change`` and ``AIReviewVerdict`` snapshots. This keeps
the gate trivially unit-testable without DB or SSH fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


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
