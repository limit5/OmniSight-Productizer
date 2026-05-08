"""OP-746 -- contract tests for the conflict-rate observability stack.

Each acceptance criterion has at least one named ``test_ac*`` so the
JIRA verification comment can cite a concrete test name. The tests
exercise:

  AC1   ps_merged_metrics emission on change-merged
  AC2   conflict_observations row on Verified -1 / sibling-merged
  AC3   daily report shape for /var/log/omnisight/conflict-report-<date>.json
  AC5   3 alert threshold envelopes
  AC6   synthetic 10-PS tally
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import gerrit_jira_bridge as bridge
from backend.agents import jira_dispatch
from backend.agents.conflict_observations import (
    ConflictObservation,
    file_event_counts,
    fetch_recent_observations_sync,
    hourly_buckets,
    record_observation_sync,
    summarise,
)
from scripts import conflict_report  # type: ignore[import-not-found]


# ── SQLite fixture ────────────────────────────────────────────────


@pytest.fixture()
def conflict_db(tmp_path, monkeypatch) -> Path:
    """Prepare a SQLite-backed conflict_observations table."""
    db_path = tmp_path / "conflict.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE conflict_observations (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ps_change_id            TEXT,
            ps_change_number        INTEGER,
            ticket                  TEXT,
            files_in_conflict       TEXT NOT NULL DEFAULT '[]',
            cause_category          TEXT NOT NULL,
            pre_existing_open_count INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    monkeypatch.delenv("OMNISIGHT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OMNI_TEST_PG_URL", raising=False)
    return db_path


# ── AC1: ps_merged_metrics ────────────────────────────────────────


def test_ac1_compute_ps_merged_metrics_basic_event() -> None:
    """compute_ps_merged_metrics extracts the spec fields from a
    well-formed change-merged event."""
    event = {
        "type": "change-merged",
        "change": {
            "id": "Iabc12345",
            "number": 19,
            "subject": "[OP-19] do the thing",
            "branch": "develop",
            "createdOn": 1_700_000_000,
            "lastUpdated": 1_700_000_000 + 60 * 18,  # 18 min
            "currentPatchSet": {
                "number": 3,
                "sizeInsertions": 100,
                "sizeDeletions": 50,
                "approvals": [
                    {"type": "Verified", "value": "-1"},
                    {"type": "Code-Review", "value": "1"},
                ],
            },
        },
        "patchSet": {"number": 3, "revision": "deadbeef"},
    }
    metrics = bridge.compute_ps_merged_metrics(event)
    assert metrics["change_id"] == "Iabc12345"
    assert metrics["change_number"] == "19"
    assert metrics["ticket"] == "OP-19"
    assert metrics["lifetime_min"] == 18.0
    assert metrics["patchset_count"] == 3
    assert metrics["final_diff_size"] == 150
    assert metrics["verified_minus_one_count"] == 1
    # rebase_count / rework_count default to 0; per-PS history is
    # populated by the daily report from the broader log stream.
    assert metrics["rebase_count"] == 0
    assert metrics["rework_count"] == 0


def test_ac1_handler_emits_ps_merged_metrics_log_line() -> None:
    """The bridge process_stream_event emits ps_merged_metrics for
    every change-merged event it sees."""
    fake = _FakeBridge()
    event = {
        "type": "change-merged",
        "change": {
            "id": "I1",
            "number": 42,
            "subject": "[OP-42] feature",
            "branch": "develop",
            "currentPatchSet": {"number": 1},
        },
    }
    fake.process_stream_event(event)
    metric_logs = [l for l in fake.logs if l[1] == "ps_merged_metrics"]
    assert len(metric_logs) == 1
    level, _, extra = metric_logs[0]
    assert level == "INFO"
    assert extra["ticket"] == "OP-42"
    assert extra["change_number"] == "42"


# ── AC2: conflict_observations writes ─────────────────────────────


def test_ac2_record_observation_sqlite_writes_row(conflict_db) -> None:
    """record_observation_sync inserts on the SQLite path."""
    obs = ConflictObservation(
        ts=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        cause_category="sibling_merged",
        files_in_conflict=("docs/sop/lessons-learned.md",),
        ps_change_number=99,
        ticket="OP-746",
    )
    ok = record_observation_sync(obs)
    assert ok
    rows = fetch_recent_observations_sync(
        since=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].cause_category == "sibling_merged"
    assert rows[0].files_in_conflict == ("docs/sop/lessons-learned.md",)
    assert rows[0].ps_change_number == 99
    assert rows[0].ticket == "OP-746"


def test_ac2_record_observation_invalid_cause_returns_false() -> None:
    obs = ConflictObservation(
        ts=datetime.now(timezone.utc),
        cause_category="not-a-category",
    )
    assert record_observation_sync(obs) is False


def test_ac2_verified_minus_one_handler_records_observation(
    conflict_db,
) -> None:
    """The bridge's comment-added handler writes one observation when
    a Verified -1 vote is the transition."""
    fake = _FakeBridge()
    event = {
        "type": "comment-added",
        "change": {
            "id": "Ixyz",
            "number": 51,
            "subject": "[OP-50] another change",
            "branch": "develop",
        },
        "approvals": [
            {"type": "Verified", "value": "-1", "oldValue": "0"},
        ],
    }
    fake.process_stream_event(event)
    rows = fetch_recent_observations_sync(
        since=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert len(rows) == 1
    assert rows[0].cause_category == "verified_minus_one"
    assert rows[0].ticket == "OP-50"
    assert rows[0].ps_change_number == 51


def test_ac2_verified_minus_one_idempotent_on_replay(conflict_db) -> None:
    """Gerrit replays approvals snapshots on every comment-added.
    We only record the transition, not subsequent re-fires."""
    fake = _FakeBridge()
    event = {
        "type": "comment-added",
        "change": {
            "id": "Ixyz",
            "number": 51,
            "subject": "[OP-50] another change",
            "branch": "develop",
        },
        # oldValue == value means "no change" -- replay snapshot
        "approvals": [
            {"type": "Verified", "value": "-1", "oldValue": "-1"},
        ],
    }
    fake.process_stream_event(event)
    rows = fetch_recent_observations_sync(
        since=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert rows == []


# ── AC3: daily report shape ───────────────────────────────────────


def test_ac3_build_report_shape_matches_spec(conflict_db, tmp_path) -> None:
    """build_report returns the JSON shape the dashboard tile +
    operator's morning standup rely on."""
    now = datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)
    obs_list = [
        ConflictObservation(
            ts=now - timedelta(hours=2),
            cause_category="sibling_merged",
            files_in_conflict=("docs/sop/lessons-learned.md",),
            ps_change_id="I1",
            ps_change_number=10,
        ),
        ConflictObservation(
            ts=now - timedelta(hours=1),
            cause_category="verified_minus_one",
            files_in_conflict=("backend/foo.py",),
            ps_change_id="I2",
            ps_change_number=11,
        ),
    ]
    ps_merged = [
        conflict_report.PsMergedMetrics(
            ts=now - timedelta(hours=3),
            change_id="I1", ticket="OP-1",
            lifetime_min=18.0, patchset_count=2,
            rebase_count=1, rework_count=1,
            final_diff_size=100, verified_minus_one_count=0,
        ),
        conflict_report.PsMergedMetrics(
            ts=now - timedelta(hours=2),
            change_id="I2", ticket="OP-2",
            lifetime_min=42.0, patchset_count=3,
            rebase_count=2, rework_count=0,
            final_diff_size=80, verified_minus_one_count=1,
        ),
    ]
    report = conflict_report.build_report(
        conflict_report.ReportInputs(
            window_start=now - timedelta(hours=24),
            window_end=now,
            observations=obs_list,
            ps_merged=ps_merged,
        )
    )
    assert report["totals"]["conflict_events"] == 2
    assert report["totals"]["ps_merged"] == 2
    assert report["totals"]["pses_with_conflict"] == 2
    assert report["medians"]["ps_lifetime_min"] == 30.0  # median(18, 42)
    assert report["medians"]["rebase_count_per_ps"] == 1.5
    assert {h["file"] for h in report["hotspots"]} == {
        "docs/sop/lessons-learned.md",
        "backend/foo.py",
    }
    assert report["by_cause"]["sibling_merged"] == 1
    assert report["by_cause"]["verified_minus_one"] == 1


