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


# ── Response models ──────────────────────────────────────────────────

class StepperDriverResponse(BaseModel):
    driver_id: str
    name: str
    description: str
    interface: str
    microstep_options: list[int]
    default_microsteps: int
    max_current_ma: int
    features: list[str]
    stall_threshold_range: list[int] | None = None
    default_stall_threshold: int | None = None


class AxisResponse(BaseModel):
    axis_id: str
    name: str
    steps_per_mm: float
    max_feedrate_mm_s: float
    max_accel_mm_s2: float
    homing_feedrate_mm_s: float
    travel_mm: float


class HeaterResponse(BaseModel):
    heater_id: str
    name: str
    description: str
    max_temp_c: float
    min_temp_c: float
    pid_kp: float
    pid_ki: float
    pid_kd: float
    thermal_runaway_period_s: float
    thermal_runaway_hysteresis_c: float
    thermal_runaway_max_deviation_c: float
    thermal_runaway_grace_period_s: float


class EndstopTypeResponse(BaseModel):
    endstop_type: str
    name: str
    description: str
    debounce_ms: int
    active_low: bool


class GCodeCommandResponse(BaseModel):
    command_id: str
    name: str
    description: str
    axes: list[str]
    parameters: list[str]


class HeaterStatusResponse(BaseModel):
    heater_id: str
    target: float
    current: float
    output: float
    enabled: bool


class DriverStatusResponse(BaseModel):
    driver_id: str
    axis_id: str
    enabled: bool
    position_steps: int
    position_mm: float
    microsteps: int
    current_ma: int
    step_count: int
    stealthchop: bool | None = None
    stall_threshold: int | None = None
    sleep: bool | None = None
    fault: bool | None = None


class EndstopStatusResponse(BaseModel):
    axis_id: str
    endstop_type: str
    triggered: bool
    trigger_position_mm: float


class ThermalMonitorStatusResponse(BaseModel):
    heater_id: str
    tripped: bool
    fault_reason: str


class MachineStatusResponse(BaseModel):
    state: str
    position: dict[str, float]
    feedrate: float
    hotend: HeaterStatusResponse
    bed: HeaterStatusResponse
    drivers: dict[str, DriverStatusResponse]
    endstops: dict[str, EndstopStatusResponse]
    thermal_monitors: dict[str, ThermalMonitorStatusResponse]


class MachineResponse(BaseModel):
    machine_id: str
    status: MachineStatusResponse


class LoadGCodeResponse(BaseModel):
    machine_id: str
    lines_parsed: int


class MotionStepResponse(BaseModel):
    line_number: int
    command: str
    position: dict[str, float]
    feedrate: float
    hotend_target: float | None = None
    bed_target: float | None = None
    hotend_temp: float | None = None
    bed_temp: float | None = None
    event: str
    timestamp_ms: float


class MotionTraceResponse(BaseModel):
    steps: list[MotionStepResponse]
    total_distance_mm: float
    total_time_ms: float
    final_position: dict[str, float]
    errors: list[str]


class ExecuteGCodeResponse(BaseModel):
    machine_id: str
    trace: MotionTraceResponse


class DeleteMachineResponse(BaseModel):
    machine_id: str
    deleted: bool


class MotionTestRecipeResponse(BaseModel):
    recipe_id: str
    name: str
    description: str
    domains: list[str]


class MotionTestRecipeRunResponse(BaseModel):
    recipe_id: str
    status: str
    total: int
    passed: int
    failed: int
    skipped: int
    duration_ms: float
    details: list[dict[str, Any]]


class MotionArtifactResponse(BaseModel):
    artifact_id: str
    kind: str
    description: str


class MotionGateResponse(BaseModel):
    verdict: str
    total_passed: int
    total_failed: int
    recipes: list[dict[str, Any]]


# ── Query endpoints ──────────────────────────────────────────────────

