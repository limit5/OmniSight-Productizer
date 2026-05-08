"""OP-713 — AI Reviewer routing + size cap + throttle + happy-path tests.

Covers AC bullets 7 + 8 of OP-713 (cost tracking + tests):
  * route_model() honours haiku / sonnet / opus tier rules
  * is_too_large() trips above the 1500 LOC cap
  * (change_id, revision) idempotency throttle skips re-reviews
  * review_patchset() returns ReviewResult with footer +
    cost computed from the pricing-table stub

Pure-data tests — they do NOT touch Postgres, SSH, or a real LLM
provider. The LLM and pricing seams in
:func:`backend.agents.ai_reviewer.review_patchset` accept callables we
inject here, which keeps the suite under 1 s and skip-free.
"""

from __future__ import annotations

import pytest

from backend.agents import ai_reviewer
from backend.agents.ai_reviewer import (
    DEFAULT_REVIEW_DIFF_LIMIT_LOC,
    MODEL_HAIKU,
    MODEL_OPUS,
    MODEL_SONNET,
    ReviewResult,
    diff_loc,
    is_too_large,
    mark_reviewed,
    reset_throttle,
    review_patchset,
    route_model,
    should_skip_recent,
    too_large_message,
)


@pytest.fixture(autouse=True)
def _clean_throttle():
    """Reset the per-worker throttle before/after each test so module
    state doesn't leak between cases."""
    reset_throttle()
    yield
    reset_throttle()


# ── route_model ──────────────────────────────────────────────────────


def test_route_model_default_is_haiku():
    """No file context (or only docs/scripts) → cheapest tier."""
    assert route_model("", files=[]) == MODEL_HAIKU
    assert route_model("", files=["docs/runbook.md"]) == MODEL_HAIKU
    assert route_model("", files=["scripts/oneshot.sh"]) == MODEL_HAIKU


def test_route_model_general_backend_is_sonnet():
    """A backend python file in the general tree → mid-risk sonnet."""
    assert route_model("", files=["backend/routers/foo.py"]) == MODEL_SONNET


def test_route_model_frontend_is_sonnet():
    """Frontend tier matches sonnet too (mid-risk app/frontend code)."""
    assert route_model("", files=["frontend/components/x.tsx"]) == MODEL_SONNET
    assert route_model("", files=["components/admin/x.tsx"]) == MODEL_SONNET
    assert route_model("", files=["app/admin/page.tsx"]) == MODEL_SONNET


@pytest.mark.parametrize(
    "path",
    [
        "alembic/versions/0203_test.py",
        "backend/alembic/versions/0204_test.py",
        "schema/users.sql",
        "deploy/prod/values.yaml",
        "security/jwt_validator.py",
        "backend/security/spend_anomaly.py",
        ".gerrit/project.config",
        "backend/submit_rule.py",
        "backend/merger_agent.py",
        "backend/merge_arbiter.py",
        "CLAUDE.md",
    ],
)
def test_route_model_high_risk_paths_force_opus(path):
    """Spec-listed HIGH-RISK paths *always* escalate to opus."""
    assert route_model("", files=[path]) == MODEL_OPUS


def test_route_model_high_risk_wins_over_sonnet():
    """If any file in the patch is HIGH-RISK, opus wins even if the
    rest is general backend code."""
    files = [
        "backend/routers/api.py",   # would be sonnet
        "backend/alembic/versions/0205_x.py",  # high-risk → opus
    ]
    assert route_model("", files=files) == MODEL_OPUS


def test_route_model_skips_commit_msg_pseudo_file():
    """Gerrit's synthetic ``/COMMIT_MSG`` must not influence routing —
    it's not a real file path."""
    assert route_model("", files=["/COMMIT_MSG", "docs/x.md"]) == MODEL_HAIKU
    assert route_model("", files=["/COMMIT_MSG"]) == MODEL_HAIKU


# ── is_too_large + diff_loc ──────────────────────────────────────────


def test_diff_loc_counts_added_and_removed_lines():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,3 +1,4 @@\n"
        "-old\n"
        "+new1\n"
        "+new2\n"
        " context\n"
    )
    # 1 removed + 2 added = 3 (file headers excluded)
    assert diff_loc(diff) == 3


def test_diff_loc_empty_inputs():
    assert diff_loc("") == 0
    assert diff_loc("only context lines\n  not a diff") == 0


def test_is_too_large_under_limit_passes():
    assert is_too_large(insertions=200, deletions=100) is False
    assert is_too_large(insertions=DEFAULT_REVIEW_DIFF_LIMIT_LOC, deletions=0) is False


