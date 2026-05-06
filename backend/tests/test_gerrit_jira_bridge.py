"""Contract tests for backend.agents.gerrit_jira_bridge."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import gerrit_jira_bridge as bridge
from backend.agents import jira_dispatch


def _client() -> jira_dispatch.DispatchClient:
    return jira_dispatch.DispatchClient(
        agent_class="subscription-claude",
        base_url="https://example.atlassian.net/rest/api/3",
        project_key="OP",
        auth_header="Basic test",
        bot_account_id="acct-1",
        bot_email="rt3628+claude-bot@gmail.com",
    )


class FakeBridge(bridge.GerritJiraBridge):
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.statuses: dict[str, str] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.approved: list[dict[str, Any]] = []
        self.gerrit: dict[str, bridge.GerritChange | None] = {}
        self.logs: list[tuple[str, str, dict[str, Any]]] = []
        super().__init__(
            _client(),
            bridge.BridgeConfig(heartbeat_seconds=9999, periodic_catchup_seconds=0),
            sleep=lambda _: None,
            logger=self._log,
        )

    def _log(self, level: str, event: str, **extra: Any) -> None:
        self.logs.append((level, event, extra))

    def jira_request(self, method: str, path: str, body: dict[str, Any] | None = None, *, max_attempts: int = 3) -> dict[str, Any]:
        self.requests.append((method, path, body))
        if path.endswith("/transitions"):
            return {}
        raise AssertionError(f"unexpected request: {method} {path}")

    def search_approved_tickets(self) -> list[dict[str, Any]]:
        return self.approved

    def fetch_issue_status(self, ticket_key: str) -> str:
        return self.statuses[ticket_key]

    def fetch_issue_comments(self, ticket_key: str) -> list[dict[str, Any]]:
        return self.comments.get(ticket_key, [])

    def query_gerrit_change(self, query: str) -> bridge.GerritChange | None:
        return self.gerrit.get(query)


def _comment(text: str) -> dict[str, Any]:
    return {"body": jira_dispatch._adf_paragraph(text)}


def _merged_event(subject: str = "[OP-19] implement bridge", change_id: str = "Iabc12345") -> str:
    return json.dumps({
        "type": "change-merged",
        "change": {
            "id": change_id,
            "subject": subject,
            "branch": "develop",
            "number": 19,
        },
    })


def test_parse_stream_line_handles_malformed_and_non_object() -> None:
    assert bridge.parse_stream_line("{not-json") is None
    assert bridge.parse_stream_line("[]") is None
    assert bridge.parse_stream_line('{"type":"comment-added"}') == {"type": "comment-added"}


def test_unknown_event_type_skipped_silently() -> None:
    b = FakeBridge()
    b.process_stream_event({"type": "comment-added"})
    assert b.requests == []
    assert b.logs == []
    assert b.counters.events_received == 1


def test_change_merged_variations_extract_change_fields() -> None:
    event = {
        "type": "change-merged",
        "change": {"id": "I12345678", "subject": "[OP-19/x] subject", "branch": "develop"},
        "patchSet": {"revision": "deadbeef"},
    }
    change = bridge.extract_gerrit_change(event)
    assert change.change_id == "I12345678"
    assert change.subject == "[OP-19/x] subject"
    assert change.branch == "develop"


def test_subject_matcher_covers_plain_slash_and_multi_key_edges() -> None:
    assert bridge.extract_ticket_keys_from_subject("[OP-19] title") == ["OP-19"]
    assert bridge.extract_ticket_keys_from_subject("[OP-19/backend] title") == ["OP-19"]
    assert bridge.extract_ticket_keys_from_subject("[OP-19] [OP-20] title") == ["OP-19", "OP-20"]
    assert bridge.extract_ticket_keys_from_subject("fix OP-21 without bracket") == ["OP-21"]


def test_comment_change_url_matcher_extracts_runner_comment_only() -> None:
    comments = [
        _comment("ordinary https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/1"),
        _comment("[runner-pushed-to-gerrit] Patchset on Gerrit: https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/42"),
    ]
    assert bridge.extract_change_numbers_from_comments(comments) == ["42"]


def test_transition_gate_refuses_non_approved_status() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Under Review"
    assert not b.process_ticket_for_change("OP-19", "Iabc12345")
    assert b.requests == []
    assert ("WARN", "ticket_unexpected_status_skip") == b.logs[0][:2]


def test_already_published_is_idempotent_silent_skip() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Published"
    assert not b.process_ticket_for_change("OP-19", "Iabc12345")
    assert b.requests == []
    assert b.logs == []


def test_approved_ticket_transitions_with_id_7_only() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Approved"
    assert b.process_ticket_for_change("OP-19", "Iabc12345")
    assert b.requests == [
        ("POST", "/issue/OP-19/transitions", {"transition": {"id": "7"}})
    ]
    assert b.counters.transitions_made == 1


def test_stream_change_merged_fires_right_transition() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Approved"
    b.run_once_from_lines([_merged_event()])
    assert ("POST", "/issue/OP-19/transitions", {"transition": {"id": "7"}}) in b.requests
    assert b.counters.events_received == 1


def test_change_id_without_matching_ticket_logs_info() -> None:
    b = FakeBridge()
    b.process_stream_event(json.loads(_merged_event(subject="docs only")))
    assert b.requests == []
    assert ("INFO", "change_no_matching_ticket") == b.logs[0][:2]


def test_multiple_jira_tickets_same_change_id_logs_error() -> None:
    b = FakeBridge()
    b.process_stream_event(json.loads(_merged_event(subject="[OP-19] [OP-20] title")))
    assert b.requests == []
    assert ("ERROR", "multiple_tickets_for_change") == b.logs[0][:2]


def test_catchup_archives_one_orphan_and_leaves_under_review_alone() -> None:
    b = FakeBridge()
    b.approved = [{"key": "OP-19"}, {"key": "OP-20"}]
    b.comments = {
        "OP-19": [_comment("[runner-pushed-to-gerrit] Patchset on Gerrit: https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/19")],
        "OP-20": [_comment("[runner-pushed-to-gerrit] Patchset on Gerrit: https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/20")],
    }
    b.gerrit = {
        "19": bridge.GerritChange("I19", number="19", subject="[OP-19]", status="MERGED", branch="develop"),
        "20": bridge.GerritChange("I20", number="20", subject="[OP-20]", status="MERGED", branch="develop"),
    }
    b.statuses = {"OP-19": "Approved", "OP-20": "Under Review"}
    b.startup_catchup()
    assert ("POST", "/issue/OP-19/transitions", {"transition": {"id": "7"}}) in b.requests
    assert not any(req[1] == "/issue/OP-20/transitions" for req in b.requests)


def test_malformed_line_increments_parse_errors_and_continues() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Approved"
    b.run_once_from_lines(["{bad", _merged_event()])
    assert b.counters.parse_errors == 1
    assert b.counters.transitions_made == 1


def test_heartbeat_warns_when_stream_silent_before_first_event() -> None:
    b = FakeBridge()
    b.config.silent_warn_seconds = 1
    b._started_at -= 2
    b._emit_heartbeat()
    assert ("WARN", "stream_silent") == b.logs[-1][:2]


def test_ssh_reconnect_backoff_and_post_reconnect_event(monkeypatch: pytest.MonkeyPatch) -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Approved"
    sleeps: list[float] = []
    b.sleep = sleeps.append
    calls = {"n": 0}

    class FakeProc:
        def __init__(self, lines: list[str], returncode: int = 255) -> None:
            self.stdout = iter(lines)
            self.stderr = self
            self._returncode = returncode

        def wait(self) -> int:
            return self._returncode

        def read(self) -> str:
            return "Connection reset"

    def fake_popen(*args: Any, **kwargs: Any) -> FakeProc:
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeProc([])
        b.stop()
        return FakeProc([_merged_event()], returncode=0)

    b.popen_factory = fake_popen
    b.stream_forever()
    assert sleeps == [1.0]
    assert b.counters.gerrit_reconnects == 1
    assert b.counters.transitions_made == 1


def test_ssh_auth_failure_exits_as_fatal() -> None:
    b = FakeBridge()

    class FakeProc:
        stdout = iter([])
        stderr = None

        def wait(self) -> int:
            return 255

    class FakeStderr:
        def read(self) -> str:
            return "Permission denied (publickey)"

    proc = FakeProc()
    proc.stderr = FakeStderr()
    b.popen_factory = lambda *args, **kwargs: proc
    with pytest.raises(bridge.GerritAuthError):
        b.stream_forever()


def test_gerrit_query_final_failure_logs_error_and_skips() -> None:
    calls = {"n": 0}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        calls["n"] += 1
        return subprocess.CompletedProcess(args[0], 1, "", "timeout")

    b = bridge.GerritJiraBridge(
        _client(),
        bridge.BridgeConfig(),
        sleep=lambda _: None,
        run_command=fake_run,
        logger=lambda *args, **kwargs: None,
    )
    assert b.query_gerrit_change("19") is None
    assert calls["n"] == 3


def test_jira_transition_map_drift_guard() -> None:
    assert jira_dispatch.TRANSITION_IDS["to_published"] == "7"
    assert "to_published" in jira_dispatch.TRANSITION_IDS


def test_jira_auth_error_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def read(self) -> bytes:
            return b""

        def close(self) -> None:
            return None

    def fake_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.HTTPError("url", 401, "unauthorized", {}, FakeResponse())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    b = bridge.GerritJiraBridge(_client(), sleep=lambda _: None, logger=lambda *args, **kwargs: None)
    with pytest.raises(bridge.JiraAuthError):
        b.jira_request("GET", "/issue/OP-19?fields=status")


def test_jira_429_respects_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    class Headers(dict):
        pass

    class FakeResponse:
        def __init__(self, payload: bytes = b"{}") -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def close(self) -> None:
            return None

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError("url", 429, "rate", Headers({"Retry-After": "3"}), FakeResponse())
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    b = bridge.GerritJiraBridge(_client(), sleep=sleeps.append, logger=lambda *args, **kwargs: None)
    assert b.jira_request("GET", "/myself") == {"ok": True}
    assert sleeps == [3.0]


def test_jira_5xx_retries_three_times_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class FakeResponse:
        def read(self) -> bytes:
            return b"bad"

        def close(self) -> None:
            return None

    def fake_urlopen(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        raise urllib.error.HTTPError("url", 503, "down", {}, FakeResponse())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    b = bridge.GerritJiraBridge(_client(), sleep=lambda _: None, logger=lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError):
        b.jira_request("GET", "/issue/OP-19")
    assert calls["n"] == 3
