"""RPG.W19.1 -- deterministic Lv5 skill fusion helpers.

ADR-0008 treats RPG skill fusion as a derived capability, not persistence.
Callers pass two observed skill levels and receive a hybrid skill only when
both component skills are mastered and an explicit recipe exists.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants, frozen dataclasses, and a read-only
recipe registry only. It performs no database access, no filesystem access,
and no cross-worker mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

FUSION_REQUIRED_LEVEL = 5
FUSION_COMPONENT_LEVEL_DECREMENT = 1
HYBRID_INITIAL_LEVEL = 3


@dataclass(frozen=True)
class SkillLevel:
    """Observed RPG level for one named skill."""

    name: str
    level: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _clean_skill_name(self.name))
        object.__setattr__(self, "level", _clean_skill_level(self.level))


@dataclass(frozen=True)
class HybridSkillRecipe:
    """Explicit recipe for fusing two Lv5 skills into one hybrid skill."""

    skill_a: str
    skill_b: str
    hybrid_skill: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_a", _clean_skill_name(self.skill_a))
        object.__setattr__(self, "skill_b", _clean_skill_name(self.skill_b))
        object.__setattr__(
            self,
            "hybrid_skill",
            _clean_skill_name(self.hybrid_skill),
        )
        if self.skill_a == self.skill_b:
            raise ValueError("fusion recipe requires two distinct skills")

    @property
    def key(self) -> tuple[str, str]:
        """Return the order-insensitive recipe key."""

        return _recipe_key(self.skill_a, self.skill_b)


@dataclass(frozen=True)
class HybridSkillFusion:
    """Result of one successful skill fusion check."""

    hybrid_skill: str
    component_skills: tuple[str, str]
    required_level: int = FUSION_REQUIRED_LEVEL
    component_skill_levels: tuple[SkillLevel, ...] = ()
    hybrid_skill_level: int = HYBRID_INITIAL_LEVEL
    component_level_decrement: int = FUSION_COMPONENT_LEVEL_DECREMENT


def list_hybrid_skill_recipes() -> tuple[HybridSkillRecipe, ...]:
    """Return all registered hybrid skill recipes in stable order."""

    return tuple(sorted(HYBRID_SKILL_RECIPES.values(), key=lambda r: r.hybrid_skill))


def can_fuse_skills(
    skill_a: SkillLevel | Mapping[str, Any] | Any,
    skill_b: SkillLevel | Mapping[str, Any] | Any,
) -> bool:
    """Return whether two skills qualify for a registered hybrid fusion."""

    return fuse_skills(skill_a, skill_b) is not None


def fuse_skills(
    skill_a: SkillLevel | Mapping[str, Any] | Any,
    skill_b: SkillLevel | Mapping[str, Any] | Any,
) -> HybridSkillFusion | None:
    """Return the hybrid skill if both inputs are Lv5 and recipe-backed.

    Inputs may be :class:`SkillLevel`, mappings, or objects with ``name`` and
    ``level`` attributes. Skill ordering is ignored.
    """

    left = _normalise_skill_level(skill_a)
    right = _normalise_skill_level(skill_b)
    if left.name == right.name:
        return None
    if left.level != FUSION_REQUIRED_LEVEL or right.level != FUSION_REQUIRED_LEVEL:
        return None

    recipe = HYBRID_SKILL_RECIPES.get(_recipe_key(left.name, right.name))
    if recipe is None:
        return None
    return HybridSkillFusion(
        hybrid_skill=recipe.hybrid_skill,
        component_skills=recipe.key,
        component_skill_levels=_post_fusion_component_levels(left, right),
    )


def _normalise_skill_level(skill: SkillLevel | Mapping[str, Any] | Any) -> SkillLevel:
    if isinstance(skill, SkillLevel):
        return skill
    if isinstance(skill, Mapping):
        values = skill
    else:
        values = {
            name: getattr(skill, name)
            for name in ("name", "skill", "skill_name", "level")
            if hasattr(skill, name)
        }

    raw_name = values.get("name", values.get("skill", values.get("skill_name")))
    if raw_name is None:
        raise ValueError("skill must include name, skill, or skill_name")
    if "level" not in values:
        raise ValueError("skill must include level")
    return SkillLevel(name=raw_name, level=values["level"])


def _recipe_key(skill_a: str, skill_b: str) -> tuple[str, str]:
    return tuple(sorted((_clean_skill_name(skill_a), _clean_skill_name(skill_b))))


def _post_fusion_component_levels(
    skill_a: SkillLevel,
    skill_b: SkillLevel,
) -> tuple[SkillLevel, SkillLevel]:
    levels = {
        skill_a.name: skill_a.level - FUSION_COMPONENT_LEVEL_DECREMENT,
        skill_b.name: skill_b.level - FUSION_COMPONENT_LEVEL_DECREMENT,
    }
    return tuple(
        SkillLevel(name=name, level=levels[name])
        for name in sorted(levels)
    )


def _clean_skill_name(name: Any) -> str:
    if not isinstance(name, str):
        raise TypeError("skill name must be a string")
    clean = name.strip().lower()
    if not clean:
        raise ValueError("skill name must be non-empty")
    return clean


def _clean_skill_level(level: Any) -> int:
    if isinstance(level, bool) or not isinstance(level, int):
        raise TypeError("skill level must be an int")
    if level < 1:
        raise ValueError("skill level must be >= 1")
    return level


_RECIPES: tuple[HybridSkillRecipe, ...] = (
    HybridSkillRecipe(
        skill_a="python-perf",
        skill_b="python-types",
        hybrid_skill="type-safe-fast-python",
    ),
)

HYBRID_SKILL_RECIPES: Mapping[tuple[str, str], HybridSkillRecipe] = MappingProxyType(
    {recipe.key: recipe for recipe in _RECIPES}
)


__all__ = [
    "FUSION_COMPONENT_LEVEL_DECREMENT",
    "FUSION_REQUIRED_LEVEL",
    "HYBRID_SKILL_RECIPES",
    "HYBRID_INITIAL_LEVEL",
    "HybridSkillFusion",
    "HybridSkillRecipe",
    "SkillLevel",
    "can_fuse_skills",
    "fuse_skills",
    "list_hybrid_skill_recipes",
]
