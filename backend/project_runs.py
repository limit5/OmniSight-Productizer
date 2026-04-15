"""B7 (#207) — project_run aggregation.

A *project run* groups several workflow_runs that belong to the same
logical session (e.g. "compile + flash + smoke-test" triggered by one
DAG submission).  The RunHistory panel renders these as collapsed parent
rows with summary stats; clicking expands to show the children.

Storage: ``project_runs`` table in SQLite.  ``workflow_run_ids`` is a
JSON array of TEXT ids referencing ``workflow_runs.id``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProjectRun:
    id: str
    project_id: str
    label: str
    created_at: float
    workflow_run_ids: list[str] = field(default_factory=list)


async def _conn():
    from backend import db
    return db._conn()


def _uid() -> str:
    return f"pr-{uuid.uuid4().hex[:10]}"


async def create(project_id: str, label: str,
                 workflow_run_ids: list[str] | None = None) -> ProjectRun:
    conn = await _conn()
    pr = ProjectRun(
        id=_uid(),
        project_id=project_id,
        label=label,
        created_at=time.time(),
        workflow_run_ids=workflow_run_ids or [],
    )
    await conn.execute(
        "INSERT INTO project_runs (id, project_id, label, created_at, workflow_run_ids) "
        "VALUES (?, ?, ?, ?, ?)",
        (pr.id, pr.project_id, pr.label, pr.created_at,
         json.dumps(pr.workflow_run_ids)),
    )
    await conn.commit()
    return pr


async def get(project_run_id: str) -> Optional[ProjectRun]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, project_id, label, created_at, workflow_run_ids "
        "FROM project_runs WHERE id = ?", (project_run_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return _row_to_pr(row)


async def list_by_project(project_id: str, limit: int = 50) -> list[ProjectRun]:
    conn = await _conn()
    async with conn.execute(
        "SELECT id, project_id, label, created_at, workflow_run_ids "
        "FROM project_runs WHERE project_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (project_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_pr(r) for r in rows]


async def list_by_project_with_children(
    project_id: str, limit: int = 50,
) -> list[dict[str, Any]]:
    """Return project_runs with their child workflow_runs materialised.

    Each element:
        {
          "id", "project_id", "label", "created_at",
          "workflow_run_ids": [...],
          "children": [ {id, kind, status, started_at, completed_at, ...}, ... ],
          "summary": { "total": N, "running": N, "completed": N, "failed": N }
        }
    """
    prs = await list_by_project(project_id, limit)
    if not prs:
        return []
    conn = await _conn()
    results: list[dict[str, Any]] = []
    for pr in prs:
        children: list[dict[str, Any]] = []
        for wf_id in pr.workflow_run_ids:
            async with conn.execute(
                "SELECT id, kind, started_at, completed_at, status, "
                "last_step_id, metadata FROM workflow_runs WHERE id = ?",
                (wf_id,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                children.append({
                    "id": row["id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "last_step_id": row["last_step_id"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                })
        summary = _tally(children)
        results.append({
            "id": pr.id,
            "project_id": pr.project_id,
            "label": pr.label,
            "created_at": pr.created_at,
            "workflow_run_ids": pr.workflow_run_ids,
            "children": children,
            "summary": summary,
        })
    return results


def _tally(children: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {"total": 0, "running": 0, "completed": 0, "failed": 0, "halted": 0}
    for c in children:
        counts["total"] += 1
        status = c.get("status", "")
        if status in counts:
            counts[status] += 1
    return counts


def _row_to_pr(row) -> ProjectRun:
    return ProjectRun(
        id=row["id"],
        project_id=row["project_id"],
        label=row["label"],
        created_at=row["created_at"],
        workflow_run_ids=json.loads(row["workflow_run_ids"] or "[]"),
    )


async def backfill(session_gap_s: float = 300.0) -> int:
    """Best-effort backfill: group existing workflow_runs into project_runs
    by splitting on gaps > ``session_gap_s`` seconds between consecutive
    runs.  Returns count of project_runs created.

    Runs that already belong to a project_run are skipped.
    """
    conn = await _conn()

    async with conn.execute(
        "SELECT workflow_run_ids FROM project_runs"
    ) as cur:
        rows = await cur.fetchall()
    already: set[str] = set()
    for r in rows:
        already.update(json.loads(r["workflow_run_ids"] or "[]"))

    async with conn.execute(
        "SELECT id, started_at FROM workflow_runs ORDER BY started_at ASC"
    ) as cur:
        all_runs = await cur.fetchall()

    ungrouped = [(r["id"], r["started_at"]) for r in all_runs if r["id"] not in already]
    if not ungrouped:
        return 0

    groups: list[list[str]] = []
    current_group: list[str] = [ungrouped[0][0]]
    prev_ts = ungrouped[0][1]
    for wf_id, ts in ungrouped[1:]:
        if ts - prev_ts > session_gap_s:
            groups.append(current_group)
            current_group = []
        current_group.append(wf_id)
        prev_ts = ts
    if current_group:
        groups.append(current_group)

    created = 0
    for idx, grp in enumerate(groups):
        pr_id = _uid()
        now = time.time()
        await conn.execute(
            "INSERT INTO project_runs (id, project_id, label, created_at, workflow_run_ids) "
            "VALUES (?, ?, ?, ?, ?)",
            (pr_id, "default", f"Session {idx + 1}", now, json.dumps(grp)),
        )
        created += 1
    await conn.commit()
    logger.info("backfill: created %d project_runs from %d ungrouped workflow_runs",
                created, len(ungrouped))
    return created


async def _reset_for_tests() -> None:
    conn = await _conn()
    await conn.execute("DELETE FROM project_runs")
    await conn.commit()
