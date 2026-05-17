"""Contract tests for backend.agents.gerrit_jira_bridge."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import gerrit_jira_bridge as bridge
from backend.agents import jira_dispatch

SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=2048,
)
JSON_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**9), max_value=10**9)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | SAFE_TEXT,
    lambda children: st.lists(children, max_size=8)
    | st.dictionaries(SAFE_TEXT, children, max_size=8),
    max_leaves=32,
)
OP_KEYS = st.from_regex(r"OP-[1-9][0-9]{0,5}", fullmatch=True)
CHANGE_NUMBERS = st.from_regex(r"[1-9][0-9]{0,8}", fullmatch=True)
UTC_DATETIMES = st.datetimes(
    min_value=datetime(1970, 1, 1),
    max_value=datetime(2100, 12, 31),
    timezones=st.just(timezone.utc),
)


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
            bridge.BridgeConfig(
                heartbeat_seconds=9999,
                periodic_catchup_seconds=0,
                cursor_file=None,
            ),
            sleep=lambda _: None,
            logger=self._log,
        )

    def _log(self, level: str, event: str, **extra: Any) -> None:
        self.logs.append((level, event, extra))

    def jira_request(self, method: str, path: str, body: dict[str, Any] | None = None, *, max_attempts: int = 3) -> dict[str, Any]:
        self.requests.append((method, path, body))
        if method == "PUT" or path.endswith("/transitions") or path.endswith("/comment"):
            return {}
        raise AssertionError(f"unexpected request: {method} {path}")

    def search_catchup_candidate_tickets(self) -> list[dict[str, Any]]:
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


@settings(max_examples=75, deadline=None)
@given(payload=JSON_VALUES)
def test_parse_stream_line_property_json_objects_round_trip(payload: Any) -> None:
    line = json.dumps(payload)
    parsed = bridge.parse_stream_line(line)

    assert parsed == bridge.parse_stream_line(line)
    if isinstance(payload, dict):
        assert parsed == payload
        assert isinstance(parsed, dict)
    else:
        assert parsed is None


@settings(max_examples=75, deadline=None)
@given(line=SAFE_TEXT)
def test_parse_stream_line_property_never_returns_non_dict(line: str) -> None:
    parsed = bridge.parse_stream_line(line)

    assert parsed is None or isinstance(parsed, dict)


@settings(max_examples=75, deadline=None)
@given(keys=st.lists(OP_KEYS, max_size=25))
def test_extract_ticket_keys_property_bracketed_subject_dedupes_in_order(
    keys: list[str],
) -> None:
    subject = " ".join(
        f"[{key}/backend] title" if idx % 2 else f"[{key}] title"
        for idx, key in enumerate(keys)
    )

    assert bridge.extract_ticket_keys_from_subject(subject) == list(dict.fromkeys(keys))


@settings(max_examples=75, deadline=None)
@given(keys=st.lists(OP_KEYS, max_size=25))
def test_extract_ticket_keys_property_plain_subject_dedupes_in_order(
    keys: list[str],
) -> None:
    subject = " ".join(f"fix {key}" for key in keys)

    assert bridge.extract_ticket_keys_from_subject(subject) == list(dict.fromkeys(keys))


@settings(max_examples=75, deadline=None)
@given(numbers=st.lists(CHANGE_NUMBERS, max_size=25))
def test_extract_change_numbers_property_runner_comments_only_dedupe(
    numbers: list[str],
) -> None:
    comments = []
    for number in numbers:
        url = f"https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/{number}"
        comments.append(_comment(f"ordinary comment {url}"))
        comments.append(_comment(f"[runner-pushed-to-gerrit] Patchset on Gerrit: {url}"))

    assert bridge.extract_change_numbers_from_comments(comments) == list(
        dict.fromkeys(numbers)
    )


@settings(max_examples=75, deadline=None)
@given(dt=UTC_DATETIMES)
def test_parse_jira_datetime_property_returns_utc_datetime(dt: datetime) -> None:
    parsed = bridge.parse_jira_datetime(dt.isoformat())

    assert parsed == dt
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert bridge.parse_jira_datetime(dt.isoformat().replace("+00:00", "Z")) == dt


@settings(max_examples=75, deadline=None)
@given(value=st.integers(min_value=-10_000, max_value=10_000))
def test_heartbeat_stale_after_seconds_property_clamps_integer_env(value: int) -> None:
    assert bridge.heartbeat_stale_after_seconds(
        {"OMNISIGHT_BRIDGE_STALE_AFTER_SEC": str(value)}
    ) == max(1, value)


@settings(max_examples=75, deadline=None)
@given(value=st.integers(min_value=0, max_value=10_000))
def test_archive_age_days_from_env_property_accepts_non_negative_integer(
    value: int,
) -> None:
    assert bridge.archive_age_days_from_env(
        {"OMNISIGHT_ARCHIVE_AGE_DAYS": str(value)}
    ) == value


@settings(max_examples=75, deadline=None)
@given(
    key=OP_KEYS,
    insertions=st.integers(min_value=-1_000_000, max_value=1_000_000),
    deletions=st.integers(min_value=-1_000_000, max_value=1_000_000),
    patchset_number=st.integers(min_value=-1_000, max_value=1_000),
    approvals=st.lists(st.integers(min_value=-3, max_value=3), max_size=12),
)
def test_compute_ps_merged_metrics_property_preserves_schema_and_counts(
    key: str,
    insertions: int,
    deletions: int,
    patchset_number: int,
    approvals: list[int],
) -> None:
    event = {
        "change": {
            "id": "Iproperty",
            "number": 123,
            "subject": f"[{key}] property test",
            "createdOn": 100,
            "lastUpdated": 250,
            "currentPatchSet": {
                "number": patchset_number,
                "sizeInsertions": insertions,
                "sizeDeletions": deletions,
                "approvals": [
                    {"type": "Verified", "value": value}
                    for value in approvals
                ],
            },
        },
    }

    metrics = bridge.compute_ps_merged_metrics(event)

    assert set(metrics) == {
        "change_id",
        "change_number",
        "ticket",
        "lifetime_min",
        "patchset_count",
        "rebase_count",
        "rework_count",
        "changed_files",
        "final_diff_size",
        "verified_minus_one_count",
    }
    assert metrics == bridge.compute_ps_merged_metrics(event)
    assert metrics["ticket"] == key
    assert metrics["lifetime_min"] == 2.5
    assert metrics["patchset_count"] == patchset_number
    assert metrics["final_diff_size"] == abs(insertions) + abs(deletions)
    assert metrics["verified_minus_one_count"] == len(
        [value for value in approvals if value <= -1]
    )


def test_public_helpers_handle_empty_none_and_large_edges() -> None:
    large_text = "x" * 10_000

    assert bridge.parse_stream_line("") is None
    assert bridge.parse_stream_line(large_text) is None
    assert bridge.parse_jira_datetime("") is None
    assert bridge.parse_jira_datetime(None) is None  # type: ignore[arg-type]
    assert bridge.extract_ticket_keys_from_subject("") == []
    assert bridge.extract_ticket_keys_from_subject(large_text) == []
    assert bridge.flatten_adf_text(None) == ""
    assert bridge.extract_change_numbers_from_comments([]) == []
    assert bridge.heartbeat_stale_after_seconds({}) == bridge.DEFAULT_BRIDGE_STALE_AFTER_SEC
    assert bridge.archive_age_days_from_env({}) == bridge.DEFAULT_ARCHIVE_AGE_DAYS


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


def test_change_merged_force_walks_five_source_states() -> None:
    cases = [
        (
            "OP-19",
            "進行中",
            ["3", "4", "7"],
            "ticket_force_published",
        ),
        (
            "OP-20",
            "Under Review",
            ["4", "7"],
            "ticket_force_published",
        ),
        (
            "OP-21",
            "承認済み",
            ["7"],
            "ticket_force_published",
        ),
        (
            "OP-22",
            "公開済み",
            [],
            "ticket_already_published",
        ),
        (
            "OP-23",
            "Archived",
            [],
            "ticket_archived_skip",
        ),
    ]

    for ticket_key, status, transition_ids, log_event in cases:
        b = FakeBridge()
        b.statuses[ticket_key] = status
        assert b.process_ticket_for_change(ticket_key, f"I{ticket_key[3:]}") is bool(transition_ids)
        actual_transition_ids = [
            req[2]["transition"]["id"]
            for req in b.requests
            if req[1].endswith("/transitions") and req[2] is not None
        ]
        assert actual_transition_ids == transition_ids
        comment_requests = [req for req in b.requests if req[1].endswith("/comment")]
        assert len(comment_requests) == (1 if transition_ids else 0)
        assert any(event == log_event for _level, event, _extra in b.logs)
        if transition_ids:
            force_logs = [extra for _level, event, extra in b.logs if event == "ticket_force_published"]
            assert force_logs[0]["from_state"] == status


def test_transition_gate_refuses_unknown_status() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "TODO"
    assert not b.process_ticket_for_change("OP-19", "Iabc12345")
    assert b.requests == []
    assert ("WARN", "ticket_unexpected_status_skip") == b.logs[0][:2]


def test_already_published_is_idempotent_info_skip() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Published"
    assert not b.process_ticket_for_change("OP-19", "Iabc12345")
    assert b.requests == []
    assert ("INFO", "ticket_already_published") == b.logs[0][:2]


def test_approved_ticket_transitions_with_id_7_and_comment() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Approved"
    assert b.process_ticket_for_change("OP-19", "Iabc12345")
    assert ("POST", "/issue/OP-19/transitions", {"transition": {"id": "7"}}) in b.requests
    assert any(req[1] == "/issue/OP-19/comment" for req in b.requests)
    assert b.counters.transitions_made == 1


def test_published_migration_removes_in_flight_label() -> None:
    b = FakeBridge()
    b.statuses["OP-752"] = "Approved"

    assert b.process_ticket_for_change("OP-752", "Iabc12345")

    assert (
        "PUT",
        "/issue/OP-752",
        {"update": {"labels": [{"remove": jira_dispatch.MIGRATION_IN_FLIGHT_LABEL}]}},
    ) in b.requests


def test_stream_change_merged_fires_right_transition() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Approved"
    b.run_once_from_lines([_merged_event()])
    assert ("POST", "/issue/OP-19/transitions", {"transition": {"id": "7"}}) in b.requests
    assert b.counters.events_received == 1


def test_cursor_save_is_atomic_private_json(tmp_path: Path) -> None:
    cursor = tmp_path / "event-cursor.json"
    ts = datetime(2026, 5, 8, 7, 12, tzinfo=timezone.utc)

    bridge.save_cursor("event-1", ts, cursor)

    assert json.loads(cursor.read_text()) == {
        "event_id": "event-1",
        "timestamp": "2026-05-08T07:12:00+00:00",
    }
    assert oct(cursor.stat().st_mode & 0o777) == "0o600"
    assert cursor.stat().st_uid == os.getuid()
    assert not cursor.with_suffix(".tmp").exists()
    assert bridge.load_cursor(cursor) == ("event-1", ts)


# OP-831: ``CURSOR_FILE`` is now env-driven so user-level systemd installs
# can point at a writable XDG state dir. Pin the contract so a future
# refactor can't silently revert to the hard-coded path that crash-looped
# the daemon on 2026-05-11 (~16-minute outage).


def test_cursor_file_default_when_env_unset(monkeypatch) -> None:
    """Default path preserved when ``OMNISIGHT_BRIDGE_CURSOR_FILE`` unset.

    Re-imports the module to re-evaluate the module-level constant
    against the patched env. System-level installs that already have
    ``/var/lib/omnisight-bridge`` provisioned should keep working.
    """
    import importlib
    monkeypatch.delenv("OMNISIGHT_BRIDGE_CURSOR_FILE", raising=False)
    reloaded = importlib.reload(bridge)
    try:
        assert reloaded.CURSOR_FILE == Path(
            "/var/lib/omnisight-bridge/event-cursor.json"
        )
    finally:
        # Ensure the rest of the suite sees the env-driven module.
        monkeypatch.setenv(
            "OMNISIGHT_BRIDGE_CURSOR_FILE",
            "/tmp/test-restore-cursor.json",
        )
        importlib.reload(bridge)


def test_cursor_file_env_override_honoured(monkeypatch, tmp_path: Path) -> None:
    """``OMNISIGHT_BRIDGE_CURSOR_FILE`` env redirects the cursor path.

    User-level systemd installs set this to ``~/.local/state/omnisight-
    bridge/event-cursor.json``; CI tests use ``tmp_path`` so the assertion
    is hermetic.
    """
    import importlib
    target = tmp_path / "event-cursor.json"
    monkeypatch.setenv("OMNISIGHT_BRIDGE_CURSOR_FILE", str(target))
    reloaded = importlib.reload(bridge)
    try:
        assert reloaded.CURSOR_FILE == target
    finally:
        monkeypatch.delenv("OMNISIGHT_BRIDGE_CURSOR_FILE", raising=False)
        importlib.reload(bridge)


def test_missing_cursor_logs_first_run_warning_once(tmp_path: Path) -> None:
    b = FakeBridge()
    b.config.cursor_file = tmp_path / "event-cursor.json"

    b.replay_from_cursor()
    b.replay_from_cursor()

    warnings = [event for level, event, _extra in b.logs if level == "WARN"]
    assert warnings == ["event_cursor_missing_first_run"]


def test_replay_missed_events_queries_gerrit_and_marks_backfilled(tmp_path: Path) -> None:
    captured_cmds: list[list[str]] = []
    seen_events: list[dict[str, Any]] = []
    stdout = "\n".join([
        json.dumps({
            "id": "Iop19",
            "number": 19,
            "subject": "[OP-19] merged while bridge was down",
            "branch": "develop",
            "project": "omnisight/OmniSight-Productizer",
            "status": "MERGED",
        }),
        json.dumps({"type": "stats", "rowCount": 1}),
    ])

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    b = bridge.GerritJiraBridge(
        _client(),
        bridge.BridgeConfig(cursor_file=tmp_path / "event-cursor.json"),
        sleep=lambda _: None,
        run_command=fake_run,
        logger=lambda *_args, **_kwargs: None,
    )
    b.process_stream_event = lambda event: seen_events.append(event)  # type: ignore[method-assign]

    count = b.replay_missed_events(datetime(2026, 5, 8, 7, 0, 5, tzinfo=timezone.utc))

    assert count == 1
    assert seen_events[0]["type"] == "change-merged"
    assert seen_events[0]["_backfilled"] is True
    assert "gerrit" in captured_cmds[0]
    assert "query" in captured_cmds[0]
    assert "--current-patch-set" in captured_cmds[0]
    assert 'project:omnisight/OmniSight-Productizer status:merged after:"2026-05-08 07:00:05"' in captured_cmds[0]


def test_replay_from_cursor_catches_up_three_merges_before_live_stream(tmp_path: Path) -> None:
    cursor = tmp_path / "event-cursor.json"
    bridge.save_cursor(
        "last-live-event",
        datetime.now(timezone.utc) - timedelta(minutes=5),
        cursor,
    )
    changes = [
        {
            "id": f"Iop{n}",
            "number": n,
            "subject": f"[OP-{n}] merged during disconnect",
            "branch": "develop",
            "status": "MERGED",
        }
        for n in (19, 20, 21)
    ]
    stdout = "\n".join(json.dumps(change) for change in changes)

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    b = FakeBridge()
    b.config.cursor_file = cursor
    b.run_command = fake_run
    b.statuses = {"OP-19": "進行中", "OP-20": "Under Review", "OP-21": "Approved"}

    b.replay_from_cursor()

    assert b.counters.events_received == 3
    transition_paths = [req[1] for req in b.requests if req[1].endswith("/transitions")]
    assert transition_paths.count("/issue/OP-19/transitions") == 3
    assert transition_paths.count("/issue/OP-20/transitions") == 2
    assert transition_paths.count("/issue/OP-21/transitions") == 1
    replay_logs = [
        extra for _level, event, extra in b.logs
        if event == "change_merged_event"
    ]
    assert [extra["source"] for extra in replay_logs] == ["replay", "replay", "replay"]


def test_run_once_replays_cursor_before_live_stream_event(tmp_path: Path) -> None:
    cursor = tmp_path / "event-cursor.json"
    bridge.save_cursor(
        "before-restart",
        datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc),
        cursor,
    )
    stdout = json.dumps({
        "id": "Iop19",
        "number": 19,
        "subject": "[OP-19] replayed merge",
        "branch": "develop",
        "status": "MERGED",
    })

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    b = FakeBridge()
    b.config.cursor_file = cursor
    b.run_command = fake_run
    b.statuses = {"OP-19": "Approved", "OP-20": "Approved"}

    b.run_once_from_lines([_merged_event(subject="[OP-20] live merge", change_id="Iop20")])

    merge_logs = [extra["source"] for _level, event, extra in b.logs if event == "change_merged_event"]
    assert merge_logs == ["replay", "live"]


def test_change_id_without_matching_ticket_logs_info() -> None:
    b = FakeBridge()
    b.process_stream_event(json.loads(_merged_event(subject="docs only")))
    assert b.requests == []
    # OP-746 — every change-merged also emits ps_merged_metrics; assert
    # by event name so the order between the two log lines does not
    # become a brittle test invariant.
    events = [e for _, e, _ in b.logs]
    assert "change_no_matching_ticket" in events


def test_multiple_jira_tickets_same_change_id_logs_error() -> None:
    b = FakeBridge()
    b.process_stream_event(json.loads(_merged_event(subject="[OP-19] [OP-20] title")))
    assert b.requests == []
    events = [e for _, e, _ in b.logs]
    assert "multiple_tickets_for_change" in events


def test_catchup_force_walks_merged_under_review_ticket() -> None:
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
    assert ("POST", "/issue/OP-20/transitions", {"transition": {"id": "4"}}) in b.requests
    assert ("POST", "/issue/OP-20/transitions", {"transition": {"id": "7"}}) in b.requests


def test_failed_force_walk_transition_logs_error_and_returns() -> None:
    class FailingBridge(FakeBridge):
        def jira_request(self, method: str, path: str, body: dict[str, Any] | None = None, *, max_attempts: int = 3) -> dict[str, Any]:
            self.requests.append((method, path, body))
            if path.endswith("/transitions"):
                raise RuntimeError("synthetic transition failure")
            return {}

    b = FailingBridge()
    b.statuses["OP-19"] = "進行中"
    assert not b.process_ticket_for_change("OP-19", "Iabc12345")
    assert len([req for req in b.requests if req[1].endswith("/transitions")]) == 1
    assert not any(req[1].endswith("/comment") for req in b.requests)
    err_logs = [extra for _level, event, extra in b.logs if event == "ticket_force_publish_transition_failed"]
    assert len(err_logs) == 1
    assert err_logs[0]["from_state"] == "進行中"
    assert err_logs[0]["at_state"] == "進行中"


def test_malformed_line_increments_parse_errors_and_continues() -> None:
    b = FakeBridge()
    b.statuses["OP-19"] = "Approved"
    b.run_once_from_lines(["{bad", _merged_event()])
    assert b.counters.parse_errors == 1
    assert b.counters.transitions_made == 1


def test_stream_startup_reaps_stale_merger_verify_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "tmp"
    scratch_root.mkdir()
    stale = scratch_root / "merger-verify-stale"
    fresh = scratch_root / "merger-verify-fresh"
    stale.mkdir()
    fresh.mkdir()
    (stale / "worktree").mkdir()
    (fresh / "worktree").mkdir()
    now = time.time()
    old = now - bridge.MERGER_VERIFY_REAP_AGE_SECONDS - 60
    os.utime(stale, (old, old))
    os.utime(fresh, (now, now))

    b = FakeBridge()
    b.config.heartbeat_file_path = tmp_path / "heartbeat"
    monkeypatch.setattr(bridge, "MERGER_VERIFY_SCRATCH_ROOT", scratch_root)

    class FakeProc:
        stdout = iter([])
        stderr = None

        def wait(self) -> int:
            b.stop()
            return 0

    b.popen_factory = lambda *args, **kwargs: FakeProc()

    b.stream_forever()

    assert not stale.exists()
    assert fresh.exists()
    assert any(
        level == "WARN"
        and event == "merger_verify_scratch_reaped"
        and extra["path"] == str(stale)
        for level, event, extra in b.logs
    )


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
    assert jira_dispatch.TRANSITION_IDS["to_under_review"] == "3"
    assert jira_dispatch.TRANSITION_IDS["to_approved"] == "4"
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


def test_run_initializes_db_pool_before_stream_and_closes_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_init_pool(dsn: str) -> None:
        calls.append(f"init:{dsn}")

    async def fake_close_pool() -> None:
        calls.append("close")

    class FakeDaemon:
        def stream_forever(self) -> None:
            calls.append("stream")
            # Synthetic stand-in for merger_agent_vote audit.log calls:
            # these are only reachable after stream processing begins.
            calls.append("audit.log")

    monkeypatch.setattr(bridge, "_resolve_pg_dsn", lambda: "postgresql://unit")
    monkeypatch.setattr(bridge.db_pool, "init_pool", fake_init_pool)
    monkeypatch.setattr(bridge.db_pool, "close_pool", fake_close_pool)
    monkeypatch.setattr(bridge, "build_bridge", lambda _agent_class: FakeDaemon())

    assert bridge.run("subscription-codex") == 0
    assert calls == ["init:postgresql://unit", "stream", "audit.log", "close"]


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


# ──────────────────────────────────────────────────────────────────
# OP-715 — patchset-created handler tests
# ──────────────────────────────────────────────────────────────────


def _patchset_event(
    *,
    change_number: int = 92,
    project: str = "omnisight/OmniSight-Productizer",
    rev: str = "d386745be2",
    ps_number: int = 1,
    uploader_name: str = "codex-bot",
    uploader_email: str = "codex-bot@x.com",
    branch: str = "develop",
) -> dict[str, Any]:
    """Build a synthetic Gerrit stream-events patchset-created payload."""
    return {
        "type": "patchset-created",
        "change": {
            "id": f"I{rev}",
            "number": change_number,
            "project": project,
            "branch": branch,
            "subject": "[OP-75] some change",
        },
        "patchSet": {
            "number": ps_number,
            "revision": rev,
            "uploader": {
                "name": uploader_name,
                "email": uploader_email,
                "username": uploader_name,
            },
        },
    }


def test_patchset_created_dispatches_to_handler() -> None:
    """process_stream_event should route patchset-created to the new handler.

    Lock the dispatcher contract so a future refactor (e.g. event
    registry) can't accidentally drop the patchset-created branch.
    """
    b = bridge.GerritJiraBridge(_client(), sleep=lambda _: None,
                                logger=lambda *a, **k: None)
    calls = {"merged": 0, "patchset": 0}

    def fake_merged(_event: dict[str, Any]) -> None:
        calls["merged"] += 1

    def fake_patchset(_event: dict[str, Any]) -> None:
        calls["patchset"] += 1

    b._handle_change_merged = fake_merged  # type: ignore[method-assign]
    b._handle_patchset_created = fake_patchset  # type: ignore[method-assign]

    b.process_stream_event(_patchset_event())
    assert calls == {"merged": 0, "patchset": 1}

    # Counter still increments for any event type
    assert b.counters.events_received == 1


def test_change_merged_still_routes_to_change_merged_handler() -> None:
    """Regression: extending the dispatcher must not break OP-689's path."""
    b = bridge.GerritJiraBridge(_client(), sleep=lambda _: None,
                                logger=lambda *a, **k: None)
    calls = {"merged": 0, "patchset": 0}

    b._handle_change_merged = lambda _e: calls.__setitem__("merged", calls["merged"] + 1)  # type: ignore[method-assign]
    b._handle_patchset_created = lambda _e: calls.__setitem__("patchset", calls["patchset"] + 1)  # type: ignore[method-assign]

    merged_event = {
        "type": "change-merged",
        "change": {
            "id": "I123abc",
            "number": 90,
            "project": "omnisight/OmniSight-Productizer",
            "branch": "develop",
            "subject": "[OP-700] something",
        },
    }
    b.process_stream_event(merged_event)
    assert calls == {"merged": 1, "patchset": 0}


