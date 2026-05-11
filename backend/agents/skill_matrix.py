"""RPG.W11.2 -- canonical RPG skill matrix and skill_id drift guard.

ADR-0008 names the canonical skill matrix YAML as the owner of the RPG
``skill_id`` namespace. This module keeps that source of truth under the RPG
backend package and exposes a small validator hidden/CI drift guards can call
without importing routers or touching persistent state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import yaml

from backend.sandbox_tier import Guild


SKILL_MATRIX_PATH = Path(__file__).with_name("skill_matrix.yaml")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILL_ID_SCAN_ROOTS = (
    REPO_ROOT / "configs",
)

_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_YAML_SUFFIXES = frozenset({".yaml", ".yml"})


class SkillMatrixError(ValueError):
    """Raised when the canonical RPG skill matrix is malformed."""


class SkillMatrixDriftError(RuntimeError):
    """Raised when a discovered ``skill_id`` is absent from the matrix."""


@dataclass(frozen=True)
class SkillBranchDefinition:
    """One Lv-3 fork option for a base skill (RPG.W12)."""

    branch_id: str
    display_name: str
    summary: str


@dataclass(frozen=True)
class SkillDefinition:
    """One canonical RPG skill axis."""

    guild: Guild
    skill_id: str
    display_name: str
    summary: str
    branches: tuple[SkillBranchDefinition, ...] = ()


def load_skill_matrix(
    path: Path | str = SKILL_MATRIX_PATH,
) -> Mapping[Guild, tuple[SkillDefinition, ...]]:
    """Load the canonical RPG skill matrix YAML."""

    matrix_path = Path(path)
    raw = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SkillMatrixError("RPG skill matrix YAML must be a mapping")
    if raw.get("schema_version") != 1:
        raise SkillMatrixError("RPG skill matrix schema_version must be 1")

    skills = raw.get("skills")
    if not isinstance(skills, dict):
        raise SkillMatrixError("RPG skill matrix must contain a skills mapping")

    seen: set[str] = set()
    rows: dict[Guild, tuple[SkillDefinition, ...]] = {}
    for guild_name, entries in skills.items():
        guild = _coerce_guild(guild_name)
        if not isinstance(entries, list) or not entries:
            raise SkillMatrixError(
                f"RPG skill matrix guild {guild.value!r} must list skills"
            )
        parsed: list[SkillDefinition] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise SkillMatrixError(
                    f"RPG skill matrix entry for {guild.value!r} must be a mapping"
                )
            skill_id = _clean_skill_id(entry.get("skill_id"))
            if skill_id in seen:
                raise SkillMatrixError(
                    f"duplicate RPG skill_id in canonical matrix: {skill_id!r}"
                )
            seen.add(skill_id)
            parsed.append(
                SkillDefinition(
                    guild=guild,
                    skill_id=skill_id,
                    display_name=_required_text(
                        entry.get("display_name"),
                        "display_name",
                    ),
                    summary=_required_text(entry.get("summary"), "summary"),
                    branches=_parse_branches(entry.get("branches"), skill_id),
                )
            )
        rows[guild] = tuple(parsed)

    return MappingProxyType(rows)


def canonical_skill_ids(
    path: Path | str = SKILL_MATRIX_PATH,
) -> frozenset[str]:
    """Return every ``skill_id`` owned by the canonical RPG matrix."""

    return frozenset(
        skill.skill_id
        for definitions in load_skill_matrix(path).values()
        for skill in definitions
    )


def canonical_branches_for_skill(
    skill_id: str,
    *,
    path: Path | str = SKILL_MATRIX_PATH,
) -> tuple[SkillBranchDefinition, ...]:
    """Return the canonical Lv-3 branches for ``skill_id`` (W12)."""

    skill_id = _clean_skill_id(skill_id)
    for definitions in load_skill_matrix(path).values():
        for skill in definitions:
            if skill.skill_id == skill_id:
                return skill.branches
    raise SkillMatrixDriftError(
        f"RPG skill_id {skill_id!r} is not declared in the canonical skill matrix"
    )


def assert_branch_choice_in_matrix(
    skill_id: str,
    branch_id: str,
    *,
    path: Path | str = SKILL_MATRIX_PATH,
) -> None:
    """Raise if ``branch_id`` is not a declared Lv-3 fork of ``skill_id``.

    The drift guard supports W12's invariant that
    ``agent_skill_state.branch_choice`` is bounded by the canonical
    ``skill_matrix.yaml`` branches list. The base skill must already
    exist in the matrix (otherwise this raises with the same
    :class:`SkillMatrixDriftError` the legacy guard uses).
    """

    branches = canonical_branches_for_skill(skill_id, path=path)
    if not branches:
        raise SkillMatrixDriftError(
            f"RPG skill_id {skill_id!r} has no branches declared in the matrix"
        )
    clean = _clean_branch_id(branch_id)
    allowed = {branch.branch_id for branch in branches}
    if clean not in allowed:
        raise SkillMatrixDriftError(
            f"RPG branch_choice {branch_id!r} for skill {skill_id!r} is not in "
            f"the canonical matrix; expected one of {sorted(allowed)}"
        )


def discover_declared_skill_ids(
    scan_roots: Iterable[Path | str] = DEFAULT_SKILL_ID_SCAN_ROOTS,
) -> frozenset[str]:
    """Discover existing YAML ``skill_id`` declarations under ``scan_roots``."""

    discovered: set[str] = set()
    for root in scan_roots:
        root_path = Path(root)
        if root_path.is_file():
            paths = (root_path,)
        elif root_path.is_dir():
            paths = (
                path
                for path in root_path.rglob("*")
                if path.is_file() and path.suffix in _YAML_SUFFIXES
            )
        else:
            continue
        for path in paths:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            _collect_skill_ids(raw, discovered)
    return frozenset(discovered)


def missing_skill_ids_from_matrix(
    discovered_skill_ids: Iterable[str] | None = None,
    *,
    matrix_path: Path | str = SKILL_MATRIX_PATH,
) -> tuple[str, ...]:
    """Return discovered ``skill_id`` values absent from the canonical matrix."""

    discovered = (
        discover_declared_skill_ids()
        if discovered_skill_ids is None
        else frozenset(_clean_skill_id(skill_id) for skill_id in discovered_skill_ids)
    )
    missing = discovered - canonical_skill_ids(matrix_path)
    return tuple(sorted(missing))


def assert_skill_id_space_within_matrix(
    discovered_skill_ids: Iterable[str] | None = None,
    *,
    matrix_path: Path | str = SKILL_MATRIX_PATH,
) -> None:
    """Raise if any discovered ``skill_id`` is missing from the matrix YAML."""

    missing = missing_skill_ids_from_matrix(
        discovered_skill_ids,
        matrix_path=matrix_path,
    )
    if missing:
        raise SkillMatrixDriftError(
            "RPG skill_id space drifted outside canonical skill matrix YAML: "
            f"{list(missing)}"
        )


def _collect_skill_ids(raw: Any, discovered: set[str]) -> None:
    if isinstance(raw, dict):
        value = raw.get("skill_id")
        if isinstance(value, str):
            discovered.add(_clean_skill_id(value))
        for child in raw.values():
            _collect_skill_ids(child, discovered)
    elif isinstance(raw, list):
        for child in raw:
            _collect_skill_ids(child, discovered)


def _coerce_guild(value: Any) -> Guild:
    if not isinstance(value, str):
        raise SkillMatrixError("RPG skill matrix guild keys must be strings")
    try:
        return Guild(value.strip())
    except ValueError as exc:
        raise SkillMatrixError(f"unknown RPG Guild in skill matrix: {value!r}") from exc


def _clean_skill_id(value: Any) -> str:
    if not isinstance(value, str):
        raise SkillMatrixError("RPG skill_id must be a string")
    skill_id = value.strip()
    if not _SKILL_ID_RE.match(skill_id):
        raise SkillMatrixError(
            f"RPG skill_id {value!r} must match {_SKILL_ID_RE.pattern}"
        )
    return skill_id


def _clean_branch_id(value: Any) -> str:
    if not isinstance(value, str):
        raise SkillMatrixError("RPG branch_id must be a string")
    branch_id = value.strip()
    if not _SKILL_ID_RE.match(branch_id):
        raise SkillMatrixError(
            f"RPG branch_id {value!r} must match {_SKILL_ID_RE.pattern}"
        )
    return branch_id


def _parse_branches(
    value: Any,
    skill_id: str,
) -> tuple[SkillBranchDefinition, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise SkillMatrixError(
            f"RPG skill {skill_id!r} branches must be a non-empty list when present"
        )
    seen: set[str] = set()
    parsed: list[SkillBranchDefinition] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise SkillMatrixError(
                f"RPG skill {skill_id!r} branch entry must be a mapping"
            )
        branch_id = _clean_branch_id(entry.get("branch_id"))
        if branch_id in seen:
            raise SkillMatrixError(
                f"duplicate branch_id {branch_id!r} for skill {skill_id!r}"
            )
        seen.add(branch_id)
        parsed.append(
            SkillBranchDefinition(
                branch_id=branch_id,
                display_name=_required_text(
                    entry.get("display_name"),
                    "branch display_name",
                ),
                summary=_required_text(entry.get("summary"), "branch summary"),
            )
        )
    return tuple(parsed)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SkillMatrixError(f"RPG skill matrix {field} must be a string")
    text = value.strip()
    if not text:
        raise SkillMatrixError(f"RPG skill matrix {field} must be non-empty")
    return text


assert_skill_id_space_within_matrix()


__all__ = [
    "DEFAULT_SKILL_ID_SCAN_ROOTS",
    "SKILL_MATRIX_PATH",
    "SkillBranchDefinition",
    "SkillDefinition",
    "SkillMatrixDriftError",
    "SkillMatrixError",
    "assert_branch_choice_in_matrix",
    "assert_skill_id_space_within_matrix",
    "canonical_branches_for_skill",
    "canonical_skill_ids",
    "discover_declared_skill_ids",
    "load_skill_matrix",
    "missing_skill_ids_from_matrix",
]
