"""Agent management endpoints.

Phase-3-Runtime-v2 SP-3.1 (2026-04-20): ported to native asyncpg +
``Depends(get_conn)`` pool-scoped connections. Every handler carries
a request-scoped ``asyncpg.Connection`` parameter that propagates
to ``_persist()`` and downstream ``db.*`` calls.
"""

from contextlib import asynccontextmanager
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from backend.agents.character_card import (
    CharacterCardRegistry,
    CharacterCardRosterEntry,
    CharacterCardSort,
    CharacterSkillEntry,
    PostgresCharacterCardStore,
    fetch_skill_entries,
)
from backend.agents.guild_hall import GuildHallGuild, build_guild_hall_view
from backend.agents.skill_leveling import (
    PostgresSkillStateStore,
    SkillBranchAlreadyLocked,
    SkillIdNotInMatrix,
    SkillLevelingError,
    lock_branch_choice as lock_skill_branch_choice,
)
from backend.agents.skill_matrix import SkillMatrixDriftError
from backend.agents.talent_tree import (
    CapstoneRequiresLv80,
    MILESTONE_LEVELS,
    MilestoneNotReached,
    PostgresCapstoneStore,
    PostgresTalentChoiceStore,
    TalentAlreadyLocked,
    TalentIdNotInTree,
    TalentTreeError,
    agent_talent_summary,
    available_talents,
    capstone_for_guild,
    lock_capstone_ability,
    lock_talent,
)
from backend.events import emit_agent_update
from backend.models import Agent, AgentCreate, AgentProgress, AgentStatus, AgentWorkspace
from backend.sandbox_tier import Guild
from backend import db
from backend.db_pool import get_conn

router = APIRouter(prefix="/agents", tags=["agents"])

# ── In-memory mirror (kept in sync with DB for fast access by invoke/chat) ──
_agents: dict[str, Agent] = {}


async def seed_defaults_if_empty(conn: asyncpg.Connection) -> None:
    """Seed default agents if the database is empty (called at startup).

    Runs outside a request context — the lifespan handler acquires a
    connection from ``db_pool`` explicitly via ``async with
    get_pool().acquire() as conn:`` and passes it here. Skipping this
    call in SQLite dev mode is the lifespan's responsibility (the
    pool is only initialised when a Postgres DSN is configured).
    """
    if await db.agent_count(conn) > 0:
        # Reload from DB into memory
        for row in await db.list_agents(conn):
            _agents[row["id"]] = _row_to_agent(row)
        return

    defaults = [
        ("firmware-alpha", "Firmware Alpha", "firmware", "idle"),
        ("software-beta", "Software Beta", "software", "idle"),
        ("validator-gamma", "Validator Gamma", "validator", "idle"),
        ("reporter-delta", "Reporter Delta", "reporter", "idle"),
    ]
    for aid, name, atype, status in defaults:
        agent = Agent(
            id=aid,
            name=name,
            type=atype,
            status=status,
            progress=AgentProgress(current=0, total=0),
            thought_chain="Standing by.",
        )
        _agents[aid] = agent
        await db.upsert_agent(conn, _agent_to_row(agent))


def _row_to_agent(row: dict) -> Agent:
    ws = row.get("workspace", {})
    return Agent(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        sub_type=row.get("sub_type", ""),
        status=row["status"],
        progress=AgentProgress(**row.get("progress", {"current": 0, "total": 0})),
        thought_chain=row.get("thought_chain", ""),
        ai_model=row.get("ai_model"),
        sub_tasks=row.get("sub_tasks", []),
        workspace=AgentWorkspace(**ws) if isinstance(ws, dict) and ws else AgentWorkspace(),
    )


def _agent_to_row(agent: Agent) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "type": agent.type.value if hasattr(agent.type, "value") else agent.type,
        "sub_type": agent.sub_type,
        "status": agent.status.value if hasattr(agent.status, "value") else agent.status,
        "progress": agent.progress.model_dump(),
        "thought_chain": agent.thought_chain,
        "ai_model": agent.ai_model,
        "sub_tasks": [st.model_dump() for st in agent.sub_tasks],
        "workspace": agent.workspace.model_dump(),
    }


