"""U4.4 OP-2306 — Fleet device-registry API.

    GET  /api/v1/fleet/devices              → registered devices + metadata
    GET  /api/v1/fleet/devices/{id}         → one device's metadata
    GET  /api/v1/fleet/devices/{id}/manifest → that device's apps.manifest

Fixture-backed: each device is one ``configs/fleet_devices/*.yaml``
file with ``{id, name, model, renderer, last_seen?, manifest_ref}``
plus an embedded ``manifest`` (apps.manifest body that validates
against third_party/omnisight-ui/design-system/apps.manifest.schema.json).
The launcher-web / fleet console (U4.5) reads these endpoints to
render the device list and per-device tile catalog.

Live device fetch/sync is a deferred tier:X follow-up — this is the
data plane only, no DB and no device-side polling.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException

from backend import auth as _auth
from backend.models import FleetDevice, FleetDeviceManifest

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_FLEET_DEVICES_DIR = _PROJECT_ROOT / "configs" / "fleet_devices"


router = APIRouter(
    prefix="/fleet",
    tags=["fleet"],
    dependencies=[Depends(_auth.current_user)],
)


def _load_device_fixture(path: Path) -> dict[str, Any]:
    """Read one device fixture YAML — raises on malformed input."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"fleet device fixture {path.name} is not a mapping")
    return data


def _load_all_fixtures() -> list[dict[str, Any]]:
    """Discover every ``configs/fleet_devices/*.yaml`` and return the
    parsed payloads in stable filename order."""
    if not _FLEET_DEVICES_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(_FLEET_DEVICES_DIR.glob("*.yaml")):
        try:
            out.append(_load_device_fixture(p))
        except Exception:
            logger.warning("Failed to read fleet device fixture %s", p, exc_info=True)
    return out


def _device_metadata(fx: dict[str, Any]) -> FleetDevice:
    """Project a raw fixture payload into the FleetDevice response model
    (drops the embedded ``manifest`` block — that's served separately)."""
    return FleetDevice(
        id=fx["id"],
        name=fx["name"],
        model=fx["model"],
        renderer=fx["renderer"],
        last_seen=fx.get("last_seen"),
        manifest_ref=fx["manifest_ref"],
    )


def _find_fixture(device_id: str) -> dict[str, Any]:
    """Look up one device fixture by id; raise 404 if absent."""
    for fx in _load_all_fixtures():
        if fx.get("id") == device_id:
            return fx
    raise HTTPException(status_code=404, detail=f"Unknown fleet device: {device_id}")


@router.get("/devices", response_model=list[FleetDevice])
async def list_devices() -> list[FleetDevice]:
    return [_device_metadata(fx) for fx in _load_all_fixtures()]


@router.get("/devices/{device_id}", response_model=FleetDevice)
async def get_device(device_id: str) -> FleetDevice:
    return _device_metadata(_find_fixture(device_id))


@router.get("/devices/{device_id}/manifest", response_model=FleetDeviceManifest)
async def get_device_manifest(device_id: str) -> FleetDeviceManifest:
    fx = _find_fixture(device_id)
    manifest = fx.get("manifest")
    if not isinstance(manifest, dict):
        raise HTTPException(
            status_code=500,
            detail=f"Fleet device {device_id} fixture is missing 'manifest' block",
        )
    return FleetDeviceManifest(**manifest)