# ── AC5: alert thresholds ─────────────────────────────────────────


def test_ac5_hourly_rate_alert_fires_above_threshold() -> None:
    """> 30% conflict rate over the last 4h → DEGRADED."""
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    # 8 conflicts across 4 hourly buckets, on top of 10 merged PSes
    # → rate = 80%, way over 30% threshold
    observations = [
        ConflictObservation(
            ts=now - timedelta(minutes=15 * i),
            cause_category="sibling_merged",
            files_in_conflict=("foo.py",),
        )
        for i in range(8)
    ]
    ps_merged = [
        conflict_report.PsMergedMetrics(
            ts=now - timedelta(minutes=20 * i),
            change_id=f"I{i}", ticket=None,
            lifetime_min=10.0, patchset_count=1,
            rebase_count=0, rework_count=0,
            final_diff_size=10, verified_minus_one_count=0,
        )
        for i in range(10)
    ]
    buckets = hourly_buckets(observations, window_end=now, bucket_count=4)
    alerts = conflict_report.compute_alerts(
        observations=observations,
        ps_merged=ps_merged,
        window_end=now,
        median_lifetime_min=10.0,
        hourly_buckets_=buckets,
    )
    codes = {a.code for a in alerts}
    assert "conflict_rate_high_4h" in codes
    rate_alert = next(a for a in alerts if a.code == "conflict_rate_high_4h")
    assert rate_alert.severity == "DEGRADED"


