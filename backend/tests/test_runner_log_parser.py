"""OP-902 (F4) — runner log backfill parser and fixture tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from backend.agents.failure_class import FailureClass, RUNNER_FAILURE_MEMBERS
from backend.agents.runner_log_parser import (
    incident_id_for,
    iter_log_files,
    parse_log_file,
    parse_log_line,
)
from scripts.backfill_runner_incidents import backfill, insert_incidents
from scripts.generate_failure_graph_fixture import fetch_runner_incidents, write_fixture


def _base() -> datetime:
    return datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)


def _create_runner_incidents_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE runner_incidents (
                    incident_id TEXT PRIMARY KEY,
                    ticket_key TEXT NOT NULL,
                    failure_class TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    raw_traceback TEXT NOT NULL DEFAULT '',
                    runner_class TEXT NOT NULL DEFAULT 'unknown',
                    mutex_label TEXT,
                    area TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
        )


def test_known_format_runner_log_parses_pickup_revert_mutex_area_and_push() -> None:
    lines = [
        "[runner] selected: OP-902 (component=HIGH)",
        "[runner] OP-902 CLI failed rc=143; reverting ticket",
        "[runner] mutex-lost OP-902 could not acquire mutex backend/agents/foo.py",
        "[runner] area-validation-failure OP-902 unknown area label firmware",
        "[runner] Gerrit push failed: OP-902 rebase conflict on develop",
    ]

    parsed = [
        parse_log_line(
            line,
            source_file=Path("codex-20260511-000000.log"),
            source_line=idx,
            base_timestamp=_base(),
            runner_class="subscription-codex",
        )
        for idx, line in enumerate(lines, 1)
    ]

    assert [p.event_type for p in parsed if p] == [
        "pickup",
        "revert",
        "mutex-lost",
        "area-validation-failure",
        "push-rejected",
    ]
    assert [p.failure_class for p in parsed if p] == [
        FailureClass.OTHER,
        FailureClass.RUNNER_TIMEOUT,
        FailureClass.MUTEX_CONTENTION,
        FailureClass.UNKNOWN_AREA_LABEL,
        FailureClass.MERGE_CONFLICT,
    ]
    assert parsed[2] is not None
    assert parsed[2].mutex_label == "backend/agents/foo.py"


def test_unknown_non_runner_line_is_skipped() -> None:
    parsed = parse_log_line(
        "ticket prose mentions OP-902 and [runner] but is not a marker",
        source_file=Path("codex-20260511-000000.log"),
        source_line=1,
        base_timestamp=_base(),
        runner_class="subscription-codex",
    )
    assert parsed is None


def test_parse_log_file_uses_filename_time_and_oldest_first(tmp_path: Path) -> None:
    newer = tmp_path / "codex-20260511-010000.log"
    older = tmp_path / "claude-20260510-230000.log"
    newer.write_text("[runner] selected: OP-902 (component=HIGH)\n", encoding="utf-8")
    older.write_text("[runner] selected: OP-901 (component=HIGH)\n", encoding="utf-8")

    assert [p.name for p in iter_log_files(tmp_path)] == [older.name, newer.name]
    parsed = parse_log_file(older)

    assert len(parsed) == 1
    assert parsed[0].timestamp == datetime(2026, 5, 10, 23, 0, 1, tzinfo=timezone.utc)
    assert parsed[0].runner_class == "subscription-claude"


def test_parse_log_file_carries_ticket_context_for_push_failure(tmp_path: Path) -> None:
    log_file = tmp_path / "codex-20260511-000000.log"
    log_file.write_text(
        "\n".join(
            [
                "[runner] selected: OP-902 (component=HIGH)",
                "[runner] OP-902 CLI returned 0; preparing Gerrit push...",
                "[runner] Gerrit push failed:",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    parsed = parse_log_file(log_file)

    assert [p.event_type for p in parsed] == ["pickup", "push-rejected"]
    assert parsed[1].ticket_key == "OP-902"
    assert parsed[1].failure_class is FailureClass.BRIDGE_DESYNC


def test_idempotent_insert_skips_second_run(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'incidents.db'}", future=True)
    _create_runner_incidents_table(engine)
    incident = parse_log_line(
        "[runner] selected: OP-902 (component=HIGH)",
        source_file=Path("codex-20260511-000000.log"),
        source_line=1,
        base_timestamp=_base(),
        runner_class="subscription-codex",
    )
    assert incident is not None

    assert insert_incidents(engine, [incident]) == (1, 0)
    assert insert_incidents(engine, [incident]) == (0, 1)

    with engine.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM runner_incidents")).scalar_one()
    assert count == 1


def test_fixture_format_matches_failure_graph_schema(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'incidents.db'}", future=True)
    _create_runner_incidents_table(engine)
    incident = parse_log_line(
        "[runner] mutex-lost OP-902 could not acquire mutex backend/agents/foo.py",
        source_file=Path("codex-20260511-000000.log"),
        source_line=1,
        base_timestamp=_base(),
        runner_class="subscription-codex",
    )
    assert incident is not None
    insert_incidents(engine, [incident])

    rows = fetch_runner_incidents(engine)
    out = tmp_path / "failure_graph_fixture.json"
    assert write_fixture(rows, out) == 1

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == [
        {
            "incident_id": incident.incident_id,
            "ticket_key": "OP-902",
            "failure_class": "MUTEX_CONTENTION",
            "mutex_label": "backend/agents/foo.py",
            "occurred_at": "2026-05-11T00:00:01Z",
            "summary": incident.summary,
        }
    ]


@pytest.mark.parametrize(
    "line, expected",
    [
        ("[runner] Gerrit push failed: OP-1 checkpatch.pl --strict failed", FailureClass.LINT_FAILURE),
        ("[runner] Gerrit push failed: OP-2 pytest backend/tests/x.py FAILED", FailureClass.TEST_FAILURE),
        ("[runner] Gerrit push failed: OP-3 merge conflict in backend/x.py", FailureClass.MERGE_CONFLICT),
        ("[runner] Gerrit push failed: OP-4 untracked working tree files", FailureClass.WORKTREE_DIRTY),
        ("[runner] OP-5 CLI failed rc=1; reverting ticket", FailureClass.LLM_LOOP_DETECTED),
        ("[runner] area-validation-failure OP-6 scope-to-paths not found", FailureClass.UNKNOWN_AREA_LABEL),
        ("[runner] Gerrit push failed: OP-7 bridge desync between Gerrit and JIRA", FailureClass.BRIDGE_DESYNC),
        ("[runner] OP-8 CLI failed rc=143; reverting ticket", FailureClass.RUNNER_TIMEOUT),
        ("[runner] mutex-lost OP-9 could not acquire mutex backend/x.py", FailureClass.MUTEX_CONTENTION),
        ("[runner] Gerrit push failed: OP-10 outcomes-grader refused insufficient signal", FailureClass.OUTCOMES_GRADER_REFUSED),
    ],
    ids=[m.value for m in RUNNER_FAILURE_MEMBERS],
)
def test_classify_per_class_fan_out_balanced(line: str, expected: FailureClass) -> None:
    parsed = parse_log_line(
        line,
        source_file=Path("codex-20260511-000000.log"),
        source_line=1,
        base_timestamp=_base(),
        runner_class="subscription-codex",
    )
    assert parsed is not None
    assert parsed.failure_class is expected


def test_backfill_writes_fixture_and_reports_totals(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "codex-20260511-000000.log").write_text(
        "\n".join(
            [
                "[runner] selected: OP-902 (component=HIGH)",
                "[runner] OP-902 CLI failed rc=143; reverting ticket",
                "[runner] Gerrit push failed: OP-902 rebase conflict on develop",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'incidents.db'}", future=True)
    _create_runner_incidents_table(engine)

    report = backfill(
        engine=engine,
        log_dir=log_dir,
        fixture_path=tmp_path / "failure_graph_fixture.json",
    )

    assert report["files"] == 1
    assert report["parsed"] == 3
    assert report["inserted"] == 3
    assert report["fixture_count"] == 3
    assert report["total"] == 3
    assert (tmp_path / "failure_graph_fixture.json").exists()


def test_incident_id_uses_timestamp_ticket_and_class() -> None:
    stamp = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    a = incident_id_for(stamp, "OP-902", FailureClass.TEST_FAILURE)
    assert a == incident_id_for(stamp, "OP-902", FailureClass.TEST_FAILURE)
    assert a != incident_id_for(stamp, "OP-902", FailureClass.LINT_FAILURE)