@router.get("/drivers", response_model=list[StepperDriverResponse], dependencies=[Depends(_require)])
async def get_stepper_drivers() -> list[dict[str, Any]]:
    return mc.list_stepper_drivers()


@router.get("/axes", response_model=list[AxisResponse], dependencies=[Depends(_require)])
async def get_axes() -> list[dict[str, Any]]:
    return mc.list_axes()


@router.get("/heaters", response_model=list[HeaterResponse], dependencies=[Depends(_require)])
async def get_heaters() -> list[dict[str, Any]]:
    return mc.list_heaters()


@router.get("/endstop-types", response_model=list[EndstopTypeResponse], dependencies=[Depends(_require)])
async def get_endstop_types() -> list[dict[str, Any]]:
    return mc.list_endstop_types()


@router.get("/gcode-commands", response_model=list[GCodeCommandResponse], dependencies=[Depends(_require)])
async def get_gcode_commands() -> list[dict[str, Any]]:
    return mc.list_gcode_commands()


# ── Machine endpoints ────────────────────────────────────────────────

_machines: dict[str, mc.Machine] = {}
_machine_counter = 0


def _next_machine_id() -> str:
    global _machine_counter
    _machine_counter += 1
    return f"machine-{_machine_counter}"


@router.post(
    "/machines",
    response_model=MachineResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require)],
)
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


@router.get(
    "/machines/{machine_id}",
    response_model=MachineResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require)],
)
async def get_machine_status(machine_id: str) -> dict[str, Any]:
    machine = _machines.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    return {"machine_id": machine_id, "status": machine.get_status()}


@router.post("/machines/{machine_id}/load", response_model=LoadGCodeResponse, dependencies=[Depends(_require)])
async def load_gcode(machine_id: str, req: LoadGCodeRequest) -> dict[str, Any]:
    machine = _machines.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    count = machine.load_gcode(req.program)
    return {"machine_id": machine_id, "lines_parsed": count}


@router.post("/machines/{machine_id}/execute", response_model=ExecuteGCodeResponse, dependencies=[Depends(_require)])
async def execute_gcode(machine_id: str) -> dict[str, Any]:
    machine = _machines.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    from dataclasses import asdict
    trace = machine.execute()
    return {"machine_id": machine_id, "trace": asdict(trace)}


@router.post(
    "/machines/{machine_id}/estop",
    response_model=MachineResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(_require)],
)
async def emergency_stop(machine_id: str) -> dict[str, Any]:
    machine = _machines.get(machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    machine.emergency_stop()
    return {"machine_id": machine_id, "status": machine.get_status()}


@router.delete("/machines/{machine_id}", response_model=DeleteMachineResponse, dependencies=[Depends(_require)])
async def delete_machine(machine_id: str) -> dict[str, Any]:
    machine = _machines.pop(machine_id, None)
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    machine.emergency_stop()
    return {"machine_id": machine_id, "deleted": True}


# ── Test recipe endpoints ────────────────────────────────────────────

@router.get("/recipes", response_model=list[MotionTestRecipeResponse], dependencies=[Depends(_require)])
async def get_test_recipes() -> list[dict[str, Any]]:
    return mc.list_test_recipes()


@router.post("/recipes/{recipe_id}/run", response_model=MotionTestRecipeRunResponse, dependencies=[Depends(_require)])
async def run_test_recipe(recipe_id: str) -> dict[str, Any]:
    try:
        return mc.run_test_recipe(recipe_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown recipe: {recipe_id}")


# ── Artifact endpoints ───────────────────────────────────────────────

@router.get("/artifacts", response_model=list[MotionArtifactResponse], dependencies=[Depends(_require)])
async def get_artifacts() -> list[dict[str, Any]]:
    return mc.list_artifacts()


# ── Gate validation ──────────────────────────────────────────────────

@router.post("/validate-gate", response_model=MotionGateResponse, dependencies=[Depends(_require)])
async def validate_gate() -> dict[str, Any]:
    return mc.validate_gate()