def test_ac5_same_file_threshold_alert_fires_at_5() -> None:
    """≥ 5 events / 24h on a single file → CRITICAL."""
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    observations = [
        ConflictObservation(
            ts=now - timedelta(minutes=30 * i),
            cause_category="sibling_merged",
            files_in_conflict=("docs/sop/lessons-learned.md",),
        )
        for i in range(5)
    ]
    alerts = conflict_report.compute_alerts(
        observations=observations,
        ps_merged=[],
        window_end=now,
        median_lifetime_min=None,
        hourly_buckets_=[0, 0, 0, 0],
    )
    hits = [a for a in alerts if a.code == "conflict_hotspot_same_file_24h"]
    assert len(hits) == 1
    assert hits[0].severity == "CRITICAL"
    assert hits[0].context["file"] == "docs/sop/lessons-learned.md"
    assert hits[0].context["count"] == 5


def test_ac5_median_lifetime_alert_fires_above_4h() -> None:
    """Median PS lifetime > 4h → WARN."""
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    alerts = conflict_report.compute_alerts(
        observations=[],
        ps_merged=[],
        window_end=now,
        median_lifetime_min=300.0,  # 5h, above the 4h threshold
        hourly_buckets_=[0, 0, 0, 0],
    )
    hits = [a for a in alerts if a.code == "ps_lifetime_high"]
    assert len(hits) == 1
    assert hits[0].severity == "WARN"


def test_ac5_no_alerts_when_below_thresholds() -> None:
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    observations = [
        ConflictObservation(
            ts=now - timedelta(minutes=10),
            cause_category="sibling_merged",
            files_in_conflict=("foo.py",),
        )
    ]
    ps_merged = [
        conflict_report.PsMergedMetrics(
            ts=now - timedelta(minutes=10),
            change_id="I1", ticket=None,
            lifetime_min=10.0, patchset_count=1,
            rebase_count=0, rework_count=0,
            final_diff_size=10, verified_minus_one_count=0,
        )
        for _ in range(20)
    ]
    alerts = conflict_report.compute_alerts(
        observations=observations,
        ps_merged=ps_merged,
        window_end=now,
        median_lifetime_min=15.0,
        hourly_buckets_=[1, 0, 0, 0],
    )
    assert alerts == []


