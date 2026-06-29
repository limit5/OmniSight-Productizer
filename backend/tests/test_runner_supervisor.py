"""L1 rule-engine supervisor (OP-2488): rules detect + take NON-DESTRUCTIVE
actions; dry-run mutates nothing; only bot-owned wedges are touched; the zombie
rule is escalate-only (no race); run_once is resilient to a single rule fault."""
from __future__ import annotations

import pytest

from backend.agents import runner_supervisor as rs
from backend.agents import jira_dispatch


def _issue(key, *, labels=None, assignee=None):
    return {"key": key, "fields": {"labels": labels or [], "assignee": assignee}}


BOT = {"displayName": "claude-bot"}
HUMAN = {"displayName": "sora"}


@pytest.fixture
def recorder(monkeypatch):
    calls: list = []
    monkeypatch.setattr(jira_dispatch, "remove_label", lambda c, k, l, **kw: calls.append(("remove_label", k, l)))
    monkeypatch.setattr(jira_dispatch, "clear_assignee", lambda c, k, **kw: calls.append(("clear_assignee", k)))
    monkeypatch.setattr(jira_dispatch, "add_label", lambda c, k, l, **kw: calls.append(("add_label", k, l)))
    monkeypatch.setattr(jira_dispatch, "add_comment", lambda c, k, t, **kw: calls.append(("add_comment", k)))
    return calls


def test_stale_claim_strips_and_comments(monkeypatch, recorder):
    monkeypatch.setattr(rs, "_search", lambda c, jql, **kw: [
        _issue("OP-1", labels=["claim:claude-1:123", "agent:auto"]),
        _issue("OP-2", labels=["other"]),
    ])
    findings = rs._rule_stale_claim_on_todo(object(), dry_run=False)
    assert [f.key for f in findings] == ["OP-1"]
    assert ("remove_label", "OP-1", "claim:claude-1:123") in recorder
    assert ("add_comment", "OP-1") in recorder


def test_stale_claim_dry_run_no_mutation(monkeypatch, recorder):
    monkeypatch.setattr(rs, "_search", lambda c, jql, **kw: [_issue("OP-1", labels=["claim:x:1"])])
    findings = rs._rule_stale_claim_on_todo(object(), dry_run=True)
    assert [f.key for f in findings] == ["OP-1"]
    assert recorder == []


def test_assignee_clears_bot_not_human(monkeypatch, recorder):
    monkeypatch.setattr(rs, "_search", lambda c, jql, **kw: [
        _issue("OP-1", labels=["agent:auto"], assignee=BOT),
        _issue("OP-2", labels=["agent:auto"], assignee=HUMAN),
    ])
    findings = rs._rule_assignee_stuck_on_todo(object(), dry_run=False)
    assert [f.key for f in findings] == ["OP-1"]
    assert ("clear_assignee", "OP-1") in recorder
    assert all(c[1] != "OP-2" for c in recorder)


def test_zombie_escalate_only_idempotent_botonly(monkeypatch, recorder):
    monkeypatch.setattr(rs, "_search", lambda c, jql, **kw: [
        _issue("OP-1", labels=[], assignee=BOT),
        _issue("OP-2", labels=["supervisor:stuck-in-progress"], assignee=BOT),
        _issue("OP-3", labels=[], assignee=HUMAN),
    ])
    findings = rs._rule_zombie_in_progress(object(), dry_run=False)
    assert [f.key for f in findings] == ["OP-1"]
    assert ("add_label", "OP-1", "supervisor:stuck-in-progress") in recorder
    assert all(c[0] not in ("clear_assignee", "remove_label") for c in recorder)


def test_run_once_resilient_to_a_rule_fault(monkeypatch, recorder):
    def boom(c, *, dry_run):
        raise RuntimeError("jira down")

    def good(c, *, dry_run):
        return [rs.Finding("OP-9", "good", "d", ("x",), dry_run)]

    monkeypatch.setattr(rs, "RULES", (boom, good))
    findings = rs.run_once(object(), dry_run=True)
    assert [f.key for f in findings] == ["OP-9"]
