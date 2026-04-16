"""O7 (#270) — Submit-rule Evaluator (Gerrit dual-+2 SSOT in Python).

Why a Python mirror of ``rules.pl``?
------------------------------------

The Gerrit Prolog ``submit_rule`` in ``.gerrit/rules.pl`` is the
*authoritative* gate at Gerrit-submit time.  This module is a
**semantically equivalent Python evaluator** used by:

  * Orchestrator / Merge Arbiter decision code — "is this change ready
    to merge?" answered without shelling out to Gerrit.
  * Unit tests — the project's submit-rule test matrix exercises the
    same evaluator the Merger agent consults in production.
  * GitHub Actions fallback (``.github/workflows/merge-arbiter.yml``) —
    clients without Gerrit get the same policy mirrored onto PR
    reviewers (``merger-agent-bot`` GitHub App + human approvers).

Policy (matches CLAUDE.md L1 Safety Rules + .gerrit/rules.pl):

  A change is submittable **iff**:

    (1) There is at least one ``Code-Review: +2`` from an account in
        the ``non-ai-reviewer`` group (the HUMAN hard gate).  No AI
        combination can substitute — this is the absolute rule.

    (2) There is at least one ``Code-Review: +2`` from an account in
        the ``merger-agent-bot`` group (the Merger has signed the
        conflict-block correctness).

    (3) No reviewer has cast ``Code-Review: -1`` or ``-2``.

    (4) AI +2 votes — from ``merger-agent-bot`` OR ``ai-reviewer-bots``
        (lint-bot / security-bot / future AI reviewers) — are *tracked*
        and can lift the overall grade, **but they cannot replace or
        bypass the human hard gate.**  That is the core property the
        test matrix pins down: no N × AI-+2 ever satisfies rule (1).

Return shape
------------

``evaluate_submit_rule`` returns a :class:`SubmitDecision` with:

  * ``allow`` — True iff all of (1), (2), (3) hold.
  * ``reason`` — stable enum code (serializable, audited).
  * ``missing`` — list of missing conditions (``"human_plus_two"``,
    ``"merger_plus_two"``, …).
  * ``detail`` — human-readable paragraph (for Gerrit comment bodies +
    Slack / email notifications).
  * ``ai_plus_twos`` / ``human_plus_twos`` / ``negative_votes`` — vote
    counts for observability.

The shape is stable; downstream systems pin it in tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Group names — match .gerrit/rules.pl / project.config.example
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GROUP_HUMAN = "non-ai-reviewer"          # HUMAN hard gate
GROUP_AI_BOTS = "ai-reviewer-bots"       # umbrella for all AI reviewers
GROUP_MERGER = "merger-agent-bot"        # Merger sub-group (must also be in AI bots)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SubmitReason(str, Enum):
    """Stable reason codes for submit-rule outcomes."""

    allow = "allow"
    reject_missing_human_plus_two = "reject_missing_human_plus_two"
    reject_missing_merger_plus_two = "reject_missing_merger_plus_two"
    reject_missing_both = "reject_missing_human_and_merger_plus_two"
    reject_negative_vote = "reject_negative_vote"


@dataclass(frozen=True)
class ReviewerVote:
    """One Code-Review vote."""

    voter: str                          # account id / email
    groups: frozenset[str]              # group memberships
    score: int                          # -2, -1, 0, +1, +2

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewerVote":
        return cls(
            voter=str(d.get("voter", "")),
            groups=frozenset(d.get("groups", ()) or ()),
            score=int(d.get("score", 0)),
        )

    def is_human(self) -> bool:
        return GROUP_HUMAN in self.groups and not self.is_ai()

    def is_ai(self) -> bool:
        return GROUP_AI_BOTS in self.groups

    def is_merger(self) -> bool:
        return GROUP_MERGER in self.groups

    def is_plus_two(self) -> bool:
        return self.score >= 2

    def is_negative(self) -> bool:
        return self.score < 0


@dataclass
class SubmitDecision:
    """Result of evaluating the dual-+2 rule over a vote list."""

    allow: bool
    reason: SubmitReason
    detail: str = ""
    missing: list[str] = field(default_factory=list)
    human_plus_twos: int = 0
    merger_plus_twos: int = 0
    ai_plus_twos: int = 0               # includes merger
    negative_votes: int = 0
    negative_voters: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "reason": self.reason.value,
            "detail": self.detail,
            "missing": list(self.missing),
            "human_plus_twos": self.human_plus_twos,
            "merger_plus_twos": self.merger_plus_twos,
            "ai_plus_twos": self.ai_plus_twos,
            "negative_votes": self.negative_votes,
            "negative_voters": list(self.negative_voters),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core evaluator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def evaluate_submit_rule(
    votes: Iterable[ReviewerVote | dict],
) -> SubmitDecision:
    """Evaluate the dual-+2 rule over ``votes`` and return a
    :class:`SubmitDecision`.

    Accepts either :class:`ReviewerVote` instances or plain dicts (for
    JSON callers).  Never raises.
    """
    normalized: list[ReviewerVote] = []
    for v in votes:
        if isinstance(v, ReviewerVote):
            normalized.append(v)
        elif isinstance(v, dict):
            try:
                normalized.append(ReviewerVote.from_dict(v))
            except Exception as exc:                    # pragma: no cover
                logger.debug("submit_rule: skipping malformed vote: %s", exc)
        else:
            logger.debug("submit_rule: ignoring vote of type %r", type(v))

    # Tally.
    human_plus_twos = sum(
        1 for v in normalized if v.is_plus_two() and v.is_human()
    )
    merger_plus_twos = sum(
        1 for v in normalized if v.is_plus_two() and v.is_merger()
    )
    ai_plus_twos = sum(
        1 for v in normalized if v.is_plus_two() and v.is_ai()
    )
    negative = [v for v in normalized if v.is_negative()]
    negative_votes = len(negative)
    negative_voters = [v.voter for v in negative]

    # ── (3) Negative vote kill-switch ────────────────────────────
    if negative_votes > 0:
        return SubmitDecision(
            allow=False,
            reason=SubmitReason.reject_negative_vote,
            detail=(
                f"{negative_votes} reviewer(s) cast a negative Code-Review "
                f"score; submission blocked. Negative voters: "
                f"{', '.join(negative_voters)}"
            ),
            missing=["negative_vote_clear"],
            human_plus_twos=human_plus_twos,
            merger_plus_twos=merger_plus_twos,
            ai_plus_twos=ai_plus_twos,
            negative_votes=negative_votes,
            negative_voters=negative_voters,
        )

    # ── (1) HUMAN +2 hard gate ───────────────────────────────────
    # ── (2) Merger +2 ────────────────────────────────────────────
    missing: list[str] = []
    if human_plus_twos < 1:
        missing.append("human_plus_two")
    if merger_plus_twos < 1:
        missing.append("merger_plus_two")

    if missing:
        if len(missing) == 2:
            reason = SubmitReason.reject_missing_both
            detail = (
                "Submission blocked: dual-+2 rule requires BOTH "
                "(a) a Code-Review: +2 from the non-ai-reviewer (human) group "
                "AND (b) a Code-Review: +2 from merger-agent-bot. Neither is "
                f"present yet. AI +2 votes tallied: {ai_plus_twos} (these "
                "do NOT substitute for the human hard gate)."
            )
        elif "human_plus_two" in missing:
            reason = SubmitReason.reject_missing_human_plus_two
            detail = (
                "Submission blocked: HUMAN +2 is the hard gate. "
                f"{ai_plus_twos} AI +2 vote(s) are present but no AI "
                "combination can substitute for a non-ai-reviewer approval. "
                "Waiting on a reviewer from the `non-ai-reviewer` group."
            )
        else:
            reason = SubmitReason.reject_missing_merger_plus_two
            detail = (
                "Submission blocked: the Merger Agent must co-sign the "
                "conflict block. Trigger the merger agent or assign an "
                "alternate member of `merger-agent-bot` to re-evaluate."
            )
        return SubmitDecision(
            allow=False,
            reason=reason,
            detail=detail,
            missing=missing,
            human_plus_twos=human_plus_twos,
            merger_plus_twos=merger_plus_twos,
            ai_plus_twos=ai_plus_twos,
            negative_votes=negative_votes,
            negative_voters=negative_voters,
        )

    # ── All gates satisfied ──────────────────────────────────────
    return SubmitDecision(
        allow=True,
        reason=SubmitReason.allow,
        detail=(
            f"Submit allowed: {human_plus_twos} human +2, "
            f"{merger_plus_twos} merger +2, "
            f"{ai_plus_twos} total AI +2, no negative votes."
        ),
        human_plus_twos=human_plus_twos,
        merger_plus_twos=merger_plus_twos,
        ai_plus_twos=ai_plus_twos,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers for constructing vote fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def human_vote(voter: str, score: int = 2) -> ReviewerVote:
    return ReviewerVote(
        voter=voter,
        groups=frozenset({GROUP_HUMAN}),
        score=score,
    )


def merger_vote(voter: str = "merger-agent-bot", score: int = 2) -> ReviewerVote:
    return ReviewerVote(
        voter=voter,
        groups=frozenset({GROUP_AI_BOTS, GROUP_MERGER}),
        score=score,
    )


def ai_bot_vote(voter: str, score: int = 2) -> ReviewerVote:
    """AI reviewer that is NOT the merger (lint-bot / security-bot / …)."""
    return ReviewerVote(
        voter=voter,
        groups=frozenset({GROUP_AI_BOTS}),
        score=score,
    )


__all__ = [
    "GROUP_AI_BOTS",
    "GROUP_HUMAN",
    "GROUP_MERGER",
    "ReviewerVote",
    "SubmitDecision",
    "SubmitReason",
    "ai_bot_vote",
    "evaluate_submit_rule",
    "human_vote",
    "merger_vote",
]