def test_unknown_event_type_is_ignored() -> None:
    """Defensive: unknown event types must not raise + must increment counter."""
    b = bridge.GerritJiraBridge(_client(), sleep=lambda _: None,
                                logger=lambda *a, **k: None)
    # No handler attribute exists for these — implicit no-op
    b.process_stream_event({"type": "ref-updated", "change": {}, "refUpdate": {}})
    b.process_stream_event({"type": "comment-added", "change": {}})
    assert b.counters.events_received == 2


def test_patchset_created_spawns_thread_with_proactive_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler must spawn a daemon thread that calls
    ``_proactive_merger_check`` with the event."""
    captured_events: list[dict[str, Any]] = []
    spawned_threads: list[Any] = []

    async def fake_proactive(event: dict[str, Any]) -> None:
        captured_events.append(event)

    # Patch the import target so the handler picks up our fake.
    import backend.routers.webhooks as _wh
    monkeypatch.setattr(_wh, "_proactive_merger_check", fake_proactive)
    # OP-801 — the bridge now also spawns an AI Reviewer thread; stub
    # it to a fast no-op so this test stays focused on the merger path.
    async def _ai_noop(_event: dict[str, Any]) -> None:
        pass
    monkeypatch.setattr(_wh, "_ai_reviewer_check", _ai_noop)

    # Capture the spawned thread so we can join() on it for the test
    import threading
    real_thread = threading.Thread

    def spy_thread(*args: Any, **kwargs: Any) -> threading.Thread:
        t = real_thread(*args, **kwargs)
        spawned_threads.append(t)
        return t

    monkeypatch.setattr(threading, "Thread", spy_thread)

    log_calls: list[tuple[str, str]] = []
    b = bridge.GerritJiraBridge(
        _client(),
        sleep=lambda _: None,
        logger=lambda level, label, **kw: log_calls.append((level, label)),
    )
    event = _patchset_event(change_number=92, ps_number=1)
    b._handle_patchset_created(event)

    # Wait for both spawned threads to finish (daemon=True).
    assert len(spawned_threads) == 2
    for t in spawned_threads:
        t.join(timeout=2.0)
        assert not t.is_alive()

    # The proactive merger fake captured the event exactly once.
    assert len(captured_events) == 1
    assert captured_events[0]["change"]["number"] == 92
    assert captured_events[0]["patchSet"]["number"] == 1

    assert any(
        label == "proactive_merger_thread_spawned"
        for _level, label in log_calls
    )
    assert any(
        label == "ai_reviewer_thread_spawned"
        for _level, label in log_calls
    )


def test_patchset_created_thread_swallows_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the proactive check raises, the bridge must log + survive
    (daemon must not crash on a single bad event)."""
    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("synthetic")

    import backend.routers.webhooks as _wh
    monkeypatch.setattr(_wh, "_proactive_merger_check", boom)
    async def _ai_noop(_event: dict[str, Any]) -> None:
        pass
    monkeypatch.setattr(_wh, "_ai_reviewer_check", _ai_noop)

    spawned: list[Any] = []
    import threading
    real_thread = threading.Thread

    def spy(*args: Any, **kwargs: Any) -> threading.Thread:
        t = real_thread(*args, **kwargs)
        spawned.append(t)
        return t
    monkeypatch.setattr(threading, "Thread", spy)

    log_calls: list[tuple[str, str, dict[str, Any]]] = []
    b = bridge.GerritJiraBridge(
        _client(),
        sleep=lambda _: None,
        logger=lambda level, label, **kw: log_calls.append((level, label, kw)),
    )
    # Must not raise here — the bridge keeps running.
    b._handle_patchset_created(_patchset_event())
    for t in spawned:
        t.join(timeout=2.0)

    # The exception must have been logged from inside the thread.
    err_logs = [
        c for c in log_calls
        if c[1] == "proactive_merger_thread_error"
    ]
    assert len(err_logs) == 1
    assert "synthetic" in err_logs[0][2].get("err", "")


