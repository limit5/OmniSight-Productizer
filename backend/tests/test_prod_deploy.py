"""OP-881 D9 -- prod deploy orchestrator + ``POST /api/v1/prod/deploy``.

Six DoD cases covering the AC matrix:

* T1 approval happy path -- end-to-end success, SSE start/complete,
  prod_deploy_audit row status='completed'.
* T2 approval refused -- bad webhook signature OR bad Slack token,
  HTTP 403, audit row 'aborted_approval_refused'.
* T3 idempotent re-run -- second POST with same release_id returns
  ``idempotent_replay=true`` and emits no further SSE events.
* T4 smoke pre-check failure -- staging-mirror smoke returns
  failures, HTTP 412, audit row 'aborted_smoke_failed', blue-green
  switch NEVER invoked.
* T5 timeout rollback -- elapsed exceeds deploy_timeout_seconds,
  HTTP 504, audit row 'aborted_timeout'.
* T6 audit row written -- explicit assertion that
  ``deploy_audit.record`` (the hash-chained compliance log) is
  called on start AND end of every attempt.

These exercise the orchestrator + router behind a single in-memory
SQLite engine and a StaticPool so request handlers see the migrated
schema across threads.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from backend import auth, deploy_audit, events
from backend.orchestrator import prod_deploy


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0204 = BACKEND_ROOT / "alembic" / "versions" / "0204_deploy_audit.py"
MIGRATION_0224 = BACKEND_ROOT / "alembic" / "versions" / "0224_prod_deploy_audit.py"

WEBHOOK_SECRET = "shh-it-is-a-secret"
SLACK_TOKEN = "slack-confirm-abc"


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


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()


# ─── shared fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def audit_engine(monkeypatch):
    """In-memory SQLite engine with both migrations applied. Injected
    into ``deploy_audit`` *and* ``prod_deploy`` for the duration of the
    test."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    m0204 = _load(MIGRATION_0204, "_alembic_test_0204_prod_deploy")
    m0224 = _load(MIGRATION_0224, "_alembic_test_0224_prod_deploy")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0204.upgrade()
            m0224.upgrade()
        conn.commit()

    deploy_audit.set_engine_for_tests(engine)
    prod_deploy.set_engine_for_tests(engine)
    monkeypatch.setenv("OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("OMNISIGHT_PROD_DEPLOY_SLACK_TOKENS", SLACK_TOKEN)
    try:
        yield engine
    finally:
        deploy_audit.set_engine_for_tests(None)
        prod_deploy.set_engine_for_tests(None)
        prod_deploy.set_orchestrator_factory(None)
        engine.dispose()


class _StepRecorder:
    def __init__(self):
        self.calls: list[str] = []
        self.smoke_ok = True
        self.smoke_failures: tuple[str, ...] = ()
        self.image_pull_raise: Exception | None = None
        self.blue_green_raise: Exception | None = None

    def image_pull(self, image_tag: str) -> None:
        self.calls.append(f"image_pull:{image_tag}")
        if self.image_pull_raise is not None:
            raise self.image_pull_raise

    def secrets_decrypt(self, release_id: str) -> dict[str, str]:
        self.calls.append(f"secrets_decrypt:{release_id}")
        return {"db_pass": "•" * 8}

    def smoke_check(self, image_tag: str) -> tuple[bool, tuple[str, ...]]:
        self.calls.append(f"smoke_check:{image_tag}")
        return self.smoke_ok, self.smoke_failures

    def blue_green_switch(
        self, image_tag: str, secrets: dict[str, str]
    ) -> None:
        self.calls.append(f"blue_green_switch:{image_tag}")
        if self.blue_green_raise is not None:
            raise self.blue_green_raise

    def as_steps(self) -> prod_deploy.DeploySteps:
        return prod_deploy.DeploySteps(
            image_pull=self.image_pull,
            secrets_decrypt=self.secrets_decrypt,
            smoke_check=self.smoke_check,
            blue_green_switch=self.blue_green_switch,
        )


class _SSESpy:
    """Listen on the global event bus and capture prod.deploy.* frames."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        self._orig = events.bus.publish

        def _patched(event_name, data, *args, **kwargs):
            if event_name.startswith("prod.deploy."):
                with self._lock:
                    self.events.append((event_name, dict(data)))
            return self._orig(event_name, data, *args, **kwargs)

        events.bus.publish = _patched  # type: ignore[assignment]

    def restore(self) -> None:
        events.bus.publish = self._orig  # type: ignore[assignment]


@pytest.fixture()
def sse_spy():
    spy = _SSESpy()
    try:
        yield spy
    finally:
        spy.restore()


@pytest.fixture()
def app(audit_engine, sse_spy):
    """A FastAPI app with the prod-deploy router mounted and require_admin
    short-circuited to a fixed admin user. Tests can pin the orchestrator
    by calling ``prod_deploy.set_orchestrator_factory(...)``."""
    app = FastAPI()
    app.include_router(prod_deploy.router)
    app.dependency_overrides[auth.require_admin] = _admin_user
    return app


def _body(**overrides) -> dict:
    body = {
        "release_id": "rel-2026.05.11",
        "image_tag": "v2026.05.11",
        "reason": "DoD sandbox-mirror run",
        "approval_token": SLACK_TOKEN,
    }
    body.update(overrides)
    return body


def _post(client: TestClient, body: dict, *, signature: str | None = None):
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    sig = signature if signature is not None else _sign(raw)
    return client.post(
        "/prod/deploy",
        content=raw,
        headers={
            "X-Prod-Deploy-Signature": sig,
            "content-type": "application/json",
        },
    )


# ─── T1: approval happy path ──────────────────────────────────────────


def test_t1_happy_path_completes_and_records(app, audit_engine, sse_spy):
    rec = _StepRecorder()
    prod_deploy.set_orchestrator_factory(
        lambda: prod_deploy.ProdDeployOrchestrator(steps=rec.as_steps())
    )
    with TestClient(app) as client:
        resp = _post(client, _body())
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "completed"
    assert payload["idempotent_replay"] is False

    # all four steps fired, in order
    assert rec.calls == [
        "image_pull:v2026.05.11",
        "secrets_decrypt:rel-2026.05.11",
        "smoke_check:v2026.05.11",
        "blue_green_switch:v2026.05.11",
    ]
    # SSE: started + completed (no aborted)
    names = [e[0] for e in sse_spy.events]
    assert names == ["prod.deploy.started", "prod.deploy.completed"]
    # AC #5: audit row final state
    row = prod_deploy._lookup_audit("rel-2026.05.11")
    assert row is not None and row["status"] == "completed"
    assert row["last_step"] == "completed"


# ─── T2: approval refused (both signals) ──────────────────────────────


def test_t2_approval_refused_bad_signature(app, audit_engine, sse_spy):
    rec = _StepRecorder()
    prod_deploy.set_orchestrator_factory(
        lambda: prod_deploy.ProdDeployOrchestrator(steps=rec.as_steps())
    )
    with TestClient(app) as client:
        resp = _post(client, _body(release_id="rel-bad-sig"),
                     signature="sha256=deadbeef")
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error_class"] == "OperatorApprovalRefused"
    assert detail["abort_status"] == "aborted_approval_refused"
    # no deploy step ran
    assert rec.calls == []
    row = prod_deploy._lookup_audit("rel-bad-sig")
    assert row["status"] == "aborted_approval_refused"


def test_t2_approval_refused_bad_slack_token(app, audit_engine, sse_spy):
    rec = _StepRecorder()
    prod_deploy.set_orchestrator_factory(
        lambda: prod_deploy.ProdDeployOrchestrator(steps=rec.as_steps())
    )
    with TestClient(app) as client:
        resp = _post(client, _body(
            release_id="rel-bad-slack", approval_token="not-issued"))
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_class"] == "OperatorApprovalRefused"
    assert rec.calls == []
    row = prod_deploy._lookup_audit("rel-bad-slack")
    assert row["status"] == "aborted_approval_refused"
    # aborted SSE emitted with the abort_status field
    aborted = [e for e in sse_spy.events if e[0] == "prod.deploy.aborted"]
    assert any(
        e[1]["status"] == "aborted_approval_refused"
        and e[1]["release_id"] == "rel-bad-slack"
        for e in aborted
    )


# ─── T3: idempotent re-run ────────────────────────────────────────────


def test_t3_idempotent_replay_no_side_effects(app, audit_engine, sse_spy):
    rec = _StepRecorder()
    prod_deploy.set_orchestrator_factory(
        lambda: prod_deploy.ProdDeployOrchestrator(steps=rec.as_steps())
    )
    with TestClient(app) as client:
        first = _post(client, _body(release_id="rel-idem"))
        assert first.status_code == 200
        # second POST with same release_id
        sse_spy.events.clear()
        rec.calls.clear()
        second = _post(client, _body(release_id="rel-idem"))
    assert second.status_code == 200
    body = second.json()
    assert body["idempotent_replay"] is True
    assert body["status"] == "completed"
    # critical: NO step re-ran, NO SSE re-emitted
    assert rec.calls == []
    assert sse_spy.events == []


# ─── T4: smoke pre-check failure ──────────────────────────────────────


def test_t4_smoke_failure_aborts_before_blue_green(app, audit_engine, sse_spy):
    rec = _StepRecorder()
    rec.smoke_ok = False
    rec.smoke_failures = ("auth_smoke", "checkout_smoke")
    prod_deploy.set_orchestrator_factory(
        lambda: prod_deploy.ProdDeployOrchestrator(steps=rec.as_steps())
    )
    with TestClient(app) as client:
        resp = _post(client, _body(release_id="rel-smoke-fail"))
    assert resp.status_code == 412
    detail = resp.json()["detail"]
    assert detail["error_class"] == "PreCheckSmokeFailed"
    assert detail["abort_status"] == "aborted_smoke_failed"
    # critical: image_pull + secrets_decrypt + smoke ran, blue_green did NOT
    assert "blue_green_switch:v2026.05.11" not in rec.calls
    assert rec.calls[-1].startswith("smoke_check")
    row = prod_deploy._lookup_audit("rel-smoke-fail")
    assert row["status"] == "aborted_smoke_failed"
    assert row["last_step"] == "smoke_pre_check"
    assert row["error_class"] == "PreCheckSmokeFailed"
    # SSE aborted carries the error class
    aborted = [e for e in sse_spy.events if e[0] == "prod.deploy.aborted"][-1]
    assert aborted[1]["error_class"] == "PreCheckSmokeFailed"


# ─── T5: timeout rollback ─────────────────────────────────────────────


def test_t5_timeout_exceeded_aborts(app, audit_engine, sse_spy):
    rec = _StepRecorder()
    # Clock calls inside execute(): start, image_pull guard,
    # secrets_decrypt guard, smoke guard (← we trip here), then _abort
    # measures elapsed. Tick 4 jumps past the deadline so smoke's
    # guard raises BEFORE smoke_check runs and blue-green is never
    # reached.
    ticks = iter([0.0, 0.0, 0.0, 9999.0, 9999.0])

    def _clock():
        try:
            return next(ticks)
        except StopIteration:
            return 9999.0

    prod_deploy.set_orchestrator_factory(
        lambda: prod_deploy.ProdDeployOrchestrator(
            steps=rec.as_steps(),
            deploy_timeout_seconds=60.0,
            clock=_clock,
        )
    )
    with TestClient(app) as client:
        resp = _post(client, _body(release_id="rel-timeout"))
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert detail["error_class"] == "DeployTimeoutExceeded"
    assert detail["abort_status"] == "aborted_timeout"
    row = prod_deploy._lookup_audit("rel-timeout")
    assert row["status"] == "aborted_timeout"
    # blue-green must NOT have fired
    assert "blue_green_switch:v2026.05.11" not in rec.calls


# ─── T6: audit row (chain + ledger) ───────────────────────────────────


def test_t6_audit_row_written_on_start_and_end(app, audit_engine):
    """Both the prod_deploy_audit ledger AND the hash-chained
    deploy_audit (0204) log get rows for every attempt."""
    rec = _StepRecorder()
    prod_deploy.set_orchestrator_factory(
        lambda: prod_deploy.ProdDeployOrchestrator(steps=rec.as_steps())
    )
    with TestClient(app) as client:
        ok = _post(client, _body(release_id="rel-audit"))
        assert ok.status_code == 200

    # ledger (orchestrator-local)
    row = prod_deploy._lookup_audit("rel-audit")
    assert row is not None
    assert row["status"] == "completed"
    assert row["elapsed_seconds"] is not None and row["elapsed_seconds"] >= 0

    # change-management chain (0204) -- one 'started' + one 'succeeded'
    chain_rows = deploy_audit.query(kind=deploy_audit.KIND_DEPLOY)
    statuses = [r["status"] for r in chain_rows]
    assert statuses.count(deploy_audit.STATUS_STARTED) == 1
    assert statuses.count(deploy_audit.STATUS_SUCCEEDED) == 1
    # both rows carry the release_id in context
    for r in chain_rows:
        assert "rel-audit" in (r["context"] or "")
