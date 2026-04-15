"""C9 — L4-CORE-09 Safety & compliance framework endpoints (#223).

REST endpoints for safety standard lookup, DAG compliance checking,
and artifact definition queries.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import safety_compliance as sc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/safety", tags=["safety"])


class ComplianceCheckRequest(BaseModel):
    standard: str = Field(..., description="Safety standard key (iso26262, iec60601, do178, iec61508)")
    level: str = Field(..., description="Safety level (e.g. 'B', 'ASIL_B', 'SW_C', 'DAL_A', 'SIL_3')")
    artifacts: list[str] = Field(default_factory=list, description="List of artifact IDs that have been produced")
    dag_id: str = Field(default="", description="DAG ID to validate (loads from DB if provided)")


class MultiCheckRequest(BaseModel):
    requirements: list[dict[str, str]] = Field(..., description="List of {standard, level} dicts")
    artifacts: list[str] = Field(default_factory=list)


@router.get("/standards")
async def list_standards(_user=Depends(_au.require_operator)) -> dict:
    stds = sc.list_standards()
    items = []
    for s in stds:
        items.append({
            "standard_id": s.standard_id,
            "name": s.name,
            "domain": s.domain,
            "levels": [
                {
                    "level_id": lv.level_id,
                    "name": lv.name,
                    "description": lv.description,
                    "required_artifacts": lv.required_artifacts,
                    "required_dag_tasks": lv.required_dag_tasks,
                    "review_required": lv.review_required,
                }
                for lv in s.levels
            ],
        })
    return {"items": items, "count": len(items)}


@router.get("/standards/{standard_id}")
async def get_standard(standard_id: str, _user=Depends(_au.require_operator)) -> dict:
    std = sc.get_standard(standard_id)
    if std is None:
        raise HTTPException(status_code=404, detail=f"Safety standard {standard_id!r} not found")
    return {
        "standard_id": std.standard_id,
        "name": std.name,
        "domain": std.domain,
        "levels": [
            {
                "level_id": lv.level_id,
                "name": lv.name,
                "description": lv.description,
                "required_artifacts": lv.required_artifacts,
                "required_dag_tasks": lv.required_dag_tasks,
                "review_required": lv.review_required,
            }
            for lv in std.levels
        ],
    }


@router.get("/artifacts")
async def list_artifacts(_user=Depends(_au.require_operator)) -> dict:
    arts = sc.list_artifact_definitions()
    items = [
        {
            "artifact_id": a.artifact_id,
            "name": a.name,
            "description": a.description,
            "file_pattern": a.file_pattern,
        }
        for a in arts
    ]
    return {"items": items, "count": len(items)}


@router.post("/check")
async def check_compliance(
    req: ComplianceCheckRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    dag = None
    if req.dag_id:
        try:
            from backend import db
            from backend.dag_schema import DAG
            row = await db.fetch_one(
                "SELECT payload FROM dag_plans WHERE dag_id = ?",
                (req.dag_id,),
            )
            if row:
                dag = DAG.model_validate_json(row["payload"])
        except Exception as exc:
            logger.warning("Failed to load DAG %s: %s", req.dag_id, exc)

    if dag is None:
        from backend.dag_schema import DAG, Task
        dag = DAG(
            dag_id="empty",
            tasks=[Task(
                task_id="placeholder",
                description="placeholder",
                required_tier="t1",
                toolchain="cmake",
                expected_output="build/out.bin",
            )],
        )

    result = sc.validate_safety_gate(dag, req.standard, req.level, req.artifacts)
    await sc.log_safety_gate_result(result)
    return result.to_dict()


@router.post("/check-multi")
async def check_multi(
    req: MultiCheckRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    from backend.dag_schema import DAG, Task
    dag = DAG(
        dag_id="multi-check",
        tasks=[Task(
            task_id="placeholder",
            description="placeholder",
            required_tier="t1",
            toolchain="cmake",
            expected_output="build/out.bin",
        )],
    )
    results = sc.check_all_standards(dag, req.requirements, req.artifacts)
    for r in results:
        await sc.log_safety_gate_result(r)
    return {
        "results": [r.to_dict() for r in results],
        "all_passed": all(r.passed for r in results),
        "count": len(results),
    }
