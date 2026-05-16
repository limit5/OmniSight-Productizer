"""SP-B-X-012 / OP-1070 state-authority precedence resolver tests."""
from __future__ import annotations

import json
from typing import Any

import pytest

from backend.agents.state_authority_resolver import (
    AuditSink,
    ResolveResult,
    StateView,
    resolve,
)


# ── Fakes ─────────────────────────────────────────────────────────────


class _InMemorySink:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write_release_audit_row(
        self,
        *,
        outcome: str,
        fix_version: str | None,
        develop_sha: str,
        main_sha: str,
        detail: dict[str, Any],
    ) -> None:
        self.rows.append(
            {
                "outcome": outcome,
                "fix_version": fix_version,
                "develop_sha": develop_sha,
                "main_sha": main_sha,
                "detail": detail,
            }
        )


class _RaisingSink:
    def write_release_audit_row(self, **kwargs: Any) -> None:
        raise RuntimeError("synthetic DB outage")


# ── Rule 1: Gerrit merged > JIRA workflow ─────────────────────────────


def test_rule_1_gerrit_merged_wins_when_jira_not_published() -> None:
    view = StateView(
        ticket_key="OP-1070",
        gerrit_change_status="MERGED",
        gerrit_change_url="https://example/c/repo/+/1070",
        jira_status_name="進行中",
    )
    result = resolve(view)
    assert result.action == "force_walk_jira_to_published"
    assert result.rule_id == 1
    assert "Gerrit merged" in result.detail["reason"]


def test_rule_1_skipped_when_jira_already_published() -> None:
    view = StateView(
        ticket_key="OP-1070",
        gerrit_change_status="MERGED",
        jira_status_name="公開済み",
    )
    result = resolve(view)
    # No conflict at Rule 1; falls through. Other rules also satisfied → noop.
    assert result.action == "noop"
    assert result.rule_id == 0


def test_rule_1_skipped_when_gerrit_not_merged() -> None:
    view = StateView(
        ticket_key="OP-1070",
        gerrit_change_status="NEW",
        jira_status_name="進行中",
    )
    result = resolve(view)
    assert result.action == "noop"


# ── Rule 2: JIRA workflow > runner-internal ──────────────────────────


def test_rule_2_aborts_when_jira_advanced_past_in_progress() -> None:
    view = StateView(
        ticket_key="OP-1070",
        jira_status_name="Under Review",
        runner_progress_phase="working",
    )
    result = resolve(view)
    assert result.action == "abort_runner_pickup"
    assert result.rule_id == 2
    assert result.detail["jira_status_name"] == "Under Review"
    assert result.detail["runner_progress_phase"] == "working"


def test_rule_2_skipped_when_jira_in_progress_matches_runner() -> None:
    view = StateView(
        ticket_key="OP-1070",
        jira_status_name="進行中",
        runner_progress_phase="working",
    )
    result = resolve(view)
    assert result.action == "noop"


def test_rule_2_skipped_when_runner_not_in_active_phase() -> None:
    view = StateView(
        ticket_key="OP-1070",
        jira_status_name="Under Review",
        runner_progress_phase="completed",
    )
    result = resolve(view)
    # runner_progress_phase != working/picking_up → Rule 2 doesn't fire
    assert result.action == "noop"


# ── Rule 3: runner_progress > runner_snapshot ─────────────────────────


def test_rule_3_requests_refresh_on_snapshot_divergence() -> None:
    view = StateView(
        ticket_key="OP-1070",
        runner_progress_phase="committing",
        runner_snapshot_phase="working",
    )
    result = resolve(view)
    assert result.action == "refresh_runner_snapshot"
    assert result.rule_id == 3


def test_rule_3_skipped_when_snapshot_matches_progress() -> None:
    view = StateView(
        ticket_key="OP-1070",
        runner_progress_phase="working",
        runner_snapshot_phase="working",
    )
    result = resolve(view)
    assert result.action == "noop"


def test_rule_3_skipped_when_either_phase_is_none() -> None:
    view = StateView(
        ticket_key="OP-1070",
        runner_progress_phase="working",
        runner_snapshot_phase=None,
    )
    result = resolve(view)
    assert result.action == "noop"


# ── Precedence ordering ───────────────────────────────────────────────


def test_rule_1_fires_before_rule_2() -> None:
    view = StateView(
        ticket_key="OP-1070",
        gerrit_change_status="MERGED",
        jira_status_name="進行中",
        runner_progress_phase="working",
    )
    result = resolve(view)
    # Rule 1 should fire (Gerrit merged); Rule 2 has progress=working +
    # jira=進行中 which would not even fire its own check.
    assert result.rule_id == 1


def test_rule_2_fires_before_rule_3() -> None:
    view = StateView(
        ticket_key="OP-1070",
        jira_status_name="Under Review",
        runner_progress_phase="working",
        runner_snapshot_phase="picking_up",  # divergent from progress
    )
    result = resolve(view)
    # Both Rule 2 and Rule 3 are eligible; Rule 2 wins because it's
    # the higher-authority precedence.
    assert result.rule_id == 2


# ── Audit-log integration ─────────────────────────────────────────────


def test_audit_row_written_on_resolution() -> None:
    sink = _InMemorySink()
    view = StateView(
        ticket_key="OP-1070",
        gerrit_change_status="MERGED",
        jira_status_name="進行中",
    )
    resolve(
        view,
        audit_sink=sink,
        develop_sha="abc1234",
        main_sha="def5678",
        idem_key="state-authority:OP-1070:abc12345",
    )
    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["outcome"] == "noop"
    assert row["fix_version"] is None
    assert row["develop_sha"] == "abc1234"
    assert row["main_sha"] == "def5678"
    detail = row["detail"]
    assert detail["kind"] == "state-authority-resolved"
    assert detail["ticket_key"] == "OP-1070"
    assert detail["rule_id"] == 1
    assert detail["action"] == "force_walk_jira_to_published"
    assert detail["idem_key"] == "state-authority:OP-1070:abc12345"


def test_audit_row_NOT_written_on_noop() -> None:
    sink = _InMemorySink()
    view = StateView(ticket_key="OP-1070")
    resolve(view, audit_sink=sink)
    # No resolution → no audit row
    assert sink.rows == []


def test_audit_failure_does_not_block_decision() -> None:
    # Synthetic DB outage in the sink; the decision still returns.
    view = StateView(
        ticket_key="OP-1070",
        gerrit_change_status="MERGED",
        jira_status_name="進行中",
    )
    result = resolve(view, audit_sink=_RaisingSink())
    assert result.action == "force_walk_jira_to_published"
    assert result.rule_id == 1


# ── JSON-serializability of detail (release_audit `detail` column) ────


def test_detail_is_json_serializable() -> None:
    sink = _InMemorySink()
    view = StateView(
        ticket_key="OP-1070",
        gerrit_change_status="MERGED",
        gerrit_change_url="https://example/c/repo/+/1070",
        jira_status_name="進行中",
    )
    resolve(view, audit_sink=sink, idem_key="x:y:z")
    detail = sink.rows[0]["detail"]
    serialized = json.dumps(detail)  # must not raise
    re_parsed = json.loads(serialized)
    assert re_parsed["ticket_key"] == "OP-1070"
