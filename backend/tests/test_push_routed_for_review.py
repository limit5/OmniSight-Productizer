"""Tests for OP-2196 / R.3 — push_routed_for_review (GerritReviewDelivery).

Network-free: subprocess.run + the gerrit_ssh breaker are patched. Asserts the
push targets routed.gerrit_url (NOT productizer) with HEAD:routed.ref, and the
Change number/URL is parsed from Gerrit's output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents import jira_dispatch
from backend.agents.routed_repo import RoutedRepo


ROUTED = RoutedRepo(
    name="conference-appliance",
    gerrit_url="ssh://claude-bot@sora.services:29418/omnisight/conference-appliance",
    ref="refs/for/develop",
    git_account_ref=None,
    context="conference-appliance",
)

PUSH_OK = (
    "remote: \n"
    "remote:   https://sora.services:29420/c/omnisight/conference-appliance/+/4242 "
    "R.3 routed change [NEW]\n"
    "To ssh://.../conference-appliance\n"
)


@pytest.fixture
def fake_push(monkeypatch, tmp_path):
    calls = []

    def _run(argv, **kw):
        calls.append(list(argv))
        class _R:
            returncode = 0
            stdout = PUSH_OK
            stderr = ""
        return _R()

    monkeypatch.setattr(jira_dispatch.subprocess, "run", _run, raising=False)
    monkeypatch.setitem(
        jira_dispatch.BREAKERS, "gerrit_ssh",
        type("B", (), {"call": staticmethod(lambda fn, *a, **k: fn(*a, **k))})(),
    )
    monkeypatch.setattr(
        jira_dispatch, "_gerrit_auth_for_instance",
        lambda ac, iid=None: ("claude-bot", tmp_path / "key"), raising=False,
    )
    (tmp_path / "key").write_text("k")
    monkeypatch.setattr(
        jira_dispatch.runner_sandbox, "build_allowlisted_env",
        lambda extra=None: {"GIT_SSH_COMMAND": "ssh -i key"}, raising=False,
    )
    return calls


def test_pushes_to_routed_url_not_productizer(fake_push):
    res = jira_dispatch.push_routed_for_review(
        Path("/tmp/wt"), ROUTED, "subscription-claude", "claude-1",
    )
    push_argv = [c for c in fake_push if c[:2] == ["git", "push"]][0]
    assert ROUTED.gerrit_url in push_argv
    assert f"HEAD:{ROUTED.ref}" in push_argv
    # never targets productizer
    assert all("OmniSight-Productizer" not in a for a in push_argv)


def test_parses_change_number_and_url(fake_push):
    res = jira_dispatch.push_routed_for_review(
        Path("/tmp/wt"), ROUTED, "subscription-claude", "claude-1",
    )
    assert res.success is True
    assert res.change_number == 4242
    assert res.change_url.endswith("/conference-appliance/+/4242")


def test_push_failure_returns_unsuccessful(monkeypatch, tmp_path):
    def _run(argv, **kw):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "remote: error: prohibited by Gerrit"
        return _R()

    monkeypatch.setattr(jira_dispatch.subprocess, "run", _run, raising=False)
    monkeypatch.setitem(
        jira_dispatch.BREAKERS, "gerrit_ssh",
        type("B", (), {"call": staticmethod(lambda fn, *a, **k: fn(*a, **k))})(),
    )
    monkeypatch.setattr(
        jira_dispatch, "_gerrit_auth_for_instance",
        lambda ac, iid=None: ("claude-bot", tmp_path / "key"), raising=False,
    )
    (tmp_path / "key").write_text("k")
    monkeypatch.setattr(
        jira_dispatch.runner_sandbox, "build_allowlisted_env",
        lambda extra=None: {}, raising=False,
    )
    res = jira_dispatch.push_routed_for_review(
        Path("/tmp/wt"), ROUTED, "subscription-claude", "claude-1",
    )
    assert res.success is False
    assert "prohibited" in res.detail


def test_missing_ssh_key_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        jira_dispatch, "_gerrit_auth_for_instance",
        lambda ac, iid=None: ("claude-bot", tmp_path / "nope"), raising=False,
    )
    res = jira_dispatch.push_routed_for_review(
        Path("/tmp/wt"), ROUTED, "subscription-claude", "claude-1",
    )
    assert res.success is False
    assert "SSH key not found" in res.detail
