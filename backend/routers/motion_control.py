"""C25 — L4-CORE-25 Motion control / G-code / CNC abstraction endpoints (#255).

REST endpoints for motion control machine management, G-code execution,
stepper driver queries, heater control, and test recipes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import motion_control as mc

_require = _au.require_operator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/motion", tags=["motion"])


# ── Request models ───────────────────────────────────────────────────

class LoadGCodeRequest(BaseModel):
    program: str = Field(..., description="G-code program text")


class SetHeaterRequest(BaseModel):
    heater_id: str = Field(..., description="Heater ID (hotend, bed)")
    temperature: float = Field(..., description="Target temperature in °C")


class MachineConfigRequest(BaseModel):
    driver_id: str = Field(default="tmc2209", description="Stepper driver ID")
    thermal_runaway_enabled: bool = Field(default=True, description="Enable thermal runaway protection")


class RunRecipeRequest(BaseModel):
    recipe_id: str = Field(..., description="Test recipe ID")


# ── Query endpoints ──────────────────────────────────────────────────

@router.get("/drivers", dependencies=[Depends(_require)])
async def get_stepper_drivers() -> list[dict[str, Any]]:
    return mc.list_stepper_drivers()


@router.get("/axes", dependencies=[Depends(_require)])
async def get_axes() -> list[dict[str, Any]]:
    return mc.list_axes()


@router.get("/heaters", dependencies=[Depends(_require)])
async def get_heaters() -> list[dict[str, Any]]:
    return mc.list_heaters()


@router.get("/endstop-types", dependencies=[Depends(_require)])
async def get_endstop_types() -> list[dict[str, Any]]:
    return mc.list_endstop_types()


@router.get("/gcode-commands", dependencies=[Depends(_require)])
async def get_gcode_commands() -> list[dict[str, Any]]:
    return mc.list_gcode_commands()


# ── Machine endpoints ────────────────────────────────────────────────

_machines: dict[str, mc.Machine] = {}
_machine_counter = 0


def _next_machine_id() -> str:
    global _machine_counter
    _machine_counter += 1
    return f"machine-{_machine_counter}"


@router.post("/machines", dependencies=[Depends(_require)])
async def create_machine(req: MachineConfigRequest | None = None) -> dict[str, Any]:
    config = None
    if req:
        axes = mc._default_axes_config()
        if req.driver_id:
            for ax in axes.values():
                ax.driver_id = req.driver_id
        config = mc.MachineConfig(
            axes=axes,
            thermal_runaway_enabled=req.thermal_runaway_enabled,
        )

    machine = mc.create_machine(config)
    mid = _next_machine_id()
    _machines[mid] = machine
    return {"machine_id": mid, "status": machine.get_status()}


@router.get("/machines/{machine_id}", dependencies=[Depends(_require)])
async def get_machine_status(machine_id: str) -> dict[str, Any]:
    machine = _machines.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    return {"machine_id": machine_id, "status": machine.get_status()}


@router.post("/machines/{machine_id}/load", dependencies=[Depends(_require)])
async def load_gcode(machine_id: str, req: LoadGCodeRequest) -> dict[str, Any]:
    machine = _machines.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    count = machine.load_gcode(req.program)
    return {"machine_id": machine_id, "lines_parsed": count}


@router.post("/machines/{machine_id}/execute", dependencies=[Depends(_require)])
async def execute_gcode(machine_id: str) -> dict[str, Any]:
    machine = _machines.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    from dataclasses import asdict
    trace = machine.execute()
    return {"machine_id": machine_id, "trace": asdict(trace)}


@router.post("/machines/{machine_id}/estop", dependencies=[Depends(_require)])
async def emergency_stop(machine_id: str) -> dict[str, Any]:
    machine = _machines.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    machine.emergency_stop()
    return {"machine_id": machine_id, "status": machine.get_status()}


@router.delete("/machines/{machine_id}", dependencies=[Depends(_require)])
async def delete_machine(machine_id: str) -> dict[str, Any]:
    machine = _machines.pop(machine_id, None)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    machine.emergency_stop()
    return {"machine_id": machine_id, "deleted": True}


# ── Test recipe endpoints ────────────────────────────────────────────

@router.get("/recipes", dependencies=[Depends(_require)])
async def get_test_recipes() -> list[dict[str, Any]]:
    return mc.list_test_recipes()


@router.post("/recipes/{recipe_id}/run", dependencies=[Depends(_require)])
async def run_test_recipe(recipe_id: str) -> dict[str, Any]:
    try:
        return mc.run_test_recipe(recipe_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown recipe: {recipe_id}")


# ── Artifact endpoints ───────────────────────────────────────────────

@router.get("/artifacts", dependencies=[Depends(_require)])
async def get_artifacts() -> list[dict[str, Any]]:
    return mc.list_artifacts()


# ── Gate validation ──────────────────────────────────────────────────

@router.post("/validate-gate", dependencies=[Depends(_require)])
async def validate_gate() -> dict[str, Any]:
    return mc.validate_gate()
