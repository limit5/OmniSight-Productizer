"""C8 — L4-CORE-08 Protocol compliance harness endpoints (#217).

REST endpoints for listing compliance tools, running compliance tests,
and querying compliance reports.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend import auth as _au
from backend import compliance_harness as ch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/compliance", tags=["compliance"])


@router.get("/tools")
async def list_tools(_user=Depends(_au.require_operator)) -> dict:
    tools = ch.list_tools()
    items = []
    for t in tools:
        items.append({
            "name": t.name,
            "protocol": t.protocol.value,
            "version": t.version,
            "binary": t.binary,
            "description": t.description,
            "supported_profiles": t.supported_profiles,
            "available": ch.get_tool(t.name).check_available(),
        })
    return {"items": items, "count": len(items)}


@router.get("/tools/{name}")
async def get_tool(name: str, _user=Depends(_au.require_operator)) -> dict:
    try:
        tool = ch.get_tool(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Compliance tool {name!r} not found")
    info = tool.tool_info
    return {
        "name": info.name,
        "protocol": info.protocol.value,
        "version": info.version,
        "binary": info.binary,
        "description": info.description,
        "supported_profiles": info.supported_profiles,
        "available": tool.check_available(),
    }


@router.post("/run/{tool_name}")
async def run_compliance_test(
    tool_name: str,
    device_target: str,
    profile: str = "",
    timeout_s: int = 600,
    _user=Depends(_au.require_admin),
) -> dict:
    try:
        tool = ch.get_tool(tool_name)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"Compliance tool {tool_name!r} not found"
        )

    try:
        report = tool.run(device_target, profile, timeout_s=timeout_s)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Compliance test %s failed: %s", tool_name, exc)
        raise HTTPException(status_code=500, detail=f"Test execution failed: {exc}")

    await ch.log_compliance_report(report)

    return report.to_dict()
