"""RPG.W14.4 (OP-188) -- task-start system-prompt enrichment from locked talents.

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

W14.4 helper surface
--------------------
This module exports two composition layers:

* :func:`enrich_system_prompt_with_talents` — pure compute helper that
  takes a tuple of :class:`TalentChoice` and appends the reminder
  block. Callers that already have the choices in hand use this.
* :func:`build_talent_prompt_enricher` — production-wiring closure
  that takes a :class:`TalentChoiceStore` and returns an
  ``async (agent_id, system_prompt, guild=None) -> str`` callable so
  dispatch callers don't have to hand-roll the ``store.list_choices``
  lookup. Mirrors the OP-180 (W13.3) ``build_feature_unlock_gate``
  pattern.

Module-global state audit (per project SOP)
-------------------------------------------
Pure compositional helpers. No store reads at import time; callers
fetch the agent's talent rows from their own store and pass them in,
either directly (compute helper) or via the closure built by
:func:`build_talent_prompt_enricher`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.agents.talent_tree import (
    CAPSTONE_SIGNATURE_HEADER,
    TALENT_TREE_PATH,
    CapstoneLock,
    TalentChoice,
    TalentChoiceStore,
    TalentTreeError,
    capstone_signature_block,
    load_talent_tree,
    prompt_reminders_for_talents,
)
from backend.sandbox_tier import Guild


TALENT_REMINDER_HEADER = "Talent reminders (per RPG.W14):"


def enrich_system_prompt_with_talents(
    system_prompt: str,
    talent_choices: tuple[TalentChoice, ...],
    *,
    guild: Guild | str | None = None,
    path: Path | str | None = None,
) -> str:
    """W14.4 (OP-188) -- append a talent-reminder block to ``system_prompt``.

    No-op when ``talent_choices`` is empty so an agent below Lv 10 (no
    milestone locked yet) sees an unmodified prompt. The reminders are
    ordered by milestone ascending so the earliest commitments come
    first.

    ``path`` is forwarded to
    :func:`backend.agents.talent_tree.prompt_reminders_for_talents`
    so unit tests / fixture-driven callers can pin the YAML to a
    non-default location; production callers leave it ``None``.
    """
    if not talent_choices:
        return system_prompt
    if path is not None:
        reminders = prompt_reminders_for_talents(
            talent_choices, guild=guild, path=path,
        )
    else:
        reminders = prompt_reminders_for_talents(talent_choices, guild=guild)
    if not reminders:
        return system_prompt
    bulleted = "\n".join(f"- {line}" for line in reminders)
    block = f"{TALENT_REMINDER_HEADER}\n{bulleted}"
    if not system_prompt:
        return block
    return f"{system_prompt.rstrip()}\n\n{block}"


def build_talent_prompt_enricher(
    store: TalentChoiceStore,
    *,
    path: Path | str | None = None,
) -> Any:
    """W14.4 (OP-188) -- closure that fetches + enriches in one call.

    The returned closure has the shape
    ``async (agent_id, system_prompt, *, guild=None) -> str`` and is
    the production-wiring surface for dispatch code that wants the
    talent reminders appended without hand-rolling the
    :meth:`TalentChoiceStore.list_choices` lookup:

    .. code-block:: python

        from backend.agents.prompt_builder import build_talent_prompt_enricher
        from backend.agents.talent_tree import PostgresTalentChoiceStore

        store = PostgresTalentChoiceStore(conn_factory)
        enrich = build_talent_prompt_enricher(store)

        enriched = await enrich(
            "agent-A",
            base_system_prompt,
            guild=Guild.backend,
        )

    The closure degrades silently and returns the *unmodified*
    ``system_prompt`` on:

    * an empty / missing per-agent talent set,
    * any unexpected error inside ``store.list_choices`` — the
      dispatch path must never crash because the talent layer is
      unreachable.

    Mirrors the OP-180 (W13.3)
    :func:`backend.agents.tool_proficiency.build_feature_unlock_gate`
    pattern: the wiring layer reads the store on each call (no
    caching) so an operator-flip of the agent's talent picks is
    reflected on the next dispatch.
    """

    async def _enrich(
        agent_id: str,
        system_prompt: str,
        *,
        guild: Guild | str | None = None,
    ) -> str:
        try:
            choices = await store.list_choices(agent_id)
        except Exception:  # noqa: BLE001 -- degrade-silently contract
            return system_prompt
        if not choices:
            return system_prompt
        return enrich_system_prompt_with_talents(
            system_prompt,
            tuple(choices),
            guild=guild,
            path=path,
        )

    return _enrich
def enrich_system_prompt_with_capstone(
    system_prompt: str,
    capstone_lock: CapstoneLock | None,
    *,
    guild: Guild | str | None = None,
    path: Path | str = TALENT_TREE_PATH,
) -> str:
    """Append a Lv-80 signature-ability block to ``system_prompt``.

    RPG.W14.6: when ``capstone_lock`` is present the agent has unlocked
    the single signature ability for its Guild — the prompt gets a
    dedicated block (under :data:`CAPSTONE_SIGNATURE_HEADER`) so the
    capstone behaviour is the first thing the model sees alongside the
    per-milestone talent reminders.

    Ordering vs :func:`enrich_system_prompt_with_talents`: callers
    typically run talent enrichment first, then capstone enrichment —
    the signature block lives below the talent reminders so the model
    reads the full ladder (Lv 10 → 80) before the capstone payload.
    No-op when ``capstone_lock`` is ``None`` (agent below Lv 80).
    """
    if capstone_lock is None:
        return system_prompt
    try:
        tree = load_talent_tree(path)
    except TalentTreeError:
        # YAML drift / missing file: degrade silently rather than block
        # the dispatch path. The RPG.W11.1 drift guard catches this at
        # CI time so we only hit it during local hot-edits.
        return system_prompt
    ability = None
    if guild is not None:
        guild_enum = guild if isinstance(guild, Guild) else Guild(guild)
        if guild_enum in tree and tree[guild_enum].capstone.ability_id == capstone_lock.ability_id:
            ability = tree[guild_enum].capstone
    if ability is None:
        # Caller didn't pin the Guild (or pinned the wrong one) — fall
        # back to a scan. Cheap: there are ≤ 21 Guilds even at the
        # eventual BP.B steady state.
        for guild_tree in tree.values():
            if guild_tree.capstone.ability_id == capstone_lock.ability_id:
                ability = guild_tree.capstone
                break
    if ability is None:
        return system_prompt
    block = f"{CAPSTONE_SIGNATURE_HEADER}\n{capstone_signature_block(ability)}"
    if not system_prompt:
        return block
    return f"{system_prompt.rstrip()}\n\n{block}"


__all__ = [
    "CAPSTONE_SIGNATURE_HEADER",
    "TALENT_REMINDER_HEADER",
    "build_talent_prompt_enricher",
    "enrich_system_prompt_with_capstone",
    "enrich_system_prompt_with_talents",
]
