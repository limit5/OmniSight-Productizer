"""Artifact management endpoints — list, download, delete generated reports."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
from fastapi.responses import FileResponse

from backend import db
from backend.routers import _pagination as _pg

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

# Centralized artifact storage (persists after workspace cleanup)
_ARTIFACTS_ROOT = Path(__file__).resolve().parent.parent.parent / ".artifacts"


def get_artifacts_root() -> Path:
    _ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    return _ARTIFACTS_ROOT


@router.get("")
async def list_artifacts(task_id: str = "", agent_id: str = "", limit: int = _pg.Limit(default=50, max_cap=200)):
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
    try:
        file_path.relative_to(artifacts_root)
    except ValueError:
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
        "sdk": "application/octet-stream",
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

    # Remove file if it exists and is within artifacts root
    file_path = Path(artifact["file_path"]).resolve()
    artifacts_root = get_artifacts_root().resolve()
    try:
        file_path.relative_to(artifacts_root)
        if file_path.exists():
            file_path.unlink(missing_ok=True)
    except ValueError:
        logger.warning("Artifact %s file_path outside artifacts root — skipping file deletion", artifact_id)

    await db.delete_artifact(artifact_id)
