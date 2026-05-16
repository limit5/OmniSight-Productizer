"""OP-854 (C2) — Failure-class-indexed memory + replay deprecation tests.

Eleven happy-path classification cases (one per enum member listed in
the AC) plus the alembic apply/rollback, recall filter, tier gate, and
deprecation-warning checks called out in the AC test plan.

The taxonomy module is pure-Python with no DB dependency; the alembic
cases run against an in-process SQLite engine so the suite stays
self-contained.
"""

from __future__ import annotations

import importlib
import io
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from backend.agents import incident_recorder
from backend.agents.failure_class import (
    RUNNER_FAILURE_MEMBERS,
    FailureClass,
    FailureClassRecallEmpty,
    FailureClassUnregistered,
    classify_from_traceback,
)
from backend.agents.incident_recorder import (
    RunnerIncidentRecord,
    get_runner_incidents,
    recall_similar_incidents,
    record_runner_incident,
)
from backend.agents.memory_tool_handler import (
    TIER_L_OPTIN_ENV,
    TierViolationUnauthorizedRecall,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
ALEMBIC_VERSIONS = REPO_ROOT / "backend" / "alembic" / "versions"


@pytest.fixture(autouse=True)
def _reset_buffers():
    incident_recorder.reset_for_tests()
    yield
    incident_recorder.reset_for_tests()


# ─── Cases 1-10: one per runner-failure enum member happy-path ────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("checkpatch.pl --strict failed: WARNING:LINE_SPACING", FailureClass.LINT_FAILURE),
        ("pytest test_foo.py::test_bar FAILED", FailureClass.TEST_FAILURE),
        ("CONFLICT (content): Merge conflict in src/foo.c", FailureClass.MERGE_CONFLICT),
        ("error: Your local changes ... untracked working tree files", FailureClass.WORKTREE_DIRTY),
        ("guard tripped: reflection loop after 3 identical errors", FailureClass.LLM_LOOP_DETECTED),
        ("scope-to-paths: unknown area label 'fizz-buzz'", FailureClass.UNKNOWN_AREA_LABEL),
        ("bridge desync: gerrit ack timestamp 12s ahead of jira", FailureClass.BRIDGE_DESYNC),
        ("runner timeout: wall-clock budget exceeded after 2400s", FailureClass.RUNNER_TIMEOUT),
        ("file coordinator could not acquire mutex within 30s", FailureClass.MUTEX_CONTENTION),
        ("outcomes-grader refused: insufficient signal in run", FailureClass.OUTCOMES_GRADER_REFUSED),
    ],
    ids=[m.value for m in RUNNER_FAILURE_MEMBERS],
)
def test_classify_recognises_each_runner_failure_member(raw, expected):
    assert classify_from_traceback(raw) is expected


# ─── Case 11: OTHER catch-all ─────────────────────────────────────


def test_classify_falls_back_to_other_for_unknown_traceback():
    assert classify_from_traceback("an entirely unrelated message") is FailureClass.OTHER
    assert classify_from_traceback("") is FailureClass.OTHER
    assert classify_from_traceback("   \n  ") is FailureClass.OTHER


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        42,
        object(),
        "x" * 1_000_000,
    ],
    ids=[
        "none",
        "empty-list",
        "empty-dict",
        "int",
        "object",
        "very-large-unmatched",
    ],
)
def test_classify_input_validation_routes_malformed_values_to_other(raw):
    assert classify_from_traceback(raw) is FailureClass.OTHER


def test_classify_input_validation_handles_very_large_matching_traceback():
    raw = ("noise " * 100_000) + " pytest backend/tests/test_foo.py FAILED"

    assert classify_from_traceback(raw) is FailureClass.TEST_FAILURE


def test_failure_class_enum_has_eleven_runner_members_plus_audit():
    """AC #1 — 10 + OTHER catch-all = 11 runner failure-class values."""
    # 10 specific members + OTHER == 11; MEMORY_RECALL_AUDIT is a C6
    # audit slot distinct from runner failures (kept for cross-table
    # join behaviour, asserted separately).
    runner_slots = {m for m in FailureClass if m is not FailureClass.MEMORY_RECALL_AUDIT}
    assert len(runner_slots) == 11
    expected = {
        "LINT_FAILURE", "TEST_FAILURE", "MERGE_CONFLICT", "WORKTREE_DIRTY",
        "LLM_LOOP_DETECTED", "UNKNOWN_AREA_LABEL", "BRIDGE_DESYNC",
        "RUNNER_TIMEOUT", "MUTEX_CONTENTION", "OUTCOMES_GRADER_REFUSED",
        "OTHER",
    }
    assert {m.value for m in runner_slots} == expected


# ─── Coercion — unknown strings collapse to OTHER ─────────────────


def test_coerce_unknown_string_routes_to_other():
    assert FailureClass.coerce("not_a_real_class") is FailureClass.OTHER
    assert FailureClass.coerce(None) is FailureClass.OTHER
    assert FailureClass.coerce("LINT_FAILURE") is FailureClass.LINT_FAILURE
    # idempotent: enum in → enum out
    assert FailureClass.coerce(FailureClass.TEST_FAILURE) is FailureClass.TEST_FAILURE


