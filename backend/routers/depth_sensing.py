"""Depth / 3D sensing pipeline REST endpoints."""
from __future__ import annotations

import base64
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import auth as _au
from backend import depth_sensing as ds

_require = _au.require_operator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/depth", tags=["depth"])


# ── Request models ───────────────────────────────────────────────────

class CaptureRequest(BaseModel):
    sensor_id: str
    exposure_us: int = 1000
    modulation_freq_mhz: float = 20.0


class StereoRequest(BaseModel):
    left_frame_base64: str
    right_frame_base64: str
    width: int
    height: int
    algorithm: str = "sgbm"
    num_disparities: int = 128
    block_size: int = 5
    baseline: float = 0.12
    focal_length: float = 500.0


class StructuredLightRequest(BaseModel):
    pattern_type: str
    captured_frames_base64: list[str]
    baseline: float = 0.1
    focal_length: float = 500.0


class PointCloudRequest(BaseModel):
    depth_frame_base64: str
    width: int
    height: int
    backend: str = "open3d"
    camera_matrix: list[list[float]] | None = None


class FilterRequest(BaseModel):
    points: list[list[float]]
    filter_type: str
    params: dict = {}


class RegistrationRequest(BaseModel):
    source_points: list[list[float]]
    target_points: list[list[float]]
    algorithm: str = "icp_point_to_point"
    initial_transform: list[list[float]] | None = None


class CalibrateRequest(BaseModel):
    calibration_type: str
    frames_base64: list[str] = []
    pattern_size: list[int] = [9, 6]
    square_size: float = 0.025
    reference_distances: list[float] = []


class RunRecipeRequest(BaseModel):
    recipe_id: str


class ValidateSceneRequest(BaseModel):
    scene_id: str


# ── Sensor endpoints ────────────────────────────────────────────────

@router.get("/sensors", dependencies=[Depends(_require)])
async def get_sensors() -> list[dict[str, Any]]:
    sensors = ds.list_sensors()
    return [asdict(s) for s in sensors]