def test_patchset_created_logs_thread_spawn_with_change_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn log line must carry the change/ps numbers so the
    operator can correlate threads to events when triaging."""
    # Make the proactive check a fast no-op so the spawned thread
    # exits cleanly without external side effects.
    async def fast_noop(_event: dict[str, Any]) -> None:
        pass

    import backend.routers.webhooks as _wh
    monkeypatch.setattr(_wh, "_proactive_merger_check", fast_noop)
    monkeypatch.setattr(_wh, "_ai_reviewer_check", fast_noop)

    log_calls: list[tuple[str, str, dict[str, Any]]] = []
    b = bridge.GerritJiraBridge(
        _client(),
        sleep=lambda _: None,
        logger=lambda level, label, **kw: log_calls.append((level, label, kw)),
    )
    b._handle_patchset_created(
        _patchset_event(change_number=42, ps_number=3),
    )

    spawn_logs = [c for c in log_calls if c[1] == "proactive_merger_thread_spawned"]
    assert len(spawn_logs) == 1
    assert spawn_logs[0][2].get("change_id") == "42"
    assert spawn_logs[0][2].get("ps") == "3"

    ai_spawn_logs = [c for c in log_calls if c[1] == "ai_reviewer_thread_spawned"]
    assert len(ai_spawn_logs) == 1
    assert ai_spawn_logs[0][2].get("change_id") == "42"
    assert ai_spawn_logs[0][2].get("ps") == "3"


# ──────────────────────────────────────────────────────────────────
# OP-801 — AI Reviewer thread tests
# ──────────────────────────────────────────────────────────────────


def test_patchset_created_spawns_ai_reviewer_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-801 — patchset-created must dispatch to ``_ai_reviewer_check``
    in a daemon thread, alongside the OP-715 merger thread."""
    captured: list[dict[str, Any]] = []

    async def fake_ai_check(event: dict[str, Any]) -> None:
        captured.append(event)

    import backend.routers.webhooks as _wh
    monkeypatch.setattr(_wh, "_ai_reviewer_check", fake_ai_check)
    # Stub the merger so we don't accidentally fire its real path.
    async def merger_noop(_event: dict[str, Any]) -> None:
        pass
    monkeypatch.setattr(_wh, "_proactive_merger_check", merger_noop)

    # Capture all spawned threads so we can join + assert names.
    import threading
    real_thread = threading.Thread
    spawned: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> threading.Thread:
        t = real_thread(*args, **kwargs)
        spawned.append(t)
        return t
    monkeypatch.setattr(threading, "Thread", spy)

    log_calls: list[tuple[str, str, dict[str, Any]]] = []
    b = bridge.GerritJiraBridge(
        _client(),
        sleep=lambda _: None,
        logger=lambda level, label, **kw: log_calls.append((level, label, kw)),
    )
    b._handle_patchset_created(_patchset_event(change_number=801, ps_number=2))

    for t in spawned:
        t.join(timeout=2.0)
        assert not t.is_alive()

    # AI Reviewer received exactly one event with the expected ids.
    assert len(captured) == 1
    assert captured[0]["change"]["number"] == 801
    assert captured[0]["patchSet"]["number"] == 2

    # Thread name carries the change/ps for ps-aux triage.
    ai_threads = [t for t in spawned if t.name.startswith("ai-reviewer-")]
    assert len(ai_threads) == 1
    assert ai_threads[0].name == "ai-reviewer-801-2"

    # Spawn log line is structured + carries change_id + ps.
    spawn_logs = [c for c in log_calls if c[1] == "ai_reviewer_thread_spawned"]
    assert len(spawn_logs) == 1
    assert spawn_logs[0][2].get("change_id") == "801"
    assert spawn_logs[0][2].get("ps") == "2"


