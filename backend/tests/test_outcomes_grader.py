"""OP-1141 — strict-match AC-evidence grader contract tests.

Six scenarios required by the ticket's Code AC:

1. clean AC verification        — all ✓ cites resolve, verdict pass.
2. hallucinated file path       — file not in diff → verdict fail.
3. off-by-N line range          — file touched but cited range misses → fail.
4. wrong Change-Id              — cited Change-Id != HEAD Change-Id → fail.
5. no evidence cited            — ✓ line lacks any cite → fail.
6. mixed-quality comment        — one good cite + one hallucinated cite → fail.

The grader is pure logic (no LLM call, no network), so the tests pass
hand-crafted diff blobs and comment bodies directly. ADF parsing is
covered by a separate test that round-trips a realistic JIRA
``/comment`` payload.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import outcomes_grader as og


# ── Shared fixtures ──────────────────────────────────────────────


HEAD_CHANGE_ID = "I1234567890abcdef1234567890abcdef12345678"


_DIFF_TOUCHES_GRADER_LINES_10_TO_20 = """\
diff --git a/backend/agents/outcomes_grader.py b/backend/agents/outcomes_grader.py
index abc1234..def5678 100644
--- a/backend/agents/outcomes_grader.py
+++ b/backend/agents/outcomes_grader.py
@@ -10,3 +10,11 @@ def parse_ac_verification(body):
+    # OP-1141 strict-match
+    if not body:
+        return []
+    for line in body.splitlines():
+        ...
+    # more
+    pass
+
+def helper():
+    return None
"""


_DIFF_NEW_FILE_WITH_TEST = """\
diff --git a/backend/tests/test_outcomes_grader.py b/backend/tests/test_outcomes_grader.py
new file mode 100644
index 0000000..abc1234
--- /dev/null
+++ b/backend/tests/test_outcomes_grader.py
@@ -0,0 +1,12 @@
+def test_hallucinated_file_path_fails():
+    assert True
+
+def test_clean_evidence_passes():
+    assert True
"""


def _scenario_diff() -> str:
    """Combined diff covering the strict-match scenarios."""
    return _DIFF_TOUCHES_GRADER_LINES_10_TO_20 + "\n" + _DIFF_NEW_FILE_WITH_TEST


# ── 1. Clean AC verification ─────────────────────────────────────


def test_scenario_1_clean_ac_verification_passes() -> None:
    """All ✓ cites resolve — file:line in diff, Change-Id matches HEAD."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ outcomes_grader module created — backend/agents/outcomes_grader.py:L10-L20\n"
        "✓ change committed with Change-Id — " + HEAD_CHANGE_ID + "\n"
        "✓ unit test added — test_clean_evidence_passes\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_scenario_diff(),
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "pass", result.reasons
    assert result.comment_found is True
    assert len(result.per_evidence) == 3
    assert all(v.ok for v in result.per_evidence)


# ── 2. Hallucinated file path ────────────────────────────────────


def test_scenario_2_hallucinated_file_path_fails() -> None:
    """Cite a file that is not touched by this diff at all."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ wired into runner pre-push — auto-runner-jira.py:L9999-L10001\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_scenario_diff(),
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "fail"
    assert result.comment_found is True
    bad = result.per_evidence[0]
    assert bad.ok is False
    assert "not touched by this diff" in bad.reason


# ── 3. Off-by-N line range ───────────────────────────────────────


def test_scenario_3_off_by_n_line_range_fails() -> None:
    """File is touched (lines 10-20) but the cited range (100-110) misses."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ strict-match implemented — backend/agents/outcomes_grader.py:L100-L110\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_DIFF_TOUCHES_GRADER_LINES_10_TO_20,
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "fail"
    bad = result.per_evidence[0]
    assert bad.ok is False
    assert "does not overlap" in bad.reason


# ── 4. Wrong Change-Id ───────────────────────────────────────────


def test_scenario_4_wrong_change_id_fails() -> None:
    """Cited Change-Id is the right shape but does not match HEAD."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ committed — Icafebabecafebabecafebabecafebabecafebabe\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_scenario_diff(),
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "fail"
    bad = result.per_evidence[0]
    assert bad.ok is False
    assert "Change-Id" in bad.reason
    assert "does not match" in bad.reason


# ── 5. No evidence cited ─────────────────────────────────────────


def test_scenario_5_no_evidence_cited_fails() -> None:
    """✓ line is missing any concrete cite — strict mode rejects."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ implemented — looks right\n"
        "✓ tests pass — should work\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_scenario_diff(),
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "fail"
    assert all(not v.ok for v in result.per_evidence)
    assert any("vague" in v.reason for v in result.per_evidence)


def test_scenario_5b_completely_missing_evidence_fails() -> None:
    """``✓ done`` has no em-dash separator → ``none`` kind → fail."""
    body = "AC verification for OP-1141:\n✓ done\n"
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_scenario_diff(),
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "fail"
    assert result.per_evidence[0].evidence.kind == "none"
    assert "no evidence cited" in result.per_evidence[0].reason