# ── AC6: synthetic 10-PS tally ────────────────────────────────────


def test_ac6_synthetic_ten_pses_tally_correctly(conflict_db) -> None:
    """Simulate 10 PSes with mixed conflict patterns → report tallies match."""
    now = datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)

    # Pattern: 10 merged PSes; 3 hit conflict on lessons-learned.md, 1
    # on auto-runner-jira.py, 1 verified -1, 5 clean.
    obs_list = [
        ConflictObservation(
            ts=now - timedelta(minutes=30 * (i + 1)),
            cause_category="sibling_merged",
            files_in_conflict=("docs/sop/lessons-learned.md",),
            ps_change_id=f"I-conflict-{i}",
            ps_change_number=100 + i,
            ticket=f"OP-{200 + i}",
        )
        for i in range(3)
    ] + [
        ConflictObservation(
            ts=now - timedelta(minutes=180),
            cause_category="sibling_merged",
            files_in_conflict=("auto-runner-jira.py",),
            ps_change_id="I-conflict-3",
            ps_change_number=110,
        ),
        ConflictObservation(
            ts=now - timedelta(minutes=120),
            cause_category="verified_minus_one",
            files_in_conflict=(),
            ps_change_id="I-conflict-4",
            ps_change_number=111,
        ),
    ]

    ps_merged = [
        conflict_report.PsMergedMetrics(
            ts=now - timedelta(minutes=10 * (i + 1)),
            change_id=f"I-merged-{i}", ticket=f"OP-{300 + i}",
            lifetime_min=float(15 + i),
            patchset_count=1 + (i % 3),
            rebase_count=i % 3,
            rework_count=0,
            final_diff_size=10 * (i + 1),
            verified_minus_one_count=0,
        )
        for i in range(10)
    ]

    report = conflict_report.build_report(
        conflict_report.ReportInputs(
            window_start=now - timedelta(hours=24),
            window_end=now,
            observations=obs_list,
            ps_merged=ps_merged,
        )
    )
    assert report["totals"]["ps_merged"] == 10
    assert report["totals"]["conflict_events"] == 5
    # 5 distinct PSes hit at least one conflict
    assert report["totals"]["pses_with_conflict"] == 5
    assert report["totals"]["pct_pses_hit_at_least_one"] == 50.0
    assert report["by_cause"]["sibling_merged"] == 4
    assert report["by_cause"]["verified_minus_one"] == 1
    # Top hotspot is lessons-learned.md with 3 events
    assert report["hotspots"][0]["file"] == "docs/sop/lessons-learned.md"
    assert report["hotspots"][0]["events"] == 3


# ── Aggregation helpers ───────────────────────────────────────────


def test_summarise_distinct_changes_uses_change_id_or_number() -> None:
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    obs_list = [
        ConflictObservation(
            ts=now,
            cause_category="sibling_merged",
            ps_change_id="I1",
        ),
        ConflictObservation(
            ts=now,
            cause_category="sibling_merged",
            ps_change_number=42,
        ),
        # Same change_id repeated; should not double-count
        ConflictObservation(
            ts=now,
            cause_category="verified_minus_one",
            ps_change_id="I1",
        ),
    ]
    summary = summarise(
        obs_list, window_start=now - timedelta(hours=1), window_end=now,
    )
    assert summary.distinct_changes == 2


def test_file_event_counts_aggregates_per_row() -> None:
    rows = [
        ConflictObservation(
            ts=datetime.now(timezone.utc),
            cause_category="sibling_merged",
            files_in_conflict=("a.py", "b.py"),
        ),
        ConflictObservation(
            ts=datetime.now(timezone.utc),
            cause_category="sibling_merged",
            files_in_conflict=("a.py",),
        ),
    ]
    counts = file_event_counts(rows)
    assert counts == {"a.py": 2, "b.py": 1}


