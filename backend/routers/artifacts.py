"""Artifact management endpoints — list, download, delete generated reports."""

import logging
from pathlib import Path

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)
from fastapi.responses import FileResponse

from backend import db
from backend.db_pool import get_conn
from backend.routers import _pagination as _pg
from backend.tenant_fs import tenant_artifacts_root, tenants_root

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

# Legacy path — kept so we can still serve files that haven't been migrated yet.
_LEGACY_ARTIFACTS_ROOT = Path(__file__).resolve().parent.parent.parent / ".artifacts"


def get_artifacts_root(tenant_id: str | None = None) -> Path:
    """Return the tenant-scoped artifacts directory (auto-created)."""
    return tenant_artifacts_root(tenant_id)


def _is_valid_artifact_path(file_path: Path) -> bool:
    """Allow files inside any tenant artifacts dir OR the legacy .artifacts/ dir."""
    resolved = file_path.resolve()
    for root in (tenants_root().resolve(), _LEGACY_ARTIFACTS_ROOT.resolve()):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


@router.get("")
async def list_artifacts(
    task_id: str = "",
    agent_id: str = "",
    limit: int = _pg.Limit(default=50, max_cap=200),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """List artifacts, optionally filtered by task or agent."""
    return await db.list_artifacts(conn, task_id=task_id, agent_id=agent_id, limit=limit)


@router.get("/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Get artifact metadata."""
    artifact = await db.get_artifact(conn, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


@router.get("/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Download artifact file."""
    artifact = await db.get_artifact(conn, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    file_path = Path(artifact["file_path"]).resolve()
    if not _is_valid_artifact_path(file_path):
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
async def delete_artifact(
    artifact_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Delete artifact metadata and file."""
    artifact = await db.get_artifact(conn, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    file_path = Path(artifact["file_path"]).resolve()
    if _is_valid_artifact_path(file_path):
        if file_path.exists():
            file_path.unlink(missing_ok=True)
    else:
        logger.warning("Artifact %s file_path outside artifacts root — skipping file deletion", artifact_id)

    await db.delete_artifact(conn, artifact_id)
