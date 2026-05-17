"""OP-1320 -- deploy_audit edge-case regression coverage."""
from __future__ import annotations

import csv
import importlib.util
import io
import sys
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
    m0204 = _load_module(MIGRATION_0204, "_alembic_test_0204_for_op1320")
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


def test_export_csv_round_trips_quoted_operator_text(audit_engine) -> None:
    reason = 'hotfix, rollback requested\noperator said "ship it back"'
    deploy_audit.record(
        kind="rollback",
        status="succeeded",
        tag="v1.2.3",
        actor="op@example.test",
        reason=reason,
        context={"ticket": "OP-1320", "note": "comma, quote \"ok\""},
    )

    csv_text = deploy_audit.export_csv()
    rows = list(csv.DictReader(io.StringIO(csv_text)))

    assert len(rows) == 1
    assert rows[0]["reason"] == reason
    assert rows[0]["context"] == (
        '{"note":"comma, quote \\"ok\\"","ticket":"OP-1320"}'
    )


def test_verify_chain_detects_prev_hash_only_tampering(audit_engine) -> None:
    deploy_audit.record(
        kind="deploy",
        status="started",
        tag="v1",
        actor="op",
        reason="release",
    )
    deploy_audit.record(
        kind="deploy",
        status="succeeded",
        tag="v1",
        actor="op",
        reason="release",
    )
    assert deploy_audit.verify_chain() == {"ok": True, "rows": 2}

    with audit_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE deploy_audit SET prev_hash = :h WHERE id = 2"),
            {"h": "f" * 64},
        )

    result = deploy_audit.verify_chain()
    assert result["ok"] is False
    assert result["rows"] == 2
    assert result["first_bad_id"] == 2