# ── 6. Mixed-quality comment ─────────────────────────────────────


def test_scenario_6_mixed_quality_comment_fails() -> None:
    """One good ✓ + one hallucinated ✓ — strict mode fails the whole ticket."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ module created — backend/agents/outcomes_grader.py:L10-L15\n"
        "✓ wired up — auto-runner-jira.py:L9999\n"  # hallucinated
        "✓ test added — test_clean_evidence_passes\n"  # good
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_scenario_diff(),
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "fail"
    oks = [v.ok for v in result.per_evidence]
    assert oks == [True, False, True], oks
    fails = [v for v in result.per_evidence if not v.ok]
    assert len(fails) == 1
    assert "not touched" in fails[0].reason


# ── Bonus contract tests ─────────────────────────────────────────


def test_missing_ac_verification_comment_fails() -> None:
    """No ``AC verification for <KEY>`` header anywhere → fail + ``comment_found=False``."""
    result = og.grade_ac_evidence(
        comment_body=None,
        diff_text=_scenario_diff(),
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "fail"
    assert result.comment_found is False


def test_skipped_ac_marker_does_not_fail_ticket() -> None:
    """✗ rows are not evidence claims — they pass through neutrally."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ implemented — backend/agents/outcomes_grader.py:L10-L15\n"
        "✗ exercised AC — needs 1mo post-deploy observation, not verifiable now\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_scenario_diff(),
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "pass", result.reasons


def test_newly_added_file_accepts_any_line_cite() -> None:
    """New-file cite at any line is honoured (whole file is touched)."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ new test file — backend/tests/test_outcomes_grader.py:L500\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_DIFF_NEW_FILE_WITH_TEST,
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "pass", result.reasons


def test_adf_round_trip_extracts_header() -> None:
    """``find_ac_verification_comment`` plucks the right comment from ADF."""
    adf_doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "AC verification for OP-1141:"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "✓ implemented — backend/agents/outcomes_grader.py:L10"}]},
        ],
    }
    other_doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Unrelated chat about OP-1141."}]},
        ],
    }
    comments = [
        {"id": "1", "body": other_doc},
        {"id": "2", "body": adf_doc},
    ]
    body = og.find_ac_verification_comment(comments, "OP-1141")
    assert body is not None
    assert "AC verification for OP-1141" in body
    assert "✓ implemented" in body


def test_most_recent_comment_wins_when_multiple_match() -> None:
    """If the agent re-posts AC verification, the runner sees the latest body."""
    comments = [
        {"id": "1", "body": "AC verification for OP-1141:\n✓ stale — backend/agents/outcomes_grader.py:L9999\n"},
        {"id": "2", "body": "AC verification for OP-1141:\n✓ fresh — backend/agents/outcomes_grader.py:L10\n"},
    ]
    body = og.find_ac_verification_comment(comments, "OP-1141")
    assert body is not None
    assert "fresh" in body
    assert "stale" not in body


def test_format_failure_comment_includes_each_failure_reason() -> None:
    """Operator-facing diagnostic comment lists every failed ✓ line."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ wired — auto-runner-jira.py:L9999\n"
        "✓ done — looks right\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_DIFF_TOUCHES_GRADER_LINES_10_TO_20,
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    rendered = og.format_failure_comment("OP-1141", result)
    assert "[outcomes-grader-strict:fail]" in rendered
    assert "OP-1141" in rendered
    assert "not touched" in rendered
    assert "vague" in rendered


def test_format_failure_comment_handles_missing_header() -> None:
    """Diagnostic still readable when the agent forgot the comment entirely."""
    result = og.grade_ac_evidence(
        comment_body=None,
        diff_text="",
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    rendered = og.format_failure_comment("OP-1141", result)
    assert "did not post" in rendered
    assert "AC verification for OP-1141" in rendered


# ── Integration AC: synthetic hallucinated cite is caught ────────


def test_integration_synthetic_hallucinated_cite_caught() -> None:
    """OP-1141 Integration AC: 1 hallucinated file-path AC comment is
    caught before reaching Gerrit. We assert the grader returns a
    fail verdict with a reason naming the bad file, which is what the
    runner's pre-push gate keys off to §11-revert."""
    body = (
        "AC verification for OP-1141:\n"
        "✓ all done — backend/agents/totally_made_up.py:L1-L50\n"
    )
    result = og.grade_ac_evidence(
        comment_body=body,
        diff_text=_DIFF_TOUCHES_GRADER_LINES_10_TO_20,
        head_change_id=HEAD_CHANGE_ID,
        ticket_key="OP-1141",
    )
    assert result.verdict == "fail"
    assert any(
        "backend/agents/totally_made_up.py" in v.reason
        for v in result.per_evidence if not v.ok
    )
