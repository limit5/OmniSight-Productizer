"""OP-778 deployment dashboard backend state and controls.

The dashboard is a read-mostly projection over OP-770 approvals,
OP-771 canary state, and OP-779 deploy_audit rows. It owns no durable
module-global state: every snapshot is derived from files or the audit
table so multiple uvicorn workers converge on the same view.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from backend import canary_rollout, deploy_audit, production_release


RELEASE_DASHBOARD_EVENT = "release.dashboard.updated"
HISTORY_LIMIT = 20


class RollbackRunner(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> None: ...


def run_command(
    args: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
) -> None:
    subprocess.run(
        args,
        check=True,
        timeout=timeout,
        env=env,
        capture_output=True,
        text=True,
    )


def _approval_progress(record: production_release.ReleaseApproval) -> int:
    by_status = {
        "pending": 0,
        "deploying": 50,
        "shipped": 100,
        "cancelled": 100,
        "rolled_back": 100,
    }
    return by_status.get(record.status, 0)


def _iter_approvals(
    store_dir: Path = production_release.DEFAULT_STORE,
) -> list[production_release.ReleaseApproval]:
    if not store_dir.exists():
        return []
    records: list[production_release.ReleaseApproval] = []
    for path in sorted(store_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            records.append(
                production_release.ReleaseApproval.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            )
        except Exception:
            continue
    return records


def current_prod_tag(
    *,
    store_dir: Path = production_release.DEFAULT_STORE,
    audit_query: Callable[..., list[dict[str, Any]]] = deploy_audit.query,
) -> str | None:
    shipped = [r for r in _iter_approvals(store_dir) if r.status == "shipped"]
    if shipped:
        return shipped[-1].tag
    rows = audit_query(kind=deploy_audit.KIND_DEPLOY, limit=10_000)
    for row in reversed(rows):
        if row["status"] == deploy_audit.STATUS_SUCCEEDED and row["tag"]:
            return str(row["tag"])
    return None


def in_flight_deploys(
    store_dir: Path = production_release.DEFAULT_STORE,
) -> list[dict[str, Any]]:
    return [
        {
            "tag": record.tag,
            "status": record.status,
            "progress_percent": _approval_progress(record),
            "started_at": record.deploy_started_at or record.created_at,
            "approved_by": record.approved_by,
        }
        for record in _iter_approvals(store_dir)
        if record.status in {"pending", "deploying"}
    ]


def milestone_progress(
    store_dir: Path = production_release.DEFAULT_STORE,
) -> list[dict[str, Any]]:
    records = _iter_approvals(store_dir)
    if not records:
        return []
    latest = records[-1]
    smoke_status = str(latest.smoke_results.get("status", "")).lower()
    smoke_state = (
        "green"
        if smoke_status in {"pass", "passed", "ok", "green"}
        else "yellow"
    )
    if smoke_status in {"fail", "failed", "red"}:
        smoke_state = "red"
    baseline_state = "green" if not latest.baseline_diff else "yellow"
    approval_state = "green" if latest.status == "shipped" else (
        "red" if latest.status == "rolled_back" else "yellow"
    )
    return [
        {"name": "staging_smoke", "state": smoke_state},
        {"name": "baseline_diff", "state": baseline_state},
        {"name": "operator_approval", "state": approval_state},
    ]


def release_history(
    *,
    audit_query: Callable[..., list[dict[str, Any]]] = deploy_audit.query,
    limit: int = HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    rows = audit_query(limit=10_000)
    relevant = [
        row for row in rows
        if row["kind"] in {deploy_audit.KIND_DEPLOY, deploy_audit.KIND_ROLLBACK}
    ]
    return [
        {
            "id": row["id"],
            "timestamp": row["ts"],
            "tag": row["tag"],
            "kind": row["kind"],
            "outcome": row["status"],
            "actor": row["actor"],
            "summary": _audit_summary(row),
        }
        for row in relevant[-limit:]
    ]


def audit_rows(
    *,
    audit_query: Callable[..., list[dict[str, Any]]] = deploy_audit.query,
    limit: int = HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": row["ts"],
            "message": _audit_summary(row),
            "kind": row["kind"],
            "status": row["status"],
        }
        for row in audit_query(limit=10_000)[-limit:]
    ]


def _audit_summary(row: dict[str, Any]) -> str:
    actor = row.get("actor") or "system"
    tag = row.get("tag") or "unknown tag"
    ts = row.get("ts") or ""
    when = ts[11:16] if isinstance(ts, str) and len(ts) >= 16 else ts
    if row.get("kind") == deploy_audit.KIND_ROLLBACK:
        return f"{actor} rolled back {tag} at {when}"
    if row.get("status") == deploy_audit.STATUS_SUCCEEDED:
        return f"{actor} deployed {tag} at {when}"
    return f"{actor} {row.get('kind')} {tag} {row.get('status')} at {when}"


def canary_snapshot() -> dict[str, Any] | None:
    try:
        state = canary_rollout.load_state()
    except FileNotFoundError:
        return None
    return {
        "rollout_id": state.rollout_id,
        "status": state.status,
        "stable_color": state.stable_color,
        "canary_color": state.canary_color,
        "stage_index": state.stage_index,
        "stage": asdict(state.stage),
        "reason": state.reason,
        "updated_at": state.updated_at,
    }


def snapshot(
    *,
    store_dir: Path = production_release.DEFAULT_STORE,
    audit_query: Callable[..., list[dict[str, Any]]] = deploy_audit.query,
) -> dict[str, Any]:
    return {
        "current_prod_tag": current_prod_tag(
            store_dir=store_dir,
            audit_query=audit_query,
        ),
        "in_flight": in_flight_deploys(store_dir),
        "milestones": milestone_progress(store_dir),
        "history": release_history(audit_query=audit_query),
        "audit_rows": audit_rows(audit_query=audit_query),
        "canary": canary_snapshot(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def publish_update(payload: dict[str, Any]) -> None:
    try:
        from backend.events import bus
        bus.publish(
            RELEASE_DASHBOARD_EVENT,
            payload,
            broadcast_scope="global",
        )
    except Exception:
        pass


async def rollback_current(
    *,
    actor: str,
    reason: str,
    tag: str | None = None,
    runner: RollbackRunner = run_command,
    audit_record: Callable[..., int] = deploy_audit.record,
) -> dict[str, Any]:
    target_tag = tag or current_prod_tag()
    if not target_tag:
        raise ValueError("current production tag is unknown")
    env = os.environ.copy()
    env["OMNISIGHT_RELEASE_IMAGE_TAG"] = target_tag
    await asyncio.to_thread(
        runner,
        ["scripts/deploy.sh", "--rollback"],
        timeout=120.0,
        env=env,
    )
    row_id = await asyncio.to_thread(
        audit_record,
        kind=deploy_audit.KIND_ROLLBACK,
        status=deploy_audit.STATUS_SUCCEEDED,
        tag=target_tag,
        actor=actor,
        reason=reason,
        context={"source": "release_dashboard"},
    )
    payload = {
        "control": "rollback",
        "tag": target_tag,
        "audit_id": row_id,
        "actor": actor,
    }
    publish_update(payload)
    return payload


def synthetic_progress_event(
    *,
    tag: str,
    progress_percent: int,
    status: str = "deploying",
) -> dict[str, Any]:
    if not 0 <= progress_percent <= 100:
        raise ValueError("progress_percent must be between 0 and 100")
    payload = {
        "control": "synthetic_progress",
        "in_flight": {
            "tag": tag,
            "status": status,
            "progress_percent": progress_percent,
        },
    }
    publish_update(payload)
    return payload
