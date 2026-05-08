"""OP-735 R5 / OP-756 -- ai_reviewer + trust_scoring gate + tier tests.

These tests pin the 5-gate decision table from OP-735 plus the
4-tier graceful-degradation ladder added in OP-756. They're pure-data
unit tests (no PG, no SSH) so they exercise the gate quickly enough to
live in the default backend test suite.

The synthetic 10-PS scenario near the bottom is the OP-735 AC sample:
8 small docs PSes auto-tag, 2 large refactors don't (size gate). The
OP-756 section below pins the tier ladder, operator pin override,
and recovery semantics.
"""

from __future__ import annotations

import pytest

from backend.agents.ai_reviewer import (
    BATCH_MERGE_HASHTAG,
    GLANCE_REQUIRED_HASHTAG,
    AIReviewVerdict,
    Change,
    ChangeFile,
    ReviewerAction,
    Severity,
    auto_plus_one_message,
    auto_plus_one_skip_message,
    can_auto_plus_one,
    comment_only_message,
    glance_plus_one_message,
    resolve_reviewer_action,
)
from backend.agents.trust_scoring import (
    InMemoryTrustStore,
    TIER_PIN_HASHTAG_PREFIX,
    TIER_THRESHOLD_AUTO,
    TIER_THRESHOLD_COMMENT,
    TIER_THRESHOLD_GLANCE,
    TRUST_SCORE_RAMP_UP_OBSERVATIONS,
    TrustScore,
    TrustTier,
    classify_file_class,
    dominant_file_class,
    parse_pinned_tier,
    trust_score_ok,
    trust_score_value,
    trust_tier,
)


# ── Verdict-severity gate ─────────────────────────────────────────────


def _approve_verdict() -> AIReviewVerdict:
    return AIReviewVerdict(severity=Severity.APPROVE, summary="LGTM")


def _green_change() -> Change:
    return Change(
        id="I0123",
        project="omnisight",
        bot="claude-bot",
        mergeable=True,
        size_insertions=20,
        size_deletions=5,
        files=(
            ChangeFile(file="docs/howto.md", insertions=20, deletions=5),
        ),
    )


def test_gate_accepts_all_green_change():
    ok, reason = can_auto_plus_one(_green_change(), _approve_verdict())
    assert ok is True
    assert reason == "all-green"


@pytest.mark.parametrize(
    "severity,reason_substr",
    [
        (Severity.NITPICK, "AI verdict=NITPICK"),
        (Severity.NEEDS_WORK, "AI verdict=NEEDS_WORK"),
        (Severity.REJECT, "AI verdict=REJECT"),
    ],
)
def test_gate_rejects_non_approve_verdicts(severity, reason_substr):
    ok, reason = can_auto_plus_one(
        _green_change(), AIReviewVerdict(severity=severity)
    )
    assert ok is False
    assert reason_substr in reason


def test_gate_rejects_unmergeable_change():
    change = Change(
        id="I0124", project="x", bot="claude-bot",
        mergeable=False, size_insertions=10, size_deletions=0,
        files=(ChangeFile("docs/x.md"),),
    )
    ok, reason = can_auto_plus_one(change, _approve_verdict())
    assert ok is False
    assert reason == "mergeable=false"


def test_gate_rejects_oversize_diff():
    change = Change(
        id="I0125", project="x", bot="claude-bot",
        mergeable=True,
        size_insertions=180, size_deletions=30,  # 210 total
        files=(ChangeFile("docs/x.md"),),
    )
    ok, reason = can_auto_plus_one(change, _approve_verdict())
    assert ok is False
    assert "diff size 210 > 200" in reason


def test_gate_rejects_safety_critical_files():
    change = Change(
        id="I0126", project="x", bot="claude-bot", mergeable=True,
        size_insertions=10, size_deletions=2,
        files=(
            ChangeFile("docs/notes.md"),
            ChangeFile("backend/alembic/versions/0203_test.py"),
        ),
    )
    ok, reason = can_auto_plus_one(change, _approve_verdict())
    assert ok is False
    assert "safety-critical" in reason
    assert "backend/alembic" in reason


def test_gate_skips_commit_msg_pseudo_file():
    # /COMMIT_MSG appears in Gerrit's file list and must never trigger
    # safety-critical gating since it isn't a real file path.
    change = Change(
        id="I0127", project="x", bot="claude-bot", mergeable=True,
        size_insertions=10, size_deletions=2,
        files=(
            ChangeFile("/COMMIT_MSG"),
            ChangeFile("docs/notes.md"),
        ),
    )
    ok, reason = can_auto_plus_one(change, _approve_verdict())
    assert ok is True
    assert reason == "all-green"