def test_patchset_created_ai_reviewer_thread_swallows_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-801 — if ``_ai_reviewer_check`` raises, the bridge must log
    ``ai_reviewer_thread_error`` and not propagate the exception."""
    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("synthetic-ai")

    import backend.routers.webhooks as _wh
    monkeypatch.setattr(_wh, "_ai_reviewer_check", boom)
    async def merger_noop(_event: dict[str, Any]) -> None:
        pass
    monkeypatch.setattr(_wh, "_proactive_merger_check", merger_noop)

    import threading
    real_thread = threading.Thread
    spawned: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> threading.Thread:
        t = real_thread(*args, **kwargs)
        spawned.append(t)
        return t
    monkeypatch.setattr(threading, "Thread", spy)

    log_calls: list[tuple[str, str, dict[str, Any]]] = []
    b = bridge.GerritJiraBridge(
        _client(),
        sleep=lambda _: None,
        logger=lambda level, label, **kw: log_calls.append((level, label, kw)),
    )

    # Must not raise — daemon survival is the contract.
    b._handle_patchset_created(_patchset_event())
    for t in spawned:
        t.join(timeout=2.0)

    err_logs = [c for c in log_calls if c[1] == "ai_reviewer_thread_error"]
    assert len(err_logs) == 1
    assert "synthetic-ai" in err_logs[0][2].get("err", "")


def test_patchset_created_ai_reviewer_independent_of_merger_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-801 — a merger crash must NOT prevent the AI Reviewer from
    firing (the two pipelines are independent fire-and-forget threads).
    """
    async def merger_boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("merger crash")

    ai_calls: list[dict[str, Any]] = []

    async def ai_ok(event: dict[str, Any]) -> None:
        ai_calls.append(event)

    import backend.routers.webhooks as _wh
    monkeypatch.setattr(_wh, "_proactive_merger_check", merger_boom)
    monkeypatch.setattr(_wh, "_ai_reviewer_check", ai_ok)

    import threading
    real_thread = threading.Thread
    spawned: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> threading.Thread:
        t = real_thread(*args, **kwargs)
        spawned.append(t)
        return t
    monkeypatch.setattr(threading, "Thread", spy)

    b = bridge.GerritJiraBridge(
        _client(),
        sleep=lambda _: None,
        logger=lambda *a, **kw: None,
    )
    b._handle_patchset_created(_patchset_event(change_number=42, ps_number=1))
    for t in spawned:
        t.join(timeout=2.0)

    assert len(ai_calls) == 1
    assert ai_calls[0]["change"]["number"] == 42