@pytest.mark.parametrize(
    "raw",
    [
        "",
        [],
        {},
        42,
        object(),
        "x" * 1_000_000,
    ],
    ids=[
        "empty-string",
        "empty-list",
        "empty-dict",
        "int",
        "object",
        "very-large-unregistered",
    ],
)
def test_coerce_input_validation_routes_malformed_values_to_other(raw):
    assert FailureClass.coerce(raw) is FailureClass.OTHER


def test_strict_record_runner_incident_raises_for_unregistered():
    with pytest.raises(FailureClassUnregistered):
        record_runner_incident(
            ticket_key="OP-1",
            failure_class="garbage",
            summary="x",
            strict=True,
        )


# ─── AC #2: write tags failure_class field at incident time ───────


def test_record_runner_incident_tags_failure_class():
    record = record_runner_incident(
        ticket_key="OP-100",
        failure_class=FailureClass.LINT_FAILURE,
        summary="ruff red",
        raw_traceback="ruff check failed: E501",
        runner_class="subscription-claude",
        mutex_label="backend/agents/foo.py",
        area="backend",
    )
    assert isinstance(record, RunnerIncidentRecord)
    assert record.failure_class is FailureClass.LINT_FAILURE
    assert record.ticket_key == "OP-100"
    assert record.area == "backend"
    assert record.mutex_label == "backend/agents/foo.py"
    rows = get_runner_incidents(failure_class=FailureClass.LINT_FAILURE)
    assert len(rows) == 1
    assert rows[0].incident_id == record.incident_id


def test_record_runner_incident_infers_class_from_traceback():
    record = record_runner_incident(
        ticket_key="OP-101",
        failure_class=None,
        summary="auto",
        raw_traceback="checkpatch.pl --strict failed",
        area="backend",
    )
    assert record.failure_class is FailureClass.LINT_FAILURE


# ─── AC #3: recall filter by (area, failure_class), top-3 ─────────


def _seed_priors(*, count: int = 4) -> list[RunnerIncidentRecord]:
    seeded: list[RunnerIncidentRecord] = []
    for i in range(count):
        seeded.append(
            record_runner_incident(
                ticket_key=f"OP-{200 + i}",
                failure_class=FailureClass.TEST_FAILURE,
                summary=f"prior #{i}",
                raw_traceback=f"pytest #{i} FAILED",
                area="backend",
            )
        )
    return seeded


def test_recall_returns_top_3_priors_newest_first():
    seeded = _seed_priors(count=4)

    out = recall_similar_incidents(
        ticket_key="OP-999",
        area="backend",
        failure_class=FailureClass.TEST_FAILURE,
        tier="S",
    )

    assert len(out) == 3
    # Newest first → reverse order of seed insertion.
    assert [r.ticket_key for r in out] == [seeded[3].ticket_key, seeded[2].ticket_key, seeded[1].ticket_key]


def test_recall_filters_out_other_areas():
    record_runner_incident(
        ticket_key="OP-300",
        failure_class=FailureClass.TEST_FAILURE,
        summary="frontend prior",
        area="frontend",
    )
    record_runner_incident(
        ticket_key="OP-301",
        failure_class=FailureClass.TEST_FAILURE,
        summary="backend prior",
        area="backend",
    )

    out = recall_similar_incidents(
        ticket_key="OP-302",
        area="backend",
        failure_class=FailureClass.TEST_FAILURE,
        tier="S",
    )

    assert len(out) == 1
    assert out[0].ticket_key == "OP-301"


def test_recall_empty_when_no_priors_match():
    record_runner_incident(
        ticket_key="OP-400",
        failure_class=FailureClass.LINT_FAILURE,
        summary="wrong class",
        area="backend",
    )

    with pytest.raises(FailureClassRecallEmpty):
        recall_similar_incidents(
            ticket_key="OP-401",
            area="backend",
            failure_class=FailureClass.MERGE_CONFLICT,
            tier="S",
        )


# ─── AC #5: tier-gated recall (X refused, L opt-in, S/M auto) ────


def test_recall_tier_x_is_refused_and_escalates():
    _seed_priors(count=1)

    with pytest.raises(TierViolationUnauthorizedRecall) as excinfo:
        recall_similar_incidents(
            ticket_key="OP-999",
            area="backend",
            failure_class=FailureClass.TEST_FAILURE,
            tier="X",
            env={},
        )

    assert excinfo.value.escalate is True


def test_recall_tier_l_requires_optin():
    _seed_priors(count=1)

    with pytest.raises(TierViolationUnauthorizedRecall):
        recall_similar_incidents(
            ticket_key="OP-999",
            area="backend",
            failure_class=FailureClass.TEST_FAILURE,
            tier="L",
            env={},  # opt-in absent
        )

    # Opt-in flips to permit.
    out = recall_similar_incidents(
        ticket_key="OP-999",
        area="backend",
        failure_class=FailureClass.TEST_FAILURE,
        tier="L",
        env={TIER_L_OPTIN_ENV: "1"},
    )
    assert len(out) == 1