def test_is_too_large_above_limit_trips():
    assert is_too_large(insertions=1200, deletions=400) is True
    assert is_too_large(insertions=DEFAULT_REVIEW_DIFF_LIMIT_LOC + 1, deletions=0) is True


def test_is_too_large_uses_diff_when_no_counts_given():
    big = "\n".join("+x" for _ in range(DEFAULT_REVIEW_DIFF_LIMIT_LOC + 5))
    assert is_too_large(diff=big) is True


def test_too_large_message_phrasing_pinned_by_ac():
    """AC #4 pins the exact message phrase ('too large for AI review,
    please ensure human deep-review'). The cost dashboard greps for
    the leading 'too large for AI review' substring, so don't lose
    it on a phrasing tweak without coordinating with ops."""
    msg = too_large_message(insertions=2000, deletions=100, model_id=MODEL_HAIKU)
    assert "too large for AI review" in msg
    assert "human deep-review" in msg
    # Footer present so observability greps for `reviewed-by:` still find it
    assert "reviewed-by:" in msg
    assert MODEL_HAIKU in msg


# ── Throttle ─────────────────────────────────────────────────────────


def test_throttle_first_review_not_skipped():
    assert should_skip_recent("Iabc", "sha1") is False


def test_throttle_blocks_within_24h():
    """Same (change_id, revision) within 24 h → second review skipped."""
    mark_reviewed("Iabc", "sha1", now=1000.0)
    # 12 h later: still throttled
    assert should_skip_recent("Iabc", "sha1", now=1000.0 + 12 * 3600) is True


def test_throttle_clears_after_24h():
    """25 h elapsed → next review fires again."""
    mark_reviewed("Iabc", "sha1", now=1000.0)
    assert should_skip_recent(
        "Iabc", "sha1", now=1000.0 + 25 * 3600,
    ) is False


def test_throttle_keys_on_revision():
    """Same change, new patchset (different SHA) → not throttled."""
    mark_reviewed("Iabc", "sha1", now=1000.0)
    assert should_skip_recent("Iabc", "sha2", now=1000.0 + 60) is False


def test_throttle_empty_keys_never_skip():
    """Defensive — missing change_id/revision must never throttle."""
    mark_reviewed("", "", now=1000.0)
    assert should_skip_recent("", "sha", now=1000.0 + 60) is False
    assert should_skip_recent("Iabc", "", now=1000.0 + 60) is False


# ── review_patchset happy path with stubbed LLM + pricing ────────────


_STUB_PRICING = {
    # input/output USD per 1M tokens
    (MODEL_HAIKU, "anthropic"): (0.80, 4.00),
    (MODEL_SONNET, "anthropic"): (3.00, 15.00),
    (MODEL_OPUS, "anthropic"): (15.00, 75.00),
}


def _stub_pricing(provider: str | None, model: str) -> tuple[float, float]:
    return _STUB_PRICING.get((model, provider or "anthropic"), (1.0, 5.0))


def test_review_patchset_happy_path_returns_plus_one():
    """A small clean diff routes to haiku, the LLM stub returns LGTM,
    we get score=+1 with the footer wired in."""
    captured: dict = {}

    def fake_invoke(prompt, *, model: str) -> str:
        captured["model"] = model
        captured["prompt"] = prompt
        return "Looks good to me — no findings."

    diff = (
        "diff --git a/docs/x.md b/docs/x.md\n"
        "--- a/docs/x.md\n"
        "+++ b/docs/x.md\n"
        "@@ -1 +1,2 @@\n"
        "+new docs line\n"
    )
    result = review_patchset(
        diff,
        files=["docs/x.md"],
        subject="docs typo fix",
        invoke=fake_invoke,
        pricing=_stub_pricing,
    )
    assert isinstance(result, ReviewResult)
    assert result.score == 1
    assert result.model_id == MODEL_HAIKU
    assert captured["model"] == MODEL_HAIKU
    # Footer convention pinned by ticket spec: every comment ends with
    # ``reviewed-by: <model_id> · cost: $X.XX``.
    assert "reviewed-by:" in result.message
    assert MODEL_HAIKU in result.message
    assert result.cost_usd >= 0.0
    assert result.input_tokens > 0
    assert result.output_tokens > 0


def test_review_patchset_high_risk_routes_to_opus():
    """AC #2: alembic/versions/* → footer shows model=claude-opus-4-7."""
    def fake_invoke(prompt, *, model: str) -> str:
        return "LGTM"

    diff = (
        "diff --git a/backend/alembic/versions/0210.py b/backend/alembic/versions/0210.py\n"
        "+# new migration\n"
    )
    result = review_patchset(
        diff,
        files=["backend/alembic/versions/0210.py"],
        invoke=fake_invoke,
        pricing=_stub_pricing,
    )
    assert result.model_id == MODEL_OPUS
    assert MODEL_OPUS in result.message


