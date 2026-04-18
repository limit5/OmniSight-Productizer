"""C19 — L4-CORE-19 Imaging / document pipeline endpoints (#240).

REST endpoints for scanner ISP, OCR integration, TWAIN driver
generation, SANE backend generation, and ICC profile embedding.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import imaging_pipeline as ip

_require = _au.require_operator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/imaging", tags=["imaging"])


# ── Request models ───────────────────────────────────────────────────

class ISPPipelineRequest(BaseModel):
    sensor_type: str = Field(..., description="Sensor type (cis, ccd)")
    color_mode: str = Field(..., description="Color mode (grey_8bit, rgb_24bit, etc.)")
    stage_ids: list[str] | None = Field(default=None, description="ISP stage IDs (None = required stages)")
    raw_pixels: list[int] | None = Field(default=None, description="Raw pixel data (None = synthetic)")


class OCRRequest(BaseModel):
    engine_id: str = Field(..., description="OCR engine (tesseract, paddleocr, vendor_sdk)")
    language: str = Field(default="eng", description="OCR language code")
    output_format: str = Field(default="text", description="Output format")
    image_base64: str | None = Field(default=None, description="Base64 image data")


class TWAINTransitionRequest(BaseModel):
    current_state: int = Field(..., description="Current TWAIN state (1-7)")
    target_state: int = Field(..., description="Target TWAIN state (1-7)")


class TWAINDriverRequest(BaseModel):
    device_name: str = Field(..., description="Scanner device name")
    capabilities: list[str] | None = Field(default=None, description="Capability IDs")


class SANEBackendRequest(BaseModel):
    device_name: str = Field(..., description="Scanner device name")
    options: list[str] | None = Field(default=None, description="Option IDs")


class ICCGenerateRequest(BaseModel):
    profile_id: str = Field(..., description="ICC profile ID (srgb, adobe_rgb, grey_gamma22)")


class ICCEmbedRequest(BaseModel):
    output_format: str = Field(..., description="Target format (tiff, jpeg, png, pdf)")
    profile_id: str = Field(..., description="ICC profile ID")
    image_size: int = Field(default=1024, description="Simulated image data size")


class GateValidateRequest(BaseModel):
    artifacts: list[str] = Field(default_factory=list, description="List of artifact IDs")
    required_domains: list[str] | None = Field(default=None, description="Required domains")


class CertGenerateRequest(BaseModel):
    domain: str = Field(default="all", description="Domain (scanner_isp, ocr, icc_profiles, all)")


# ── Scanner ISP endpoints ────────────────────────────────────────────

@router.get("/sensors")
async def get_sensor_types(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in ip.list_sensor_types()]


@router.get("/sensors/{sensor_id}")
async def get_sensor_type(
    sensor_id: str,
    _user: dict = Depends(_require),
) -> dict:
    s = ip.get_sensor_type(sensor_id)
    if s is None:
        raise HTTPException(404, f"Sensor type not found: {sensor_id}")
    return asdict(s)


@router.get("/color-modes")
async def get_color_modes(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(m) for m in ip.list_color_modes()]


@router.get("/isp/stages")
async def get_isp_stages(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in ip.list_isp_stages()]


@router.get("/output-formats")
async def get_output_formats(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(f) for f in ip.list_output_formats()]


@router.post("/isp/run")
async def run_isp_pipeline(
    req: ISPPipelineRequest,
    _user: dict = Depends(_require),
) -> dict:
    result = ip.run_isp_pipeline(
        sensor_type=req.sensor_type,
        color_mode=req.color_mode,
        stage_ids=req.stage_ids,
        raw_pixels=req.raw_pixels,
    )
    return asdict(result)


# ── OCR endpoints ────────────────────────────────────────────────────

@router.get("/ocr/engines")
async def get_ocr_engines(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(e) for e in ip.list_ocr_engines()]


@router.get("/ocr/engines/{engine_id}")
async def get_ocr_engine(
    engine_id: str,
    _user: dict = Depends(_require),
) -> dict:
    e = ip.get_ocr_engine(engine_id)
    if e is None:
        raise HTTPException(404, f"OCR engine not found: {engine_id}")
    return asdict(e)


@router.get("/ocr/preprocessing")
async def get_ocr_preprocessing(
    _user: dict = Depends(_require),
) -> list[dict]:
    return ip.list_ocr_preprocessing()


@router.post("/ocr/run")
async def run_ocr(
    req: OCRRequest,
    _user: dict = Depends(_require),
) -> dict:
    image_data = None
    if req.image_base64:
        import base64
        try:
            image_data = base64.b64decode(req.image_base64)
        except Exception:
            raise HTTPException(400, "Invalid base64 image data")

    result = ip.run_ocr(
        engine_id=req.engine_id,
        image_data=image_data,
        language=req.language,
        output_format=req.output_format,
    )
    return asdict(result)


# ── TWAIN endpoints ──────────────────────────────────────────────────

@router.get("/twain/capabilities")
async def get_twain_capabilities(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(c) for c in ip.list_twain_capabilities()]


@router.get("/twain/states")
async def get_twain_states(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in ip.list_twain_states()]


@router.post("/twain/transition")
async def twain_transition(
    req: TWAINTransitionRequest,
    _user: dict = Depends(_require),
) -> dict:
    valid, msg = ip.twain_transition(req.current_state, req.target_state)
    return {"valid": valid, "message": msg}


@router.post("/twain/generate")
async def generate_twain_driver(
    req: TWAINDriverRequest,
    _user: dict = Depends(_require),
) -> dict:
    template = ip.generate_twain_driver(
        device_name=req.device_name,
        capabilities=req.capabilities,
    )
    return asdict(template)


# ── SANE endpoints ───────────────────────────────────────────────────

@router.get("/sane/options")
async def get_sane_options(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(o) for o in ip.list_sane_options()]


@router.get("/sane/api-functions")
async def get_sane_api_functions(
    _user: dict = Depends(_require),
) -> list[dict]:
    return ip.list_sane_api_functions()


@router.post("/sane/generate")
async def generate_sane_backend(
    req: SANEBackendRequest,
    _user: dict = Depends(_require),
) -> dict:
    template = ip.generate_sane_backend(
        device_name=req.device_name,
        options=req.options,
    )
    return asdict(template)


# ── ICC Profile endpoints ────────────────────────────────────────────

@router.get("/icc/profiles")
async def get_icc_profiles(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(p) for p in ip.list_icc_profiles()]


@router.get("/icc/profiles/{profile_id}")
async def get_icc_profile(
    profile_id: str,
    _user: dict = Depends(_require),
) -> dict:
    p = ip.get_icc_profile(profile_id)
    if p is None:
        raise HTTPException(404, f"ICC profile not found: {profile_id}")
    return asdict(p)


@router.get("/icc/classes")
async def get_icc_classes(
    _user: dict = Depends(_require),
) -> list[dict]:
    return ip.list_icc_profile_classes()


@router.get("/icc/embedding-formats")
async def get_icc_embedding_formats(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(f) for f in ip.list_icc_embedding_formats()]


@router.get("/icc/rendering-intents")
async def get_rendering_intents(
    _user: dict = Depends(_require),
) -> list[dict]:
    return ip.list_rendering_intents()


@router.post("/icc/generate")
async def generate_icc_profile(
    req: ICCGenerateRequest,
    _user: dict = Depends(_require),
) -> dict:
    result = ip.generate_icc_profile_binary(req.profile_id)
    if not result.data:
        raise HTTPException(404, f"ICC profile not found: {req.profile_id}")
    return {
        "profile_id": result.profile_id,
        "profile_class": result.profile_class,
        "size": result.size,
        "checksum": result.checksum,
    }


@router.post("/icc/embed")
async def embed_icc_profile(
    req: ICCEmbedRequest,
    _user: dict = Depends(_require),
) -> dict:
    profile_bin = ip.generate_icc_profile_binary(req.profile_id)
    if not profile_bin.data:
        raise HTTPException(404, f"ICC profile not found: {req.profile_id}")

    image_data = bytes(req.image_size)
    result = ip.embed_icc_profile(image_data, req.output_format, profile_bin.data)
    return asdict(result)


# ── Test recipe endpoints ────────────────────────────────────────────

@router.get("/test-recipes")
async def get_test_recipes(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(r) for r in ip.list_test_recipes()]


@router.post("/test-recipes/{recipe_id}/run")
async def run_test_recipe(
    recipe_id: str,
    _user: dict = Depends(_require),
) -> dict:
    result = ip.run_test_recipe(recipe_id)
    if result.status == "error":
        raise HTTPException(404, f"Test recipe not found: {recipe_id}")
    return asdict(result)


# ── SoC + artifacts + gate ───────────────────────────────────────────

@router.get("/socs")
async def get_compatible_socs(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in ip.list_compatible_socs()]


@router.get("/artifacts")
async def get_artifact_definitions(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(a) for a in ip.list_artifact_definitions()]


@router.post("/validate")
async def validate_imaging_gate(
    req: GateValidateRequest,
    _user: dict = Depends(_require),
) -> dict:
    result = ip.validate_imaging_gate(
        artifacts=req.artifacts,
        required_domains=req.required_domains,
    )
    return asdict(result)


@router.get("/certs")
async def get_imaging_certs(
    _user: dict = Depends(_require),
) -> list[dict]:
    return ip.get_imaging_certs()


@router.post("/certs/generate")
async def generate_certs(
    req: CertGenerateRequest,
    _user: dict = Depends(_require),
) -> dict:
    return ip.generate_cert_artifacts(req.domain)
