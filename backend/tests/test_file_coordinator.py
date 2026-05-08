"""OP-734 file-touch coordinator tests."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import file_coordinator as fc
from backend.agents import jira_dispatch as jd
from backend.agents import scheduler


def _client() -> jd.DispatchClient:
    return jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://test.invalid/rest/api/3",
        project_key="OP",
        auth_header="Basic test",
        bot_account_id="acc-test",
        bot_email="bot@example.invalid",
    )


def _ticket(
    key: str,
    created_at: str,
    *,
    description: str = "## Files / Paths\n- docs/operations/multi-provider-setup.md\n",
    labels: tuple[str, ...] = (),
) -> fc.FileCoordinatorTicket:
    return fc.FileCoordinatorTicket(
        key=key,
        labels=labels,
        description=description,
        created_at=created_at,
    )


def _snapshot(key: str = "OP-97") -> scheduler.TicketSnapshot:
    return scheduler.TicketSnapshot(
        key=key,
        component="META",
        fix_version=None,
        created_at="2026-05-08T00:00:00.000+0000",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=(),
    )


def test_build_file_graph_groups_four_tickets_sharing_one_file_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets = [
        _ticket("OP-107", "2026-05-08T04:00:00.000+0000"),
        _ticket("OP-93", "2026-05-08T01:00:00.000+0000"),
        _ticket("OP-105", "2026-05-08T03:00:00.000+0000"),
        _ticket("OP-97", "2026-05-08T02:00:00.000+0000"),
    ]
    monkeypatch.setattr(fc, "jira_search_jql", lambda client, jql: tickets)

    graph = fc.build_file_graph(_client())

    assert graph["docs/operations/multi-provider-setup.md"] == ["OP-93", "OP-97", "OP-105", "OP-107"]


def test_serialize_file_chains_creates_three_links_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing: set[tuple[str, str]] = set()
    created: list[tuple[str, str]] = []
    comments: list[tuple[str, str]] = []

    def fake_create(client, *, inward, outward, link_type="Blocks"):
        existing.add((outward, inward))
        created.append((inward, outward))

    monkeypatch.setattr(fc, "jira_link_exists", lambda client, *, blocked, blocker, link_type="Blocks": (blocked, blocker) in existing)
    monkeypatch.setattr(fc, "jira_create_issue_link", fake_create)
    monkeypatch.setattr(jd, "add_comment", lambda client, key, text, idem_key=None: comments.append((key, text)))

    graph = {"docs/operations/multi-provider-setup.md": ["OP-93", "OP-97", "OP-105", "OP-107"]}
    assert fc.serialize_file_chains(_client(), graph) == 3
    assert fc.serialize_file_chains(_client(), graph) == 0

    assert created == [("OP-93", "OP-97"), ("OP-97", "OP-105"), ("OP-105", "OP-107")]
    assert [key for key, _ in comments] == ["OP-97", "OP-105", "OP-107"]


def test_skip_file_coordinator_label_bypasses_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets = [
        _ticket("OP-1", "2026-05-08T01:00:00.000+0000"),
        _ticket("OP-2", "2026-05-08T02:00:00.000+0000", labels=(fc.SKIP_FILE_COORDINATOR_LABEL,)),
    ]
    monkeypatch.setattr(fc, "jira_search_jql", lambda client, jql: tickets)

    assert fc.build_file_graph(_client()) == {}


def test_cycle_detection_logs_warning_and_leaves_pair_unlinked(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    created: list[tuple[str, str]] = []
    monkeypatch.setattr(fc, "jira_link_exists", lambda *a, **kw: False)
    monkeypatch.setattr(fc, "jira_create_issue_link", lambda client, *, inward, outward, link_type="Blocks": created.append((inward, outward)))
    monkeypatch.setattr(jd, "add_comment", lambda *a, **kw: None)

    with caplog.at_level(logging.WARNING):
        count = fc.serialize_file_chains(_client(), {"a.py": ["OP-A", "OP-B"], "b.py": ["OP-B", "OP-A"]})

    assert count == 0
    assert created == []
    assert "Skipping file-coordinator cycle" in caplog.text


def test_pre_pickup_ok_returns_false_for_unresolved_blockedby(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jd, "fetch_description", lambda c, k: "## Goal\n")
    monkeypatch.setattr(jd, "migration_freeze_check", lambda c, s, description=None: (True, "no freeze"))
    monkeypatch.setattr(fc, "jira_get_blocked_by", lambda c, k: ["OP-93"])
    monkeypatch.setattr(fc, "jira_get_state", lambda c, k: "Under Review")

    ok, reason = jd.pre_pickup_ok(_client(), _snapshot("OP-97"))

    assert ok is False
    assert reason == "blocked-by:OP-93 blocked by OP-93 (state=Under Review)"


def test_pre_pickup_ok_allows_next_ticket_when_blocker_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jd, "fetch_description", lambda c, k: "## Goal\n")
    monkeypatch.setattr(jd, "migration_freeze_check", lambda c, s, description=None: (True, "no freeze"))
    monkeypatch.setattr(fc, "jira_get_blocked_by", lambda c, k: ["OP-93"])
    monkeypatch.setattr(fc, "jira_get_state", lambda c, k: "公開済み")
    monkeypatch.setattr(jd, "find_mutex_holders", lambda c, m, exclude_key: [])

    ok, reason = jd.pre_pickup_ok(_client(), _snapshot("OP-97"))

    assert ok is True
    assert reason == "pre-pickup checks passed"


def test_synthetic_op_93_97_105_107_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets = [
        _ticket("OP-93", "2026-05-08T01:00:00.000+0000"),
        _ticket("OP-97", "2026-05-08T02:00:00.000+0000"),
        _ticket("OP-105", "2026-05-08T03:00:00.000+0000"),
        _ticket("OP-107", "2026-05-08T04:00:00.000+0000"),
    ]
    links: set[tuple[str, str]] = set()
    states = {"OP-93": "Under Review", "OP-97": "To Do", "OP-105": "To Do", "OP-107": "To Do"}

    monkeypatch.setattr(fc, "jira_search_jql", lambda client, jql: tickets)
    monkeypatch.setattr(fc, "jira_link_exists", lambda client, *, blocked, blocker, link_type="Blocks": (blocked, blocker) in links)
    monkeypatch.setattr(fc, "jira_create_issue_link", lambda client, *, inward, outward, link_type="Blocks": links.add((outward, inward)))
    monkeypatch.setattr(jd, "add_comment", lambda *a, **kw: None)
    monkeypatch.setattr(fc, "jira_get_blocked_by", lambda c, k: [blocker for blocked, blocker in links if blocked == k])
    monkeypatch.setattr(fc, "jira_get_state", lambda c, k: states[k])

    graph = fc.build_file_graph(_client())
    assert fc.serialize_file_chains(_client(), graph) == 3
    assert not fc.has_unresolved_blockedby(_client(), _snapshot("OP-93"))[0]
    assert fc.has_unresolved_blockedby(_client(), _snapshot("OP-97")) == (
        True,
        "blocked by OP-93 (state=Under Review)",
    )

    states["OP-93"] = "公開済み"
    assert fc.has_unresolved_blockedby(_client(), _snapshot("OP-97")) == (
        False,
        "all blockers resolved",
    )
