"""Phase 62 — Operator-facing endpoints for skill promotion.

Listing pending candidates + promoting/discarding them. Promotion is
gated by `require_admin` because the resulting file lives inside
`configs/skills/` and feeds future agent prompts.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from backend import auth as _au
from backend import skills_extractor  # late attr-lookup so fixture monkeypatch wins

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/skills", tags=["skills"])

_SKILLS_LIVE = Path(__file__).resolve().parent.parent.parent / "configs" / "skills"


def _pending_dir() -> Path:
    """Resolve PENDING_DIR at request time so test fixtures can
    monkey-patch the extractor module."""
    return skills_extractor.PENDING_DIR


def _safe_pending_path(name: str) -> Path:
    """Resolve a candidate filename safely against PENDING_DIR.

    Refuses ``..`` traversal and any path that escapes PENDING_DIR.
    """
    base = _pending_dir().resolve()
    target = (base / name).resolve()
    if base not in target.parents and target != base:
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"pending skill {name!r} not found")
    return target


@router.get("/pending")
async def list_pending(_user=Depends(_au.require_operator)) -> dict:
    """List all skill candidates awaiting promotion."""
    base = _pending_dir()
    if not base.exists():
        return {"items": [], "count": 0}
    items = []
    for p in sorted(base.glob("skill-*.md")):
        items.append({
            "name": p.name,
            "size_bytes": p.stat().st_size,
            "modified_at": p.stat().st_mtime,
        })
    return {"items": items, "count": len(items)}


@router.get("/pending/{name}")
async def read_pending(name: str,
                       _user=Depends(_au.require_operator)) -> JSONResponse:
    """Return the markdown body of a pending skill for review."""
    target = _safe_pending_path(name)
    return JSONResponse({"name": name, "body": target.read_text(encoding="utf-8")})


@router.post("/pending/{name}/promote")
async def promote(name: str,
                  _user=Depends(_au.require_admin)) -> dict:
    """Move a pending candidate into the live skills tree.

    Layout: `configs/skills/<slug>/SKILL.md` (matches the existing
    skill format used by mcp-builder, npu-detection, etc.).
    """
    src = _safe_pending_path(name)
    # Slug is the filename minus prefix and .md extension.
    slug = src.stem.removeprefix("skill-")
    dest_dir = _SKILLS_LIVE / slug
    if dest_dir.exists():
        raise HTTPException(
            status_code=409,
            detail=f"skill {slug!r} already exists in live tree",
        )
    dest_dir.mkdir(parents=True, exist_ok=False)
    dest_file = dest_dir / "SKILL.md"
    shutil.move(str(src), str(dest_file))
    logger.info("promoted skill: %s -> %s", name, dest_file)
    try:
        from backend import metrics as _m
        _m.skill_promoted_total.inc()
    except Exception:
        pass
    try:
        from backend import audit as _audit
        await _audit.log(
            action="skill_promoted",
            entity_kind="skill",
            entity_id=slug,
            after={"path": str(dest_file)},
            actor=getattr(_user, "email", "operator"),
        )
    except Exception as exc:
        logger.debug("audit log for skill_promoted failed: %s", exc)
    return {"slug": slug, "path": str(dest_file)}


@router.delete("/pending/{name}")
async def discard(name: str, _user=Depends(_au.require_admin)) -> dict:
    """Delete a pending candidate without promoting it."""
    target = _safe_pending_path(name)
    target.unlink()
    logger.info("discarded skill candidate: %s", name)
    try:
        from backend import audit as _audit
        await _audit.log(
            action="skill_discarded",
            entity_kind="skill",
            entity_id=name,
            actor=getattr(_user, "email", "operator"),
        )
    except Exception as exc:
        logger.debug("audit log for skill_discarded failed: %s", exc)
    return {"discarded": name}
