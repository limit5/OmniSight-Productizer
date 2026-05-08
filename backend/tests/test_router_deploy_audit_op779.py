"""OP-779 D18 -- /admin/deploy-audit router contract."""
from __future__ import annotations

import csv
import importlib.util
import io
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from backend import auth, deploy_audit
from backend.routers import deploy_audit as deploy_audit_router


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0204 = BACKEND_ROOT / "alembic" / "versions" / "0204_deploy_audit.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _admin_user() -> auth.User:
    return auth.User(
        id="op-1", email="op@example.test", name="Op", role="admin",
    )


@pytest.fixture()
def app(audit_engine):
    app = FastAPI()
    app.include_router(deploy_audit_router.router)
    app.dependency_overrides[auth.require_admin] = _admin_user
    return app


@pytest.fixture()
def audit_engine():
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    # FastAPI's TestClient runs requests in a worker thread; an in-memory
    # SQLite engine with the default SingletonThreadPool would not share
    # the schema across threads. ``StaticPool`` keeps a single shared
    # connection so the migration's tables remain visible to the request
    # handlers.
    engine = sa.create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    m0204 = _load(MIGRATION_0204, "_alembic_test_0204_router")
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


def _seed():
    deploy_audit.record(kind="deploy", status="started",
                        tag="v1", actor="op@example.test", reason="r1")
    deploy_audit.record(kind="deploy", status="succeeded",
                        tag="v1", actor="op@example.test", reason="r1",
                        elapsed_seconds=12.0)
    deploy_audit.record(kind="rollback", status="succeeded",
                        tag="v1", actor="op@example.test", reason="bad release")
    deploy_audit.record(kind="slo_breach", status="started",
                        context={"route": "/api/v1/foo"})
    deploy_audit.record(kind="operator_action", status="succeeded",
                        tag="v1", actor="op@example.test",
                        reason="DR drill")


def test_list_returns_all_rows(app: FastAPI, audit_engine) -> None:
    _seed()
    client = TestClient(app)
    response = client.get("/admin/deploy-audit")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 5
    kinds = [row["kind"] for row in body["rows"]]
    assert kinds == ["deploy", "deploy", "rollback", "slo_breach", "operator_action"]


def test_list_filters_by_kind(app: FastAPI, audit_engine) -> None:
    _seed()
    client = TestClient(app)
    response = client.get("/admin/deploy-audit?kind=rollback")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["rows"][0]["kind"] == "rollback"


def test_list_rejects_unknown_kind(app: FastAPI, audit_engine) -> None:
    client = TestClient(app)
    response = client.get("/admin/deploy-audit?kind=bogus")
    assert response.status_code == 400


def test_csv_export_attachment(app: FastAPI, audit_engine) -> None:
    _seed()
    client = TestClient(app)
    response = client.get("/admin/deploy-audit.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.text)))
    # header + 5 rows
    assert rows[0] == list(deploy_audit.CSV_COLUMNS)
    assert len(rows) == 6


def test_verify_endpoint_ok(app: FastAPI, audit_engine) -> None:
    _seed()
    client = TestClient(app)
    response = client.get("/admin/deploy-audit/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["rows"] == 5


def test_verify_endpoint_flags_tampering(app: FastAPI, audit_engine) -> None:
    _seed()
    with audit_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE deploy_audit SET reason = 'tampered' WHERE id = 1")
        )
    client = TestClient(app)
    response = client.get("/admin/deploy-audit/verify")
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["first_bad_id"] == 1
