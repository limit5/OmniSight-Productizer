"""Config loader for generated-file auto-resolvers (OP-782)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml


@dataclass(frozen=True)
class AutoResolveRule:
    """One generated file and the script that can regenerate it."""

    path: str
    resolver: str
    description: str = ""


def load_auto_resolve_config(
    path: Path,
    *,
    log: Callable[..., None] | None = None,
) -> dict[str, AutoResolveRule]:
    """Load ``.gerrit/auto-resolve.yaml`` as a path-keyed registry.

    Duplicate paths are tolerated per OP-782: first entry wins and a
    warning is emitted so config drift is visible without breaking the
    whole rebase sweep.
    """
    logger = log or (lambda *args, **kwargs: None)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("auto_resolved_files") or []
    if not isinstance(entries, list):
        raise ValueError("auto_resolved_files must be a list")

    rules: dict[str, AutoResolveRule] = {}
    for idx, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"auto_resolved_files[{idx}] must be a mapping")
        file_path = _normalise_relpath(str(item.get("path") or ""))
        resolver = _normalise_relpath(str(item.get("resolver") or ""))
        description = str(item.get("description") or "")
        if not file_path:
            raise ValueError(f"auto_resolved_files[{idx}].path is required")
        if not resolver:
            raise ValueError(f"auto_resolved_files[{idx}].resolver is required")
        if file_path in rules:
            logger(
                "WARN",
                "auto_resolve_duplicate_path",
                path=file_path,
                ignored_resolver=resolver,
                kept_resolver=rules[file_path].resolver,
            )
            continue
        rules[file_path] = AutoResolveRule(
            path=file_path,
            resolver=resolver,
            description=description,
        )
    return rules


def _normalise_relpath(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in Path(value).parts:
        return ""
    return value
