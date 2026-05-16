"""OP-1057 Under Review idempotency-token contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from backend.agents.idempotency import IdempotencyStore
from backend.agents import jira_dispatch as jd


class _Client:
    agent_class = "subscription-codex"
    base_url = "https://jira.example/rest/api/3"
    auth_header = "Basic x"
    bot_account_id = "acct-1"


def test_idempotency_token_format_matches_architecture_section_7(monkeypatch) -> None:
    calls: list[str] = []

    def fake_request_idempotent(client, method, path, body, idem_key):
        calls.append(idem_key)
        return {}

    monkeypatch.setattr(jd, "get_issue_status", lambda client, key: "In Progress")
    monkeypatch.setattr(jd, "_request_idempotent", fake_request_idempotent)

    jd.transition_to_under_review_if_needed(_Client(), "OP-1057", change_number=42)

    canonical_json = json.dumps(
        {"target_status": "under-review", "patchset": 42},
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_hash = hashlib.sha256(canonical_json.encode()).hexdigest()[:12]
    assert calls == [f"OP-1057:transition:{expected_hash}"]
    assert re.fullmatch(r"OP-1057:transition:[0-9a-f]{12}", calls[0])


def test_replay_same_token_does_not_duplicate_transition(monkeypatch, tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "idem-keys.db")
    calls: list[tuple[str, str, str | None]] = []

    def fake_request(client, method, path, body=None, idem_key=None):
        calls.append((method, path, idem_key))
        return {}

    monkeypatch.setattr(jd, "DEFAULT_STORE", store)
    monkeypatch.setattr(jd, "get_issue_status", lambda client, key: "In Progress")
    monkeypatch.setattr(jd, "_request", fake_request)

    jd.transition_to_under_review_if_needed(_Client(), "OP-1057", change_number=42)
    jd.transition_to_under_review_if_needed(_Client(), "OP-1057", change_number=42)

    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/issue/OP-1057/transitions"
    assert re.fullmatch(r"OP-1057:transition:[0-9a-f]{12}", calls[0][2] or "")


def test_replay_same_token_does_not_duplicate_comment(monkeypatch, tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "idem-keys.db")
    calls: list[tuple[str, str, str | None]] = []

    def fake_request(client, method, path, body=None, idem_key=None):
        calls.append((method, path, idem_key))
        return {}

    monkeypatch.setattr(jd, "DEFAULT_STORE", store)
    monkeypatch.setattr(jd, "_request", fake_request)

    idem_key = jd._under_review_idem_key("OP-1057", change_number=42)
    jd.post_runner_pushed_comment(_Client(), "OP-1057", "https://x/+/42", idem_key=f"{idem_key}-comment")
    jd.post_runner_pushed_comment(_Client(), "OP-1057", "https://x/+/42", idem_key=f"{idem_key}-comment")

    assert len(calls) == 1
    assert calls[0] == ("POST", "/issue/OP-1057/comment", f"{idem_key}-comment")


def test_no_uuid_fallback_in_runner_path(monkeypatch) -> None:
    calls: list[str] = []

    def fake_request_idempotent(client, method, path, body, idem_key):
        calls.append(idem_key)
        return {}

    monkeypatch.setattr(jd, "get_issue_status", lambda client, key: "In Progress")
    monkeypatch.setattr(jd, "_request_idempotent", fake_request_idempotent)

    jd.transition_to_under_review_if_needed(_Client(), "OP-1057", change_number=314)

    assert calls == [jd._under_review_idem_key("OP-1057", change_number=314)]
    assert "under-review" not in calls[0]
    assert re.fullmatch(r"OP-1057:transition:[0-9a-f]{12}", calls[0])
