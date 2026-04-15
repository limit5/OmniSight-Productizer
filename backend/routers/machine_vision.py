"""C24 — L4-CORE-24 Machine vision & industrial imaging framework endpoints (#254).

REST endpoints for GenICam camera management, transport queries,
trigger configuration, calibration, line-scan, PLC integration,
and test recipes.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import machine_vision as mv

_require = _au.require_operator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/vision", tags=["vision"])


# ── Request models ───────────────────────────────────────────────────


class CameraConnectRequest(BaseModel):
    transport_id: str = Field(..., description="Transport ID (gige_vision, usb3_vision, camera_link, coaxpress)")
    camera_model: str = Field(default="", description="Camera model identifier")
    pixel_format: str = Field(default="Mono8", description="Pixel format")
    width: int = Field(default=640, description="Image width")
    height: int = Field(default=480, description="Image height")
    exposure_us: float = Field(default=1000.0, description="Exposure time in microseconds")
    gain_db: float = Field(default=0.0, description="Gain in dB")


class SetFeatureRequest(BaseModel):
    name: str = Field(..., description="GenICam feature name or ID")
    value: Any = Field(..., description="Feature value")


class TriggerConfigRequest(BaseModel):
    mode: str = Field(..., description="Trigger mode ID")
    source: str = Field(default="Software", description="Trigger source")
    activation: str = Field(default="rising_edge", description="Trigger activation")


class CalibrationRequest(BaseModel):
    frames_base64: list[str] = Field(..., description="List of base64-encoded calibration frames")
    method: str = Field(default="checkerboard", description="Calibration method")
    width: int = Field(default=640, description="Frame width")
    height: int = Field(default=480, description="Frame height")
    board_size: list[int] = Field(default=[9, 6], description="Checkerboard inner corners [cols, rows]")
    square_size_mm: float = Field(default=25.0, description="Square size in mm")


class StereoCalibrationRequest(BaseModel):
    frames_left_base64: list[str] = Field(..., description="Left camera frames (base64)")
    frames_right_base64: list[str] = Field(..., description="Right camera frames (base64)")
    width: int = Field(default=640)
    height: int = Field(default=480)
    board_size: list[int] = Field(default=[9, 6])
    square_size_mm: float = Field(default=25.0)


class LineScanComposeRequest(BaseModel):
    lines_base64: list[str] = Field(..., description="List of base64-encoded scan lines")
    width: int = Field(..., description="Line width in pixels")
    pixel_format: str = Field(default="Mono8")
    direction: str = Field(default="forward", description="Scan direction (forward, reverse, bidirectional)")
    line_rate_hz: float = Field(default=10000.0)


class PLCReadRequest(BaseModel):
    protocol: str = Field(..., description="PLC protocol (modbus, opcua)")
    address: Any = Field(..., description="Register address (int for Modbus, string for OPC-UA node_id)")


class PLCWriteRequest(BaseModel):
    protocol: str = Field(..., description="PLC protocol (modbus, opcua)")
    address: Any = Field(..., description="Register address")
    value: Any = Field(..., description="Value to write")


class EncoderCreateRequest(BaseModel):
    interface_type: str = Field(default="quadrature_ab")
    resolution: int = Field(default=1024)
    divider: int = Field(default=1)
    direction: str = Field(default="forward")


class RunRecipeRequest(BaseModel):
    recipe_id: str = Field(..., description="Test recipe ID")


# ── Transport endpoints ─────────────────────────────────────────────


@router.get("/transports", dependencies=[Depends(_require)])
async def get_transports() -> list[dict[str, Any]]:
    return mv.list_transports()


@router.get("/transports/{transport_id}", dependencies=[Depends(_require)])
async def get_transport(transport_id: str) -> dict[str, Any]:
    transports = mv.list_transports()
    for t in transports:
        if t["transport_id"] == transport_id:
            return t
    raise HTTPException(status_code=404, detail=f"Unknown transport: {transport_id}")


# ── GenICam feature endpoints ────────────────────────────────────────


@router.get("/genicam/features", dependencies=[Depends(_require)])
async def get_genicam_features(category: str | None = None) -> list[dict[str, Any]]:
    return mv.list_genicam_features(category=category)


# ── Camera model endpoints ───────────────────────────────────────────


@router.get("/cameras/models", dependencies=[Depends(_require)])
async def get_camera_models(scan_type: str | None = None) -> list[dict[str, Any]]:
    return mv.list_camera_models(scan_type=scan_type)


# ── Camera lifecycle endpoints ───────────────────────────────────────


@router.post("/cameras/connect", dependencies=[Depends(_require)])
async def connect_camera(req: CameraConnectRequest) -> dict[str, Any]:
    try:
        config = mv.CameraConfig(
            transport_id=req.transport_id,
            camera_model=req.camera_model,
            pixel_format=req.pixel_format,
            width=req.width,
            height=req.height,
            exposure_us=req.exposure_us,
            gain_db=req.gain_db,
        )
        cam = mv.create_camera(req.transport_id, req.camera_model, config)
        cam.connect()
        cam.configure(config)
        frame = cam.acquire()
        status = cam.get_status()
        status["sample_frame_size"] = len(frame.pixel_data)
        cam.disconnect()
        return status
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/cameras/set-feature", dependencies=[Depends(_require)])
async def set_feature(req: SetFeatureRequest) -> dict[str, Any]:
    cam = mv.create_camera("gige_vision", "virtual")
    cam.connect()
    cam.configure(mv.CameraConfig(transport_id="gige_vision"))
    ok = cam.set_feature(req.name, req.value)
    result = {"name": req.name, "value": req.value, "accepted": ok}
    cam.disconnect()
    return result


# ── Trigger endpoints ────────────────────────────────────────────────


@router.get("/trigger-modes", dependencies=[Depends(_require)])
async def get_trigger_modes() -> list[dict[str, Any]]:
    return mv.list_trigger_modes()


@router.post("/cameras/configure-trigger", dependencies=[Depends(_require)])
async def configure_trigger(req: TriggerConfigRequest) -> dict[str, Any]:
    cam = mv.create_camera("gige_vision", "virtual")
    cam.connect()
    cam.configure(mv.CameraConfig(transport_id="gige_vision"))
    ok = cam.configure_trigger(req.mode, req.source, req.activation)
    status = cam.get_status()
    status["trigger_configured"] = ok
    cam.disconnect()
    return status


# ── Encoder endpoints ────────────────────────────────────────────────


@router.get("/encoder/interfaces", dependencies=[Depends(_require)])
async def get_encoder_interfaces() -> list[dict[str, Any]]:
    return mv.list_encoder_interfaces()


@router.post("/encoder/create", dependencies=[Depends(_require)])
async def create_encoder(req: EncoderCreateRequest) -> dict[str, Any]:
    try:
        enc = mv.create_encoder(req.interface_type, req.resolution, req.divider, req.direction)
        state = enc.read_position()
        return asdict(state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Calibration endpoints ───────────────────────────────────────────


@router.get("/calibration/methods", dependencies=[Depends(_require)])
async def get_calibration_methods() -> list[dict[str, Any]]:
    return mv.list_calibration_methods()


@router.post("/calibration/run", dependencies=[Depends(_require)])
async def run_calibration(req: CalibrationRequest) -> dict[str, Any]:
    try:
        frames = [base64.b64decode(f) for f in req.frames_base64]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 frame data")

    result = mv.calibrate_camera(
        frames, req.method, req.width, req.height,
        tuple(req.board_size), req.square_size_mm,
    )
    return asdict(result)


@router.post("/calibration/stereo", dependencies=[Depends(_require)])
async def run_stereo_calibration(req: StereoCalibrationRequest) -> dict[str, Any]:
    try:
        frames_l = [base64.b64decode(f) for f in req.frames_left_base64]
        frames_r = [base64.b64decode(f) for f in req.frames_right_base64]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 frame data")

    result = mv.calibrate_stereo(
        frames_l, frames_r, req.width, req.height,
        tuple(req.board_size), req.square_size_mm,
    )
    return asdict(result)


# ── Line-scan endpoints ─────────────────────────────────────────────


@router.get("/line-scan/config", dependencies=[Depends(_require)])
async def get_line_scan_config() -> dict[str, Any]:
    return mv.list_line_scan_config()


@router.post("/line-scan/compose", dependencies=[Depends(_require)])
async def compose_line_scan(req: LineScanComposeRequest) -> dict[str, Any]:
    try:
        lines = [base64.b64decode(l) for l in req.lines_base64]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 line data")

    image = mv.compose_line_scan(lines, req.width, req.pixel_format, req.direction, req.line_rate_hz)
    return {
        "width": image.width,
        "height": image.height,
        "pixel_format": image.pixel_format,
        "line_rate_hz": image.line_rate_hz,
        "total_lines": image.total_lines,
        "direction": image.direction,
        "data_size": len(image.pixel_data),
        "image_base64": base64.b64encode(image.pixel_data).decode("ascii"),
    }


# ── PLC integration endpoints ───────────────────────────────────────


@router.get("/plc/context", dependencies=[Depends(_require)])
async def get_plc_context() -> dict[str, Any]:
    ctx = mv.get_plc_context()
    return asdict(ctx)


@router.post("/plc/read", dependencies=[Depends(_require)])
async def read_plc_register(req: PLCReadRequest) -> dict[str, Any]:
    return mv.read_plc_register(req.protocol, req.address)


@router.post("/plc/write", dependencies=[Depends(_require)])
async def write_plc_register(req: PLCWriteRequest) -> dict[str, Any]:
    return mv.write_plc_register(req.protocol, req.address, req.value)


# ── Test recipe endpoints ────────────────────────────────────────────


@router.get("/test-recipes", dependencies=[Depends(_require)])
async def get_test_recipes() -> list[dict[str, Any]]:
    return mv.list_test_recipes()


@router.post("/test-recipes/run", dependencies=[Depends(_require)])
async def run_recipe(req: RunRecipeRequest) -> dict[str, Any]:
    result = mv.run_test_recipe(req.recipe_id)
    return asdict(result)


# ── Artifact & gate endpoints ────────────────────────────────────────


@router.get("/artifacts", dependencies=[Depends(_require)])
async def get_artifacts() -> list[dict[str, Any]]:
    return mv.list_artifacts()


@router.post("/gate/validate", dependencies=[Depends(_require)])
async def validate_gate() -> dict[str, Any]:
    return mv.validate_gate()
