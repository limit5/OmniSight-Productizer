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
    PostgresCharacterCardStore,
)
from backend.agents.guild_hall import GuildHallGuild, build_guild_hall_view
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