# ──────────────────────────────────────────────────────────────────────
#  OP-1409 — develop-tip drift re-evaluation
# ──────────────────────────────────────────────────────────────────────


def test_develop_merge_drift_sweep_re_evaluates_newly_unmergeable_ps(
    tmp_path,
) -> None:
    """A develop ``change-merged`` event should sweep open develop PSes
    and synthesize a proactive-merger event for a newly-unmergeable
    change."""
    import subprocess as _sub

    open_change = (
        '{"id":"Iop1409","number":900,"project":"omnisight/x",'
        '"branch":"develop","subject":"[OP-1409] drift target",'
        '"owner":{"username":"alice"},'
        '"currentPatchSet":{"number":1,"revision":"abc900",'
        '"uploader":{"username":"alice"},"parents":[{"revision":"base1"}],'
        '"ref":"refs/changes/00/900/1"}}\n'
        '{"type":"stats","rowCount":1,"runTimeMilliseconds":12}'
    )
    run_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        return _sub.CompletedProcess(
            args=args, returncode=0, stdout=open_change, stderr="",
        )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b")]}\'\n{\"mergeable\":false}"

    requested_urls: list[str] = []

    def fake_urlopen(req, timeout=30):
        requested_urls.append(req.full_url)
        return FakeResponse()

    logs: list[tuple[str, str, dict[str, Any]]] = []
    spawned: list[dict[str, Any]] = []
    b = bridge.GerritJiraBridge(
        _client(),
        bridge.BridgeConfig(
            cursor_file=tmp_path / "event-cursor.json",
            gerrit_rest_base_url="https://gerrit.example",
        ),
        sleep=lambda _: None,
        run_command=fake_run,
        urlopen=fake_urlopen,
        logger=lambda level, ev, **kw: logs.append((level, ev, kw)),
    )
    b._handle_change_merged = lambda _event: None  # type: ignore[method-assign]
    b._schedule_auto_rebase_sweep = lambda _event: None  # type: ignore[method-assign]
    b._spawn_proactive_merger_thread = (  # type: ignore[method-assign]
        lambda event: spawned.append(event)
    )

    class ImmediateScheduler:
        def schedule(self, *, project: str, merged_sha: str) -> None:
            b._run_develop_drift_sweep(project=project, merged_sha=merged_sha)

    b._develop_drift_scheduler = ImmediateScheduler()

    b.process_stream_event({
        "type": "change-merged",
        "newRev": "develop-tip-2",
        "change": {
            "id": "Imerged",
            "number": 899,
            "project": "omnisight/x",
            "branch": "develop",
            "subject": "[OP-1409] advance develop",
        },
    })

    assert len(run_calls) == 1
    assert run_calls[0][-2:] == ["status:open", "branch:develop"]
    assert requested_urls == [
        "https://gerrit.example/changes/900/revisions/current/mergeable"
    ]
    assert len(spawned) == 1
    assert spawned[0]["type"] == "patchset-created"
    assert spawned[0]["change"]["number"] == 900
    assert spawned[0]["patchSet"]["number"] == 1
    assert any(ev == "merger_drift_re_eval_triggered" for _level, ev, _kw in logs)

    b.process_stream_event({
        "type": "change-merged",
        "newRev": "develop-tip-3",
        "change": {
            "id": "Imerged2",
            "number": 901,
            "project": "omnisight/x",
            "branch": "develop",
            "subject": "[OP-1409] advance develop again",
        },
    })

    assert len(spawned) == 1