def test_gate_rejects_when_trust_degraded():
    # Even with all 4 hard gates passing, a degraded trust score
    # falls back to Phase-1 behaviour (no auto-+1).
    ok, reason = can_auto_plus_one(
        _green_change(), _approve_verdict(), trust_ok=False,
    )
    assert ok is False
    assert reason == "trust-degraded"


def test_gate_diff_limit_is_inclusive():
    # The spec uses "< 200 lines" but we treat the 200 boundary as the
    # last allowed value (inclusive) so a 200-line diff still passes.
    change = Change(
        id="I0128", project="x", bot="claude-bot", mergeable=True,
        size_insertions=199, size_deletions=1,  # exactly 200
        files=(ChangeFile("docs/x.md"),),
    )
    ok, reason = can_auto_plus_one(change, _approve_verdict())
    assert ok is True, f"expected pass at boundary, got {reason}"


# ── Comment helpers ──────────────────────────────────────────────────


def test_auto_plus_one_message_mentions_hashtag_and_human_plus_two():
    msg = auto_plus_one_message("all-green")
    assert BATCH_MERGE_HASHTAG in msg
    assert "+2" in msg  # Reminds reader that +2 still requires human


def test_auto_plus_one_skip_message_includes_reason():
    msg = auto_plus_one_skip_message("diff size 312 > 200")
    assert "AUTO-+1 skipped" in msg
    assert "312" in msg


# ── TrustScore threshold logic ───────────────────────────────────────


def test_trust_score_default_during_ramp_up():
    # Empty score: default trust → True (0 observations)
    assert trust_score_ok(TrustScore(bot="claude-bot", file_class="docs"))


def test_trust_score_below_ramp_up_threshold_still_ok():
    # 4 successes + 0 failures < 5 observations → default trust True
    assert TRUST_SCORE_RAMP_UP_OBSERVATIONS == 5
    score = TrustScore(
        bot="claude-bot", file_class="docs", successes=4, failures=0,
    )
    assert trust_score_ok(score)


def test_trust_score_above_threshold_with_low_failure_rate():
    # 19 ok / 20 total → success_rate 0.95 → AUTO tier → trust_score_ok
    score = TrustScore(
        bot="claude-bot", file_class="docs", successes=19, failures=1,
    )
    assert score.failure_rate == pytest.approx(0.05)
    assert trust_score_value(score) == pytest.approx(0.95)
    assert trust_score_ok(score)


def test_trust_score_above_threshold_with_high_failure_rate():
    # 17 ok / 20 total → success_rate 0.85 → GLANCE tier → not AUTO
    score = TrustScore(
        bot="claude-bot", file_class="docs", successes=17, failures=3,
    )
    assert trust_score_value(score) == pytest.approx(0.85)
    assert not trust_score_ok(score)
    assert trust_tier(score) == TrustTier.GLANCE


def test_classify_file_class_known_prefixes():
    assert classify_file_class("docs/foo/bar.md") == "docs"
    assert classify_file_class("backend/agents/ai_reviewer.py") == "backend-agents"
    assert classify_file_class("backend/routers/x.py") == "backend-routers"
    assert classify_file_class("backend/main.py") == "backend"
    assert classify_file_class("components/admin/x.tsx") == "frontend-components"
    assert classify_file_class("app/admin/x/page.tsx") == "frontend-app"
    assert classify_file_class("test/foo.test.tsx") == "tests"
    assert classify_file_class("backend/tests/foo.py") == "tests"


def test_classify_file_class_unknown_returns_other():
    assert classify_file_class("Makefile") == "other"
    assert classify_file_class("random.txt") == "other"


def test_dominant_file_class_picks_most_frequent():
    paths = [
        "docs/a.md", "docs/b.md", "docs/c.md",
        "backend/agents/x.py",
    ]
    assert dominant_file_class(paths) == "docs"


def test_dominant_file_class_ignores_commit_msg():
    paths = ["/COMMIT_MSG", "backend/agents/x.py"]
    assert dominant_file_class(paths) == "backend-agents"


def test_dominant_file_class_empty_list_is_other():
    assert dominant_file_class([]) == "other"
    assert dominant_file_class(["/COMMIT_MSG"]) == "other"


