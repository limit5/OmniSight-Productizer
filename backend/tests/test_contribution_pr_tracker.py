"""OP-1847 — camviewpro PR-state to JIRA tracker."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import httpx

import backend
from backend.agents import contribution_pr_tracker as tracker
from backend.agents.scheduler import TicketSnapshot


TOKEN = "ghp_tracker_token"
REPO_URL = "https://github.com/limit5/camviewpro-android.git"


def _ticket(*labels: str) -> TicketSnapshot:
    return TicketSnapshot(
        key="OP-1847",
        component="META",
        fix_version=None,
        created_at="2026-05-29T00:00:00.000+0000",
        days_since_created=1.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


def _install_credential(monkeypatch):
    async def _fake(account_id, *, tenant_id=None, touch=True):
        _fake.seen = (account_id, tenant_id)
        return {
            "id": account_id,
            "platform": "github",
            "repo_url": REPO_URL,
            "token": TOKEN,
        }

    _fake.seen = None
    fake_module = SimpleNamespace(pick_by_id=_fake)
    monkeypatch.setitem(sys.modules, "backend.git_credentials", fake_module)
    monkeypatch.setattr(backend, "git_credentials", fake_module, raising=False)
    return _fake


class FakeGithubClient:
    def __init__(self, state: dict):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *, headers):
        self.state["calls"].append(("GET", url, dict(headers)))
        return httpx.Response(200, json=self.state["pr"])


def _install_github(monkeypatch, pr: dict) -> dict:
    state = {"calls": [], "pr": pr}

    def _client_factory(*, timeout):
        state["timeout"] = timeout
        return FakeGithubClient(state)

    monkeypatch.setattr(tracker.httpx, "AsyncClient", _client_factory)
    return state


def _install_jira(monkeypatch) -> dict:
    calls: dict[str, list] = {
        "under_review": [],
        "comments": [],
        "labels": [],
        "requests": [],
    }
    client = object()
    monkeypatch.setattr(tracker, "_jira_client", lambda: client)

    def _under_review(got_client, key, idem_key=None, change_number=None):
        calls["under_review"].append((got_client, key, idem_key, change_number))
        assert got_client is client

    def _comment(got_client, key, text, idem_key=None):
        calls["comments"].append((got_client, key, text, idem_key))
        assert got_client is client

    def _label(got_client, key, label, idem_key=None):
        calls["labels"].append((got_client, key, label, idem_key))
        assert got_client is client

    def _request(got_client, method, path, body=None, idem_key=None):
        calls["requests"].append((method, path, body, idem_key))
        assert got_client is client
        if method == "GET" and path.endswith("/transitions"):
            return {"transitions": [{"id": "31", "name": "Done", "to": {"name": "Done"}}]}
        return {}

    def _request_idempotent(got_client, method, path, body, idem_key):
        calls["requests"].append((method, path, body, idem_key))
        assert got_client is client
        return {}

    monkeypatch.setattr(
        tracker.jira_dispatch,
        "transition_to_under_review_if_needed",
        _under_review,
    )
    monkeypatch.setattr(tracker.jira_dispatch, "add_comment", _comment)
    monkeypatch.setattr(tracker.jira_dispatch, "add_label", _label)
    monkeypatch.setattr(tracker.jira_dispatch, "_request", _request)
    monkeypatch.setattr(
        tracker.jira_dispatch,
        "_request_idempotent",
        _request_idempotent,
    )
    return calls


def _install_audit(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def _record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tracker, "_record_regulated_lane_event", _record)
    return calls


def _sync(ticket: TicketSnapshot, **kwargs) -> tracker.PrSyncOutcome:
    data = {"git_account_ref": "camviewpro-pr", "tenant_id": "t-op"}
    data.update(kwargs)
    return asyncio.run(tracker.sync_contribution_pr_state(ticket, **data))


def _done_transition_fired(jira: dict) -> bool:
    return any(
        call[0] == "POST" and call[1] == "/issue/OP-1847/transitions"
        for call in jira["requests"]
    )


def test_no_camviewpro_pr_label_skips_without_api_call(monkeypatch):
    pick = _install_credential(monkeypatch)
    http = _install_github(monkeypatch, {"state": "open", "merged": False})
    jira = _install_jira(monkeypatch)

    outcome = _sync(_ticket("area:backend"))

    assert outcome == tracker.PrSyncOutcome("OP-1847", None, None, "none")
    assert pick.seen is None
    assert http["calls"] == []
    assert jira["under_review"] == []
    assert jira["comments"] == []
    assert jira["labels"] == []


def test_open_regulated_lane_pr_moves_ticket_to_under_review(monkeypatch):
    pick = _install_credential(monkeypatch)
    http = _install_github(
        monkeypatch,
        {
            "state": "open",
            "merged": False,
            "labels": [{"name": tracker.REGULATED_LANE_LABEL}],
        },
    )
    jira = _install_jira(monkeypatch)

    outcome = _sync(_ticket("camviewpro-pr:42"))

    assert outcome == tracker.PrSyncOutcome(
        "OP-1847", 42, "open", "to_under_review"
    )
    assert jira["under_review"][0][1] == "OP-1847"
    assert http["calls"][0][1] == (
        "https://api.github.com/repos/limit5/camviewpro-android/pulls/42"
    )
    assert http["calls"][0][2]["Authorization"] == f"Bearer {TOKEN}"
    assert pick.seen == ("camviewpro-pr", "t-op")


def test_merged_regulated_lane_pr_holds_for_regulatory_clearance(monkeypatch):
    _install_credential(monkeypatch)
    _install_github(
        monkeypatch,
        {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "abc123def456",
            "html_url": "https://github.com/limit5/camviewpro-android/pull/42",
            "labels": [{"name": tracker.REGULATED_LANE_LABEL}],
        },
    )
    jira = _install_jira(monkeypatch)
    audit = _install_audit(monkeypatch)

    outcome = _sync(_ticket("camviewpro-pr:42"))

    assert outcome == tracker.PrSyncOutcome("OP-1847", 42, "merged", "hold_regulated")
    assert "abc123def456" in jira["comments"][0][2]
    assert jira["labels"][0][2] == tracker.REGULATORY_CLEARED_REQUIRED_LABEL
    assert not _done_transition_fired(jira)
    assert audit == [
        {
            "ticket_key": "OP-1847",
            "pr_number": 42,
            "pr_url": "https://github.com/limit5/camviewpro-android/pull/42",
            "action": "hold_regulated",
            "merge_sha": "abc123def456",
            "pr_state": "merged",
        }
    ]


def test_merged_pr_without_regulated_lane_moves_ticket_to_done(monkeypatch):
    _install_credential(monkeypatch)
    _install_github(
        monkeypatch,
        {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "abc123def456",
            "labels": [],
        },
    )
    jira = _install_jira(monkeypatch)
    audit = _install_audit(monkeypatch)

    outcome = _sync(_ticket("camviewpro-pr:42"))

    assert outcome == tracker.PrSyncOutcome("OP-1847", 42, "merged", "to_done")
    assert ("POST", "/issue/OP-1847/transitions", {"transition": {"id": "31"}}, "contribution-pr-OP-1847-to-done") in jira["requests"]
    assert "abc123def456" in jira["comments"][0][2]
    assert audit == []


def test_closed_unmerged_regulated_lane_flags_for_human_triage_without_done(monkeypatch):
    _install_credential(monkeypatch)
    _install_github(
        monkeypatch,
        {
            "state": "closed",
            "merged": False,
            "html_url": "https://github.com/limit5/camviewpro-android/pull/42",
            "labels": [{"name": tracker.REGULATED_LANE_LABEL}],
        },
    )
    jira = _install_jira(monkeypatch)
    audit = _install_audit(monkeypatch)

    outcome = _sync(_ticket("camviewpro-pr:42"))

    assert outcome == tracker.PrSyncOutcome("OP-1847", 42, "closed", "flag_closed")
    assert "closed without merge" in jira["comments"][0][2]
    assert jira["labels"][0][2] == "camviewpro-pr-closed"
    assert not _done_transition_fired(jira)
    assert audit == [
        {
            "ticket_key": "OP-1847",
            "pr_number": 42,
            "pr_url": "https://github.com/limit5/camviewpro-android/pull/42",
            "action": "flag_closed",
            "merge_sha": None,
            "pr_state": "closed",
        }
    ]


def test_open_regulated_lane_pr_does_not_record_audit(monkeypatch):
    _install_credential(monkeypatch)
    _install_github(
        monkeypatch,
        {
            "state": "open",
            "merged": False,
            "labels": [{"name": tracker.REGULATED_LANE_LABEL}],
        },
    )
    _install_jira(monkeypatch)
    audit = _install_audit(monkeypatch)

    outcome = _sync(_ticket("camviewpro-pr:42"))

    assert outcome == tracker.PrSyncOutcome(
        "OP-1847", 42, "open", "to_under_review"
    )
    assert audit == []


def test_batch_helper_returns_outcomes(monkeypatch):
    _install_credential(monkeypatch)
    _install_github(monkeypatch, {"state": "open", "merged": False})
    _install_jira(monkeypatch)

    outcomes = asyncio.run(
        tracker.sync_all_open_contributions(
            [_ticket("camviewpro-pr:42"), _ticket("area:tests")],
            git_account_ref="camviewpro-pr",
            tenant_id="t-op",
        )
    )

    assert [outcome.action for outcome in outcomes] == ["to_under_review", "none"]
