"""C12 — L4-CORE-12 Real-time / determinism track endpoints (#226).

REST endpoints for RT profiles, cyclictest harness, scheduler trace capture,
latency analysis, and threshold gate checking.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import realtime_determinism as rt

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/realtime", tags=["realtime"])


class CyclictestRequest(BaseModel):
    config_id: str = Field(default="default", description="Cyclictest configuration ID")
    profile_id: str = Field(default="preempt_rt", description="RT profile ID")
    latency_samples: list[float] | None = Field(default=None, description="Optional pre-recorded latency samples (µs)")


class TraceCaptureRequest(BaseModel):
    tool_id: str = Field(default="trace_cmd", description="Trace tool ID (trace_cmd or bpftrace)")
    duration_s: float = Field(default=5.0, description="Capture duration in seconds")
    trace_events: list[dict[str, Any]] | None = Field(default=None, description="Optional pre-recorded trace events")


class ThresholdGateRequest(BaseModel):
    config_id: str = Field(default="default", description="Cyclictest config ID")
    profile_id: str = Field(default="preempt_rt", description="RT profile ID")
    latency_samples: list[float] = Field(..., description="Latency samples (µs)")
    tier_id: str | None = Field(default=None, description="Latency tier ID for budget")
    custom_budget_us: float | None = Field(default=None, description="Custom P99 budget in µs")


# -- RT Profiles --

@router.get("/profiles")
async def list_rt_profiles(_user=Depends(_au.require_operator)) -> dict:
    profiles = rt.list_rt_profiles()
    return {
        "items": [p.to_dict() for p in profiles],
        "count": len(profiles),
    }


@router.get("/profiles/{profile_id}")
async def get_rt_profile(profile_id: str, _user=Depends(_au.require_operator)) -> dict:
    profile = rt.get_rt_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"RT profile {profile_id!r} not found")
    return profile.to_dict()


@router.get("/profiles/{profile_id}/kernel-config")
async def get_kernel_config(profile_id: str, _user=Depends(_au.require_operator)) -> dict:
    profile = rt.get_rt_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"RT profile {profile_id!r} not found")
    fragment = rt.generate_kernel_config_fragment(profile_id)
    header = rt.generate_rtos_config_header(profile_id)
    return {
        "profile_id": profile_id,
        "build_type": profile.build_type,
        "kernel_config_fragment": fragment,
        "rtos_config_header": header,
    }


# -- Cyclictest configs --

@router.get("/cyclictest/configs")
async def list_cyclictest_configs(_user=Depends(_au.require_operator)) -> dict:
    configs = rt.list_cyclictest_configs()
    return {
        "items": [c.to_dict() for c in configs],
        "count": len(configs),
    }


@router.get("/cyclictest/configs/{config_id}")
async def get_cyclictest_config(config_id: str, _user=Depends(_au.require_operator)) -> dict:
    config = rt.get_cyclictest_config(config_id)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Cyclictest config {config_id!r} not found")
    return config.to_dict()


# -- Trace tools --

@router.get("/trace/tools")
async def list_trace_tools(_user=Depends(_au.require_operator)) -> dict:
    tools = rt.list_trace_tools()
    return {
        "items": [t.to_dict() for t in tools],
        "count": len(tools),
    }


@router.get("/trace/tools/{tool_id}")
async def get_trace_tool(tool_id: str, _user=Depends(_au.require_operator)) -> dict:
    tool = rt.get_trace_tool(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Trace tool {tool_id!r} not found")
    return tool.to_dict()


# -- Latency tiers --

@router.get("/tiers")
async def list_latency_tiers(_user=Depends(_au.require_operator)) -> dict:
    tiers = rt.list_latency_tiers()
    return {
        "items": [t.to_dict() for t in tiers],
        "count": len(tiers),
    }


@router.get("/tiers/{tier_id}")
async def get_latency_tier(tier_id: str, _user=Depends(_au.require_operator)) -> dict:
    tier = rt.get_latency_tier(tier_id)
    if tier is None:
        raise HTTPException(status_code=404, detail=f"Latency tier {tier_id!r} not found")
    return tier.to_dict()


# -- Operations --

@router.post("/cyclictest/run")
async def run_cyclictest(req: CyclictestRequest, _user=Depends(_au.require_operator)) -> dict:
    result = rt.run_cyclictest(
        config_id=req.config_id,
        profile_id=req.profile_id,
        latency_samples=req.latency_samples,
    )
    return result.to_dict()


@router.post("/trace/capture")
async def capture_trace(req: TraceCaptureRequest, _user=Depends(_au.require_operator)) -> dict:
    capture = rt.capture_scheduler_trace(
        tool_id=req.tool_id,
        duration_s=req.duration_s,
        trace_events=req.trace_events,
    )
    return capture.to_dict()


@router.post("/gate/check")
async def check_threshold_gate(req: ThresholdGateRequest, _user=Depends(_au.require_operator)) -> dict:
    result = rt.run_cyclictest(
        config_id=req.config_id,
        profile_id=req.profile_id,
        latency_samples=req.latency_samples,
    )
    gate = rt.threshold_gate(
        result=result,
        tier_id=req.tier_id,
        custom_budget_us=req.custom_budget_us,
    )
    return gate.to_dict()


@router.post("/report")
async def generate_report(req: CyclictestRequest, _user=Depends(_au.require_operator)) -> dict:
    result = rt.run_cyclictest(
        config_id=req.config_id,
        profile_id=req.profile_id,
        latency_samples=req.latency_samples,
    )
    report = rt.generate_latency_report(result)
    return {
        "report_markdown": report,
        "result": result.to_dict(),
    }