async def _persist(agent: Agent, conn: asyncpg.Connection | None = None) -> None:
    """Write agent state to both memory and DB.

    Memory-first: the in-memory mirror is updated before the DB write so
    a subsequent read (which hits memory, not DB) reflects the new state
    immediately. If the DB write fails, memory is stale — acceptable
    because ``seed_defaults_if_empty`` on next cold start re-syncs from
    DB, and handlers should NOT catch+swallow DB exceptions.

    Polymorphic on ``conn`` (symmetric with routers/tasks.py::_persist):
    request handlers pass the Depends-injected conn; background workers
    (invoke.py watchdog + stuck-remediation, etc.) call without conn and
    this function acquires a pool-scoped one for the duration of the
    single write. The worker path is required because FastAPI's Depends
    is request-scoped and can't reach asyncio.create_task() children.
    """
    _agents[agent.id] = agent
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            await db.upsert_agent(owned_conn, _agent_to_row(agent))
    else:
        await db.upsert_agent(conn, _agent_to_row(agent))


@router.get("", response_model=list[Agent])
async def list_agents():
    # Reads the in-memory mirror — no DB conn needed.
    return list(_agents.values())


@router.get("/cards")
async def list_agent_cards(
    guild: str | None = None,
    sort_by: CharacterCardSort = "level",
    conn: asyncpg.Connection = Depends(get_conn),
):
    registry = CharacterCardRegistry(
        PostgresCharacterCardStore(lambda: _borrowed_conn(conn))
    )
    try:
        entries = await registry.list_cards(guild=guild, sort_by=sort_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_roster_entry_to_dict(entry) for entry in entries]