def test_develop_drift_cooldown_survives_bridge_restart(
    monkeypatch, tmp_path: Path,
) -> None:
    """OP-1418: persisted cooldown state blocks immediate restart replay."""
    now = 1_900_000_000.0
    monkeypatch.setattr(bridge.time, "time", lambda: now)
    config = bridge.BridgeConfig(
        cursor_file=tmp_path / "event-cursor.json",
        merger_drift_cooldown_seconds=30.0 * 60.0,
    )
    logs: list[tuple[str, str, dict[str, Any]]] = []
    first = bridge.GerritJiraBridge(
        _client(),
        config,
        sleep=lambda _: None,
        logger=lambda level, ev, **kw: logs.append((level, ev, kw)),
    )

    assert first._should_re_evaluate_drift("900", True) is False
    assert first._should_re_evaluate_drift("900", False) is True

    state_file = tmp_path / "drift-cooldown.json"
    assert json.loads(state_file.read_text()) == {
        "changes": {
            "900": {
                "last_re_eval_ts": now,
                "last_seen_mergeable": False,
            },
        },
    }
    assert oct(state_file.stat().st_mode & 0o777) == "0o600"

    restarted = bridge.GerritJiraBridge(
        _client(),
        config,
        sleep=lambda _: None,
        logger=lambda level, ev, **kw: logs.append((level, ev, kw)),
    )

    assert restarted._should_re_evaluate_drift("900", False) is False
    assert any(
        ev == "merger_drift_re_eval_skipped_cooldown"
        and kw["change"] == "900"
        for _level, ev, kw in logs
    )


def test_develop_drift_sweep_abstains_on_stale_patchset(
    tmp_path, monkeypatch,
) -> None:
    """OP-1440: merger-time drift re-check skips stale PSes before spawn."""
    import subprocess as _sub

    now = 1_800_000_000.0
    open_change = (
        '{"id":"Iop1440","number":901,"project":"omnisight/x",'
        '"branch":"develop","subject":"[OP-1440] stale drift target",'
        '"owner":{"username":"alice"},'
        '"currentPatchSet":{"number":1,"revision":"abc901",'
        f'"createdOn":{now - 15 * 86400},'
        '"uploader":{"username":"alice"},"parents":[{"revision":"base15"}],'
        '"ref":"refs/changes/01/901/1"}}\n'
        '{"type":"stats","rowCount":1,"runTimeMilliseconds":12}'
    )
    run_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if args[:3] == ["git", "rev-list", "--count"]:
            return _sub.CompletedProcess(
                args=args, returncode=0, stdout="101\n", stderr="",
            )
        return _sub.CompletedProcess(
            args=args, returncode=0, stdout=open_change, stderr="",
        )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b")]}\'\n{\"mergeable\":false}"

    comments: list[tuple[str, str]] = []
    monkeypatch.setattr(bridge.time, "time", lambda: now)
    monkeypatch.setattr(jira_dispatch.time, "time", lambda: now)
    monkeypatch.setattr(
        jira_dispatch,
        "add_comment",
        lambda client, key, text, idem_key=None: comments.append((key, text)),
    )

    logs: list[tuple[str, str, dict[str, Any]]] = []
    spawned: list[dict[str, Any]] = []
    b = bridge.GerritJiraBridge(
        _client(),
        bridge.BridgeConfig(
            cursor_file=tmp_path / "event-cursor.json",
            gerrit_rest_base_url="https://gerrit.example",
        ),
        sleep=lambda _: None,
        run_command=fake_run,
        urlopen=lambda req, timeout=30: FakeResponse(),
        logger=lambda level, ev, **kw: logs.append((level, ev, kw)),
    )
    b._spawn_proactive_merger_thread = (  # type: ignore[method-assign]
        lambda event: spawned.append(event)
    )

    triggered = b._run_develop_drift_sweep(
        project="omnisight/x", merged_sha="develop-tip-2",
    )

    assert triggered == 0
    assert spawned == []
    assert any(call[:3] == ["git", "rev-list", "--count"] for call in run_calls)
    assert comments and comments[0][0] == "OP-1440"
    assert "[merger-ps-staleness-abstain]" in comments[0][1]
    assert "age_days=15.0" in comments[0][1]
    assert "commits_behind=101" in comments[0][1]
    assert any(
        ev == "merger_drift_re_eval_skipped_stale_ps"
        and kw["reason"] == "staleness_exceeded"
        for _level, ev, kw in logs
    )


