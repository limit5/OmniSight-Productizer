"""OP-770 production release approval gate tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import production_release as pr
from backend.routers import release_approval


class RecordingRunner:
    def __init__(self, *, fail_on: str = "") -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, args: list[str], *, timeout: float, env: dict | None = None) -> None:
        self.calls.append(args)
        if self.fail_on and self.fail_on in " ".join(args):
            raise RuntimeError(f"forced failure: {self.fail_on}")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        current = self.now
        self.now += 42.0
        return current


def test_release_tagged_creates_pending_approval_and_notification_link(tmp_path: Path) -> None:
    notifications: list[dict] = []

    record = pr.create_approval_from_release_tagged(
        {
            "event": "release_tagged",
            "tag": "v9.99.0",
            "main_sha": "abc123",
            "release_branch": "release/v9.99",
            "smoke_results": {"status": "pass"},
            "baseline_diff": {"p95_ms": "-12"},
            "change_list": ["OP-762 milestone gate", "OP-769 release tag"],
        },
        store_dir=tmp_path,
        notify_fn=lambda *args, **kwargs: notifications.append(
            {"args": args, "kwargs": kwargs}
        ),
    )

    loaded = pr.load_approval("v9.99.0", tmp_path)
    assert record == loaded
    assert loaded.status == "pending"
    assert loaded.approval_url.endswith("/admin/release-approval?tag=v9.99.0")
    assert notifications
    assert notifications[0]["args"][1] == "release.ship_approval_required"
    assert "/admin/release-approval?tag=v9.99.0" in notifications[0]["args"][2]


def test_run_once_consumes_release_tagged_log_and_advances_cursor(tmp_path: Path) -> None:
    event_log = tmp_path / "staging.log"
    cursor = tmp_path / "cursor"
    store = tmp_path / "approvals"
    notifications: list[dict] = []
    event_log.write_text(
        "ignored " + json.dumps({"event": "staging_passed", "tag": "v0.0.1"}) + "\n"
        "ready " + json.dumps({"event": "release_tagged", "tag": "v9.99.0"}) + "\n",
        encoding="utf-8",
    )

    records = pr.run_once(
        event_log=event_log,
        cursor=cursor,
        store_dir=store,
        notify_fn=lambda *args, **kwargs: notifications.append(
            {"args": args, "kwargs": kwargs}
        ),
    )

    assert [record.tag for record in records] == ["v9.99.0"]
    assert pr.load_approval("v9.99.0", store).status == "pending"
    assert notifications[0]["args"][1] == "release.ship_approval_required"
    assert int(cursor.read_text(encoding="utf-8")) == event_log.stat().st_size


def test_approval_ui_displays_release_info(monkeypatch: pytest.MonkeyPatch) -> None:
    record = pr.ReleaseApproval(
        tag="v9.99.0",
        smoke_results={"status": "pass"},
        baseline_diff={"error_rate": "0.00%"},
        change_list=["OP-770 approval gate"],
        status="pending",
    )
    monkeypatch.setattr(release_approval, "load_approval", lambda tag: record)

    app = FastAPI()
    app.include_router(release_approval.router)
    response = TestClient(app).get("/admin/release-approval?tag=v9.99.0")

    assert response.status_code == 200
    assert "Production Release v9.99.0" in response.text
    assert "&quot;status&quot;: &quot;pass&quot;" in response.text
    assert "&quot;error_rate&quot;: &quot;0.00%&quot;" in response.text
    assert "OP-770 approval gate" in response.text
    assert "SHIP IT" in response.text


def test_ship_blue_green_sequence_completes_under_ten_minutes() -> None:
    runner = RecordingRunner()
    orch = pr.ProductionDeployOrchestrator(runner=runner, clock=FakeClock())

    elapsed = orch.ship("v9.99.0")

    assert elapsed < 600
    assert runner.calls == [
        ["docker", "pull", "ghcr.io/omnisight/productizer:v9.99.0"],
        [
            "docker", "compose", "-f", "docker-compose.prod.yml", "run", "--rm",
            "--no-deps", "-e", "PYTHONSAFEPATH=1", "-w", "/app/backend",
            "backend-a", "python", "-m", "alembic", "upgrade", "head",
        ],
        ["docker", "compose", "-f", "docker-compose.prod.yml", "up", "-d", "--no-deps", "backend-a"],
        ["curl", "-sf", "http://localhost:8000/readyz"],
        ["docker", "compose", "-f", "docker-compose.prod.yml", "up", "-d", "--no-deps", "backend-b"],
        ["curl", "-sf", "http://localhost:8001/readyz"],
        ["docker", "compose", "-f", "docker-compose.prod.yml", "up", "-d", "--no-deps", "frontend"],
    ]


def test_ship_failure_triggers_rollback() -> None:
    runner = RecordingRunner(fail_on="backend-b")
    orch = pr.ProductionDeployOrchestrator(runner=runner, clock=FakeClock())

    with pytest.raises(RuntimeError, match="backend-b"):
        orch.ship("v9.99.0")

    assert runner.calls[-1] == ["scripts/deploy.sh", "--rollback"]


@pytest.mark.asyncio
async def test_approval_writes_audit_and_marks_shipped(tmp_path: Path) -> None:
    pr.save_approval(pr.ReleaseApproval(tag="v9.99.0"), tmp_path)
    audit_calls: list[dict] = []

    async def fake_audit(**kwargs):
        audit_calls.append(kwargs)
        return 123

    record = await pr.approve_and_ship(
        "v9.99.0",
        actor="operator@example.test",
        store_dir=tmp_path,
        orchestrator=pr.ProductionDeployOrchestrator(
            runner=RecordingRunner(),
            clock=FakeClock(),
        ),
        audit_log=fake_audit,
    )

    assert record.status == "shipped"
    assert record.approved_by == "operator@example.test"
    assert audit_calls[0]["action"] == "release.ship_approved"
    assert audit_calls[0]["entity_id"] == "v9.99.0"
    assert audit_calls[0]["actor"] == "operator@example.test"


@pytest.mark.asyncio
async def test_cancel_rolls_back_staging_artifacts(tmp_path: Path) -> None:
    pr.save_approval(pr.ReleaseApproval(tag="v9.99.0"), tmp_path)
    runner = RecordingRunner()

    record = await pr.cancel_release(
        "v9.99.0",
        actor="operator@example.test",
        store_dir=tmp_path,
        orchestrator=pr.ProductionDeployOrchestrator(
            runner=runner,
            staging_rollback_command=["rollback-staging-artifacts", "v9.99.0"],
        ),
    )

    assert record.status == "cancelled"
    assert record.cancelled_by == "operator@example.test"
    assert runner.calls == [["rollback-staging-artifacts", "v9.99.0"]]
