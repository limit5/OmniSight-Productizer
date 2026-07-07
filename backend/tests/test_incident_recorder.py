"""OP-1293 branch coverage for the incident recorder seam.

OP-2537 adds the durable-writer contract tests (``record_incident_durable``):
live-v1 deterministic ids, claim-token dedup, DSN resolution, and the
never-raise failure path (log line + counter file).
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents import incident_recorder
from backend.agents.failure_class import FailureClass
from backend.agents.memory_tool_handler import MemoryAuditWriteFailed

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0206 = BACKEND_ROOT / "alembic" / "versions" / "0206_runner_incidents.py"


_DURABLE_ENV_KEYS = (
    "OMNISIGHT_DATABASE_URL",
    "OMNISIGHT_AUDIT_DB_ENV_FILE",
    "OMNISIGHT_INCIDENT_WRITE_FAIL_COUNTER",
)


@pytest.fixture(autouse=True)
def _isolate_durable_writer(tmp_path: Path):
    """OP-2537 safety: unit tests must be PHYSICALLY UNABLE to write prod.

    The dev host's real ``~/.config/omnisight/audit-db.env`` carries the
    PROD DSN, so every test here delenvs the direct DSN, points the env
    file + fail counter into ``tmp_path``, and resets the engine cache.
    Env handling is deliberately monkeypatch-free so this fixture's
    teardown ordering stays independent of per-test monkeypatching
    (the BrokenLock test patches the module lock).
    """
    saved = {key: os.environ.get(key) for key in _DURABLE_ENV_KEYS}
    os.environ.pop("OMNISIGHT_DATABASE_URL", None)
    os.environ["OMNISIGHT_AUDIT_DB_ENV_FILE"] = str(tmp_path / "audit-db.env")
    os.environ["OMNISIGHT_INCIDENT_WRITE_FAIL_COUNTER"] = str(
        tmp_path / "incident_write_failures.count"
    )
    incident_recorder.reset_for_tests()
    yield
    incident_recorder.reset_for_tests()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _load_0206_module():
    spec = importlib.util.spec_from_file_location(
        "_alembic_0206_runner_incidents", MIGRATION_0206
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["_alembic_0206_runner_incidents"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _make_incidents_db(tmp_path: Path) -> tuple[str, Path]:
    """tmp sqlite DSN with ``runner_incidents`` created from the 0206 DDL."""
    db_path = tmp_path / "runner_incidents.sqlite"
    ddl = _load_0206_module()._SQLITE_TABLE_DDL
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()
    return f"sqlite:///{db_path}", db_path


def _db_rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return list(
            conn.execute(
                "SELECT incident_id, ticket_key, failure_class, summary, "
                "raw_traceback, runner_class, mutex_label, area, created_at "
                "FROM runner_incidents ORDER BY rowid"
            )
        )
    finally:
        conn.close()


def _read_counter(tmp_path: Path) -> int:
    return int(
        (tmp_path / "incident_write_failures.count").read_text().strip()
    )


def _audit_request() -> SimpleNamespace:
    return SimpleNamespace(query_fleet="fleet-prod", target_fleet="fleet-dev")


def _audit_decision(
    *,
    summary: str,
    permitted: bool,
    escalate: bool = False,
    tier: str = "S",
) -> SimpleNamespace:
    return SimpleNamespace(
        audit_summary=summary,
        permitted=permitted,
        escalate=escalate,
        tier=SimpleNamespace(value=tier),
    )


def test_record_memory_recall_audit_snapshots_and_filters_events():
    incident_recorder.record_memory_recall_audit(
        _audit_request(),
        _audit_decision(summary="permitted recall", permitted=True),
    )
    incident_recorder.record_memory_recall_audit(
        _audit_request(),
        _audit_decision(summary="denied recall", permitted=False, escalate=True, tier="X"),
    )

    rows = incident_recorder.get_recorded_events()
    filtered = incident_recorder.get_recorded_events(FailureClass.MEMORY_RECALL_AUDIT)

    assert [row.summary for row in rows] == ["permitted recall", "denied recall"]
    assert filtered == rows
    assert rows[1].permitted is False
    assert rows[1].escalate is True
    assert rows[1].tier == "X"


def test_record_memory_recall_audit_translates_buffer_write_failure(monkeypatch):
    class BrokenLock:
        def __enter__(self):
            raise RuntimeError("lock unavailable")

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(incident_recorder, "_buffer_lock", BrokenLock())

    with pytest.raises(MemoryAuditWriteFailed, match="lock unavailable"):
        incident_recorder.record_memory_recall_audit(
            _audit_request(),
            _audit_decision(summary="audit", permitted=True),
        )


def test_record_runner_incident_coerces_string_failure_class_non_strict():
    record = incident_recorder.record_runner_incident(
        ticket_key="OP-1293",
        failure_class="not_registered",
        summary="unknown failure class",
        incident_id="incident-op-1293",
    )

    assert record.incident_id == "incident-op-1293"
    assert record.failure_class is FailureClass.OTHER
    assert incident_recorder.get_runner_incidents(failure_class=FailureClass.OTHER) == [
        record
    ]


def test_get_runner_incidents_filters_by_ticket_key():
    first = incident_recorder.record_runner_incident(
        ticket_key="OP-1293",
        failure_class=FailureClass.TEST_FAILURE,
        summary="target incident",
        area="backend",
    )
    incident_recorder.record_runner_incident(
        ticket_key="OP-0001",
        failure_class=FailureClass.TEST_FAILURE,
        summary="other ticket",
        area="backend",
    )

    assert incident_recorder.get_runner_incidents(ticket_key="OP-1293") == [first]


# ── OP-2537: durable writer contract ────────────────────────────────


def test_isolation_fixture_blocks_prod_dsn(tmp_path: Path) -> None:
    """The autouse fixture makes tests physically unable to reach prod."""
    assert "OMNISIGHT_DATABASE_URL" not in os.environ
    env_file = Path(os.environ["OMNISIGHT_AUDIT_DB_ENV_FILE"])
    counter = Path(os.environ["OMNISIGHT_INCIDENT_WRITE_FAIL_COUNTER"])
    assert env_file.is_relative_to(tmp_path)
    assert counter.is_relative_to(tmp_path)
    assert not env_file.exists()  # no DSN reachable at all by default


def test_live_v1_id_uses_exact_hash_input_string() -> None:
    """Pinned contract: incident_id = live-v1- + sha256 of the EXACT
    input string ``f"{ticket_key}|{failure_class.value}|{claim_token}"``.
    """
    ticket_key = "OP-2537"
    failure_class = FailureClass.TEST_FAILURE
    claim_token = "claim-tok-123"

    record = incident_recorder.record_incident_durable(
        ticket_key, failure_class, claim_token=claim_token
    )

    expected = "live-v1-" + hashlib.sha256(
        f"{ticket_key}|{failure_class.value}|{claim_token}".encode("utf-8")
    ).hexdigest()
    assert record.incident_id == expected


def test_live_v1_id_hashes_the_coerced_failure_class_value() -> None:
    """String classes coerce like the existing recorder; the hash uses
    the COERCED ``.value`` (unregistered → OTHER)."""
    record = incident_recorder.record_incident_durable(
        "OP-2537", "definitely-not-registered", claim_token="tok"
    )

    assert record.failure_class is FailureClass.OTHER
    expected = "live-v1-" + hashlib.sha256(
        f"OP-2537|{FailureClass.OTHER.value}|tok".encode("utf-8")
    ).hexdigest()
    assert record.incident_id == expected


def test_same_claim_token_dedups_to_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same (ticket, class, token) twice → ON CONFLICT DO NOTHING → 1 row."""
    dsn, db_path = _make_incidents_db(tmp_path)
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", dsn)

    first = incident_recorder.record_incident_durable(
        "OP-2537", FailureClass.TEST_FAILURE,
        claim_token="tok-1", summary="first seam",
    )
    second = incident_recorder.record_incident_durable(
        "OP-2537", "TEST_FAILURE",
        claim_token="tok-1", summary="second seam",
    )

    assert first.incident_id == second.incident_id
    rows = _db_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == first.incident_id
    assert rows[0][0].startswith("live-v1-")
    # First write wins; the replay is a DO NOTHING no-op.
    assert rows[0][3] == "first seam"
    # Deque dedups by incident_id too (recall read-cache).
    assert len(incident_recorder.get_runner_incidents(ticket_key="OP-2537")) == 1