# ── In-memory store contract ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_in_memory_trust_store_round_trip():
    store = InMemoryTrustStore()
    s1 = await store.get("claude-bot", "docs")
    assert s1.successes == 0 and s1.failures == 0

    await store.record_outcome("claude-bot", "docs", success=True)
    await store.record_outcome("claude-bot", "docs", success=True)
    await store.record_outcome("claude-bot", "docs", success=False)

    s2 = await store.get("claude-bot", "docs")
    assert s2.successes == 2 and s2.failures == 1
    # 1/3 ≈ 33 % > 10 %, but still under ramp-up threshold (3 < 5)
    assert trust_score_ok(s2)


@pytest.mark.asyncio
async def test_in_memory_trust_store_disables_after_threshold_breach():
    store = InMemoryTrustStore()
    for _ in range(8):
        await store.record_outcome("claude-bot", "docs", success=True)
    for _ in range(2):
        await store.record_outcome("claude-bot", "docs", success=False)
    s = await store.get("claude-bot", "docs")
    assert s.total == 10
    assert s.failure_rate == 0.20
    # 20 % ≥ 10 % → trust gate fails → auto-+1 disabled for this tuple.
    assert not trust_score_ok(s)


# ── Synthetic 10-PS scenario (AC #7) ──────────────────────────────────


def test_ac_synthetic_10_psets_classification():
    """AC #7: 10 PSes (8 small docs, 2 large refactors).

    Expected: 8 auto-tag + AI +1; 2 don't (size gate). Operator
    one-click batch-+2 the 8; all merge cleanly.
    """
    psets: list[Change] = []
    # 8 small docs PSes (all auto-tag eligible)
    for i in range(8):
        psets.append(Change(
            id=f"Idoc{i:02d}",
            project="omnisight", bot="claude-bot",
            mergeable=True,
            size_insertions=30 + i, size_deletions=2,
            files=(ChangeFile(f"docs/page-{i}.md"),),
        ))
    # 2 large refactors (size gate trips)
    for i in range(2):
        psets.append(Change(
            id=f"Iref{i:02d}",
            project="omnisight", bot="claude-bot",
            mergeable=True,
            size_insertions=400 + i, size_deletions=120,
            files=(ChangeFile(f"backend/services/refactor-{i}.py"),),
        ))

    verdict = _approve_verdict()
    decisions = [(p.id, can_auto_plus_one(p, verdict)) for p in psets]
    auto_tagged = [cid for cid, (ok, _) in decisions if ok]
    rejected = [(cid, reason) for cid, (ok, reason) in decisions if not ok]

    assert len(auto_tagged) == 8
    assert len(rejected) == 2
    for cid, reason in rejected:
        assert cid.startswith("Iref")
        assert "diff size" in reason and "200" in reason


# ══════════════════════════════════════════════════════════════════════
# OP-756 -- 4-tier graceful-degradation ladder
# ══════════════════════════════════════════════════════════════════════


# ── Tier threshold constants pin the spec literals ────────────────────


def test_tier_thresholds_match_spec():
    # Pinned by the OP-756 ticket description; if these change, the
    # operator-facing behaviour changes too — bump the spec first.
    assert TIER_THRESHOLD_AUTO == 0.95
    assert TIER_THRESHOLD_GLANCE == 0.80
    assert TIER_THRESHOLD_COMMENT == 0.50


# ── trust_score_value: success_rate with ramp-up default ──────────────


def test_trust_score_value_defaults_to_one_during_ramp_up():
    # 0..4 observations → default 1.0 (AUTO during ramp-up).
    for n in range(0, TRUST_SCORE_RAMP_UP_OBSERVATIONS):
        s = TrustScore(bot="b", file_class="c", successes=n, failures=0)
        assert trust_score_value(s) == 1.0


def test_trust_score_value_after_ramp_up_uses_success_rate():
    # 5+ observations → 1 - failure_rate.
    s = TrustScore(bot="b", file_class="c", successes=18, failures=2)
    # 2/20 = 0.10 failure → 0.90 trust score
    assert trust_score_value(s) == pytest.approx(0.90)


# ── trust_tier: 4-way ladder ─────────────────────────────────────────


@pytest.mark.parametrize(
    "successes,failures,expected_tier",
    [
        # AUTO: success_rate >= 0.95
        (95, 5, TrustTier.AUTO),       # exactly 0.95 → boundary in AUTO
        (99, 1, TrustTier.AUTO),
        # GLANCE: 0.80 <= success_rate < 0.95
        (94, 6, TrustTier.GLANCE),     # 0.94 → just below AUTO
        (80, 20, TrustTier.GLANCE),    # exactly 0.80 → boundary in GLANCE
        # COMMENT: 0.50 <= success_rate < 0.80
        (79, 21, TrustTier.COMMENT),
        (50, 50, TrustTier.COMMENT),   # exactly 0.50 → boundary in COMMENT
        # DISABLED: success_rate < 0.50
        (49, 51, TrustTier.DISABLED),
        (0, 10, TrustTier.DISABLED),
    ],
)
def test_trust_tier_ladder(successes, failures, expected_tier):
    s = TrustScore(
        bot="claude-bot", file_class="backend",
        successes=successes, failures=failures,
    )
    assert trust_tier(s) == expected_tier


