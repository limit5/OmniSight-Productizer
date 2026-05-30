"""OP-1853 regulated-lane audit JSONL tests."""
from __future__ import annotations

import json
import logging

from backend.agents import regulated_lane_audit


EXPECTED_KEYS = {
    "timestamp",
    "ticket_key",
    "pr_number",
    "pr_url",
    "action",
    "pr_state",
    "merge_sha",
    "extra",
}


def test_record_regulated_lane_event_writes_one_jsonl_line_with_schema(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "audit" / "regulated_lane_events.jsonl"
    monkeypatch.setenv(regulated_lane_audit.AUDIT_PATH_ENV, str(path))

    regulated_lane_audit.record_regulated_lane_event(
        ticket_key="OP-1853",
        pr_number=42,
        pr_url="https://github.com/limit5/camviewpro-android/pull/42",
        action="hold_regulated",
        merge_sha="abc123",
        pr_state="merged",
        extra={"source": "test"},
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert set(event) == EXPECTED_KEYS
    assert event["ticket_key"] == "OP-1853"
    assert event["pr_number"] == 42
    assert event["pr_url"] == (
        "https://github.com/limit5/camviewpro-android/pull/42"
    )
    assert event["action"] == "hold_regulated"
    assert event["pr_state"] == "merged"
    assert event["merge_sha"] == "abc123"
    assert event["extra"] == {"source": "test"}
    assert event["timestamp"]


def test_record_regulated_lane_event_creates_parent_dir(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "audit" / "regulated_lane_events.jsonl"
    monkeypatch.setenv(regulated_lane_audit.AUDIT_PATH_ENV, str(path))

    regulated_lane_audit.record_regulated_lane_event(
        ticket_key="OP-1853",
        pr_number=42,
        pr_url="https://github.com/limit5/camviewpro-android/pull/42",
        action="flag_closed",
    )

    assert path.exists()


def test_record_regulated_lane_event_write_error_warns_once_without_raising(
    tmp_path,
    monkeypatch,
    caplog,
):
    path = tmp_path / "audit" / "regulated_lane_events.jsonl"
    monkeypatch.setenv(regulated_lane_audit.AUDIT_PATH_ENV, str(path))
    monkeypatch.setattr(regulated_lane_audit, "_write_failure_warned", False)

    real_open = regulated_lane_audit.Path.open

    def _broken_open(self, *args, **kwargs):
        if self == path:
            raise OSError("disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(regulated_lane_audit.Path, "open", _broken_open)

    with caplog.at_level(logging.WARNING, logger=regulated_lane_audit.LOGGER.name):
        for _ in range(2):
            regulated_lane_audit.record_regulated_lane_event(
                ticket_key="OP-1853",
                pr_number=42,
                pr_url="https://github.com/limit5/camviewpro-android/pull/42",
                action="hold_regulated",
            )

    warnings = [
        record
        for record in caplog.records
        if "failed to write regulated-lane audit event" in record.message
    ]
    assert len(warnings) == 1


def test_record_regulated_lane_event_honours_configurable_path(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "configured.jsonl"
    monkeypatch.setenv(regulated_lane_audit.AUDIT_PATH_ENV, str(path))

    regulated_lane_audit.record_regulated_lane_event(
        ticket_key="OP-1853",
        pr_number=7,
        pr_url="https://github.com/limit5/camviewpro-android/pull/7",
        action="flag_closed",
    )

    assert path.exists()
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["ticket_key"] == "OP-1853"