# ──────────────────────────────────────────────────────────────────────
#  OP-1196 phase 3b — startup conflict backfill
# ──────────────────────────────────────────────────────────────────────


class TestBackfillExistingConflicts:
    """The daemon's stream_forever only sees LIVE patchset-created
    events. Conflicts that pre-date a daemon restart (or were created
    during a daemon downtime window) never reach the merger pipeline
    via the normal path. ``backfill_existing_conflicts`` is the
    one-shot scan invoked from ``_run_with_db_pool`` to close that gap.

    Tests below cover the pure-function filter logic + the orchestrator
    end-to-end with a fake ``run_command``.
    """

    # Sample `gerrit query --current-patch-set --format=JSON` output —
    # 5 changes spanning every filter branch + 1 stats trailer line.
    _SAMPLE_GERRIT_QUERY = "\n".join([
        # Change A — mergeable=False, no hashtag, not merger-bot uploader → CANDIDATE
        '{"id":"Ia111","number":689,"project":"omnisight/x","branch":"develop",'
        '"subject":"OP-186 talent_tree","mergeable":false,"status":"NEW",'
        '"hashtags":[],"owner":{"username":"alice"},'
        '"currentPatchSet":{"number":1,"revision":"abcdef1","uploader":{"username":"alice"}}}',
        # Change B — mergeable=True → skip
        '{"id":"Ib222","number":700,"project":"omnisight/x","branch":"develop",'
        '"subject":"clean change","mergeable":true,"status":"NEW",'
        '"hashtags":[],"owner":{"username":"alice"},'
        '"currentPatchSet":{"number":1,"revision":"abcdef2","uploader":{"username":"alice"}}}',
        # Change C — already-attempted (Merger-Proactive-PS1) → skip
        '{"id":"Ic333","number":701,"project":"omnisight/x","branch":"develop",'
        '"subject":"already tried","mergeable":false,"status":"NEW",'
        '"hashtags":["Merger-Proactive-PS1"],"owner":{"username":"alice"},'
        '"currentPatchSet":{"number":1,"revision":"abcdef3","uploader":{"username":"alice"}}}',
        # Change D — resolved-awaiting-human → skip
        '{"id":"Id444","number":702,"project":"omnisight/x","branch":"develop",'
        '"subject":"merger already won","mergeable":false,"status":"NEW",'
        '"hashtags":["Merge-Conflict-Resolved"],"owner":{"username":"alice"},'
        '"currentPatchSet":{"number":2,"revision":"abcdef4","uploader":{"username":"merger-agent-bot"}}}',
        # Change E — uploader is merger-agent-bot → skip (loop prevention)
        '{"id":"Ie555","number":703,"project":"omnisight/x","branch":"develop",'
        '"subject":"merger uploaded","mergeable":false,"status":"NEW",'
        '"hashtags":[],"owner":{"username":"alice"},'
        '"currentPatchSet":{"number":1,"revision":"abcdef5","uploader":{"username":"merger-agent-bot"}}}',
        # Stats trailer that gerrit appends
        '{"type":"stats","rowCount":5,"runTimeMilliseconds":12,"moreChanges":false}',
    ])

    def test_select_candidates_filters_correctly(self):
        candidates, counters = bridge.GerritJiraBridge._select_backfill_candidates(
            self._SAMPLE_GERRIT_QUERY
        )
        # Only Change A passes the filter.
        assert len(candidates) == 1
        assert candidates[0]["number"] == 689
        # Counters reflect each skip reason.
        assert counters == {
            "scanned": 5,
            "skipped_mergeable": 1,
            "skipped_already_attempted": 1,
            "skipped_resolved_awaiting_human": 1,
            "skipped_uploader_is_merger": 1,
            "skipped_no_current_patchset": 0,
            "skipped_unparseable": 0,
            "candidates": 1,
        }

    def test_select_candidates_skips_unparseable(self):
        garbage = "this is not json\n{also not json\n"
        candidates, counters = bridge.GerritJiraBridge._select_backfill_candidates(garbage)
        assert candidates == []
        assert counters["skipped_unparseable"] == 2
        assert counters["scanned"] == 0

    def test_select_candidates_skips_missing_currentPatchSet(self):
        partial = (
            '{"id":"If666","number":704,"project":"omnisight/x",'
            '"branch":"develop","subject":"missing cps","mergeable":false,'
            '"hashtags":[]}\n'
        )
        candidates, counters = bridge.GerritJiraBridge._select_backfill_candidates(partial)
        assert candidates == []
        assert counters["skipped_no_current_patchset"] == 1

    def test_synthesize_event_shape_matches_live_event(self):
        change_obj = {
            "id": "Itest", "number": 689, "project": "omnisight/x",
            "branch": "develop", "subject": "test", "url": "http://...",
            "owner": {"username": "alice"},
            "hashtags": ["tag1"],
            "currentPatchSet": {
                "number": 2, "revision": "deadbeef",
                "uploader": {"username": "alice"},
                "parents": ["aaa"], "ref": "refs/changes/89/689/2",
            },
        }
        event = bridge.GerritJiraBridge._synthesize_patchset_created_event(change_obj)
        assert event["type"] == "patchset-created"
        # Same keys merger code paths read from a live event.
        assert event["change"]["id"] == "Itest"
        assert event["change"]["number"] == 689
        assert event["change"]["project"] == "omnisight/x"
        assert event["change"]["branch"] == "develop"
        assert event["patchSet"]["number"] == 2
        assert event["patchSet"]["revision"] == "deadbeef"
        assert event["patchSet"]["uploader"]["username"] == "alice"
        # Carries hashtags for prefilter visibility in logs.
        assert event["change"]["hashtags"] == ["tag1"]

    def test_backfill_end_to_end_spawns_one_thread_for_candidate(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end (synchronous): subprocess returns the 5-change
        sample, filter picks 1, bridge spawns 1 proactive-merger thread."""
        import subprocess as _sub
        sample = self._SAMPLE_GERRIT_QUERY

        # Env knobs the backfill needs.
        monkeypatch.setenv("OMNISIGHT_GERRIT_PROJECT", "omnisight/x")
        monkeypatch.setenv("OMNISIGHT_GERRIT_SSH_HOST", "claude-bot@host")
        monkeypatch.setenv("OMNISIGHT_GERRIT_SSH_PORT", "29418")
        monkeypatch.setenv("OMNISIGHT_GIT_SSH_KEY_PATH", "/dev/null")

        # Fake run_command — returns the sample on the gerrit-query
        # invocation, raises on anything else (defensive).
        run_calls: list[list[str]] = []
        def fake_run(args, **kwargs):
            run_calls.append(args)
            return _sub.CompletedProcess(
                args=args, returncode=0,
                stdout=sample, stderr="",
            )

        # Capture log lines.
        logs: list[tuple[str, str, dict]] = []
        def fake_logger(level, event, **kwargs):
            logs.append((level, event, kwargs))

        # Stub out _spawn_proactive_merger_thread so we don't actually
        # spin up a thread (the live function imports the merger
        # webhook code which has heavy backend deps; that's covered by
        # other tests).
        spawned: list[dict] = []

        b = bridge.GerritJiraBridge(
            _client(),
            bridge.BridgeConfig(cursor_file=tmp_path / "event-cursor.json"),
            sleep=lambda _: None,
            run_command=fake_run,
            logger=fake_logger,
        )
        b._spawn_proactive_merger_thread = (  # type: ignore[method-assign]
            lambda event: spawned.append(event)
        )

        synthesized = b.backfill_existing_conflicts()

        # The function returned the same number of candidates and the
        # spawn list reflects exactly one event for change 689.
        assert synthesized == 1
        assert len(spawned) == 1
        assert spawned[0]["change"]["number"] == 689
        assert spawned[0]["patchSet"]["revision"] == "abcdef1"
        # run_command was called exactly once (the gerrit query).
        assert len(run_calls) == 1
        assert "gerrit" in run_calls[0]
        # backfill_complete log line was emitted with the counter dict.
        complete_logs = [
            kwargs for level, ev, kwargs in logs if ev == "backfill_complete"
        ]
        assert len(complete_logs) == 1
        assert complete_logs[0]["candidates"] == 1
        assert complete_logs[0]["skipped_mergeable"] == 1
        # backfill_synthesize_event was logged for the one candidate.
        synth_logs = [
            kwargs for level, ev, kwargs in logs
            if ev == "backfill_synthesize_event"
        ]
        assert len(synth_logs) == 1
        assert synth_logs[0]["change_id"] == "689"

    def test_backfill_skipped_when_project_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OMNISIGHT_GERRIT_PROJECT", raising=False)
        # Override settings.gerrit_project too — otherwise tests inheriting
        # a configured prod-like settings object would fall through.
        from backend.config import settings as _settings
        monkeypatch.setattr(_settings, "gerrit_project", "", raising=False)
        monkeypatch.delenv("OMNISIGHT_GERRIT_SSH_HOST", raising=False)
        monkeypatch.setattr(_settings, "gerrit_ssh_host", "", raising=False)

        logs: list[tuple[str, str, dict]] = []
        def fake_logger(level, event, **kwargs):
            logs.append((level, event, kwargs))

        # run_command should NEVER be called when project is unset.
        def exploding_run(*args, **kwargs):
            raise AssertionError(
                "OP-1196 phase 3b regression: subprocess invoked despite "
                "missing OMNISIGHT_GERRIT_PROJECT"
            )

        b = bridge.GerritJiraBridge(
            _client(),
            bridge.BridgeConfig(cursor_file=tmp_path / "event-cursor.json"),
            sleep=lambda _: None,
            run_command=exploding_run,
            logger=fake_logger,
        )

        assert b.backfill_existing_conflicts() == 0
        # backfill_skipped warning emitted.
        skipped_logs = [
            (level, ev, kwargs) for level, ev, kwargs in logs
            if ev == "backfill_skipped"
        ]
        assert len(skipped_logs) >= 1
        assert skipped_logs[0][0] == "WARN"

    def test_backfill_search_expression_is_shell_quoted(
        self, tmp_path, monkeypatch,
    ):
        """OP-1196 phase 3b hotfix regression — the search expression
        handed to ``gerrit query`` MUST be shell-quoted BEFORE SSH
        concatenates argv into the remote command string. Without
        quoting, ``-is:wip`` is parsed as a stray CLI option by
        gerrit's argparser and the query aborts with
        ``fatal: "-is:wip" is not a valid option``. Observed in
        prod-like deploy 2026-05-17 ~03:07 BEFORE this fix landed.
        """
        import subprocess as _sub
        monkeypatch.setenv("OMNISIGHT_GERRIT_PROJECT", "omnisight/x")
        monkeypatch.setenv("OMNISIGHT_GERRIT_SSH_HOST", "claude-bot@host")
        monkeypatch.setenv("OMNISIGHT_GERRIT_SSH_PORT", "29418")
        monkeypatch.setenv("OMNISIGHT_GIT_SSH_KEY_PATH", "/dev/null")

        captured_args: list[str] = []
        def fake_run(args, **kwargs):
            captured_args.extend(args)
            return _sub.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="",
            )

        b = bridge.GerritJiraBridge(
            _client(),
            bridge.BridgeConfig(cursor_file=tmp_path / "event-cursor.json"),
            sleep=lambda _: None,
            run_command=fake_run,
            logger=lambda *a, **kw: None,
        )
        b._spawn_proactive_merger_thread = (  # type: ignore[method-assign]
            lambda event: None
        )

        b.backfill_existing_conflicts()

        # The shlex-quoted search expression must appear as a SINGLE
        # argv element, NOT as separate `project:...`, `status:open`,
        # `-is:wip` tokens. (SSH concatenates argv with spaces; the
        # remote shell re-tokenises; the quote chars preserve the
        # expression as one token.)
        assert "'project:omnisight/x status:open -is:wip'" in captured_args, (
            f"argv lacks the shell-quoted search expression — "
            f"captured: {captured_args!r}"
        )
        # No bare `-is:wip` token (would be interpreted as a CLI
        # option by gerrit's argparser, aborting the query).
        assert "-is:wip" not in captured_args, captured_args
        # And `project:...` / `status:open` must NOT be standalone
        # argv elements either — they're embedded in the quoted expr.
        assert "project:omnisight/x" not in captured_args, captured_args
        assert "status:open" not in captured_args, captured_args

    def test_backfill_handles_gerrit_query_failure_gracefully(
        self, tmp_path, monkeypatch,
    ):
        """Backfill MUST NOT raise — gerrit query failure logs + returns 0
        so the main stream loop still gets to start."""
        import subprocess as _sub
        monkeypatch.setenv("OMNISIGHT_GERRIT_PROJECT", "omnisight/x")
        monkeypatch.setenv("OMNISIGHT_GERRIT_SSH_HOST", "claude-bot@host")

        def fake_run(args, **kwargs):
            return _sub.CompletedProcess(
                args=args, returncode=255,
                stdout="", stderr="ssh: connection refused",
            )

        logs: list[tuple[str, str, dict]] = []
        b = bridge.GerritJiraBridge(
            _client(),
            bridge.BridgeConfig(cursor_file=tmp_path / "event-cursor.json"),
            sleep=lambda _: None,
            run_command=fake_run,
            logger=lambda level, ev, **kw: logs.append((level, ev, kw)),
        )

        result = b.backfill_existing_conflicts()
        assert result == 0
        fail_logs = [
            (level, ev) for level, ev, _ in logs
            if ev == "backfill_gerrit_query_failed"
        ]
        assert len(fail_logs) == 1
        assert fail_logs[0][0] == "WARN"