def test_trust_tier_ramp_up_starts_in_auto():
    # New (bot, file_class) pair: 0 obs → default 1.0 → AUTO. Spec
    # invariant: bots get the benefit of the doubt during their first
    # few observations so they aren't permanently blocked.
    s = TrustScore(bot="brand-new-bot", file_class="docs")
    assert trust_tier(s) == TrustTier.AUTO


# ── Operator pin override ─────────────────────────────────────────────


def test_trust_tier_honors_explicit_pin_override():
    # Even a perfect score pin can drop to DISABLED for debugging.
    s = TrustScore(
        bot="claude-bot", file_class="backend", successes=99, failures=1,
    )
    assert trust_tier(s) == TrustTier.AUTO
    assert trust_tier(s, pinned=TrustTier.DISABLED) == TrustTier.DISABLED
    assert trust_tier(s, pinned=TrustTier.COMMENT) == TrustTier.COMMENT
    assert trust_tier(s, pinned=TrustTier.GLANCE) == TrustTier.GLANCE


def test_parse_pinned_tier_extracts_runner_trust_tier_hashtag():
    assert parse_pinned_tier(["runner-trust-tier=auto"]) == TrustTier.AUTO
    assert parse_pinned_tier(["runner-trust-tier=glance"]) == TrustTier.GLANCE
    assert parse_pinned_tier(["runner-trust-tier=comment"]) == TrustTier.COMMENT
    assert parse_pinned_tier(["runner-trust-tier=disabled"]) == TrustTier.DISABLED


def test_parse_pinned_tier_is_case_insensitive_on_value():
    assert parse_pinned_tier(["runner-trust-tier=AUTO"]) == TrustTier.AUTO
    assert parse_pinned_tier(["runner-trust-tier=Glance"]) == TrustTier.GLANCE


def test_parse_pinned_tier_ignores_unknown_or_irrelevant_hashtags():
    assert parse_pinned_tier([]) is None
    assert parse_pinned_tier(None) is None
    assert parse_pinned_tier(["runner-batch-merge-candidate"]) is None
    assert parse_pinned_tier(["runner-trust-tier=bogus"]) is None
    # First valid pin wins; bogus values are skipped over.
    found = parse_pinned_tier([
        "unrelated", "runner-trust-tier=bogus", "runner-trust-tier=glance",
    ])
    assert found == TrustTier.GLANCE


def test_pin_hashtag_prefix_matches_spec():
    # The OP-756 ticket spec names this label; operators set it
    # via `gerrit set-hashtags --add runner-trust-tier=...`.
    assert TIER_PIN_HASHTAG_PREFIX == "runner-trust-tier="


# ── resolve_reviewer_action: action selection per tier ────────────────


def _green_change_for_tier() -> Change:
    return Change(
        id="ITier01", project="omnisight", bot="claude-bot",
        mergeable=True, size_insertions=20, size_deletions=5,
        files=(ChangeFile("docs/howto.md"),),
    )


def test_resolve_action_auto_tier_all_green():
    decision = resolve_reviewer_action(
        _green_change_for_tier(), _approve_verdict(), TrustTier.AUTO,
    )
    assert decision.action == ReviewerAction.AUTO_PLUS_ONE
    assert decision.hashtag == BATCH_MERGE_HASHTAG
    assert decision.tier == "auto"


def test_resolve_action_glance_tier_all_green_uses_glance_hashtag():
    decision = resolve_reviewer_action(
        _green_change_for_tier(), _approve_verdict(), TrustTier.GLANCE,
    )
    assert decision.action == ReviewerAction.GLANCE_PLUS_ONE
    assert decision.hashtag == GLANCE_REQUIRED_HASHTAG
    assert decision.tier == "glance"


def test_resolve_action_comment_tier_posts_comment_only():
    decision = resolve_reviewer_action(
        _green_change_for_tier(), _approve_verdict(), TrustTier.COMMENT,
    )
    assert decision.action == ReviewerAction.COMMENT_ONLY
    assert decision.hashtag == ""
    assert decision.reason == "tier=comment"


