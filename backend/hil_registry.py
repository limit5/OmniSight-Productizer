"""C7 — L4-CORE-07 HIL plugin registry (#216).

Central registry that maps plugin names to concrete ``HILPlugin`` classes,
and validates that skill packs' declared HIL requirements are satisfiable.

Skill pack integration
----------------------
A skill pack may declare required HIL plugins in its ``skill.yaml``::

    hil_plugins:
      - camera
      - audio

The registry checks whether all declared plugins are registered and
available before a skill pack's HIL tests can run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Type

import yaml

from backend.hil_plugin import (
    HILPlugin,
    PluginInfo,
    PluginRunSummary,
    run_plugin_lifecycle,
)

logger = logging.getLogger(__name__)

_BUILTIN_PLUGINS: dict[str, Type[HILPlugin]] = {}


def register_builtin(name: str, cls: Type[HILPlugin]) -> None:
    """Register a built-in HIL plugin class by name."""
    _BUILTIN_PLUGINS[name] = cls


def _register_defaults() -> None:
    """Register all built-in family plugins."""
    from backend.hil_plugins.camera import CameraHILPlugin
    from backend.hil_plugins.audio import AudioHILPlugin
    from backend.hil_plugins.display import DisplayHILPlugin

    register_builtin("camera", CameraHILPlugin)
    register_builtin("audio", AudioHILPlugin)
    register_builtin("display", DisplayHILPlugin)


_register_defaults()


@dataclass
class HILRequirement:
    plugin_name: str
    metrics: list[str] = field(default_factory=list)
    criteria: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class HILValidationResult:
    skill_name: str
    ok: bool
    missing_plugins: list[str] = field(default_factory=list)
    missing_metrics: dict[str, list[str]] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def list_registered_plugins() -> dict[str, PluginInfo]:
    """Return info for all registered built-in plugins."""
    result: dict[str, PluginInfo] = {}
    for name, cls in _BUILTIN_PLUGINS.items():
        instance = cls()
        result[name] = instance.plugin_info
    return result


def get_plugin_class(name: str) -> Optional[Type[HILPlugin]]:
    return _BUILTIN_PLUGINS.get(name)


def create_plugin(name: str, **kwargs: Any) -> HILPlugin:
    """Instantiate a registered plugin by name."""
    cls = _BUILTIN_PLUGINS.get(name)
    if cls is None:
        raise KeyError(f"HIL plugin {name!r} not registered")
    return cls(**kwargs)


def parse_skill_hil_requirements(skill_dir: Path) -> list[HILRequirement]:
    """Parse HIL plugin requirements from a skill pack's skill.yaml.

    Looks for a ``hil_plugins`` key in the manifest. Accepts two formats:

    Simple (list of names)::

        hil_plugins:
          - camera
          - audio

    Extended (with metrics and criteria)::

        hil_plugins:
          - name: camera
            metrics: [focus_sharpness, stream_latency]
            criteria:
              focus_sharpness: {min: 100}
              stream_latency: {max: 200}
    """
    manifest_path = skill_dir / "skill.yaml"
    if not manifest_path.exists():
        return []

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return []
    if not isinstance(raw, dict):
        return []

    hil_list = raw.get("hil_plugins", [])
    if not isinstance(hil_list, list):
        return []

    requirements: list[HILRequirement] = []
    for item in hil_list:
        if isinstance(item, str):
            requirements.append(HILRequirement(plugin_name=item))
        elif isinstance(item, dict) and "name" in item:
            requirements.append(HILRequirement(
                plugin_name=item["name"],
                metrics=item.get("metrics", []),
                criteria=item.get("criteria", {}),
            ))
    return requirements


def validate_skill_hil(
    skill_name: str,
    skill_dir: Path,
) -> HILValidationResult:
    """Check that a skill pack's HIL requirements are satisfiable.

    Verifies:
    1. All declared plugin names are registered.
    2. All declared metrics are supported by those plugins.
    """
    reqs = parse_skill_hil_requirements(skill_dir)
    missing_plugins: list[str] = []
    missing_metrics: dict[str, list[str]] = {}
    issues: list[str] = []

    for req in reqs:
        cls = get_plugin_class(req.plugin_name)
        if cls is None:
            missing_plugins.append(req.plugin_name)
            continue

        if req.metrics:
            instance = cls()
            unsupported = [
                m for m in req.metrics if not instance.supports_metric(m)
            ]
            if unsupported:
                missing_metrics[req.plugin_name] = unsupported
                issues.append(
                    f"plugin {req.plugin_name!r} does not support metrics: "
                    f"{unsupported}"
                )

    if missing_plugins:
        issues.insert(0, f"missing HIL plugins: {missing_plugins}")

    ok = not missing_plugins and not missing_metrics
    return HILValidationResult(
        skill_name=skill_name,
        ok=ok,
        missing_plugins=missing_plugins,
        missing_metrics=missing_metrics,
        issues=issues,
    )


def run_skill_hil(
    skill_name: str,
    skill_dir: Path,
    **plugin_kwargs: Any,
) -> list[PluginRunSummary]:
    """Run all HIL plugins required by a skill pack.

    For each requirement, instantiates the plugin, runs the declared
    metrics with criteria, and returns summaries.
    """
    reqs = parse_skill_hil_requirements(skill_dir)
    summaries: list[PluginRunSummary] = []

    for req in reqs:
        plugin = create_plugin(req.plugin_name, **plugin_kwargs)
        metrics_and_criteria: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

        if req.metrics:
            for metric in req.metrics:
                criteria = req.criteria.get(metric, {})
                metrics_and_criteria.append((metric, {}, criteria))
        else:
            for metric in plugin.plugin_info.supported_metrics:
                metrics_and_criteria.append((metric, {}, {}))

        summary = run_plugin_lifecycle(plugin, metrics_and_criteria)
        summaries.append(summary)

    return summaries
