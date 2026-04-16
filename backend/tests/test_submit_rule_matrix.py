"""O7 (#270) — Submit-rule test matrix.

Pins the dual-+2 policy.  Whatever changes in ``.gerrit/rules.pl`` or
``backend/submit_rule.py``, these cases MUST keep the same answers —
they are the contract with CLAUDE.md L1 Safety Rules.

Spec-mandated matrix (TODO.md → O7 → Submit-rule 測試矩陣):

  - Merger +2 only (no human)                            → reject
  - Human +2 only (no merger)                            → reject
  - Merger +2 + human +2                                 → allow
  - Merger +2 + human -1                                 → reject
  - N × AI +2 (merger + lint-bot + security-bot + …)     → reject
    (the core "no AI combo can substitute for human" test)
  - N × AI +2 + human +2                                 → allow
    (AI +2 is additive — cannot replace the human gate)
"""

from __future__ import annotations

import pytest

from backend.submit_rule import (
    GROUP_AI_BOTS,
    GROUP_HUMAN,
    GROUP_MERGER,
    ReviewerVote,
    SubmitReason,
    ai_bot_vote,
    evaluate_submit_rule,
    human_vote,
    merger_vote,
)


# ──────────────────────────────────────────────────────────────
#  Primary rejection / acceptance matrix
# ──────────────────────────────────────────────────────────────


def test_merger_plus_two_alone_rejects():
    decision = evaluate_submit_rule([merger_vote()])
    assert decision.allow is False
    assert decision.reason is SubmitReason.reject_missing_human_plus_two
    assert "human_plus_two" in decision.missing
    assert decision.merger_plus_twos == 1
    assert decision.human_plus_twos == 0


def test_human_plus_two_alone_rejects():
    decision = evaluate_submit_rule([human_vote("alice@example.com")])
    assert decision.allow is False
    assert decision.reason is SubmitReason.reject_missing_merger_plus_two
    assert "merger_plus_two" in decision.missing
    assert decision.human_plus_twos == 1
    assert decision.merger_plus_twos == 0


def test_merger_plus_two_plus_human_plus_two_allows():
    decision = evaluate_submit_rule([
        merger_vote(),
        human_vote("alice@example.com"),
    ])
    assert decision.allow is True
    assert decision.reason is SubmitReason.allow
    assert decision.missing == []
    assert decision.human_plus_twos == 1
    assert decision.merger_plus_twos == 1


def test_merger_plus_two_plus_human_minus_one_rejects():
    decision = evaluate_submit_rule([
        merger_vote(),
        human_vote("alice@example.com", score=-1),
    ])
    assert decision.allow is False
    assert decision.reason is SubmitReason.reject_negative_vote
    assert decision.negative_votes == 1
    assert "alice@example.com" in decision.negative_voters


# ──────────────────────────────────────────────────────────────
#  N × AI +2 — the core "AI combo can't substitute" test
# ──────────────────────────────────────────────────────────────


def test_n_ai_plus_twos_without_human_rejects():
    """Core test: six AI +2 votes (merger + 5 bots) must not satisfy
    the rule when a human has not voted +2."""
    ai_bots = [
        merger_vote(),
        ai_bot_vote("lint-bot"),
        ai_bot_vote("security-bot"),
        ai_bot_vote("perf-bot"),
        ai_bot_vote("doc-bot"),
        ai_bot_vote("style-bot"),
    ]
    decision = evaluate_submit_rule(ai_bots)
    assert decision.allow is False
    assert decision.reason is SubmitReason.reject_missing_human_plus_two
    # The AI count is tracked — observability must reflect reality.
    assert decision.ai_plus_twos == 6
    assert decision.human_plus_twos == 0
    # Sanity: the detail message explicitly calls out the rule.
    assert "human hard gate" in decision.detail.lower() \
        or "non-ai-reviewer" in decision.detail.lower()


def test_n_ai_plus_twos_plus_human_plus_two_allows():
    """AI +2s are additive — when a human also signs off, we allow."""
    votes = [
        merger_vote(),
        ai_bot_vote("lint-bot"),
        ai_bot_vote("security-bot"),
        human_vote("alice@example.com"),
    ]
    decision = evaluate_submit_rule(votes)
    assert decision.allow is True
    assert decision.reason is SubmitReason.allow
    assert decision.ai_plus_twos == 3
    assert decision.merger_plus_twos == 1
    assert decision.human_plus_twos == 1


