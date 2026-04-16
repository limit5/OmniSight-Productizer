"""P6 #291 — Mobile compliance gates REST endpoints.

Lightweight REST wrapper around :mod:`backend.mobile_compliance` so
the HMI front-end (and CI orchestration) can list the three P6 gates,
run them on demand, and retrieve the generated privacy labels as
structured JSON. Mirrors the C8 ``compliance`` router's style.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from backend import auth as _au
from backend import mobile_compliance as mc
from backend.mobile_compliance import bundle as mc_bundle

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mobile-compliance", tags=["mobile-compliance"])


_GATE_INFO = [
    {
        "gate_id": "app_store_guidelines",
        "name": "App Store Review Guidelines",
        "description": (
            "Static scan for Guideline 3.1.1 (fake payments), 2.3.10 "
            "(misleading copy), 2.5.1 (private API) and 5.1.1 (missing "
            "privacy usage descriptions)."
        ),
        "platforms": ["ios", "both"],
    },
    {
        "gate_id": "play_policy",
        "name": "Google Play Policy",
        "description": (
            "Static scan for background-location permission justification, "
            "targetSdk floor, and Data Safety form completeness."
        ),
        "platforms": ["android", "both"],
    },
    {
        "gate_id": "privacy_labels",
        "name": "Privacy label generator",
        "description": (
            "Derive iOS App Privacy nutrition label + Play Data Safety "
            "form from detected SDK dependencies."
        ),
        "platforms": ["ios", "android", "both"],
    },
]


@router.get("/gates")
async def list_gates(_user=Depends(_au.require_operator)) -> dict:
    """List the three P6 gates. Shape mirrors C8 ``/compliance/tools``."""
    return {"items": _GATE_INFO, "count": len(_GATE_INFO)}


@router.post("/run")
async def run_bundle(
    payload: dict[str, Any] = Body(...),
    _user=Depends(_au.require_admin),
) -> dict:
    """Run the P6 bundle over ``app_path``.

    Request body:
        {
          "app_path": "/absolute/path/to/mobile/app",
          "platform": "ios" | "android" | "both",   # optional (default: both)
          "min_target_sdk": 35,                      # optional
        }

    Returns the bundle JSON. A non-passing bundle returns 200 too —
    the caller inspects ``passed`` to decide whether to proceed to
    P5 store submission.
    """
    app_path = payload.get("app_path")
    if not app_path or not isinstance(app_path, str):
        raise HTTPException(
            status_code=400,
            detail="`app_path` is required and must be a string",
        )
    platform = payload.get("platform", "both")
    if platform not in ("ios", "android", "both"):
        raise HTTPException(
            status_code=400,
            detail="`platform` must be one of: ios, android, both",
        )
    min_target_sdk = payload.get("min_target_sdk", mc.MIN_TARGET_SDK)
    try:
        min_target_sdk = int(min_target_sdk)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="`min_target_sdk` must be an integer",
        )

    root = Path(app_path).resolve()
    if not root.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Mobile project path not found: {app_path}",
        )

    try:
        bundle = mc.run_all(
            root, platform=platform, min_target_sdk=min_target_sdk,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.error("Mobile compliance bundle failed: %s", exc)
        raise HTTPException(
            status_code=500, detail=f"Bundle execution failed: {exc}"
        )

    # Log into C8 audit chain if the harness is present.
    try:
        from backend import compliance_harness as ch
        report = mc_bundle.bundle_to_compliance_report(bundle)
        await ch.log_compliance_report(report)
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to persist mobile bundle to audit log: %s", exc)

    return bundle.to_dict()


@router.post("/privacy-label")
async def generate_privacy_label(
    payload: dict[str, Any] = Body(...),
    _user=Depends(_au.require_operator),
) -> dict:
    """Generate a privacy label report without running the other gates.

    Useful for previewing a label before shipping a build.
    """
    app_path = payload.get("app_path")
    if not app_path or not isinstance(app_path, str):
        raise HTTPException(
            status_code=400,
            detail="`app_path` is required and must be a string",
        )
    platform = payload.get("platform", "both")
    if platform not in ("ios", "android", "both"):
        raise HTTPException(
            status_code=400,
            detail="`platform` must be one of: ios, android, both",
        )
    root = Path(app_path).resolve()
    if not root.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Mobile project path not found: {app_path}",
        )
    report = mc.generate_privacy_label(root, platform=platform)
    return report.to_dict()
