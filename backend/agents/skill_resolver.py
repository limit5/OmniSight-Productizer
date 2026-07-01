"""RPG.W12 skill-xp-accrual EPIC S1 -- deterministic skill_id resolver.

OP-2500 is Stage 1 of the ``skill-xp-accrual`` EPIC: a pure, fail-open
resolver that maps a delivered ticket's label set to a canonical
``skill_id`` when (and only when) the character:'s guild owns that skill.
The skill matrix is guild-keyed by :mod:`backend.agents.skill_matrix`, so a
character may only train skills its guild claims — a backend character
never earns depth-sensing XP even if a bad filing typed ``skill:depth_sensing``.

Design invariants (must not drift):

* **Inert on delivery.** S1 ships without any caller — no runner
  delivery / finalizer path invokes this module, ``award_skill_xp`` is
  not called, no DB write, no character-XP behaviour change. S1 is a
  safe floor; a later stage wires the accrual once policy is settled.
* **Pure.** No I/O other than loading the canonical skill matrix YAML
  (which is what ``skill_matrix.load_skill_matrix`` already does).
* **Fail-open.** Every failure mode (unknown character, missing skill
  label, off-matrix skill, off-guild skill, exception loading the matrix,
  malformed labels iterable) collapses to ``None``. Never raises. A
  future caller therefore treats "resolver returned None" as "do nothing"
  and cannot be wedged by a bad label.
* **Guild-constrained by construction.** The set of allowed skills is
  computed from ``skill_matrix.load_skill_matrix()[character.guild]`` —
  the resolver does not reimplement the guild eligibility table.

See :mod:`backend.agents.character_registry` for the character→(brain,
guild, tier) source of truth and :mod:`backend.agents.skill_matrix` for
the canonical skill matrix (ADR-0008).
"""

from __future__ import annotations

from typing import Iterable

from backend.agents import character_registry
from backend.agents.skill_matrix import load_skill_matrix
from backend.sandbox_tier import Guild


_SKILL_LABEL_PREFIX = "skill:"


def resolve_skill_for_character(labels: Iterable[str] | None) -> str | None:
    """Return the ``skill_id`` a ticket's labels should accrue to, or ``None``.

    Returns the ``skill_id`` iff ALL of the following hold:

    (a) a ``character:<slug>`` label resolves to a known character in
        :data:`backend.agents.character_registry.CHARACTERS` (via
        :func:`character_registry.character_from_labels`);
    (b) a ``skill:<skill_id>`` label is present in ``labels``;
    (c) the ``skill_id`` is declared in the canonical skill matrix
        (guild-keyed :mod:`backend.agents.skill_matrix`); AND
    (d) the ``skill_id`` is registered under the character's guild in
        that matrix — the guild-constrained invariant.

    Otherwise returns ``None``. Pure and fail-open — any exception
    (matrix parse failure, malformed labels iterable, unknown guild
    enum, ...) is swallowed and returns ``None`` so a bad label cannot
    wedge downstream code.
    """

    try:
        char = character_registry.character_from_labels(labels)
        if char is None:
            return None
        skill_id = _extract_skill_label(labels)
        if skill_id is None:
            return None
        try:
            matrix = load_skill_matrix()
        except Exception:
            return None
        try:
            guild = Guild(char.guild)
        except ValueError:
            return None
        guild_skills = matrix.get(guild)
        if not guild_skills:
            return None
        allowed = {definition.skill_id for definition in guild_skills}
        if skill_id not in allowed:
            return None
        return skill_id
    except Exception:
        return None


def _extract_skill_label(labels: Iterable[str] | None) -> str | None:
    """Return the first ``skill:<value>`` value, or ``None``.

    Follows the same shape as ``character_from_labels``: strips
    whitespace, ignores non-string entries, and returns ``None`` on any
    malformed or empty value.
    """

    if labels is None:
        return None
    for label in labels:
        if not isinstance(label, str):
            continue
        if not label.startswith(_SKILL_LABEL_PREFIX):
            continue
        value = label.split(":", 1)[1].strip()
        if value:
            return value
        return None
    return None


__all__ = ["resolve_skill_for_character"]
