"""OP-1777 — env-scrub at the two subprocess call sites.

Companion to ``test_runner_sandbox.py`` (which covers the allowlist source
of truth + the bubblewrap ``--clearenv`` jail). Here we assert the *runtime*
subprocess envs equal the allowlist — no stray ``OMNISIGHT_*`` infra secret
leaks into:

  1. the agent CLI ``subprocess.Popen(env=…)`` (``auto-runner-jira.py``); and
  2. the git child ``subprocess.run(env=…)`` (``jira_dispatch.push_to_gerrit_for_review``).

Both call sites reuse ``runner_sandbox.build_allowlisted_env`` — the single
source of truth — so they cannot drift apart.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from backend.agents import jira_dispatch
from backend.agents import runner_sandbox as rs


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_env_scrub_uut", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_env_scrub_uut", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── CLI Popen env (auto-runner-jira.py) ────────────────────────────


def test_cli_popen_env_is_scrubbed_allowlist(tmp_path, monkeypatch):
    """``_invoke_cli`` spawns the CLI with env=<scrubbed allowlist> — the
    OMNISIGHT_* infra secret never reaches the agent (L1/L2)."""
    monkeypatch.delenv(rs.ENV_ENFORCE, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/bot")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OMNISIGHT_JIRA_TOKEN", "super-secret-token")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    mod = _load_jira_runner()
    monkeypatch.setattr(mod, "DRY_RUN", False)
    monkeypatch.setattr(mod, "CLAUDE_WORKTREE", str(worktree))

    captured: dict[str, Any] = {}

    class _FakeProc:
        returncode = 0

        def communicate(self, *a, **k):
            return ("", "")

        def kill(self):
            pass

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", _fake_popen)

    rc = mod._invoke_cli(
        "subscription-claude", "do the thing",
        ticket_key="OP-1777", worktree_path=worktree,
    )

    assert rc == 0
    env = captured["env"]
    assert env is not None, "Popen must receive an explicit scrubbed env"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert "OMNISIGHT_JIRA_TOKEN" not in env
    assert all(not k.startswith("OMNISIGHT_") for k in env)
    assert set(env).issubset(set(rs.ENV_ALLOWLIST))


# ─── git child env (jira_dispatch.push_to_gerrit_for_review) ────────


class _FakeBreaker:
    """Captures the kwargs the runner passes through the circuit breaker."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(self, fn, *args, **kwargs):
        self.calls.append(kwargs)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()


def test_git_child_env_is_minimal_allowlist(tmp_path, monkeypatch):
    """Git children get a minimal allowlisted env (PATH/HOME/... +
    GIT_SSH_COMMAND), NOT os.environ.copy() — OMNISIGHT_* never leaks (L3)."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/bot")
    monkeypatch.setenv("OMNISIGHT_JIRA_TOKEN", "super-secret-token")
    monkeypatch.setenv("OMNISIGHT_PROJECT_STATE_API_TOKEN", "ps-secret")

    ssh_key = tmp_path / "gerrit-key"
    ssh_key.write_text("PRIVATE")

    monkeypatch.setattr(
        jira_dispatch, "_gerrit_auth_for_instance",
        lambda agent_class, instance_id=None: ("codex-bot", ssh_key),
    )
    monkeypatch.setattr(jira_dispatch, "_head_change_id", lambda wt: None)
    monkeypatch.setattr(
        jira_dispatch, "_gerrit_ssh_url",
        lambda agent_class, instance_id=None: "ssh://gerrit.example/omnisight",
    )

    fake = _FakeBreaker()
    monkeypatch.setitem(jira_dispatch.BREAKERS, "gerrit_ssh", fake)

    jira_dispatch.push_to_gerrit_for_review(tmp_path, "subscription-codex")

    assert fake.calls, "git push must have run through the breaker"
    env = fake.calls[0]["env"]
    assert env["GIT_SSH_COMMAND"] == f"ssh -i {ssh_key}"
    assert env["PATH"] == "/usr/bin:/bin"
    assert "OMNISIGHT_JIRA_TOKEN" not in env
    assert "OMNISIGHT_PROJECT_STATE_API_TOKEN" not in env
    assert all(not k.startswith("OMNISIGHT_") for k in env)
    # Only the allowlist (+ the injected GIT_SSH_COMMAND, which is allowlisted).
    assert set(env).issubset(set(rs.ENV_ALLOWLIST))
