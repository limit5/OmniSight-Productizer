"""OP-741 CI recovery state-machine tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from backend import auth
from backend.agents import ci_recovery as cr


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _change(**overrides: Any) -> cr.Change:
    base = {
        "number": 741,
        "jira_key": "OP-741",
        "patchset": 3,
        "commit_sha": "abcdef1234567890",
        "subject": "[OP-741] ci recovery",
    }
    base.update(overrides)
    return cr.Change(**base)


def _comment(text: str) -> cr.CiComment:
    return cr.CiComment(text=text, ci_id="ci-741")


def _actions(hooks: cr.InMemoryRecoveryBackend, action: str) -> list[dict]:
    return [call for call in hooks.calls if call.get("action") == action]


def _user(role: str = "admin") -> auth.User:
    return auth.User(
        id=f"u-{role}",
        email=f"{role}@example.com",
        name=role,
        role=role,
        tenant_id="t-default",
    )


# ── Failure categorizer ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("log_text", "expected"),
    [
        ("TimeoutError while contacting worker", ("flaky", "auto-retry")),
        ("FAILED backend/tests/test_x.py::test_y AssertionError", ("real-bug", "bot-patch")),
        ("ModuleNotFoundError: develop helper", ("stale", "rebase")),
        ("docker.errors.BuildError: build failed image", ("infra", "auto-retry-or-escalate")),
        ("segmentation fault in unrelated binary", ("unknown", "auto-retry-once")),
    ],
)
def test_categorize_five_failure_types(log_text: str, expected: tuple[str, str]) -> None:
    assert cr.categorize(_comment(log_text)) == expected


# ── Strategy dispatch ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("log_text", "expected_action", "expected_call"),
    [
        ("BrokenPipe in CI stream", "retry", "retry"),
        ("FAILED backend/tests/test_x.py::test_y\nAssertionError: bad", "bot-patch", "bot-patch"),
        ("ImportError from develop branch", "rebase", "rebase"),
        ("docker.errors.APIError build failed image", "retry", "retry"),
        ("unmatched weird failure", "retry", "retry"),
    ],
)
def test_each_strategy_dispatches_expected_first_attempt(
    log_text: str, expected_action: str, expected_call: str
) -> None:
    hooks = cr.InMemoryRecoveryBackend()

    decision = cr.handle_verified_minus_one(_change(), _comment(log_text), hooks)

    assert decision.action == expected_action
    assert _actions(hooks, expected_call)


def test_infra_second_attempt_escalates() -> None:
    hooks = cr.InMemoryRecoveryBackend()
    hooks.add_label("OP-741", "ci-attempt:1")

    decision = cr.handle_verified_minus_one(
        _change(),
        _comment("CI worker timeout while building image"),
        hooks,
    )

    assert decision.action == "escalate"
    assert _actions(hooks, "escalate")
    assert any(c.get("severity") == "high" for c in _actions(hooks, "notify"))


def test_unknown_second_attempt_escalates() -> None:
    hooks = cr.InMemoryRecoveryBackend()
    hooks.add_label("OP-741", "ci-attempt:1")

    decision = cr.handle_verified_minus_one(_change(), _comment("mystery"), hooks)

    assert decision.action == "escalate"
    assert _actions(hooks, "escalate")[0]["reason"] == "unknown failure pattern"


# ── Loop guard ──────────────────────────────────────────────────────


def test_loop_guard_third_attempt_notifies_medium_and_continues() -> None:
    hooks = cr.InMemoryRecoveryBackend()
    hooks.add_label("OP-741", "ci-attempt:2")

    decision = cr.handle_verified_minus_one(_change(), _comment("TimeoutError"), hooks)

    assert decision.action == "retry"
    assert any(c.get("severity") == "medium" for c in _actions(hooks, "notify"))
    assert cr.LOOP_PAUSED_LABEL not in hooks.get_labels("OP-741")


def test_loop_guard_sixth_attempt_hard_stops_with_label() -> None:
    hooks = cr.InMemoryRecoveryBackend()
    hooks.add_label("OP-741", "ci-attempt:5")

    decision = cr.handle_verified_minus_one(_change(), _comment("TimeoutError"), hooks)

    assert decision.action == "hard-stop"
    assert cr.LOOP_PAUSED_LABEL in hooks.get_labels("OP-741")
    assert hooks.dead_letters["OP-741"].attempt_count == 6
    assert any(c.get("severity") == "high" for c in _actions(hooks, "notify"))


# ── Bot-patch prompt contract ───────────────────────────────────────


def test_bot_patch_prompt_addendum_contains_failures_log_and_preserve_instruction() -> None:
    hooks = cr.InMemoryRecoveryBackend()
    log = "\n".join(
        [
            "FAILED backend/tests/test_x.py::test_y",
            "AssertionError: expected 'foo', got 'bar'",
            "File: backend/agents/z.py:45",
        ]
    )

    decision = cr.handle_verified_minus_one(_change(), _comment(log), hooks)

    assert decision.failure_context is not None
    addendum = _actions(hooks, "bot-patch")[0]["prompt_addendum"]
    assert "backend/tests/test_x.py::test_y" in addendum
    assert "AssertionError: expected 'foo', got 'bar'" in addendum
    assert "Preserve the rest of your previous changes" in addendum
    assert "/home/user/work/sora/logs/ci/ci-741-PS3.log" in addendum


def test_auto_runner_invoke_cli_appends_failure_context(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules.pop("jira_runner_op741", None)
    spec = importlib.util.spec_from_file_location("jira_runner_op741", _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured: dict[str, str] = {}

    class DummyProc:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            return ("", "")

        def kill(self):
            pass

    monkeypatch.setattr(mod.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: DummyProc())

    rc = mod._invoke_cli("subscription-codex", "original prompt", "ADDENDUM - CI feedback")

    assert rc == 0
    assert captured["input"] == "original prompt\n\nADDENDUM - CI feedback\n"


# ── 30 minute timeout watcher ───────────────────────────────────────


def test_ci_timeout_requeues_once_then_escalates() -> None:
    change = _change()
    hooks = cr.InMemoryRecoveryBackend()

    deadline = cr.mark_ci_started(change, hooks, now=1000)
    assert deadline == 2800
    first = cr.handle_ci_timeout(change, hooks, now=2801)

    assert first is not None
    assert first.action == "retry"
    assert _actions(hooks, "retry")

    cr.mark_ci_started(change, hooks, now=3000)
    second = cr.handle_ci_timeout(change, hooks, now=4801)

    assert second is not None
    assert second.action == "escalate"
    assert _actions(hooks, "escalate")[-1]["reason"] == "CI timeout repeated"


# ── Operator escapes ────────────────────────────────────────────────


def test_operator_escape_force_submit_builds_audit_comment() -> None:
    from scripts.force_submit import FORCE_SUBMIT_LABEL, apply_force_submit

    result = apply_force_submit(
        change_number="123",
        jira_key="OP-741",
        reason="prod fire",
        operator="operator@example.com",
        dry_run=True,
    )

    assert result.label == FORCE_SUBMIT_LABEL
    assert "[ci-force-submit]" in result.audit_comment
    assert "reason=prod fire" in result.audit_comment


def test_operator_escape_runner_skip_ci_is_audited() -> None:
    hooks = cr.InMemoryRecoveryBackend()
    hooks.add_label("OP-741", cr.SKIP_CI_LABEL)

    decision = cr.handle_skip_ci_escape(_change(), hooks)

    assert decision is not None
    assert decision.action == "skip-ci"
    audit_actions = [c.get("audit_action") for c in _actions(hooks, "audit")]
    assert "ci_recovery_skip_ci_escape" in audit_actions


def test_operator_escape_quarantine_file_excludes_and_expires(tmp_path: Path) -> None:
    path = tmp_path / "quarantine.txt"
    path.write_text(
        "\n".join(
            [
                "# known flaky",
                "backend/tests/test_a.py::test_flaky expires=2000000",
                "backend/tests/test_b.py::test_old expires=100",
            ]
        )
    )

    assert cr.load_quarantine(path, now=1000) == (
        "backend/tests/test_a.py::test_flaky",
    )
    assert cr.expired_quarantines(path, now=100 + 15 * 86400) == (
        "backend/tests/test_b.py::test_old",
    )


@pytest.mark.asyncio
async def test_operator_escape_dead_letter_dashboard_actions_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.routers import ci_dead_letter

    ci_dead_letter._reset_for_tests()
    ci_dead_letter.seed_dead_letter_for_tests()
    audit_calls: list[dict] = []

    async def fake_audit(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr("backend.audit.log", fake_audit)

    listed = await ci_dead_letter.list_ci_dead_letters(None, actor=_user())
    body = json.loads(listed.body)
    assert body["items"][0]["change"]["jira_key"] == "OP-741"

    for action in ("retrigger-ci", "abandon-ps", "mark-quarantine", "manual-review"):
        req = ci_dead_letter.DeadLetterActionRequest(
            jira_key="OP-741",
            action=action,
            reason="operator test",
        )
        res = await ci_dead_letter.apply_ci_dead_letter_action(req, request=None, actor=_user())
        assert json.loads(res.body)["ok"] is True

    assert len(audit_calls) == 4
    assert {call["after"]["action"] for call in audit_calls} == {
        "retrigger-ci", "abandon-ps", "mark-quarantine", "manual-review",
    }


# ── Soak ────────────────────────────────────────────────────────────


def test_synthetic_50_ps_soak_meets_resolution_guardrails() -> None:
    comments = [
        _comment("TimeoutError"),
        _comment("FAILED backend/tests/test_x.py::test_y AssertionError"),
        _comment("ModuleNotFoundError"),
        _comment("docker.errors.APIError build failed image"),
        _comment("unknown"),
    ] * 10
    changes = [
        _change(number=800 + i, jira_key=f"OP-{800 + i}", patchset=1)
        for i in range(50)
    ]

    metrics = cr.simulate_soak(changes, comments, cr.InMemoryRecoveryBackend)

    assert metrics["total"] == 50
    assert metrics["auto_resolve_rate"] >= 0.70
    assert metrics["hard_stop_rate"] <= 0.06
    assert metrics["audit_rate"] == 1.0
