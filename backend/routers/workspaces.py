"""Workspace + Container management endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.workspace import (
    provision,
    finalize,
    cleanup,
    get_workspace,
    list_workspaces,
)
from backend.container import (
    start_container,
    stop_container,
    get_container,
    list_containers,
    ensure_image,
)
from backend.routers.agents import _agents
from backend.models import AgentWorkspace

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class ProvisionRequest(BaseModel):
    agent_id: str
    task_id: str
    repo_url: str | None = None  # None = use main project repo


@router.post("/provision")
async def provision_workspace(body: ProvisionRequest):
    """Create an isolated workspace for an agent."""
    agent = _agents.get(body.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        info = await provision(body.agent_id, body.task_id, body.repo_url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Provisioning failed: {exc}")

    # Update agent model with workspace info
    agent.workspace = AgentWorkspace(
        branch=info.branch,
        path=str(info.path),
        status="active",
        task_id=info.task_id,
    )

    return {
        "status": "provisioned",
        "agent_id": info.agent_id,
        "branch": info.branch,
        "path": str(info.path),
        "task_id": info.task_id,
    }


@router.post("/finalize/{agent_id}")
async def finalize_workspace(agent_id: str):
    """Finalize workspace: commit changes, generate diff summary."""
    info = get_workspace(agent_id)
    if not info:
        raise HTTPException(status_code=404, detail="No active workspace for this agent")

    result = await finalize(agent_id)

    # Update agent model
    agent = _agents.get(agent_id)
    if agent:
        agent.workspace.status = "finalized"
        agent.workspace.commit_count = result.get("commit_count", 0)

    return result


@router.post("/cleanup/{agent_id}")
async def cleanup_workspace(agent_id: str):
    """Remove workspace and clean up."""
    ok = await cleanup(agent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="No workspace to clean for this agent")

    # Update agent model
    agent = _agents.get(agent_id)
    if agent:
        agent.workspace = AgentWorkspace()

    return {"status": "cleaned", "agent_id": agent_id}


@router.get("")
async def list_all_workspaces():
    """List all active workspaces."""
    return [
        {
            "agent_id": ws.agent_id,
            "task_id": ws.task_id,
            "branch": ws.branch,
            "path": str(ws.path),
            "status": ws.status,
            "created_at": ws.created_at,
            "commit_count": ws.commit_count,
        }
        for ws in list_workspaces()
    ]


@router.get("/containers")
async def list_active_containers():
    """List all running agent containers."""
    return [
        {
            "agent_id": c.agent_id,
            "container_id": c.container_id,
            "container_name": c.container_name,
            "image": c.image,
            "status": c.status,
            "workspace": str(c.workspace_path),
            "created_at": c.created_at,
        }
        for c in list_containers()
    ]


@router.get("/{agent_id}")
async def get_workspace_info(agent_id: str):
    """Get workspace details for a specific agent."""
    info = get_workspace(agent_id)
    if not info:
        raise HTTPException(status_code=404, detail="No workspace for this agent")
    container = get_container(agent_id)
    return {
        "agent_id": info.agent_id,
        "task_id": info.task_id,
        "branch": info.branch,
        "path": str(info.path),
        "status": info.status,
        "created_at": info.created_at,
        "commit_count": info.commit_count,
        "container": {
            "id": container.container_id,
            "name": container.container_name,
            "image": container.image,
            "status": container.status,
        } if container else None,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Docker container endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/container/start/{agent_id}")
async def start_agent_container(agent_id: str):
    """Start a Docker container for an agent (workspace must exist first)."""
    ws = get_workspace(agent_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Provision a workspace first")

    try:
        info = await start_container(agent_id, ws.path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "status": "started",
        "agent_id": agent_id,
        "container_id": info.container_id,
        "container_name": info.container_name,
        "image": info.image,
        "workspace_mounted": str(ws.path),
    }


@router.post("/container/stop/{agent_id}")
async def stop_agent_container(agent_id: str):
    """Stop and remove an agent's container."""
    ok = await stop_container(agent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="No container for this agent")
    return {"status": "stopped", "agent_id": agent_id}


@router.post("/container/build-image")
async def build_agent_image():
    """Build the agent Docker image (omnisight-agent:latest)."""
    ok = await ensure_image()
    if not ok:
        raise HTTPException(status_code=500, detail="Image build failed")
    return {"status": "ready", "image": "omnisight-agent:latest"}
