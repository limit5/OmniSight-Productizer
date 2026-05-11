"""RPG.W14 -- task-start system-prompt enrichment from locked talents.

ADR-0008 §"Talent tree (W14)" requires that talent picks influence
two downstream surfaces: MP routing weight (handled in
:mod:`backend.agents.routing_policy`) and the per-task system prompt
(handled here).

Why a new module and not :mod:`backend.agents.system_prompt_builder`?
The existing module is the tool-catalog injector — single-purpose and
narrow on purpose. Talent reminders are a separate composition step
that runs further upstream (per agent, not per dispatch) and may grow
to include W13 tool-proficiency reminders + W17 party-buff reminders.
Keeping it in its own module lets each helper evolve independently.

Module-global state audit (per project SOP)
-------------------------------------------
Pure compositional helpers. No store reads at import time; callers
fetch the agent's talent rows from their own store and pass them in.
"""

from __future__ import annotations

from backend.agents.talent_tree import (
    TalentChoice,
    prompt_reminders_for_talents,
)
from backend.sandbox_tier import Guild


TALENT_REMINDER_HEADER = "Talent reminders (per RPG.W14):"


def enrich_system_prompt_with_talents(
    system_prompt: str,
    talent_choices: tuple[TalentChoice, ...],
    *,
    guild: Guild | str | None = None,
) -> str:
    """Append a talent-reminder block to ``system_prompt``.

    No-op when ``talent_choices`` is empty so an agent below Lv 10 (no
    milestone locked yet) sees an unmodified prompt. The reminders are
    ordered by milestone ascending so the earliest commitments come
    first.
    """
    if not talent_choices:
        return system_prompt
    reminders = prompt_reminders_for_talents(talent_choices, guild=guild)
    if not reminders:
        return system_prompt
    bulleted = "\n".join(f"- {line}" for line in reminders)
    block = f"{TALENT_REMINDER_HEADER}\n{bulleted}"
    if not system_prompt:
        return block
    return f"{system_prompt.rstrip()}\n\n{block}"


__all__ = [
    "TALENT_REMINDER_HEADER",
    "enrich_system_prompt_with_talents",
]
