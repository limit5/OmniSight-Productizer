"""Agent management endpoints."""

import uuid

from fastapi import APIRouter, HTTPException

from backend.models import Agent, AgentCreate, AgentProgress, AgentStatus
from backend.events import emit_agent_update

router = APIRouter(prefix="/agents", tags=["agents"])

# In-memory store (will be replaced by DB / LangGraph state in Phase 3.3)
_agents: dict[str, Agent] = {}


def _seed_defaults() -> None:
    """Seed default agents matching frontend's defaultAgents."""
    defaults = [
        ("firmware-alpha", "Firmware Alpha", "firmware", "idle"),
        ("software-beta", "Software Beta", "software", "idle"),
        ("validator-gamma", "Validator Gamma", "validator", "idle"),
        ("reporter-delta", "Reporter Delta", "reporter", "idle"),
    ]
    for aid, name, atype, status in defaults:
        _agents[aid] = Agent(
            id=aid,
            name=name,
            type=atype,
            status=status,
            progress=AgentProgress(current=0, total=0),
            thought_chain="Standing by.",
        )


_seed_defaults()


@router.get("", response_model=list[Agent])
async def list_agents():
    return list(_agents.values())


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(agent_id: str):
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _agents[agent_id]


@router.post("", response_model=Agent, status_code=201)
async def create_agent(body: AgentCreate):
    agent_id = f"{body.type}-{uuid.uuid4().hex[:6]}"
    agent = Agent(
        id=agent_id,
        name=body.name,
        type=body.type,
        status=AgentStatus.booting,
        progress=AgentProgress(current=0, total=0),
        thought_chain="Initializing...",
        ai_model=body.ai_model,
    )
    _agents[agent_id] = agent
    emit_agent_update(agent_id, agent.status, agent.thought_chain)
    return agent


@router.patch("/{agent_id}", response_model=Agent)
async def update_agent_status(agent_id: str, status: AgentStatus):
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    _agents[agent_id].status = status
    emit_agent_update(agent_id, status, _agents[agent_id].thought_chain)
    return _agents[agent_id]


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str):
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    emit_agent_update(agent_id, "terminated", "Agent removed")
    del _agents[agent_id]
