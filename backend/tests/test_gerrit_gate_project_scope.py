"""Tests for OP-2197 / R.4 — additive project-scoping of shared Gerrit gates.

The 5 (well, 6) shared Gerrit queries gain an optional gerrit_project param.
None → query byte-identical to pre-R.4 (productizer-only default). Set → a
`project:<x>` filter is prepended. These tests pin both the helpers and the
additive query shape by capturing the gerrit-query argv.
"""

from __future__ import annotations

import pytest

from backend.agents import jira_dispatch as j


# ── helpers ──────────────────────────────────────────────────────────────
def test_project_filter_none_is_empty():
    assert j._project_filter(None) == ""


def test_project_filter_set():
    assert j._project_filter("omnisight/conference-appliance") == "project:omnisight/conference-appliance "


def test_gerrit_project_from_url():
    assert j.gerrit_project_from_url(
        "ssh://claude-bot@sora.services:29418/omnisight/conference-appliance"
    ) == "omnisight/conference-appliance"
    assert j.gerrit_project_from_url("ssh://h/omnisight/x.git") == "omnisight/x"


def test_gerrit_project_for_labels_none_for_normal(monkeypatch):
    monkeypatch.setattr(j.routed_repo.settings, "routed_repos", "", raising=False)
    assert j.gerrit_project_for_labels(["area:backend"]) is None


def test_gerrit_project_for_labels_routed(monkeypatch):
    monkeypatch.setattr(
        j.routed_repo.settings, "routed_repos",
        '{"conference-appliance": {"gerrit_url": "ssh://h/omnisight/conference-appliance"}}',
        raising=False,
    )
    assert j.gerrit_project_for_labels(["repo:conference-appliance"]) == "omnisight/conference-appliance"


def test_gerrit_project_for_labels_failsoft_on_unresolved(monkeypatch):
    # unresolved repo: → None here (R.1 is where the abstain happens, not the gate)
    monkeypatch.setattr(j.routed_repo.settings, "routed_repos", "", raising=False)
    assert j.gerrit_project_for_labels(["repo:nope"]) is None


# ── additive query shape (capture argv) ──────────────────────────────────
@pytest.fixture
def capture_query(monkeypatch):
    seen = {}

    def _run(argv, **kw):
        seen["argv"] = list(argv)
        class _R:
            returncode = 0
            stdout = '{"type":"stats","rowCount":0}\n'
            stderr = ""
            def check_returncode(self): pass
        return _R()

    monkeypatch.setattr(j.subprocess, "run", _run, raising=False)
    monkeypatch.setitem(
        j.BREAKERS, "gerrit_ssh",
        type("B", (), {"call": staticmethod(lambda fn, *a, **k: fn(*a, **k))})(),
    )
    monkeypatch.setattr(j, "_gerrit_auth_for_bot", lambda bot: (bot, "/k"), raising=False)
    return seen


def test_backpressure_none_unchanged(capture_query):
    j.open_ps_count_for("claude-bot")
    q = capture_query["argv"][-1]
    assert q == "is:open owner:claude-bot"  # byte-identical default


def test_backpressure_scoped(capture_query):
    j.open_ps_count_for("claude-bot", gerrit_project="omnisight/conference-appliance")
    q = capture_query["argv"][-1]
    assert q == "project:omnisight/conference-appliance is:open owner:claude-bot"
