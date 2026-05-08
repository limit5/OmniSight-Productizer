"""OP-735 R5 -- ai_reviewer.can_auto_plus_one + trust_scoring tests.

These tests pin the 5-gate decision table from the ticket spec plus
the TrustScore threshold logic. They're pure-data unit tests (no PG,
no SSH) so they exercise the gate quickly enough to live in the
default backend test suite.

The synthetic 10-PS scenario at the bottom of the file is the AC
sample: 8 small docs PSes auto-tag, 2 large refactors don't (size
gate) -- mirroring the ticket's "synthetic test" acceptance criterion.
"""

from __future__ import annotations

import pytest

from backend.agents.ai_reviewer import (
    BATCH_MERGE_HASHTAG,
    AIReviewVerdict,
    Change,
    ChangeFile,
    Severity,
    auto_plus_one_message,
    auto_plus_one_skip_message,
    can_auto_plus_one,
)
from backend.agents.trust_scoring import (
    InMemoryTrustStore,
    TRUST_SCORE_FAILURE_THRESHOLD,
    TRUST_SCORE_RAMP_UP_OBSERVATIONS,
    TrustScore,
    classify_file_class,
    dominant_file_class,
    trust_score_ok,
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
    # 19 ok / 20 total → 5 % failure rate < 10 % → True
    score = TrustScore(
        bot="claude-bot", file_class="docs", successes=19, failures=1,
    )
    assert score.failure_rate == pytest.approx(0.05)
    assert trust_score_ok(score)


def test_trust_score_above_threshold_with_high_failure_rate():
    # 17 ok / 20 total → 15 % failure rate ≥ 10 % → False (auto disabled)
    score = TrustScore(
        bot="claude-bot", file_class="docs", successes=17, failures=3,
    )
    assert score.failure_rate >= TRUST_SCORE_FAILURE_THRESHOLD
    assert not trust_score_ok(score)


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