def test_no_claim_token_means_uuid_ids_and_no_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claim_token=None → uuid4().hex ids, two calls → two rows."""
    dsn, db_path = _make_incidents_db(tmp_path)
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", dsn)

    first = incident_recorder.record_incident_durable(
        "OP-2537", FailureClass.TEST_FAILURE, summary="a"
    )
    second = incident_recorder.record_incident_durable(
        "OP-2537", FailureClass.TEST_FAILURE, summary="a"
    )

    assert first.incident_id != second.incident_id
    assert not first.incident_id.startswith("live-v1-")
    assert len(first.incident_id) == 32  # uuid4().hex
    assert len(_db_rows(db_path)) == 2
    assert len(incident_recorder.get_runner_incidents(ticket_key="OP-2537")) == 2


def test_empty_string_kwargs_normalize_to_null_and_created_at_db_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-string mutex_label/area land as NULL; created_at is DB-default."""
    dsn, db_path = _make_incidents_db(tmp_path)
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", dsn)

    record = incident_recorder.record_incident_durable(
        "OP-2537", FailureClass.TEST_FAILURE,
        claim_token="tok-nulls", mutex_label="", area="",
    )

    assert record.mutex_label is None
    assert record.area is None
    rows = _db_rows(db_path)
    assert len(rows) == 1
    _, _, _, _, _, _, mutex_label, area, created_at = rows[0]
    assert mutex_label is None
    assert area is None
    assert created_at  # populated by the DB default, not the writer


