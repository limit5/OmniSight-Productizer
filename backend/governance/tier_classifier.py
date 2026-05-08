"""OP-804 (G2) - pure path-based resolver for ADR-0005 review tiers.

The classifier loads ``configs/governance/tier-paths.yaml`` once at import
time, then exposes pure helpers that operate only on the provided path list.
Hook-layer concerns such as reviewer downgrade rejection stay outside this
module; ``compose_with_existing_label`` only provides the monotonic max helper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

import yaml


Tier = Literal["s", "m", "l", "x"]

_TIER_ORDER: dict[Tier, int] = {"s": 0, "m": 1, "l": 2, "x": 3}
_VALID_TIERS = frozenset(_TIER_ORDER)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "governance" / "tier-paths.yaml"


@dataclass(frozen=True)
class TierPathRules:
    """Compiled path-tier rules from ``tier-paths.yaml``."""

    s_whitelist_globs: tuple[str, ...]
    l_force_upgrade_globs: tuple[str, ...]
    x_force_upgrade_globs: tuple[str, ...]


def _load_tier_path_rules(config_path: Path = DEFAULT_CONFIG_PATH) -> TierPathRules:
    with config_path.open("r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)

    tiers = doc["tiers"]
    return TierPathRules(
        s_whitelist_globs=tuple(tiers["s"]["whitelist_globs"]),
        l_force_upgrade_globs=tuple(tiers["l"]["force_upgrade_globs"]),
        x_force_upgrade_globs=tuple(tiers["x"]["force_upgrade_globs"]),
    )


_RULES = _load_tier_path_rules()


def classify_tier(paths: Iterable[str]) -> Tier:
    """Resolve a patchset's effective tier from changed repo-relative paths."""

    normalized_paths = [_normalize_path(path) for path in paths]
    force_tier = _highest_force_upgrade(normalized_paths, _RULES)
    if force_tier is not None:
        return force_tier
    if normalized_paths and all(
        _matches_any(path, _RULES.s_whitelist_globs)
        for path in normalized_paths
    ):
        return "s"
    return "m"


def compose_with_existing_label(current: Tier, computed: Tier) -> Tier:
    """Return the monotonic maximum of an existing label and computed tier."""

    _validate_tier(current)
    _validate_tier(computed)
    return current if _TIER_ORDER[current] >= _TIER_ORDER[computed] else computed


def path_to_tier_reasons(paths: Iterable[str]) -> dict[str, list[str]]:
    """Explain which path rules matched each changed file."""

    reasons: dict[str, list[str]] = {}
    for raw_path in paths:
        path = _normalize_path(raw_path)
        path_reasons: list[str] = []
        path_reasons.extend(
            f"force-upgrade:x:{glob}"
            for glob in _RULES.x_force_upgrade_globs
            if _glob_matches(path, glob)
        )
        path_reasons.extend(
            f"force-upgrade:l:{glob}"
            for glob in _RULES.l_force_upgrade_globs
            if _glob_matches(path, glob)
        )
        path_reasons.extend(
            f"whitelist:s:{glob}"
            for glob in _RULES.s_whitelist_globs
            if _glob_matches(path, glob)
        )
        if not path_reasons:
            path_reasons.append("default:m:no force-upgrade or whitelist match")
        reasons[path] = path_reasons
    return reasons


def _highest_force_upgrade(paths: Iterable[str], rules: TierPathRules) -> Tier | None:
    highest: Tier | None = None
    for path in paths:
        if _matches_any(path, rules.x_force_upgrade_globs):
            highest = compose_with_existing_label(highest or "s", "x")
        if _matches_any(path, rules.l_force_upgrade_globs):
            highest = compose_with_existing_label(highest or "s", "l")
    return highest


def _matches_any(path: str, globs: Iterable[str]) -> bool:
    return any(_glob_matches(path, glob) for glob in globs)


def _glob_matches(path: str, glob: str) -> bool:
    return re.fullmatch(_glob_to_regex(glob), path) is not None


def _glob_to_regex(glob: str) -> str:
    parts: list[str] = []
    i = 0
    while i < len(glob):
        char = glob[i]
        if char == "*":
            if i + 1 < len(glob) and glob[i + 1] == "*":
                if i + 2 < len(glob) and glob[i + 2] == "/":
                    parts.append("(?:.*/)?")
                    i += 3
                else:
                    parts.append(".*")
                    i += 2
            else:
                parts.append("[^/]*")
                i += 1
        elif char == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(char))
            i += 1
    return "".join(parts)


def _normalize_path(path: str) -> str:
    normalized = str(path).replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _validate_tier(tier: str) -> None:
    if tier not in _VALID_TIERS:
        raise ValueError(f"unknown tier: {tier!r}")
