"""OTA Server — manifest hosting + phased rollout management.

Scaffold for the server-side OTA update infrastructure. Provides manifest
creation, signing, and phased rollout with health gate evaluation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ImageDescriptor:
    image_id: str
    target_partition: str
    url: str
    sha256: str
    size_bytes: int
    delta_from_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "image_id": self.image_id,
            "target_partition": self.target_partition,
            "url": self.url,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }
        if self.delta_from_version:
            d["delta_from_version"] = self.delta_from_version
        return d


@dataclass
class UpdateManifest:
    manifest_id: str = ""
    firmware_version: str = ""
    min_firmware_version: str = ""
    release_notes: str = ""
    images: list[ImageDescriptor] = field(default_factory=list)
    signature: str = ""
    signature_scheme: str = "ed25519_direct"
    rollout_strategy: str = "immediate"
    created_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "firmware_version": self.firmware_version,
            "min_firmware_version": self.min_firmware_version,
            "release_notes": self.release_notes,
            "images": [i.to_dict() for i in self.images],
            "signature": self.signature,
            "signature_scheme": self.signature_scheme,
            "rollout_strategy": self.rollout_strategy,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def create_manifest(
    firmware_version: str,
    images: list[ImageDescriptor],
    signature_scheme: str = "ed25519_direct",
    rollout_strategy: str = "immediate",
    min_firmware_version: str = "",
    release_notes: str = "",
) -> UpdateManifest:
    """Create and sign an update manifest."""
    manifest_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    payload = json.dumps({
        "manifest_id": manifest_id,
        "firmware_version": firmware_version,
        "images": [i.to_dict() for i in images],
    }, sort_keys=True)
    signature = hashlib.sha256(payload.encode()).hexdigest()

    return UpdateManifest(
        manifest_id=manifest_id,
        firmware_version=firmware_version,
        min_firmware_version=min_firmware_version,
        release_notes=release_notes,
        images=images,
        signature=f"sim-sig:{signature[:48]}",
        signature_scheme=signature_scheme,
        rollout_strategy=rollout_strategy,
        created_at=now,
    )


def check_device_eligibility(
    device_version: str,
    manifest: UpdateManifest,
) -> bool:
    """Check if a device is eligible for the update."""
    # TODO: Semantic version comparison
    if manifest.min_firmware_version and device_version < manifest.min_firmware_version:
        return False
    if device_version >= manifest.firmware_version:
        return False
    return True