@router.get("/sensors/{sensor_id}/capabilities", dependencies=[Depends(_require)])
async def get_sensor_capabilities(sensor_id: str) -> dict[str, Any]:
    try:
        return ds.get_sensor_capabilities(sensor_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown sensor: {sensor_id}")


@router.post("/sensors/capture", dependencies=[Depends(_require)])
async def capture_depth_frame(req: CaptureRequest) -> dict[str, Any]:
    try:
        result = ds.capture_depth_frame(
            sensor_id=req.sensor_id,
            exposure_us=req.exposure_us,
            modulation_freq_mhz=req.modulation_freq_mhz,
        )
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Capture failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Structured light endpoints ──────────────────────────────────────

@router.get("/structured-light/patterns", dependencies=[Depends(_require)])
async def get_structured_light_patterns() -> list[dict[str, Any]]:
    return ds.list_structured_light_patterns()


@router.post("/structured-light/decode", dependencies=[Depends(_require)])
async def decode_structured_light(req: StructuredLightRequest) -> dict[str, Any]:
    try:
        frames = []
        for b64 in req.captured_frames_base64:
            frames.append(base64.b64decode(b64))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 frame data")

    try:
        result = ds.decode_structured_light(
            pattern_type=req.pattern_type,
            captured_frames=frames,
            baseline=req.baseline,
            focal_length=req.focal_length,
        )
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Structured light decode failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Stereo endpoints ────────────────────────────────────────────────

@router.get("/stereo/algorithms", dependencies=[Depends(_require)])
async def get_stereo_algorithms() -> list[dict[str, Any]]:
    return ds.list_stereo_algorithms()


@router.post("/stereo/disparity", dependencies=[Depends(_require)])
async def compute_stereo_disparity(req: StereoRequest) -> dict[str, Any]:
    try:
        left_frame = base64.b64decode(req.left_frame_base64)
        right_frame = base64.b64decode(req.right_frame_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 frame data")

    try:
        result = ds.compute_stereo_disparity(
            left_frame=left_frame,
            right_frame=right_frame,
            width=req.width,
            height=req.height,
            algorithm=req.algorithm,
            num_disparities=req.num_disparities,
            block_size=req.block_size,
            baseline=req.baseline,
            focal_length=req.focal_length,
        )
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Stereo disparity computation failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Point cloud endpoints ───────────────────────────────────────────

@router.get("/point-cloud/backends", dependencies=[Depends(_require)])
async def get_point_cloud_backends() -> list[dict[str, Any]]:
    return ds.list_point_cloud_backends()


@router.post("/point-cloud/generate", dependencies=[Depends(_require)])
async def generate_point_cloud(req: PointCloudRequest) -> dict[str, Any]:
    try:
        depth_frame = base64.b64decode(req.depth_frame_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 depth frame data")

    try:
        result = ds.generate_point_cloud(
            depth_frame=depth_frame,
            width=req.width,
            height=req.height,
            backend=req.backend,
            camera_matrix=req.camera_matrix,
        )
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Point cloud generation failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/point-cloud/filter", dependencies=[Depends(_require)])
async def filter_point_cloud(req: FilterRequest) -> dict[str, Any]:
    try:
        result = ds.filter_point_cloud(
            points=req.points,
            filter_type=req.filter_type,
            params=req.params,
        )
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Point cloud filtering failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/point-cloud/export", dependencies=[Depends(_require)])
async def export_point_cloud(req: PointCloudRequest) -> dict[str, Any]:
    try:
        depth_frame = base64.b64decode(req.depth_frame_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 depth frame data")

    try:
        result = ds.export_point_cloud(
            depth_frame=depth_frame,
            width=req.width,
            height=req.height,
            backend=req.backend,
            camera_matrix=req.camera_matrix,
        )
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Point cloud export failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Registration endpoints ──────────────────────────────────────────

@router.get("/registration/algorithms", dependencies=[Depends(_require)])
async def get_registration_algorithms() -> list[dict[str, Any]]:
    return ds.list_registration_algorithms()


@router.post("/registration/register", dependencies=[Depends(_require)])
async def register_point_clouds(req: RegistrationRequest) -> dict[str, Any]:
    try:
        result = ds.register_point_clouds(
            source_points=req.source_points,
            target_points=req.target_points,
            algorithm=req.algorithm,
            initial_transform=req.initial_transform,
        )
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Point cloud registration failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── SLAM endpoints ──────────────────────────────────────────────────

@router.get("/slam/types", dependencies=[Depends(_require)])
async def get_slam_types() -> list[dict[str, Any]]:
    return ds.list_slam_types()


# ── Calibration endpoints ───────────────────────────────────────────

@router.get("/calibration/types", dependencies=[Depends(_require)])
async def get_calibration_types() -> list[dict[str, Any]]:
    return ds.list_calibration_types()


@router.post("/calibration/calibrate", dependencies=[Depends(_require)])
async def run_calibration(req: CalibrateRequest) -> dict[str, Any]:
    try:
        frames = []
        for b64 in req.frames_base64:
            frames.append(base64.b64decode(b64))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 frame data")

    try:
        result = ds.run_calibration(
            calibration_type=req.calibration_type,
            frames=frames,
            pattern_size=req.pattern_size,
            square_size=req.square_size,
            reference_distances=req.reference_distances,
        )
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Calibration failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Test scene endpoints ────────────────────────────────────────────

@router.get("/test-scenes", dependencies=[Depends(_require)])
async def get_test_scenes() -> list[dict[str, Any]]:
    return ds.list_test_scenes()


@router.get("/test-scenes/{scene_id}", dependencies=[Depends(_require)])
async def get_test_scene(scene_id: str) -> dict[str, Any]:
    try:
        return ds.generate_test_scene(scene_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown scene: {scene_id}")


@router.post("/test-scenes/validate", dependencies=[Depends(_require)])
async def validate_scene(req: ValidateSceneRequest) -> dict[str, Any]:
    try:
        return ds.validate_test_scene(req.scene_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Test recipe endpoints ───────────────────────────────────────────

@router.get("/test-recipes", dependencies=[Depends(_require)])
async def get_test_recipes() -> list[dict[str, Any]]:
    return ds.list_test_recipes()


@router.post("/test-recipes/run", dependencies=[Depends(_require)])
async def run_recipe(req: RunRecipeRequest) -> dict[str, Any]:
    result = ds.run_test_recipe(req.recipe_id)
    return asdict(result)


# ── Artifact & gate endpoints ───────────────────────────────────────

@router.get("/artifacts", dependencies=[Depends(_require)])
async def get_artifacts() -> list[dict[str, Any]]:
    return ds.list_artifacts()


@router.post("/gate/validate", dependencies=[Depends(_require)])
async def validate_gate() -> dict[str, Any]:
    return ds.validate_gate()
