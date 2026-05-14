"""Plugin registry and loader for phase governance rules."""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from types import ModuleType

from governance_engine.plugins.base import BasePhasePlugin, PhasePlugin, PluginError
from governance_engine.schema.v1 import TicketContractV1

logger = logging.getLogger(__name__)

DEFAULT_PLUGIN_MODULES = [
    f"governance_engine.plugins.phase_31{phase}" for phase in "abcdefghijk"
]


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, PhasePlugin] = {}

    def register(self, plugin: PhasePlugin) -> None:
        self._plugins[plugin.phase_id] = plugin

    def get(self, phase_id: str) -> PhasePlugin | None:
        return self._plugins.get(phase_id)

    def all(self) -> list[PhasePlugin]:
        return list(self._plugins.values())

    def validate_all(self, ticket: TicketContractV1) -> list[PluginError]:
        errors: list[PluginError] = []
        for plugin in self.all():
            if plugin.applies_to(ticket):
                errors.extend(plugin.validate(ticket))
        return errors


def _module_name_for_path(path: Path) -> str:
    return f"_governance_plugin_{abs(hash(path.resolve()))}_{path.stem}"


def _import_module_from_file(path: Path) -> ModuleType:
    module_name = _module_name_for_path(path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iter_imported_modules(path: str) -> list[ModuleType]:
    candidate = Path(path)
    if candidate.is_dir():
        return [
            _import_module_from_file(module_path)
            for module_path in sorted(candidate.glob("*.py"))
            if module_path.name != "__init__.py"
        ]
    if candidate.is_file():
        return [_import_module_from_file(candidate)]
    return [importlib.import_module(path)]


def _register_module_plugins(registry: PluginRegistry, module: ModuleType) -> None:
    for value in vars(module).values():
        if (
            isinstance(value, type)
            and issubclass(value, BasePhasePlugin)
            and value is not BasePhasePlugin
        ):
            registry.register(value())


def load_plugins_from_module_paths(paths: list[str]) -> PluginRegistry:
    registry = PluginRegistry()
    for path in paths:
        try:
            modules = _iter_imported_modules(path)
        except ImportError as exc:
            logger.warning("Could not import plugin module %s: %s", path, exc)
            continue
        for module in modules:
            _register_module_plugins(registry, module)
    return registry


DEFAULT_REGISTRY = load_plugins_from_module_paths(DEFAULT_PLUGIN_MODULES)

__all__ = [
    "DEFAULT_REGISTRY",
    "DEFAULT_PLUGIN_MODULES",
    "PluginRegistry",
    "load_plugins_from_module_paths",
]
