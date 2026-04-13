"""Artifact management endpoints — list, download, delete generated reports."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend import db

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

# Centralized artifact storage (persists after workspace cleanup)
_ARTIFACTS_ROOT = Path(__file__).resolve().parent.parent.parent / ".artifacts"
_ARTIFACTS_ROOT.mkdir(exist_ok=True)


def get_artifacts_root() -> Path:
    return _ARTIFACTS_ROOT


@router.get("")
async def list_artifacts(task_id: str = "", agent_id: str = "", limit: int = 50):
    """List artifacts, optionally filtered by task or agent."""
    return await db.list_artifacts(task_id=task_id, agent_id=agent_id, limit=limit)


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str):
    """Get artifact metadata."""
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


@router.get("/{artifact_id}/download")
async def download_artifact(artifact_id: str):
    """Download artifact file."""
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    file_path = Path(artifact["file_path"]).resolve()
    artifacts_root = get_artifacts_root().resolve()
    if not str(file_path).startswith(str(artifacts_root)):
        raise HTTPException(status_code=403, detail="Access denied: file outside artifact storage")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing from disk")

    media_types = {
        "pdf": "application/pdf",
        "markdown": "text/markdown",
        "json": "application/json",
        "log": "text/plain",
        "html": "text/html",
        "binary": "application/octet-stream",
        "firmware": "application/octet-stream",
        "kernel_module": "application/octet-stream",
        "sdk": "application/gzip",
        "model": "application/octet-stream",
        "archive": "application/gzip",
    }
    media = media_types.get(artifact.get("type", ""), "application/octet-stream")
    return FileResponse(file_path, media_type=media, filename=artifact["name"])


@router.delete("/{artifact_id}", status_code=204)
async def delete_artifact(artifact_id: str):
    """Delete artifact metadata and file."""
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Remove file if it exists
    file_path = Path(artifact["file_path"])
    if file_path.exists():
        file_path.unlink(missing_ok=True)

    await db.delete_artifact(artifact_id)
