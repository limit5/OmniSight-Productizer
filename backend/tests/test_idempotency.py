"""OP-749 runner idempotency tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import audit
from backend.agents.idempotency import IdempotencyStore
from backend.agents import jira_dispatch as jd


class _Client:
    agent_class = "subscription-codex"
    base_url = "https://jira.example/rest/api/3"
    auth_header = "Basic x"
    bot_account_id = "acct-1"


def test_idempotency_store_replays_cached_success(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "idem-keys.db")
    calls = {"count": 0}

    def mutate() -> dict[str, str]:
        calls["count"] += 1
        return {"ok": "yes"}

    assert store.run("comment-OP-749-fixed", mutate) == {"ok": "yes"}
    assert store.run("comment-OP-749-fixed", mutate) == {"ok": "yes"}
    assert calls["count"] == 1


def test_idempotency_store_prunes_after_24h(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "idem-keys.db")
    store.put("stale", {"ok": True}, now=100)

    assert store.get("stale", now=100 + store.ttl_seconds + 1) is None


def test_jira_mutations_share_stable_subkeys(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_request(client, method, path, body, idem_key):
        calls.append((method, path, idem_key))
        return {}

    monkeypatch.setattr(jd, "_request_idempotent", fake_request)
    monkeypatch.setattr(jd, "get_issue_status", lambda client, key: "In Progress")

    jd.transition_to_in_progress(_Client(), "OP-749", idem_key="pickup-1")
    jd.transition_back_to_todo(_Client(), "OP-749", "reason", idem_key="todo-1")
    jd.transition_to_under_review_if_needed(_Client(), "OP-749", idem_key="review-1")
    jd.add_comment(_Client(), "OP-749", "done", idem_key="comment-1")
    jd.clear_assignee(_Client(), "OP-749", idem_key="clear-1")

    assert ("PUT", "/issue/OP-749", "pickup-1-assign") in calls
    assert ("POST", "/issue/OP-749/transitions", "pickup-1-transition") in calls
    assert ("POST", "/issue/OP-749/comment", "pickup-1-comment") in calls
    assert ("PUT", "/issue/OP-749", "todo-1-clear-assignee") in calls
    assert ("POST", "/issue/OP-749/transitions", "todo-1-transition") in calls
    assert ("POST", "/issue/OP-749/transitions", "review-1") in calls
    assert ("POST", "/issue/OP-749/comment", "comment-1") in calls
    assert ("PUT", "/issue/OP-749", "clear-1") in calls


@pytest.mark.asyncio
async def test_audit_log_dedups_same_action_entity_second() -> None:
    class FakeConn:
        def __init__(self) -> None:
            self.fetchrow_calls = 0
            self.insert_seen = False

        async def execute(self, *args, **kwargs):
            return None

        async def fetchrow(self, query, *args):
            self.fetchrow_calls += 1
            if "ORDER BY id DESC LIMIT 1" in query:
                return None
            if "SELECT id FROM audit_log" in query:
                return {"id": 7}
            if "INSERT INTO audit_log" in query:
                self.insert_seen = True
                return {"id": 8}
            raise AssertionError(query)

    conn = FakeConn()

    row_id = await audit._log_impl(
        conn,
        "ticket_transition",
        "jira_issue",
        "OP-749",
        None,
        {"status": "In Progress"},
        "runner",
        None,
    )

    assert row_id == 7
    assert conn.insert_seen is False