def test_resolve_action_disabled_tier_skips_entirely():
    decision = resolve_reviewer_action(
        _green_change_for_tier(), _approve_verdict(), TrustTier.DISABLED,
    )
    assert decision.action == ReviewerAction.SKIP
    assert decision.hashtag == ""
    assert decision.reason == "tier=disabled"


def test_resolve_action_glance_tier_downgrades_when_hard_gate_trips():
    # GLANCE tier with an oversized diff downgrades to COMMENT_ONLY —
    # safety boundary still applies even when trust is healthy.
    big = Change(
        id="ITier02", project="x", bot="claude-bot",
        mergeable=True, size_insertions=400, size_deletions=10,
        files=(ChangeFile("docs/big.md"),),
    )
    decision = resolve_reviewer_action(big, _approve_verdict(), TrustTier.GLANCE)
    assert decision.action == ReviewerAction.COMMENT_ONLY
    assert "diff size" in decision.reason


def test_resolve_action_auto_tier_downgrades_on_safety_critical_files():
    unsafe = Change(
        id="ITier03", project="x", bot="claude-bot",
        mergeable=True, size_insertions=10, size_deletions=2,
        files=(ChangeFile("backend/alembic/versions/0203.py"),),
    )
    decision = resolve_reviewer_action(unsafe, _approve_verdict(), TrustTier.AUTO)
    assert decision.action == ReviewerAction.COMMENT_ONLY
    assert "safety-critical" in decision.reason


def test_resolve_action_non_approve_verdict_drops_to_comment_even_at_auto():
    decision = resolve_reviewer_action(
        _green_change_for_tier(),
        AIReviewVerdict(severity=Severity.NEEDS_WORK),
        TrustTier.AUTO,
    )
    assert decision.action == ReviewerAction.COMMENT_ONLY
    assert "AI verdict=NEEDS_WORK" in decision.reason


# ── Comment helpers (glance / comment-only) ───────────────────────────


def test_glance_plus_one_message_mentions_glance_hashtag_and_human_plus_two():
    msg = glance_plus_one_message("all-green")
    assert GLANCE_REQUIRED_HASHTAG in msg
    assert "+2" in msg


def test_comment_only_message_includes_reason():
    msg = comment_only_message("tier=comment")
    assert "COMMENT-ONLY" in msg
    assert "tier=comment" in msg


# ── Recovery semantics: failures don't permanently disable ────────────


@pytest.mark.asyncio
async def test_tier_recovers_with_post_failure_successes():
    """A handful of failures at the start drops the pair to GLANCE or
    COMMENT, but subsequent successes climb the score back toward AUTO.

    Mirrors the OP-756 spec line: "After successful merges, score
    recovers (decay back up over time)". We don't time-decay (would
    need a schema change), but cumulative successes shrink the
    failure_rate denominator, which is what the spec is asking for.
    """
    store = InMemoryTrustStore()
    bot, klass = "claude-bot", "docs"

    # 6 successes + 2 failures → 6/8 = 0.75 → COMMENT tier.
    for _ in range(6):
        await store.record_outcome(bot, klass, success=True)
    for _ in range(2):
        await store.record_outcome(bot, klass, success=False)
    s_before = await store.get(bot, klass)
    assert trust_tier(s_before) == TrustTier.COMMENT

    # 12 more successes → 18/20 = 0.90 → climb to GLANCE.
    for _ in range(12):
        await store.record_outcome(bot, klass, success=True)
    s_glance = await store.get(bot, klass)
    assert trust_tier(s_glance) == TrustTier.GLANCE

    # 100 more successes → 118/120 ≈ 0.983 → climb back to AUTO.
    for _ in range(100):
        await store.record_outcome(bot, klass, success=True)
    s_auto = await store.get(bot, klass)
    assert trust_tier(s_auto) == TrustTier.AUTO


@pytest.mark.asyncio
async def test_single_failure_does_not_permanently_disable():
    """OP-756 anti-fragility invariant: one failure must not bury a
    bot into DISABLED. Verifies a 1/N failure rate stays out of the
    DISABLED tier for any reasonable N.
    """
    store = InMemoryTrustStore()
    bot, klass = "claude-bot", "backend"

    # 4 successes + 1 failure → 4/5 = 0.80 success_rate → GLANCE.
    for _ in range(4):
        await store.record_outcome(bot, klass, success=True)
    await store.record_outcome(bot, klass, success=False)
    s = await store.get(bot, klass)
    tier = trust_tier(s)
    assert tier != TrustTier.DISABLED
    assert tier in (TrustTier.AUTO, TrustTier.GLANCE, TrustTier.COMMENT)