def test_db_failure_still_appends_to_deque_and_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Insert failure → record still in the deque, one log line + counter."""
    # sqlite DB exists but has NO runner_incidents table → insert fails.
    empty_db = tmp_path / "empty.sqlite"
    sqlite3.connect(empty_db).close()
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", f"sqlite:///{empty_db}")

    with caplog.at_level(logging.WARNING, logger="backend.agents.incident_recorder"):
        record = incident_recorder.record_incident_durable(
            "OP-2537", FailureClass.TEST_FAILURE, claim_token="tok-db-down"
        )

    assert incident_recorder.get_runner_incidents(ticket_key="OP-2537") == [record]
    failure_lines = [
        r for r in caplog.records
        if r.getMessage().startswith("incident_recorder.durable_write_failed")
    ]
    assert len(failure_lines) == 1
    assert _read_counter(tmp_path) == 1


def test_no_dsn_never_raises_logs_once_and_increments_counter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No env DSN + no env file → no raise, ONE structured line, counter."""
    with caplog.at_level(logging.WARNING, logger="backend.agents.incident_recorder"):
        record = incident_recorder.record_incident_durable(
            "OP-2537", FailureClass.LINT_FAILURE, claim_token="tok-no-dsn"
        )
        incident_recorder.record_incident_durable(
            "OP-2537", FailureClass.LINT_FAILURE, claim_token="tok-no-dsn-2"
        )

    assert record.incident_id.startswith("live-v1-")
    failure_lines = [
        r for r in caplog.records
        if r.getMessage().startswith("incident_recorder.durable_write_failed")
    ]
    assert len(failure_lines) == 2  # one line per failed write
    assert _read_counter(tmp_path) == 2


def test_env_file_fallback_resolves_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No OMNISIGHT_DATABASE_URL env → KEY=VALUE env file supplies it."""
    dsn, db_path = _make_incidents_db(tmp_path)
    assert "OMNISIGHT_DATABASE_URL" not in os.environ
    (tmp_path / "audit-db.env").write_text(
        "# audit DB config\n"
        "SOME_OTHER_KEY=ignored\n"
        f"OMNISIGHT_DATABASE_URL={dsn}\n",
        encoding="utf-8",
    )

    record = incident_recorder.record_incident_durable(
        "OP-2537", FailureClass.TEST_FAILURE, claim_token="tok-env-file"
    )

    rows = _db_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == record.incident_id
    assert not (tmp_path / "incident_write_failures.count").exists()


def test_engine_is_cached_per_dsn_and_reset_hook_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn, _ = _make_incidents_db(tmp_path)

    first = incident_recorder._get_engine(dsn)
    second = incident_recorder._get_engine(dsn)
    assert first is second

    incident_recorder.reset_for_tests()
    assert incident_recorder._get_engine(dsn) is not first