def test_review_patchset_too_large_returns_score_zero():
    """AC #4: > 1500 LOC patches are skipped with the canonical 'too
    large' message and score=0 (not +1)."""
    def fake_invoke(prompt, *, model: str) -> str:
        # Should never be called because we trip the size cap first.
        raise AssertionError("LLM must not be invoked for over-cap diffs")

    result = review_patchset(
        diff="(unused)",
        insertions=1200,
        deletions=400,  # 1600 total > 1500 cap
        files=["backend/routers/foo.py"],
        invoke=fake_invoke,
        pricing=_stub_pricing,
    )
    assert result.score == 0
    assert result.skipped_reason == "too_large"
    assert "too large for AI review" in result.message
    assert "human deep-review" in result.message
    assert result.cost_usd == 0.0


def test_review_patchset_explicit_model_overrides_routing():
    """If the caller passes ``model``, route_model() is bypassed."""
    captured: dict = {}

    def fake_invoke(prompt, *, model: str) -> str:
        captured["model"] = model
        return "ok"

    review_patchset(
        diff="+x\n",
        model=MODEL_SONNET,
        files=["docs/x.md"],   # would route to haiku
        invoke=fake_invoke,
        pricing=_stub_pricing,
    )
    assert captured["model"] == MODEL_SONNET


def test_review_patchset_handles_llm_error_with_score_zero():
    """LLM provider error → graceful degradation to score 0 with a
    'please review manually' message; no exception escapes."""
    def fake_invoke(prompt, *, model: str) -> str:
        raise RuntimeError("provider down")

    result = review_patchset(
        diff="+x\n",
        files=["docs/x.md"],
        invoke=fake_invoke,
        pricing=_stub_pricing,
    )
    assert result.score == 0
    assert result.skipped_reason == "invoke_error"
    assert "review manually" in result.message.lower()


def test_review_patchset_empty_reply_falls_back_to_score_zero():
    def fake_invoke(prompt, *, model: str) -> str:
        return ""

    result = review_patchset(
        diff="+x\n",
        files=["docs/x.md"],
        invoke=fake_invoke,
        pricing=_stub_pricing,
    )
    assert result.score == 0
    assert result.skipped_reason == "empty_reply"


def test_review_patchset_reject_keyword_downgrades_to_zero():
    """An explicit REJECT verdict from the LLM falls back to 0 (the AI
    never casts -1 in this ticket — see _score_from_reply docstring)."""
    def fake_invoke(prompt, *, model: str) -> str:
        return "REJECT — this introduces a SQL injection."

    result = review_patchset(
        diff="+x\n",
        files=["docs/x.md"],
        invoke=fake_invoke,
        pricing=_stub_pricing,
    )
    assert result.score == 0


def test_review_patchset_cost_uses_pricing_seam():
    """AC #7: cost computation must drive off the pricing table (so a
    YAML rate change is honoured without code edits)."""
    def fake_invoke(prompt, *, model: str) -> str:
        return "ok"

    seen_pricing: dict = {}

    def loud_pricing(provider, model):
        seen_pricing["called"] = (provider, model)
        return (1000.0, 5000.0)  # absurdly high so the cost is non-trivial

    result = review_patchset(
        diff="+x\n",
        files=["docs/x.md"],
        invoke=fake_invoke,
        pricing=loud_pricing,
    )
    assert seen_pricing["called"] == ("anthropic", MODEL_HAIKU)
    assert result.cost_usd > 0.0


# ── route_model ↔ review_patchset integration ────────────────────────


def test_route_then_review_for_default_path_uses_haiku_in_footer():
    """AC #2 mirror — low-risk doc patchset → footer shows haiku."""
    def fake_invoke(prompt, *, model: str) -> str:
        return "LGTM"

    files = ["docs/howto.md", "/COMMIT_MSG"]
    chosen = route_model(files=files)
    assert chosen == MODEL_HAIKU
    result = review_patchset(
        diff="+typo\n", files=files,
        invoke=fake_invoke, pricing=_stub_pricing,
    )
    assert MODEL_HAIKU in result.message
    assert result.score == 1


# Module-level constants pinned (so a renamed model breaks loudly here
# rather than silently shifting the prod cost shape).
def test_model_constants_match_spec():
    assert MODEL_HAIKU == "claude-haiku-4-5"
    assert MODEL_SONNET == "claude-sonnet-4-6"
    assert MODEL_OPUS == "claude-opus-4-7"
    assert DEFAULT_REVIEW_DIFF_LIMIT_LOC == 1500
    assert ai_reviewer.DEFAULT_THROTTLE_TTL_S == 24 * 3600