def test_hourly_buckets_packs_oldest_first() -> None:
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    obs_list = [
        ConflictObservation(
            ts=now - timedelta(minutes=10),  # bucket 3 (newest)
            cause_category="sibling_merged",
        ),
        ConflictObservation(
            ts=now - timedelta(hours=2, minutes=5),  # bucket 1
            cause_category="sibling_merged",
        ),
        ConflictObservation(
            ts=now - timedelta(hours=2, minutes=30),  # bucket 1
            cause_category="sibling_merged",
        ),
    ]
    buckets = hourly_buckets(obs_list, window_end=now, bucket_count=4)
    assert buckets == [0, 2, 0, 1]


# ── Bridge log parsing for the daily report ─────────────────────


def test_parse_bridge_log_filters_to_window(tmp_path) -> None:
    log_path = tmp_path / "bridge.log"
    in_window = json.dumps({
        "timestamp": "2026-05-08T12:00:00+00:00",
        "level": "INFO",
        "event": "ps_merged_metrics",
        "change_id": "I1", "ticket": "OP-1",
        "lifetime_min": 18.0, "patchset_count": 2,
        "rebase_count": 1, "rework_count": 0,
        "final_diff_size": 100, "verified_minus_one_count": 0,
    })
    out_window = json.dumps({
        "timestamp": "2026-05-01T00:00:00+00:00",
        "level": "INFO",
        "event": "ps_merged_metrics",
        "change_id": "I-old",
    })
    other_event = json.dumps({
        "timestamp": "2026-05-08T12:30:00+00:00",
        "level": "INFO",
        "event": "heartbeat",
    })
    log_path.write_text(in_window + "\n" + out_window + "\n" + other_event + "\n")
    rows = conflict_report.parse_bridge_log(
        log_path,
        window_start=datetime(2026, 5, 8, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].change_id == "I1"
    assert rows[0].rebase_count == 1


def test_parse_bridge_log_handles_systemd_prefix(tmp_path) -> None:
    """Bridge log may be prefixed by python's logger format
    ('YYYY-MM-DD HH:MM:SS LEVEL name: {json}'). The parser strips the
    prefix and reads from the first '{'."""
    log_path = tmp_path / "bridge.log"
    payload = {
        "timestamp": "2026-05-08T12:00:00+00:00",
        "level": "INFO",
        "event": "ps_merged_metrics",
        "change_id": "I1",
        "patchset_count": 1,
    }
    log_path.write_text(
        f"2026-05-08 12:00:00 INFO bridge: {json.dumps(payload)}\n"
    )
    rows = conflict_report.parse_bridge_log(
        log_path,
        window_start=datetime(2026, 5, 8, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].change_id == "I1"


# ── Test scaffolding ──────────────────────────────────────────────


def _client() -> jira_dispatch.DispatchClient:
    return jira_dispatch.DispatchClient(
        agent_class="subscription-claude",
        base_url="https://example.atlassian.net/rest/api/3",
        project_key="OP",
        auth_header="Basic test",
        bot_account_id="acct-1",
        bot_email="rt3628+claude-bot@gmail.com",
    )


class _FakeBridge(bridge.GerritJiraBridge):
    """Minimal bridge that records log calls and shorts JIRA RPC."""

    def __init__(self) -> None:
        self.logs: list[tuple[str, str, dict[str, Any]]] = []
        super().__init__(
            _client(),
            bridge.BridgeConfig(
                heartbeat_seconds=9999, periodic_catchup_seconds=0,
            ),
            sleep=lambda _: None,
            logger=self._log,
        )

    def _log(self, level: str, event: str, **extra: Any) -> None:
        self.logs.append((level, event, extra))

    # No JIRA RPC needed — we only test logs and DB writes.
    def jira_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    def query_gerrit_change(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def _schedule_auto_rebase_sweep(self, _event: dict[str, Any]) -> None:
        # Keep the in-process scheduler from spawning timers in tests.
        pass
