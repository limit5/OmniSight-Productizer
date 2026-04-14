"""Phase 59 — Host-Native target detection helpers.

Single source of truth for "is this project a same-arch host-native
build?" + "should we shortcut the pipeline to the 4-phase app_only
flow?". Used by:

  - decision_engine.propose() injects is_host_native into source so
    chooser confidence can step up
  - forecast.py reads project_track to pick task templates
  - pipeline.py (Phase 59 v2 — out of scope for this commit) will
    branch on app_only_pipeline()

Reads `hardware_manifest.yaml` lazily; results cached for 60s so the
hot path doesn't hit disk on every decision propose.
"""

from __future__ import annotations

import logging
import os
import platform
import time
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CACHE_TTL_S = 60.0
_cache: tuple[float, dict] | None = None


def _read_manifest() -> dict:
    """Read + cache hardware_manifest.yaml. Returns {} on any error."""
    global _cache
    now = time.time()
    if _cache and (now - _cache[0]) < _CACHE_TTL_S:
        return _cache[1]
    path = _PROJECT_ROOT / "configs" / "hardware_manifest.yaml"
    if not path.exists():
        _cache = (now, {})
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("manifest read failed: %s", exc)
        data = {}
    _cache = (now, data)
    return data


def _bust_cache() -> None:
    global _cache
    _cache = None


_HOST_ARCH_ALIASES = {
    "x86_64": "x86_64", "amd64": "x86_64",
    "aarch64": "arm64", "arm64": "arm64",
    "armv7l": "arm32", "armv7": "arm32",
    "riscv64": "riscv64",
}


def host_arch_canonical() -> str:
    raw = platform.machine().lower().strip()
    return _HOST_ARCH_ALIASES.get(raw, raw)


def target_platform_id() -> str:
    """The `project.target_platform` value from manifest, or empty."""
    return ((_read_manifest().get("project") or {}).get("target_platform") or "").strip()


def project_track() -> str:
    """The `project.project_track` value from manifest, or empty."""
    return ((_read_manifest().get("project") or {}).get("project_track") or "").strip().lower()


def is_host_native() -> bool:
    """True iff target_platform == 'host_native', i.e. operator
    explicitly opted into the same-arch fast path. We don't auto-detect
    because some embedded SoCs identify as x86_64 but still need a
    vendor toolchain (e.g. proprietary RT extensions)."""
    return target_platform_id() == "host_native"


def should_use_app_only_pipeline() -> bool:
    """True when the project should run the reduced 4-phase pipeline.
    Currently gated on `project_track == 'app_only'`; future logic
    might also enable it for tiny algo-only projects."""
    return project_track() == "app_only"


def app_only_phases() -> list[str]:
    """The reduced NPI sequence used when should_use_app_only_pipeline()."""
    return ["concept", "build", "test", "deploy"]


def host_device_passthrough() -> str:
    """Returns the configured accelerator passthrough mode for sandbox
    container creation. Empty string means none (default).
    Recognised values: 'hailo' | 'movidius' | 'usb' | 'none' | ''."""
    return (os.environ.get("OMNISIGHT_HOST_DEVICE_PASSTHROUGH", "") or "").strip().lower()


def context_dict() -> dict:
    """Convenience: compact dict of host-native flags suitable for
    spreading into decision.source or forecast.from_manifest input."""
    return {
        "is_host_native": is_host_native(),
        "project_track": project_track(),
        "host_arch": host_arch_canonical(),
        "target_platform": target_platform_id(),
        "app_only_pipeline": should_use_app_only_pipeline(),
        "device_passthrough": host_device_passthrough(),
    }