@router.get("/guild-hall/{guild}/roster")
async def get_guild_hall_roster(
    guild: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    try:
        selected_guild = Guild(guild.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unknown guild: {guild}") from exc

    registry = CharacterCardRegistry(
        PostgresCharacterCardStore(lambda: _borrowed_conn(conn))
    )
    try:
        entries = await registry.list_cards(
            guild=selected_guild.value,
            sort_by="level",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    view = build_guild_hall_view(entry.card for entry in entries)
    for guild_row in view.guilds:
        if guild_row.guild == selected_guild.value:
            return _guild_hall_guild_to_dict(guild_row)

    raise HTTPException(status_code=404, detail="Guild not found")


@router.get("/{agent_id}/skills")
async def get_agent_skills(
    agent_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """RPG.W12: list per-skill state for the Character Card Skills tab."""
    store = PostgresSkillStateStore(lambda: _borrowed_conn(conn))
    entries = await fetch_skill_entries(store, agent_id)
    return [_skill_entry_to_dict(entry) for entry in entries]


@router.post("/{agent_id}/skills/{skill_id}/branch")
async def lock_agent_skill_branch(
    agent_id: str,
    skill_id: str,
    body: dict,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """RPG.W12: lock the Lv-3 branch_choice for ``(agent_id, skill_id)``.

    Idempotent on identical input; refuses to overwrite a different
    branch with 409 (per ``SkillBranchAlreadyLocked``). Branch values
    must match ``skill_matrix.yaml`` — drift returns 422.
    """
    branch = (body or {}).get("branch") if isinstance(body, dict) else None
    if not isinstance(branch, str) or not branch.strip():
        raise HTTPException(status_code=400, detail="branch is required")
    store = PostgresSkillStateStore(lambda: _borrowed_conn(conn))
    try:
        state = await lock_skill_branch_choice(store, agent_id, skill_id, branch)
    except SkillBranchAlreadyLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SkillIdNotInMatrix as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SkillMatrixDriftError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SkillLevelingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "agent_id": state.agent_id,
        "skill_id": state.skill_id,
        "level": state.level,
        "xp": state.xp,
        "branch_choice": state.branch_choice,
        "last_active_at": state.last_active_at,
    }


@router.get("/{agent_id}/talents")
async def get_agent_talents(
    agent_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """RPG.W14: full talent chain + capstone for the Character Card."""
    talent_store = PostgresTalentChoiceStore(lambda: _borrowed_conn(conn))
    capstone_store = PostgresCapstoneStore(lambda: _borrowed_conn(conn))
    summary = await agent_talent_summary(
        talent_store, agent_id, capstone_store=capstone_store,
    )
    return {
        "agent_id": summary.agent_id,
        "milestones": [int(level) for level in MILESTONE_LEVELS],
        "choices": [
            {
                "milestone_level": choice.milestone_level,
                "talent_id": choice.talent_id,
                "chosen_at": choice.chosen_at,
            }
            for choice in summary.choices
        ],
        "capstone": (
            {
                "ability_id": summary.capstone.ability_id,
                "locked_at": summary.capstone.locked_at,
            }
            if summary.capstone is not None
            else None
        ),
    }


@router.get("/{agent_id}/talents/options")
async def get_agent_talent_options(
    agent_id: str,
    guild: str,
    milestone: int,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """RPG.W14: the 3 picks for one Guild × milestone (read-only)."""
    try:
        options = available_talents(agent_id, guild, milestone)
    except TalentTreeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "agent_id": agent_id,
        "guild": guild,
        "milestone": int(milestone),
        "options": [
            {
                "talent_id": option.talent_id,
                "display_name": option.display_name,
                "summary": option.summary,
                "routing_label": option.routing_label,
            }
            for option in options
        ],
    }


@router.post("/{agent_id}/talents/lock")
async def lock_agent_talent(
    agent_id: str,
    body: dict,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """RPG.W14: lock an immutable talent pick for ``(agent_id, milestone)``.

    Idempotent on identical input; refuses to overwrite a different
    talent with 409 (per :class:`TalentAlreadyLocked`). The agent's
    Lv must already meet the milestone — :class:`MilestoneNotReached`
    returns 409. Talent ids must match ``config/talent_tree.yaml`` —
    drift returns 422.
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    guild = body.get("guild")
    milestone = body.get("milestone")
    talent_id = body.get("talent_id")
    agent_level = body.get("agent_level")
    if not isinstance(guild, str) or not guild.strip():
        raise HTTPException(status_code=400, detail="guild is required")
    if not isinstance(milestone, int) or isinstance(milestone, bool):
        raise HTTPException(status_code=400, detail="milestone must be an integer")
    if not isinstance(talent_id, str) or not talent_id.strip():
        raise HTTPException(status_code=400, detail="talent_id is required")
    if not isinstance(agent_level, int) or isinstance(agent_level, bool):
        raise HTTPException(status_code=400, detail="agent_level must be an integer")

    talent_store = PostgresTalentChoiceStore(lambda: _borrowed_conn(conn))
    try:
        choice = await lock_talent(
            talent_store,
            agent_id,
            guild,
            milestone,
            talent_id,
            agent_level=agent_level,
        )
    except TalentAlreadyLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MilestoneNotReached as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TalentIdNotInTree as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TalentTreeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "agent_id": choice.agent_id,
        "milestone_level": choice.milestone_level,
        "talent_id": choice.talent_id,
        "chosen_at": choice.chosen_at,
    }


@router.post("/{agent_id}/talents/capstone")
async def lock_agent_capstone(
    agent_id: str,
    body: dict,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """RPG.W14: lock the Lv-80 capstone ability after the final pick."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    guild = body.get("guild")
    agent_level = body.get("agent_level")
    if not isinstance(guild, str) or not guild.strip():
        raise HTTPException(status_code=400, detail="guild is required")
    if not isinstance(agent_level, int) or isinstance(agent_level, bool):
        raise HTTPException(status_code=400, detail="agent_level must be an integer")

    talent_store = PostgresTalentChoiceStore(lambda: _borrowed_conn(conn))
    capstone_store = PostgresCapstoneStore(lambda: _borrowed_conn(conn))
    try:
        lock = await lock_capstone_ability(
            capstone_store,
            talent_store,
            agent_id,
            guild,
            agent_level=agent_level,
        )
        ability = capstone_for_guild(guild)
    except CapstoneRequiresLv80 as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TalentAlreadyLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TalentTreeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "agent_id": lock.agent_id,
        "ability_id": lock.ability_id,
        "display_name": ability.display_name,
        "summary": ability.summary,
        "locked_at": lock.locked_at,
    }


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(agent_id: str):
    # Reads the in-memory mirror — no DB conn needed.
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _agents[agent_id]


@router.post("", response_model=Agent, status_code=201)
async def create_agent(
    body: AgentCreate,
    conn: asyncpg.Connection = Depends(get_conn),
):
    # Validate ai_model provider has API key
    if body.ai_model:
        from backend.agents.llm import validate_model_spec
        validation = validate_model_spec(body.ai_model)
        if not validation["valid"]:
            from backend.events import emit_token_warning
            emit_token_warning(
                "warn", f"Agent model warning: {validation['warning']}",
            )

    type_str = body.type.value if hasattr(body.type, "value") else body.type
    agent_id = f"{type_str}-{uuid.uuid4().hex[:6]}"
    agent = Agent(
        id=agent_id,
        name=body.name,
        type=body.type,
        sub_type=body.sub_type,
        status=AgentStatus.booting,
        progress=AgentProgress(current=0, total=0),
        thought_chain="Initializing..." + (f" ⚠ {validation['warning']}" if body.ai_model and not validation.get("valid", True) else ""),
        ai_model=body.ai_model,
    )
    await _persist(agent, conn)
    emit_agent_update(agent_id, agent.status, agent.thought_chain)
    return agent


@router.patch("/{agent_id}", response_model=Agent)
async def update_agent_status(
    agent_id: str,
    status: AgentStatus,
    conn: asyncpg.Connection = Depends(get_conn),
):
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    _agents[agent_id].status = status
    await _persist(_agents[agent_id], conn)
    emit_agent_update(agent_id, status, _agents[agent_id].thought_chain)
    return _agents[agent_id]


@router.post("/{agent_id}/unfreeze", response_model=Agent)
async def unfreeze_agent(
    agent_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Unfreeze an agent that was auto-frozen after exceeding retry limit.

    Resets the agent to idle so it can receive new tasks.
    Called by human maintainers after reviewing the situation.
    """
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = _agents[agent_id]
    agent.status = AgentStatus.idle
    agent.thought_chain = "Unfrozen by human maintainer. Ready for new tasks."
    await _persist(agent, conn)
    emit_agent_update(agent_id, agent.status, agent.thought_chain)
    return agent


@router.post("/{agent_id}/reset", response_model=Agent)
async def force_reset_agent(
    agent_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Force reset any agent to idle, cleaning up workspace and container."""
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = _agents[agent_id]
    agent.status = AgentStatus.idle
    agent.thought_chain = "[RESET] Force reset by operator"
    await _persist(agent, conn)
    emit_agent_update(agent_id, agent.status, agent.thought_chain)
    # Best-effort cleanup of workspace and container
    try:
        from backend.workspace import cleanup
        await cleanup(agent_id)
    except Exception:
        pass
    try:
        from backend.container import stop_container
        await stop_container(agent_id)
    except Exception:
        pass
    return agent


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    emit_agent_update(agent_id, "terminated", "Agent removed")
    del _agents[agent_id]
    await db.delete_agent(conn, agent_id)


@asynccontextmanager
async def _borrowed_conn(conn: asyncpg.Connection):
    yield conn


def _roster_entry_to_dict(entry: CharacterCardRosterEntry) -> dict:
    card = entry.card
    return {
        "agent_id": card.agent_id,
        "agent_class": card.agent_class,
        "instance_suffix": card.instance_suffix,
        "guild": card.guild,
        "level": card.level,
        "xp": card.xp,
        "specialization_label": card.specialization_label,
        "style_fingerprint": card.style_fingerprint,
        "created_at": card.created_at,
        "last_activity_at": entry.last_activity_at,
    }


def _skill_entry_to_dict(entry: CharacterSkillEntry) -> dict:
    return {
        "skill_id": entry.skill_id,
        "level": entry.level,
        "xp": entry.xp,
        "next_level_xp": entry.next_level_xp,
        "branch_choice": entry.branch_choice,
        "last_active_at": entry.last_active_at,
        "branch_choice_required": entry.branch_choice_required,
    }


def _guild_hall_guild_to_dict(guild: GuildHallGuild) -> dict:
    return {
        "guild": guild.guild,
        "display_name": guild.display_name,
        "summary": guild.summary,
        "member_count": guild.member_count,
        "is_empty": guild.is_empty,
        "members": [
            {
                "agent_id": member.agent_id,
                "agent_class": member.agent_class,
                "instance_suffix": member.instance_suffix,
                "level": member.level,
                "xp": member.xp,
                "specialization_label": member.specialization_label,
            }
            for member in guild.members
        ],
        "empty_placeholder": guild.empty_placeholder,
        "recruit_cta": (
            {
                "label": guild.recruit_cta.label,
                "action": guild.recruit_cta.action,
                "guild": guild.recruit_cta.guild,
            }
            if guild.recruit_cta is not None
            else None
        ),
    }
