"""OP-732 runner backpressure tests.

Pins the Gerrit PS-count state machine and the auto-runner-jira.py
short-circuit before JIRA pickup.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents import jira_dispatch as jd


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_backpressure_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_backpressure_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_open_ps_count_for_parses_gerrit_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResult:
        stdout = "\n".join([
            json.dumps({"project": "omnisight/OmniSight-Productizer"}),
            "not-json",
            json.dumps({"type": "stats", "rowCount": 9}),
        ])

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeResult()

    monkeypatch.setattr(jd.subprocess, "run", fake_run)

    assert jd.open_ps_count_for("claude-bot") == 9
    cmd, kwargs = calls[0]
    assert cmd[:3] == ["ssh", "-i", str(jd._GERRIT_AUTH_BY_CLASS["subscription-claude"][1])]
    assert "claude-bot@sora.services" in cmd
    assert "is:open owner:claude-bot" in cmd
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 10


def test_runner_ps_threshold_settings_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.config import Settings

    monkeypatch.delenv("OMNISIGHT_RUNNER_PS_CAP", raising=False)
    monkeypatch.delenv("OMNISIGHT_RUNNER_PS_FLOOR", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.runner_ps_cap == 8
    assert defaults.runner_ps_floor == 4

    monkeypatch.setenv("OMNISIGHT_RUNNER_PS_CAP", "12")
    monkeypatch.setenv("OMNISIGHT_RUNNER_PS_FLOOR", "6")
    overridden = Settings(_env_file=None)
    assert overridden.runner_ps_cap == 12
    assert overridden.runner_ps_floor == 6


def test_backpressure_pauses_and_notifies_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_file = tmp_path / "subscription-claude.state"
    alerts = []

    monkeypatch.setattr(jd, "_backpressure_state_file", lambda agent_class: state_file)
    monkeypatch.setattr(jd.settings, "runner_ps_cap", 8)
    monkeypatch.setattr(jd.settings, "runner_ps_floor", 4)
    monkeypatch.setattr(jd, "notify_operator", lambda **kwargs: alerts.append(kwargs))
    monkeypatch.setattr(jd, "open_ps_count_for", lambda bot_username: 8)

    ok, reason = jd.backpressure_decide("subscription-claude")

    assert ok is False
    assert reason == "8 open PSes (cap 8)"
    assert state_file.read_text() == "paused"
    assert len(alerts) == 1
    assert alerts[0]["channel"] == "runner-alerts"
    assert "subscription-claude runner paused" in alerts[0]["detail"]

    ok, reason = jd.backpressure_decide("subscription-claude")

    assert ok is False
    assert reason == "8 open PSes (cap 8, floor 4)"
    assert state_file.read_text() == "paused"
    assert len(alerts) == 1


@pytest.mark.parametrize("count", [5, 6, 7])
def test_backpressure_hysteresis_stays_paused_above_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, count: int
) -> None:
    state_file = tmp_path / "subscription-claude.state"
    state_file.write_text("paused")

    monkeypatch.setattr(jd, "_backpressure_state_file", lambda agent_class: state_file)
    monkeypatch.setattr(jd.settings, "runner_ps_cap", 8)
    monkeypatch.setattr(jd.settings, "runner_ps_floor", 4)
    monkeypatch.setattr(jd, "notify_operator", lambda **kwargs: pytest.fail("must not re-notify"))
    monkeypatch.setattr(jd, "open_ps_count_for", lambda bot_username: count)

    ok, reason = jd.backpressure_decide("subscription-claude")

    assert ok is False
    assert reason == f"{count} open PSes (cap 8, floor 4)"
    assert state_file.read_text() == "paused"


def test_backpressure_resumes_at_floor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_file = tmp_path / "subscription-claude.state"
    state_file.write_text("paused")

    monkeypatch.setattr(jd, "_backpressure_state_file", lambda agent_class: state_file)
    monkeypatch.setattr(jd.settings, "runner_ps_cap", 8)
    monkeypatch.setattr(jd.settings, "runner_ps_floor", 4)
    monkeypatch.setattr(jd, "open_ps_count_for", lambda bot_username: 4)

    ok, reason = jd.backpressure_decide("subscription-claude")

    assert ok is True
    assert reason == "resumed: 4 open PSes (floor 4)"
    assert not state_file.exists()


def test_backpressure_allows_active_runner_below_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_file = tmp_path / "subscription-claude.state"

    monkeypatch.setattr(jd, "_backpressure_state_file", lambda agent_class: state_file)
    monkeypatch.setattr(jd.settings, "runner_ps_cap", 8)
    monkeypatch.setattr(jd.settings, "runner_ps_floor", 4)
    monkeypatch.setattr(jd, "open_ps_count_for", lambda bot_username: 7)

    ok, reason = jd.backpressure_decide("subscription-claude")

    assert ok is True
    assert reason == "active: 7 open PSes (cap 8)"
    assert not state_file.exists()


def test_runner_paused_exits_zero_before_jira_pickup(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    mod = _load_jira_runner()
    monkeypatch.setattr(mod, "AGENT_CLASS", "subscription-claude")
    monkeypatch.setattr(
        mod.jira_dispatch,
        "backpressure_decide",
        lambda agent_class: (False, "8 open PSes (cap 8)"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "make_client",
        lambda agent_class: pytest.fail("make_client must not run while paused"),
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "fetch_pickable_tickets",
        lambda client: pytest.fail("JIRA pickup must not run while paused"),
    )

    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "[runner] backpressure paused: 8 open PSes (cap 8). Sleeping until next tick." in out


def test_runner_active_continues_to_target_flow(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    mod = _load_jira_runner()
    calls = {"make_client": 0, "request": 0}

    class StubClient:
        agent_class = "subscription-codex"
        bot_email = "rt3628+codex-bot@gmail.com"
        bot_account_id = "acc-1"

    issue = {
        "key": "OP-732",
        "fields": {
            "summary": "Backpressure",
            "labels": ["class:subscription-codex", "tier:S", "area:backend"],
            "status": {"name": "To Do"},
            "issuetype": {"name": "Story"},
            "fixVersions": [],
            "created": "2026-05-08T00:00:00.000+0000",
            "components": [{"name": "META"}],
        },
    }

    def make_client(agent_class):
        calls["make_client"] += 1
        return StubClient()

    def request(client, method, path, body=None):
        calls["request"] += 1
        return issue

    monkeypatch.setattr(mod, "AGENT_CLASS", "subscription-codex")
    monkeypatch.setattr(mod, "TARGET_OVERRIDE", "OP-732")
    monkeypatch.setattr(mod, "DRY_RUN", True)
    monkeypatch.setattr(
        mod.jira_dispatch,
        "backpressure_decide",
        lambda agent_class: (True, "active: 7 open PSes (cap 8)"),
    )
    monkeypatch.setattr(mod.jira_dispatch, "make_client", make_client)
    monkeypatch.setattr(mod.jira_dispatch, "_request", request)
    monkeypatch.setattr(mod.jira_dispatch, "pre_pickup_ok", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(mod.jira_dispatch, "fetch_description", lambda client, key: "desc")

    assert mod.main() == 0
    assert calls == {"make_client": 1, "request": 2}
    assert "DRY_RUN: would transition OP-732" in capsys.readouterr().out
