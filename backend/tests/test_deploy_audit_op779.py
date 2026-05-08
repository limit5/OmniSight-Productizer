"""OP-779 D18 -- deploy_audit module + change-management contract."""
from __future__ import annotations

import csv
import importlib.util
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from backend import deploy_audit


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0204 = BACKEND_ROOT / "alembic" / "versions" / "0204_deploy_audit.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def audit_engine():
    """Spin up an in-memory sqlite engine with the 0204 schema applied,
    inject it into ``deploy_audit`` for the duration of the test, and
    tear down on exit."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:", future=True)
    m0204 = _load_module(MIGRATION_0204, "_alembic_test_0204_for_module")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0204.upgrade()
        conn.commit()

    deploy_audit.set_engine_for_tests(engine)
    try:
        yield engine
    finally:
        deploy_audit.set_engine_for_tests(None)
        engine.dispose()


def test_record_returns_row_id_and_persists(audit_engine) -> None:
    rid = deploy_audit.record(
        kind="deploy",
        status="started",
        tag="v1.0.0",
        actor="op@example.test",
        reason="normal release",
    )
    assert rid > 0
    rows = deploy_audit.query()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "deploy"
    assert row["actor"] == "op@example.test"
    assert row["reason"] == "normal release"
    assert row["prev_hash"] == deploy_audit.GENESIS_HASH
    assert row["curr_hash"]


def test_operator_kind_requires_reason(audit_engine) -> None:
    with pytest.raises(ValueError, match="reason"):
        deploy_audit.record(
            kind="deploy",
            status="started",
            tag="v1.0.0",
            actor="op@example.test",
            reason="",
        )
    with pytest.raises(ValueError, match="reason"):
        deploy_audit.record(
            kind="rollback",
            status="succeeded",
            tag="v1.0.0",
            actor="op@example.test",
            reason=None,
        )
    with pytest.raises(ValueError, match="reason"):
        deploy_audit.record(
            kind="operator_action",
            status="succeeded",
            actor="op@example.test",
            reason="   ",
        )


def test_slo_breach_does_not_require_reason(audit_engine) -> None:
    rid = deploy_audit.record(
        kind="slo_breach",
        status="started",
        context={"route": "/api/v1/foo", "p95_ms": 612.5},
    )
    assert rid > 0
    row = deploy_audit.query()[0]
    assert row["kind"] == "slo_breach"
    assert row["reason"] is None
    assert "p95_ms" in (row["context"] or "")


def test_invalid_kind_rejected(audit_engine) -> None:
    with pytest.raises(ValueError, match="kind"):
        deploy_audit.record(
            kind="not_a_kind",
            status="started",
            actor="op@example.test",
            reason="x",
        )


def test_invalid_status_rejected(audit_engine) -> None:
    with pytest.raises(ValueError, match="status"):
        deploy_audit.record(
            kind="deploy",
            status="kinda_done",
            actor="op@example.test",
            reason="x",
        )


def test_chain_grows_correctly(audit_engine) -> None:
    deploy_audit.record(
        kind="deploy", status="started",
        tag="v1", actor="op", reason="r1",
    )
    deploy_audit.record(
        kind="deploy", status="succeeded",
        tag="v1", actor="op", reason="r1", elapsed_seconds=42.5,
    )
    deploy_audit.record(
        kind="rollback", status="succeeded",
        tag="v1", actor="op", reason="bad release",
    )
    rows = deploy_audit.query()
    assert len(rows) == 3
    assert rows[0]["prev_hash"] == deploy_audit.GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["curr_hash"]
    assert rows[2]["prev_hash"] == rows[1]["curr_hash"]


def test_verify_chain_detects_tampering(audit_engine) -> None:
    deploy_audit.record(
        kind="deploy", status="started",
        tag="v1", actor="op", reason="r1",
    )
    deploy_audit.record(
        kind="deploy", status="succeeded",
        tag="v1", actor="op", reason="r1",
    )
    assert deploy_audit.verify_chain() == {"ok": True, "rows": 2}

    # Tamper: rewrite the reason on the first row without recomputing
    # the chain. The verifier should catch it.
    with audit_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE deploy_audit SET reason = 'tampered' WHERE id = 1")
        )
    result = deploy_audit.verify_chain()
    assert result["ok"] is False
    assert result["first_bad_id"] == 1


def test_query_filters_by_kind_and_window(audit_engine) -> None:
    deploy_audit.record(kind="deploy", status="started",
                        tag="v1", actor="op", reason="r1")
    deploy_audit.record(kind="rollback", status="succeeded",
                        tag="v1", actor="op", reason="bad")
    deploy_audit.record(kind="slo_breach", status="started",
                        context={"route": "/x"})

    deploys = deploy_audit.query(kind="deploy")
    assert len(deploys) == 1
    assert deploys[0]["kind"] == "deploy"

    rollbacks = deploy_audit.query(kind="rollback")
    assert len(rollbacks) == 1

    # Window: a tight future window should return 0
    far_future = datetime.now(timezone.utc) + timedelta(days=30)
    rows = deploy_audit.query(since=far_future)
    assert rows == []


def test_export_csv_round_trips(audit_engine) -> None:
    deploy_audit.record(kind="deploy", status="started",
                        tag="v1.0.0", actor="op", reason="initial release")
    deploy_audit.record(kind="deploy", status="succeeded",
                        tag="v1.0.0", actor="op", reason="initial release",
                        elapsed_seconds=123.4)
    deploy_audit.record(kind="slo_breach", status="started",
                        context={"route": "/api/v1/foo"})

    csv_text = deploy_audit.export_csv()
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    assert rows[0] == list(deploy_audit.CSV_COLUMNS)
    assert len(rows) == 4  # header + 3 rows
    # columns: id,ts,kind,tag,actor,reason,status,elapsed_seconds,
    #          context,prev_hash,curr_hash
    body = rows[1:]
    kinds = [r[2] for r in body]
    assert kinds == ["deploy", "deploy", "slo_breach"]
    # reason for slo_breach is empty (None -> "")
    assert body[2][5] == ""
    # elapsed_seconds populated for the succeeded deploy
    assert body[1][7] == "123.4"


def test_query_default_window_is_one_year(audit_engine) -> None:
    """Spec: 'queryable for any change in last 1y'."""
    deploy_audit.record(kind="deploy", status="started",
                        tag="v1", actor="op", reason="recent")
    rows = deploy_audit.query()
    assert len(rows) == 1
    # Sanity-check: with an explicit since=2y-ago we still see it
    two_years_ago = datetime.now(timezone.utc) - timedelta(days=730)
    assert len(deploy_audit.query(since=two_years_ago)) == 1


def test_no_update_or_delete_endpoint_in_module() -> None:
    """Spec: audit entries are immutable. The module must NOT expose
    any update or delete primitive."""
    public = {name for name in dir(deploy_audit) if not name.startswith("_")}
    forbidden = {"update", "delete", "edit", "remove", "purge", "truncate"}
    assert public.isdisjoint(forbidden), f"forbidden mutators exposed: {public & forbidden}"
