"""OP-759 H12 runner self-heal tests.

Pins the pre-pickup Gerrit merged-check that prevents doing already-merged
work again when JIRA was reverted to To Do.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents import jira_dispatch as jd


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_h12_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_h12_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    agent_class = "subscription-codex"
    bot_email = "rt3628+codex-bot@gmail.com"
    bot_account_id = "bot-account-id"
    project_key = "OP"


def _issue(key: str = "OP-691") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": "Already merged ticket",
            "labels": ["class:subscription-codex", "tier:S", "area:backend"],
            "status": {"name": "To Do"},
            "issuetype": {"name": "Story"},
            "fixVersions": [],
            "created": "2026-05-08T00:00:00.000+0000",
            "components": [{"name": "META"}],
        },
    }


def test_already_merged_in_gerrit_returns_strict_subject_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()
    lines = [
        {"number": 126, "url": "https://sora.services/+/126", "subject": "mentions OP-691 only"},
        {"number": 127, "url": "https://sora.services/+/127", "subject": "[OP-691] shipped"},
        {"type": "stats", "rowCount": 2},
    ]

    def fake_run(cmd, **kwargs):
        assert "message:OP-691 status:merged" in cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout="\n".join(json.dumps(line) for line in lines), stderr=""
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod.already_merged_in_gerrit("OP-691") == (
        127,
        "https://sora.services/+/127",
    )


def test_already_merged_in_gerrit_returns_none_for_body_only_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({
                "number": 127,
                "url": "https://sora.services/+/127",
                "subject": "fix shipped; body mentions OP-691",
            }),
            stderr="",
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod.already_merged_in_gerrit("OP-691") is None


@pytest.mark.parametrize(
    ("returncode", "raises"),
    [(1, None), (0, subprocess.TimeoutExpired(["ssh"], 10))],
)
def test_already_merged_in_gerrit_fails_open_on_query_errors(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    raises: Exception | None,
) -> None:
    mod = _load_jira_runner()

    def fake_run(cmd, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="network down")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod.already_merged_in_gerrit("OP-691") is None


@pytest.mark.parametrize(
    ("status", "transition_ids"),
    [
        ("To Do", ["21", "3", "4", "7"]),
        ("進行中", ["3", "4", "7"]),
        ("Under Review", ["4", "7"]),
        ("承認済み", ["7"]),
    ],
)
def test_force_walk_to_published_transitions_safe_predecessors(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    transition_ids: list[str],
) -> None:
    calls: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(jd, "get_issue_status", lambda client, key: status)

    def fake_request_idempotent(client, method, path, body, idem_key):
        calls.append((method, path, body))
        return {}

    monkeypatch.setattr(jd, "_request_idempotent", fake_request_idempotent)

    assert jd.force_walk_to_published(_StubClient(), "OP-691", idem_key="h12") is True
    assert [
        body["transition"]["id"] for method, path, body in calls
        if path == "/issue/OP-691/transitions"
    ] == transition_ids


def test_force_walk_to_published_is_noop_when_already_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jd, "get_issue_status", lambda client, key: "公開済み")
    monkeypatch.setattr(
        jd,
        "_request_idempotent",
        lambda *args, **kwargs: pytest.fail("already Published must not transition"),
    )

    assert jd.force_walk_to_published(_StubClient(), "OP-691", idem_key="h12") is False


def test_runner_self_heals_merged_ticket_and_skips_pickup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_jira_runner()
    calls: dict[str, list] = {"force": [], "comment": []}

    monkeypatch.setattr(mod, "TARGET_OVERRIDE", "OP-691")
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod.circuit_breaker, "open_services", lambda: [])
    monkeypatch.setattr(mod.jira_dispatch, "assert_worktree_config_enabled", lambda repo: None)
    monkeypatch.setattr(mod.orphan_salvage, "salvage_orphan_commits", lambda path, cls: 0)
    monkeypatch.setattr(mod.jira_dispatch, "backpressure_decide", lambda *a, **kw: (True, "active"))
    monkeypatch.setattr(mod.jira_dispatch, "make_client", lambda *a, **kw: _StubClient())
    monkeypatch.setattr(mod.jira_dispatch, "_request", lambda client, method, path: _issue())
    monkeypatch.setattr(mod.jira_dispatch, "to_snapshot", jd.to_snapshot)
    monkeypatch.setattr(
        mod,
        "already_merged_in_gerrit",
        lambda key: (127, "https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/127"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "force_walk_to_published",
        lambda client, key: calls["force"].append((key,)) or True,
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda client, key, text: calls["comment"].append((key, text)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "set_bot_identity_in_worktree",
        lambda *args, **kwargs: pytest.fail("self-heal must skip worktree preparation"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "transition_to_in_progress",
        lambda *args, **kwargs: pytest.fail("self-heal must skip pickup"),
    )
    monkeypatch.setattr(
        mod,
        "_invoke_cli",
        lambda *args, **kwargs: pytest.fail("self-heal must skip CLI"),
    )

    assert mod.main() == 0
    assert calls["force"] == [("OP-691",)]
    assert len(calls["comment"]) == 1
    assert calls["comment"][0][0] == "OP-691"
    assert "[runner-h12-self-heal]" in calls["comment"][0][1]
    assert "#127" in calls["comment"][0][1]
    assert "already merged via Gerrit #127" in capsys.readouterr().out


def test_runner_self_heal_is_noop_when_force_walk_reports_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_jira_runner()

    monkeypatch.setattr(mod, "TARGET_OVERRIDE", "OP-691")
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod.circuit_breaker, "open_services", lambda: [])
    monkeypatch.setattr(mod.jira_dispatch, "assert_worktree_config_enabled", lambda repo: None)
    monkeypatch.setattr(mod.orphan_salvage, "salvage_orphan_commits", lambda path, cls: 0)
    monkeypatch.setattr(mod.jira_dispatch, "backpressure_decide", lambda *a, **kw: (True, "active"))
    monkeypatch.setattr(mod.jira_dispatch, "make_client", lambda *a, **kw: _StubClient())
    monkeypatch.setattr(mod.jira_dispatch, "_request", lambda client, method, path: _issue())
    monkeypatch.setattr(mod.jira_dispatch, "to_snapshot", jd.to_snapshot)
    monkeypatch.setattr(
        mod,
        "already_merged_in_gerrit",
        lambda key: (127, "https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/127"),
    )
    monkeypatch.setattr(mod.jira_dispatch, "force_walk_to_published", lambda client, key: False)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda *args, **kwargs: pytest.fail("already Published self-heal must not comment"),
    )

    assert mod.main() == 0
