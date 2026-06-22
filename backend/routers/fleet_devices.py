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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from fastapi import APIRouter, Depends, HTTPException

from backend import auth as _auth
from backend.models import (
    FleetDevice,
    FleetDeviceLaunchAck,
    FleetDeviceManifest,
)

logger = logging.getLogger(__name__)

# Mirrors the SAFE_PATH regex in
# third_party/omnisight-ui/launcher-web/lib/launch.ts and the
# apps.manifest.schema.json $defs.entry.web pattern. Kept in lock-step
# so the productizer's launch-stub gate matches the on-device launcher
# (no eval, no shell, no `javascript:`, no protocol-relative).
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9/_-]*$")

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


def _classify_target(target: str) -> tuple[str, str]:
    """Classify ``target`` into ``("internal"|"external", normalised)``.

    Rejects empty, protocol-relative, non-http(s), and any same-origin
    path that doesn't satisfy the launcher-web SAFE_PATH regex. Raises
    HTTPException(422) on rejection so the caller can render the
    rejection reason verbatim. Mirrors
    third_party/omnisight-ui/launcher-web/lib/launch.ts::classifyTarget.
    """
    if not isinstance(target, str) or not target:
        raise HTTPException(status_code=422, detail="unsafe-target: empty target")
    if target.startswith("//"):
        raise HTTPException(
            status_code=422,
            detail="unsafe-target: protocol-relative URL not allowed",
        )
    if target.startswith("/"):
        if not _SAFE_PATH.match(target):
            raise HTTPException(
                status_code=422,
                detail="unsafe-target: path contains disallowed characters",
            )
        return "internal", target
    # External: must parse as an absolute http(s) URL.
    try:
        parts = urlsplit(target)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="unsafe-target: not an absolute URL",
        )
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(
            status_code=422,
            detail=f"unsafe-target: scheme {parts.scheme!r} not allowed",
        )
    return "external", target


@router.post(
    "/devices/{device_id}/apps/{app_id}/launch",
    response_model=FleetDeviceLaunchAck,
)
async def launch_device_app(device_id: str, app_id: str) -> FleetDeviceLaunchAck:
    """U4.6 OP-2308 — fleet device-launcher command stub.

    Validates the device + app + the app's ``entry.web`` target against
    the same SAFE_PATH / http(s) policy launcher-web enforces on-device,
    then returns a ``dispatched`` ack. The endpoint records the launch
    intent in the request log but does NOT run anything against a real
    remote device — live remote command dispatch is a deferred tier:X
    HIL follow-up (see ticket NOTE). The productizer fleet console
    composes this with a client-side deep-link to present the launcher
    flow end-to-end against the U4.4 fixtures.
    """
    fx = _find_fixture(device_id)
    manifest = fx.get("manifest")
    if not isinstance(manifest, dict):
        raise HTTPException(
            status_code=500,
            detail=f"Fleet device {device_id} fixture is missing 'manifest' block",
        )
    apps = manifest.get("apps")
    if not isinstance(apps, list):
        raise HTTPException(
            status_code=500,
            detail=f"Fleet device {device_id} manifest is missing 'apps' list",
        )
    matching = next(
        (a for a in apps if isinstance(a, dict) and a.get("id") == app_id),
        None,
    )
    if matching is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown app {app_id!r} for device {device_id}",
        )
    entry = matching.get("entry") if isinstance(matching, dict) else None
    web = entry.get("web") if isinstance(entry, dict) else None
    if not isinstance(web, str) or not web:
        # qt-only / process-only apps cannot be remote-launched from
        # the web-side launcher: there's no renderable target. Mirrors
        # the launcher-web `no-web-entry` rejection.
        raise HTTPException(
            status_code=422,
            detail=(
                f"no-web-entry: app {app_id!r} on device {device_id} has no "
                "web/route target (qt-only / process-only)"
            ),
        )
    mode, normalised = _classify_target(web)
    logger.info(
        "fleet device launch stub: device=%s app=%s target=%s mode=%s",
        device_id, app_id, normalised, mode,
    )
    return FleetDeviceLaunchAck(
        device_id=device_id,
        app_id=app_id,
        target=normalised,
        mode=mode,
        status="dispatched",
        dispatched_at=datetime.now(timezone.utc).isoformat(),
    )
