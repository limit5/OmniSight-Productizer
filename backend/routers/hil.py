"""C7 — L4-CORE-07 HIL plugin API endpoints (#216).

Provides REST endpoints for listing registered HIL plugins, validating
skill pack HIL requirements, and running HIL tests.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend import auth as _au
from backend import hil_registry
from backend import skill_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hil", tags=["hil"])


@router.get("/plugins")
async def list_plugins(_user=Depends(_au.require_operator)) -> dict:
    """List all registered HIL plugins with their supported metrics."""
    plugins = hil_registry.list_registered_plugins()
    items = []
    for name, info in plugins.items():
        items.append({
            "name": info.name,
            "family": info.family.value,
            "version": info.version,
            "description": info.description,
            "supported_metrics": info.supported_metrics,
        })
    return {"items": items, "count": len(items)}


@router.get("/plugins/{name}")
async def get_plugin(name: str, _user=Depends(_au.require_operator)) -> dict:
    """Get details of a specific HIL plugin."""
    cls = hil_registry.get_plugin_class(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"HIL plugin {name!r} not found")
    instance = cls()
    info = instance.plugin_info
    return {
        "name": info.name,
        "family": info.family.value,
        "version": info.version,
        "description": info.description,
        "supported_metrics": info.supported_metrics,
    }


@router.post("/validate/{skill_name}")
async def validate_skill_hil(
    skill_name: str,
    _user=Depends(_au.require_operator),
) -> dict:
    """Validate that a skill pack's HIL requirements are satisfiable."""
    info = skill_registry.get_skill(skill_name)
    if info is None:
        raise HTTPException(
            status_code=404, detail=f"skill {skill_name!r} not found"
        )
    result = hil_registry.validate_skill_hil(skill_name, info.path)
    return {
        "skill_name": result.skill_name,
        "ok": result.ok,
        "missing_plugins": result.missing_plugins,
        "missing_metrics": result.missing_metrics,
        "issues": result.issues,
    }


@router.post("/run/{skill_name}")
async def run_skill_hil(
    skill_name: str,
    _user=Depends(_au.require_admin),
) -> dict:
    """Run all HIL plugins required by a skill pack.

    This executes the full measure → verify → teardown lifecycle for each
    declared HIL plugin.
    """
    info = skill_registry.get_skill(skill_name)
    if info is None:
        raise HTTPException(
            status_code=404, detail=f"skill {skill_name!r} not found"
        )

    validation = hil_registry.validate_skill_hil(skill_name, info.path)
    if not validation.ok:
        raise HTTPException(
            status_code=400,
            detail=f"HIL validation failed: {validation.issues}",
        )

    summaries = hil_registry.run_skill_hil(skill_name, info.path)
    results = []
    for s in summaries:
        results.append({
            "plugin_name": s.plugin_name,
            "family": s.family,
            "status": s.status.value,
            "pass_count": s.pass_count,
            "fail_count": s.fail_count,
            "duration_s": round(s.duration_s, 4),
            "error_message": s.error_message,
            "measurements": [
                {
                    "metric_name": m.metric_name,
                    "value": m.value,
                    "unit": m.unit,
                }
                for m in s.measurements
            ],
            "results": [
                {
                    "metric_name": r.metric_name,
                    "passed": r.passed,
                    "measured_value": r.measured_value,
                    "message": r.message,
                }
                for r in s.results
            ],
        })

    all_ok = all(s.all_passed for s in summaries)
    return {
        "skill_name": skill_name,
        "ok": all_ok,
        "plugin_results": results,
        "total_plugins": len(results),
    }
