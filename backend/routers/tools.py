"""Tool registry endpoint — exposes available tools to the frontend."""

from fastapi import APIRouter

from backend.agents.tools import ALL_TOOLS, AGENT_TOOLS

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("")
async def list_tools():
    """List all available tools with their descriptions."""
    return [
        {
            "name": t.name,
            "description": t.description,
        }
        for t in ALL_TOOLS
    ]


@router.get("/by-agent/{agent_type}")
async def tools_for_agent(agent_type: str):
    """List tools available to a specific agent type."""
    tools = AGENT_TOOLS.get(agent_type, [])
    return [
        {
            "name": t.name,
            "description": t.description,
        }
        for t in tools
    ]