def test_recall_tier_s_and_m_pass_without_optin():
    _seed_priors(count=1)

    for tier in ("S", "M"):
        out = recall_similar_incidents(
            ticket_key=f"OP-{tier}",
            area="backend",
            failure_class=FailureClass.TEST_FAILURE,
            tier=tier,
            env={},
        )
        assert len(out) == 1


# ─── AC #2: alembic apply + rollback for migration 0206 ───────────


@pytest.fixture()
def _sqlite_engine(tmp_path):
    db_path = tmp_path / "op854.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


def _load_migration_module():
    """Import the migration file once, by path."""
    mod_path = ALEMBIC_VERSIONS / "0206_runner_incidents.py"
    spec = importlib.util.spec_from_file_location("_op854_mig_0206", mod_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_migration_0206(engine, *, direction: str):
    """Run the migration's upgrade/downgrade against ``engine``.

    The migration uses ``alembic.op.get_bind()`` to obtain its
    connection. Rather than spin a full alembic MigrationContext (which
    would also chain 0001..0205, not under test here), we patch
    ``op.get_bind`` to hand the migration our scoped connection. The
    DDL paths in 0206 are dialect-aware and the same ``exec_driver_sql``
    surface is exercised either way.
    """
    module = _load_migration_module()
    from alembic import op as alembic_op

    with engine.begin() as connection:
        with patch.object(alembic_op, "get_bind", return_value=connection):
            if direction == "up":
                module.upgrade()
            else:
                module.downgrade()


def test_alembic_0206_upgrade_creates_table_and_indexes(_sqlite_engine):
    _run_migration_0206(_sqlite_engine, direction="up")

    with _sqlite_engine.connect() as conn:
        # Insert a row to verify the schema accepts the column set.
        conn.exec_driver_sql(
            "INSERT INTO runner_incidents "
            "(incident_id, ticket_key, failure_class, summary, raw_traceback, "
            " runner_class, mutex_label, area) "
            "VALUES (:id,:k,:fc,:s,:tb,:rc,:m,:a)",
            {
                "id": "i1",
                "k": "OP-1",
                "fc": "LINT_FAILURE",
                "s": "lint red",
                "tb": "",
                "rc": "subscription-claude",
                "m": None,
                "a": "backend",
            },
        )
        rows = list(conn.exec_driver_sql("SELECT failure_class FROM runner_incidents"))
        assert rows == [("LINT_FAILURE",)]

        # CHECK constraint refuses values outside the enum.
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO runner_incidents "
                "(incident_id, ticket_key, failure_class) "
                "VALUES ('i2','OP-2','TOTALLY_INVALID')"
            )

        # Indexes exist (sqlite_master is the canonical lookup).
        idx_names = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='runner_incidents'"
            )
        }
        assert "idx_runner_incidents_ticket_key" in idx_names
        assert "idx_runner_incidents_class_area" in idx_names
        assert "idx_runner_incidents_mutex" in idx_names


def test_alembic_0206_downgrade_drops_table(_sqlite_engine):
    _run_migration_0206(_sqlite_engine, direction="up")
    _run_migration_0206(_sqlite_engine, direction="down")

    with _sqlite_engine.connect() as conn:
        rows = list(
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='runner_incidents'"
            )
        )
        assert rows == []


# ─── AC #4: replay deprecation warning ────────────────────────────


def test_replay_script_emits_deprecation_warning():
    spec = importlib.util.spec_from_file_location(
        "_op854_replay_shim",
        REPO_ROOT / "scripts" / "replay.py",
    )
    assert spec and spec.loader
    replay = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = replay
    spec.loader.exec_module(replay)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        replay.emit_deprecation_warning()

    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    assert any("OP-854" in str(w.message) for w in caught)


def test_replay_main_exits_nonzero():
    spec = importlib.util.spec_from_file_location(
        "_op854_replay_shim_main",
        REPO_ROOT / "scripts" / "replay.py",
    )
    assert spec and spec.loader
    replay = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = replay
    spec.loader.exec_module(replay)

    captured = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = captured
    try:
        rc = replay.main([])
    finally:
        sys.stderr = real_stderr

    assert rc == 2, "deprecated entry must fail loudly to surface in cron logs"
    assert "replay-deprecation" in captured.getvalue()


# ─── Migration enum and Python enum stay in sync ──────────────────


def test_migration_enum_matches_python_enum():
    """Co-update guard: the CHECK literal must list every Python member."""
    mod_path = ALEMBIC_VERSIONS / "0206_runner_incidents.py"
    text_body = mod_path.read_text()

    for member in FailureClass:
        assert f"'{member.value}'" in text_body, (
            f"migration 0206 missing failure-class literal {member.value!r}; "
            "add to _FAILURE_CLASS_LITERAL"
        )
