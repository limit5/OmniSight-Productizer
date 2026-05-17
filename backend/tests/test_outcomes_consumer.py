"""OP-860 Outcomes API runner integration tests."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents.anthropic_native_client import TokenUsage
from backend.agents.loop_detector import OutcomesGraderUnavailable
from backend.agents.outcomes_consumer import (
    MAX_COMPLETION_TEXT_CHARS,
    MAX_DIFF_TEXT_CHARS,
    MAX_GRADER_REASONING_CHARS,
    MAX_TRANSITION_REASON_CHARS,
    OUTCOMES_FAIL_LABEL,
    OUTCOMES_PARTIAL_LABEL,
    OutcomesBudgetTracker,
    OutcomesGraderRefused,
    RunnerOutcomesVerdict,
    consume_outcomes_verdict,
    grade_outcomes,
    parse_outcomes_grader_response,
)


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_outcomes_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_outcomes_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    agent_class = "subscription-codex"
    bot_email = "rt3628+codex-bot@gmail.com"


def _patch_jira_writes(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    from backend.agents import jira_dispatch

    calls: dict[str, list] = {
        "comments": [],
        "labels": [],
        "todo": [],
    }

    monkeypatch.setattr(
        jira_dispatch,
        "add_comment",
        lambda client, key, text, **kw: calls["comments"].append((key, text)),
    )
    monkeypatch.setattr(
        jira_dispatch,
        "add_label",
        lambda client, key, label, **kw: calls["labels"].append((key, label)),
    )
    monkeypatch.setattr(
        jira_dispatch,
        "transition_back_to_todo",
        lambda client, key, reason, **kw: calls["todo"].append((key, reason)),
    )
    return calls


def test_pass_verdict_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_jira_writes(monkeypatch)

    consume_outcomes_verdict(
        client=_StubClient(),
        key="OP-860",
        verdict=RunnerOutcomesVerdict(verdict="pass", grader_reasoning="ok"),
    )

    assert calls == {"comments": [], "labels": [], "todo": []}


def test_partial_verdict_comments_and_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_jira_writes(monkeypatch)

    consume_outcomes_verdict(
        client=_StubClient(),
        key="OP-860",
        verdict=RunnerOutcomesVerdict(
            verdict="partial",
            grader_reasoning="AC 2 missing live evidence",
            cost_usd=0.0123,
        ),
    )

    assert calls["labels"] == [("OP-860", OUTCOMES_PARTIAL_LABEL)]
    assert len(calls["comments"]) == 1
    assert "[outcomes:partial]" in calls["comments"][0][1]
    assert "AC 2 missing live evidence" in calls["comments"][0][1]
    assert calls["todo"] == []


def test_fail_verdict_reverts_patchsets_reopens_and_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_jira_writes(monkeypatch)
    reverted: list[str] = []

    consume_outcomes_verdict(
        client=_StubClient(),
        key="OP-860",
        verdict=RunnerOutcomesVerdict(
            verdict="fail",
            grader_reasoning="AC 3 contradicted by diff",
        ),
        revert_patchsets=lambda: reverted.append("reverted"),
    )

    assert reverted == ["reverted"]
    assert ("OP-860", OUTCOMES_FAIL_LABEL) in calls["labels"]
    assert len(calls["comments"]) == 1
    assert "[outcomes:fail]" in calls["comments"][0][1]
    assert calls["todo"] == [("OP-860", "[outcomes:fail] AC 3 contradicted by diff")]


def test_fail_transition_reason_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_jira_writes(monkeypatch)
    long_reasoning = "x" * (MAX_TRANSITION_REASON_CHARS + 25)

    consume_outcomes_verdict(
        client=_StubClient(),
        key="OP-860",
        verdict=RunnerOutcomesVerdict(
            verdict="fail",
            grader_reasoning=long_reasoning,
        ),
    )

    assert calls["todo"] == [
        (
            "OP-860",
            f"[outcomes:fail] {long_reasoning[:MAX_TRANSITION_REASON_CHARS]}",
        )
    ]
    assert long_reasoning in calls["comments"][0][1]


def test_budget_cap_disables_runner_grader_for_rest_of_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    mod = _load_jira_runner()
    state_path = tmp_path / "budget.json"
    state_path.write_text(json.dumps({
        "date": mod.outcomes_consumer._today_utc(),
        "spent_usd": 5.0,
    }))
    monkeypatch.setenv("OMNISIGHT_OUTCOMES_GRADER_ENABLED", "1")
    monkeypatch.setenv("OMNISIGHT_OUTCOMES_BUDGET_USD_PER_DAY", "5.00")
    monkeypatch.setenv("OMNISIGHT_OUTCOMES_BUDGET_STATE_PATH", str(state_path))

    def should_not_grade(**kwargs: Any) -> RunnerOutcomesVerdict:
        raise AssertionError("grader should be disabled after budget cap")

    monkeypatch.setattr(mod.outcomes_consumer, "grade_outcomes", should_not_grade)

    status = mod._grade_and_consume_outcomes(
        _StubClient(), "OP-860", "## Acceptance criteria\n\n1. x",
        tmp_path, "HEAD~1", 123,
    )

    assert status == "disabled"


def test_unavailable_grader_degrades_without_ticket_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    mod = _load_jira_runner()
    calls = _patch_jira_writes(monkeypatch)
    monkeypatch.setenv("OMNISIGHT_OUTCOMES_GRADER_ENABLED", "1")
    monkeypatch.setenv("OMNISIGHT_OUTCOMES_BUDGET_USD_PER_DAY", "5.00")
    monkeypatch.setenv(
        "OMNISIGHT_OUTCOMES_BUDGET_STATE_PATH",
        str(tmp_path / "budget.json"),
    )
    monkeypatch.setattr(mod, "_collect_outcomes_context", lambda *a, **kw: ("done", "diff"))
    import backend.agents.anthropic_native_client as native_client

    monkeypatch.setattr(native_client, "AnthropicClient", lambda **kw: object())

    def unavailable(**kwargs: Any) -> RunnerOutcomesVerdict:
        raise OutcomesGraderUnavailable("stubbed outage")

    monkeypatch.setattr(mod.outcomes_consumer, "grade_outcomes", unavailable)

    status = mod._grade_and_consume_outcomes(
        _StubClient(), "OP-860", "## Acceptance criteria\n\n1. x",
        tmp_path, "abc123", 123,
    )

    assert status == "disabled"
    assert calls == {"comments": [], "labels": [], "todo": []}


def test_grade_outcomes_sends_ac_completion_and_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class Client:
        def simple(self, *, prompt: str, model: str, temperature: float):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["temperature"] = str(temperature)
            return (
                '{"verdict": "partial", "grader_reasoning": "AC 5 not proven"}',
                TokenUsage(input_tokens=1000, output_tokens=100),
            )

    verdict = grade_outcomes(
        client=Client(),  # type: ignore[arg-type]
        ticket_key="OP-860",
        ac_text="1. Wire runner path",
        completion_text="abc [OP-860] implement outcomes",
        diff_text="diff --git a/auto-runner-jira.py b/auto-runner-jira.py",
        grader_model="claude-haiku-4-20250506",
    )

    assert verdict.verdict == "partial"
    assert verdict.grader_reasoning == "AC 5 not proven"
    assert verdict.cost_usd > 0.0
    assert "1. Wire runner path" in captured["prompt"]
    assert "abc [OP-860] implement outcomes" in captured["prompt"]
    assert "diff --git" in captured["prompt"]
    assert captured["model"] == "claude-haiku-4-20250506"
    assert captured["temperature"] == "0.0"


def test_parse_outcomes_grader_response_accepts_wrapped_json() -> None:
    verdict, reasoning = parse_outcomes_grader_response(
        'Here is the assessment:\n{"verdict": "PARTIAL", '
        '"grader_reasoning": "AC 2 lacks evidence"}\nDone.'
    )

    assert verdict == "partial"
    assert reasoning == "AC 2 lacks evidence"


def test_parse_outcomes_grader_response_truncates_reasoning() -> None:
    long_reasoning = "r" * (MAX_GRADER_REASONING_CHARS + 25)

    verdict, reasoning = parse_outcomes_grader_response(
        json.dumps({"verdict": "fail", "grader_reasoning": long_reasoning})
    )

    assert verdict == "fail"
    assert reasoning == long_reasoning[:MAX_GRADER_REASONING_CHARS]


def test_parse_outcomes_grader_response_rejects_empty_reasoning() -> None:
    with pytest.raises(OutcomesGraderRefused, match="omitted grader_reasoning"):
        parse_outcomes_grader_response(
            json.dumps({"verdict": "pass", "grader_reasoning": "   "})
        )


def test_grade_outcomes_truncates_completion_and_diff() -> None:
    captured: dict[str, str] = {}

    class Client:
        def simple(self, *, prompt: str, model: str, temperature: float):
            captured["prompt"] = prompt
            return (
                '{"verdict": "pass", "grader_reasoning": "all AC covered"}',
                TokenUsage(input_tokens=10, output_tokens=10),
            )

    completion_text = "c" * (MAX_COMPLETION_TEXT_CHARS + 1)
    diff_text = "d" * (MAX_DIFF_TEXT_CHARS + 1)

    grade_outcomes(
        client=Client(),  # type: ignore[arg-type]
        ticket_key="OP-860",
        ac_text="1. Verify truncation",
        completion_text=completion_text,
        diff_text=diff_text,
        grader_model="claude-haiku-4-20250506",
    )

    assert completion_text[:MAX_COMPLETION_TEXT_CHARS] in captured["prompt"]
    assert completion_text not in captured["prompt"]
    assert diff_text[:MAX_DIFF_TEXT_CHARS] in captured["prompt"]
    assert diff_text not in captured["prompt"]


def test_budget_tracker_records_spend(tmp_path: Path) -> None:
    tracker = OutcomesBudgetTracker(
        daily_budget_usd=5.0,
        state_path=tmp_path / "budget.json",
    )
    tracker.ensure_available()
    tracker.record(0.25)

    assert tracker.spent_usd() == pytest.approx(0.25)