def test_many_ai_plus_twos_and_human_minus_two_still_rejects():
    votes = [
        merger_vote(),
        ai_bot_vote("lint-bot"),
        ai_bot_vote("security-bot"),
        ai_bot_vote("perf-bot"),
        human_vote("alice@example.com", score=-2),
    ]
    decision = evaluate_submit_rule(votes)
    assert decision.allow is False
    assert decision.reason is SubmitReason.reject_negative_vote


# ──────────────────────────────────────────────────────────────
#  Edge cases
# ──────────────────────────────────────────────────────────────


def test_empty_vote_list_rejects_with_both_missing():
    decision = evaluate_submit_rule([])
    assert decision.allow is False
    assert decision.reason is SubmitReason.reject_missing_both
    assert "human_plus_two" in decision.missing
    assert "merger_plus_two" in decision.missing


def test_only_plus_ones_do_not_satisfy():
    decision = evaluate_submit_rule([
        human_vote("alice@example.com", score=1),
        merger_vote(score=1),
    ])
    assert decision.allow is False
    assert decision.reason is SubmitReason.reject_missing_both


def test_two_humans_plus_two_without_merger_rejects():
    decision = evaluate_submit_rule([
        human_vote("alice@example.com"),
        human_vote("bob@example.com"),
    ])
    assert decision.allow is False
    assert decision.reason is SubmitReason.reject_missing_merger_plus_two
    assert decision.human_plus_twos == 2


def test_dict_votes_are_accepted():
    decision = evaluate_submit_rule([
        {"voter": "merger-agent-bot",
         "groups": [GROUP_AI_BOTS, GROUP_MERGER],
         "score": 2},
        {"voter": "alice@example.com",
         "groups": [GROUP_HUMAN],
         "score": 2},
    ])
    assert decision.allow is True


def test_merger_in_both_groups_counts_as_ai_only_when_not_human():
    """A merger bot account must not accidentally count as a human even
    if someone mis-configures its groups."""
    bad = ReviewerVote(
        voter="merger-agent-bot",
        groups=frozenset({GROUP_AI_BOTS, GROUP_MERGER, GROUP_HUMAN}),
        score=2,
    )
    decision = evaluate_submit_rule([bad])
    # The ReviewerVote.is_human() guard short-circuits on AI group.
    assert decision.allow is False
    assert decision.human_plus_twos == 0, (
        "Merger bot in `non-ai-reviewer` must NEVER count toward the "
        "human gate; admin configuration must exclude bots from the "
        "human group, but the evaluator also hard-guards this."
    )


def test_negative_detail_lists_voters():
    decision = evaluate_submit_rule([
        human_vote("alice@example.com"),
        merger_vote(),
        human_vote("bob@example.com", score=-1),
    ])
    assert decision.allow is False
    assert "bob@example.com" in decision.detail


def test_to_dict_roundtrip_is_stable():
    decision = evaluate_submit_rule([merger_vote(), human_vote("alice@x")])
    d = decision.to_dict()
    assert d["allow"] is True
    assert d["reason"] == "allow"
    assert d["human_plus_twos"] == 1
    assert d["merger_plus_twos"] == 1


# ──────────────────────────────────────────────────────────────
#  Defensive: malformed votes are ignored, not crashed on
# ──────────────────────────────────────────────────────────────


def test_unknown_object_types_are_skipped():
    decision = evaluate_submit_rule([
        merger_vote(),
        human_vote("alice@x"),
        "not-a-vote",
        42,
        None,
    ])
    assert decision.allow is True


def test_dict_with_bad_types_is_skipped_gracefully():
    # Bad score type — should be treated as 0 (skipped), not raised.
    votes = [
        merger_vote(),
        human_vote("alice@x"),
        {"voter": "typo-bot", "groups": ["ai-reviewer-bots"], "score": "two"},
    ]
    decision = evaluate_submit_rule(votes)
    # "two" → ValueError in int() inside from_dict → the bad vote is
    # skipped entirely (submit_rule.from_dict catches via the outer
    # try/except in evaluate_submit_rule).
    assert decision.allow is True
