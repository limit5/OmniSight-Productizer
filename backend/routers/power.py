"""C11 — L4-CORE-11 Power / battery profiling endpoints (#225).

REST endpoints for sleep states, power domains, ADC configs, current profiling,
battery lifetime estimation, and feature power budget analysis.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import power_profiling as pp

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/power", tags=["power"])


class ProfilingRequest(BaseModel):
    adc_id: str = Field(..., description="ADC configuration ID (ina219, ina226, ads1115, internal_adc)")
    duration_s: float = Field(default=10.0, description="Sampling duration in seconds")
    raw_samples: list[dict[str, Any]] | None = Field(default=None, description="Optional raw samples for simulation")


class LifetimeRequest(BaseModel):
    battery: dict[str, Any] = Field(..., description="Battery spec (capacity_mah, chemistry, cycle_count, ...)")
    duty_cycle: dict[str, Any] = Field(..., description="Duty cycle profile (active/idle/sleep pct + currents)")


class FeatureBudgetRequest(BaseModel):
    enabled_features: list[str] = Field(default_factory=list, description="List of enabled feature toggle IDs")
    battery: dict[str, Any] = Field(..., description="Battery spec")
    base_duty_cycle: dict[str, Any] | None = Field(default=None, description="Base duty cycle without features")


class TransitionDetectRequest(BaseModel):
    trace: list[dict[str, Any]] = Field(..., description="Timestamped current trace [{timestamp_s, current_ma}, ...]")


@router.get("/sleep-states")
async def list_sleep_states(_user=Depends(_au.require_operator)) -> dict:
    states = pp.list_sleep_states()
    return {
        "items": [s.to_dict() for s in states],
        "count": len(states),
    }


@router.get("/sleep-states/{state_id}")
async def get_sleep_state(state_id: str, _user=Depends(_au.require_operator)) -> dict:
    state = pp.get_sleep_state(state_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Sleep state {state_id!r} not found")
    return state.to_dict()


@router.get("/domains")
async def list_power_domains(_user=Depends(_au.require_operator)) -> dict:
    domains = pp.list_power_domains()
    return {
        "items": [d.to_dict() for d in domains],
        "count": len(domains),
    }


@router.get("/domains/{domain_id}")
async def get_power_domain(domain_id: str, _user=Depends(_au.require_operator)) -> dict:
    domain = pp.get_power_domain(domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail=f"Power domain {domain_id!r} not found")
    return domain.to_dict()


@router.get("/adc")
async def list_adc_configs(_user=Depends(_au.require_operator)) -> dict:
    configs = pp.list_adc_configs()
    return {
        "items": [c.to_dict() for c in configs],
        "count": len(configs),
    }


@router.get("/adc/{adc_id}")
async def get_adc_config(adc_id: str, _user=Depends(_au.require_operator)) -> dict:
    config = pp.get_adc_config(adc_id)
    if config is None:
        raise HTTPException(status_code=404, detail=f"ADC config {adc_id!r} not found")
    return config.to_dict()


@router.get("/features")
async def list_feature_toggles(_user=Depends(_au.require_operator)) -> dict:
    toggles = pp.list_feature_toggles()
    return {
        "items": [t.to_dict() for t in toggles],
        "count": len(toggles),
    }


@router.get("/chemistries")
async def list_battery_chemistries(_user=Depends(_au.require_operator)) -> dict:
    chems = pp.list_battery_chemistries()
    return {
        "items": chems,
        "count": len(chems),
    }


@router.post("/profile")
async def run_profiling(
    req: ProfilingRequest,
    _user=Depends(_au.require_admin),
) -> dict:
    session = pp.sample_current(
        req.adc_id,
        req.duration_s,
        raw_samples=req.raw_samples,
    )
    await pp.log_profiling_result(session)
    return session.to_dict()


@router.post("/transitions")
async def detect_transitions(
    req: TransitionDetectRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    events = pp.detect_sleep_transitions(req.trace)
    return {
        "events": [e.to_dict() for e in events],
        "count": len(events),
    }


@router.post("/lifetime")
async def estimate_lifetime(
    req: LifetimeRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    estimate = pp.estimate_battery_lifetime(req.battery, req.duty_cycle)
    await pp.log_lifetime_estimate(estimate)
    return estimate.to_dict()


@router.post("/budget")
async def compute_budget(
    req: FeatureBudgetRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    budget = pp.compute_feature_power_budget(
        req.enabled_features,
        req.battery,
        req.base_duty_cycle,
    )
    return budget.to_dict()
