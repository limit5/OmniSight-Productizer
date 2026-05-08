"""OP-778 deployment dashboard backend contracts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth, deploy_audit
from backend import production_release as pr
from backend import release_dashboard as rd
from backend.routers import release_dashboard as release_dashboard_router


def _admin_user() -> auth.User:
    return auth.User(id="u1", email="op@example.test", name="Operator", role="admin")


def _audit_rows() -> list[dict]:
    return [
        {
            "id": 1,
            "ts": "2026-05-08T14:23:00+00:00",
            "kind": deploy_audit.KIND_DEPLOY,
            "tag": "v1.2.0",
            "actor": "sora",
            "reason": "ship",
            "status": deploy_audit.STATUS_SUCCEEDED,
            "elapsed_seconds": 12.0,
            "context": None,
            "prev_hash": "0",
            "curr_hash": "1",
        },
        {
            "id": 2,
            "ts": "2026-05-08T15:00:00+00:00",
            "kind": deploy_audit.KIND_ROLLBACK,
            "tag": "v1.2.0",
            "actor": "op@example.test",
            "reason": "regression",
            "status": deploy_audit.STATUS_SUCCEEDED,
            "elapsed_seconds": None,
            "context": None,
            "prev_hash": "1",
            "curr_hash": "2",
        },
    ]


def _query(*, kind=None, limit=None, **_kwargs):
    rows = _audit_rows()
    if kind is not None:
        rows = [row for row in rows if row["kind"] == kind]
    return rows[:limit] if limit is not None else rows


def test_snapshot_projects_current_inflight_milestones_and_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr.save_approval(
        pr.ReleaseApproval(
            tag="v1.2.0",
            status="shipped",
            smoke_results={"status": "pass"},
        ),
        tmp_path,
    )
    pr.save_approval(
        pr.ReleaseApproval(
            tag="v1.3.0",
            status="deploying",
            approved_by="op@example.test",
            deploy_started_at="2026-05-08T15:01:00+00:00",
            smoke_results={"status": "pass"},
            baseline_diff={},
        ),
        tmp_path,
    )

    def _missing_canary_state():
        raise FileNotFoundError()

    monkeypatch.setattr(rd.canary_rollout, "load_state", _missing_canary_state)

    body = rd.snapshot(store_dir=tmp_path, audit_query=_query)

    assert body["current_prod_tag"] == "v1.2.0"
    assert body["in_flight"] == [
        {
            "tag": "v1.3.0",
            "status": "deploying",
            "progress_percent": 50,
            "started_at": "2026-05-08T15:01:00+00:00",
            "approved_by": "op@example.test",
        }
    ]
    assert body["milestones"] == [
        {"name": "staging_smoke", "state": "green"},
        {"name": "baseline_diff", "state": "green"},
        {"name": "operator_approval", "state": "yellow"},
    ]
    assert [row["outcome"] for row in body["history"]] == ["succeeded", "succeeded"]
    assert body["audit_rows"][0]["message"] == "sora deployed v1.2.0 at 14:23"


@pytest.mark.asyncio
async def test_rollback_control_runs_script_and_records_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    audits: list[dict] = []
    events: list[dict] = []

    def runner(args, *, timeout, env=None):
        calls.append(
            {
                "args": args,
                "timeout": timeout,
                "tag": env["OMNISIGHT_RELEASE_IMAGE_TAG"],
            }
        )

    def audit_record(**kwargs):
        audits.append(kwargs)
        return 7

    monkeypatch.setattr(rd, "current_prod_tag", lambda: "v1.2.0")
    monkeypatch.setattr(rd, "publish_update", lambda payload: events.append(payload))

    result = await rd.rollback_current(
        actor="op@example.test",
        reason="bad release",
        runner=runner,
        audit_record=audit_record,
    )

    assert calls == [
        {
            "args": ["scripts/deploy.sh", "--rollback"],
            "timeout": 120.0,
            "tag": "v1.2.0",
        }
    ]
    assert audits[0]["kind"] == deploy_audit.KIND_ROLLBACK
    assert audits[0]["reason"] == "bad release"
    assert result["audit_id"] == 7
    assert events[0]["control"] == "rollback"


def test_synthetic_progress_publishes_sse_payload() -> None:
    from backend.events import bus

    queue = bus.subscribe()
    try:
        payload = rd.synthetic_progress_event(tag="v1.3.0", progress_percent=42)
        msg = queue.get_nowait()
    finally:
        bus.unsubscribe(queue)

    assert payload["in_flight"]["progress_percent"] == 42
    assert msg["event"] == rd.RELEASE_DASHBOARD_EVENT
    frame = json.loads(msg["data"])
    assert frame["in_flight"]["tag"] == "v1.3.0"
    assert frame["in_flight"]["progress_percent"] == 42


def test_router_requires_admin_and_exposes_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(release_dashboard_router.router)
    app.dependency_overrides[auth.require_admin] = _admin_user
    monkeypatch.setattr(
        rd,
        "snapshot",
        lambda: {
            "current_prod_tag": "v1.2.0",
            "in_flight": [],
            "milestones": [],
            "history": [],
            "audit_rows": [],
            "canary": None,
            "generated_at": "2026-05-08T00:00:00+00:00",
        },
    )

    response = TestClient(app).get("/admin/releases")

    assert response.status_code == 200
    assert response.json()["current_prod_tag"] == "v1.2.0"
