"""BP.M.3 -- REST surface for auto-distilled skill drafts.

Surface
-------
* ``GET    /auto-skills``              -- list tenant-scoped drafts
* ``POST   /auto-skills``              -- create a draft manually
* ``GET    /auto-skills/{id}``         -- detail
* ``PATCH  /auto-skills/{id}``         -- edit draft metadata/body
* ``DELETE /auto-skills/{id}``         -- delete an unpromoted draft
* ``POST   /auto-skills/{id}/review``  -- mark draft as human-reviewed
* ``POST   /auto-skills/{id}/promote`` -- write production skill pack

Module-global state audit: this router keeps only immutable constants.
The review/promote lifecycle is coordinated through PG row locks and the
production skill pack is derived from the locked row, so every worker
observes the same source of truth.
"""

from __future__ import annotations

import re
import hashlib
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend import auth as _au
from backend.db_context import current_tenant_id, set_tenant_id

router = APIRouter(prefix="/auto-skills", tags=["auto-skills"])
logger = logging.getLogger(__name__)

_SKILLS_LIVE = Path(__file__).resolve().parent.parent.parent / "configs" / "skills"
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class AutoSkillCreate(BaseModel):
    skill_name: str = Field(..., max_length=128)
    source_task_id: str | None = Field(default=None, max_length=256)
    markdown_content: str = Field(..., min_length=1, max_length=262_144)


class AutoSkillUpdate(BaseModel):
    skill_name: str | None = Field(default=None, max_length=128)
    source_task_id: str | None = Field(default=None, max_length=256)
    markdown_content: str | None = Field(
        default=None, min_length=1, max_length=262_144,
    )
    expected_version: int | None = Field(default=None, ge=1)


def _validate_skill_name(name: str) -> str:
    candidate = name.strip()
    if not _SKILL_NAME_RE.match(candidate):
        raise HTTPException(
            status_code=400,
            detail=(
                "skill_name must be lowercase slug text matching "
                "[a-z0-9][a-z0-9_-]{0,127}"
            ),
        )
    return candidate


def _row(row: Any) -> dict:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "skill_name": row["skill_name"],
        "source_task_id": row["source_task_id"],
        "markdown_content": row["markdown_content"],
        "version": int(row["version"]),
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _actor_tenant(user: _au.User) -> str:
    return user.tenant_id or "t-default"


def _markdown_sha256(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


async def _fetch_owned(conn: Any, skill_id: str, tenant_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, tenant_id, skill_name, source_task_id, markdown_content, "
        "version, status, created_at "
        "FROM auto_distilled_skills WHERE id = $1 AND tenant_id = $2",
        skill_id,
        tenant_id,
    )
    return _row(row) if row else None


async def _emit_promotion_audit(
    skill: dict,
    *,
    path: Path,
    actor: str,
) -> None:
    """Best-effort Phase D traceability row for reviewed skill promotion.

    The durable auto_distilled_skills row supplies the tenant context, so
    the audit chain is coordinated through PG rather than process state.
    """

    saved = current_tenant_id()
    try:
        set_tenant_id(skill["tenant_id"])
        try:
            from backend import audit as _audit
            await _audit.log(
                action="skill_promoted",
                entity_kind="skill",
                entity_id=skill["skill_name"],
                before={
                    "auto_distilled_skill_id": skill["id"],
                    "status": "reviewed",
                    "version": skill["version"] - 1,
                },
                after={
                    "auto_distilled_skill_id": skill["id"],
                    "tenant_id": skill["tenant_id"],
                    "skill_name": skill["skill_name"],
                    "source_task_id": skill["source_task_id"],
                    "status": skill["status"],
                    "version": skill["version"],
                    "path": str(path),
                    "markdown_sha256": _markdown_sha256(
                        skill["markdown_content"],
                    ),
                },
                actor=actor,
            )
        except Exception as exc:  # pragma: no cover — audit.log swallows
            logger.debug("audit log for skill_promoted failed: %s", exc)
    finally:
        set_tenant_id(saved)


@router.get("")
async def list_auto_skills(
    status: str | None = Query(
        None, pattern=r"^(draft|reviewed|promoted)$",
    ),
    limit: int = Query(100, ge=1, le=500),
    user: _au.User = Depends(_au.require_operator),
) -> dict:
    tenant_id = _actor_tenant(user)
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT id, tenant_id, skill_name, source_task_id, "
                "markdown_content, version, status, created_at "
                "FROM auto_distilled_skills "
                "WHERE tenant_id = $1 AND status = $2 "
                "ORDER BY created_at DESC, id DESC LIMIT $3",
                tenant_id,
                status,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, tenant_id, skill_name, source_task_id, "
                "markdown_content, version, status, created_at "
                "FROM auto_distilled_skills "
                "WHERE tenant_id = $1 "
                "ORDER BY created_at DESC, id DESC LIMIT $2",
                tenant_id,
                limit,
            )
    items = [_row(r) for r in rows]
    return {"items": items, "count": len(items)}


@router.post("", status_code=201)
async def create_auto_skill(
    body: AutoSkillCreate,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    tenant_id = _actor_tenant(user)
    skill_name = _validate_skill_name(body.skill_name)
    skill_id = f"ads-{uuid4().hex}"
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO auto_distilled_skills ("
            "id, tenant_id, skill_name, source_task_id, markdown_content, "
            "version, status"
            ") VALUES ($1, $2, $3, $4, $5, 1, 'draft') "
            "RETURNING id, tenant_id, skill_name, source_task_id, "
            "markdown_content, version, status, created_at",
            skill_id,
            tenant_id,
            skill_name,
            body.source_task_id,
            body.markdown_content,
        )
    return _row(row)


@router.get("/{skill_id}")
async def get_auto_skill(
    skill_id: str,
    user: _au.User = Depends(_au.require_operator),
) -> dict:
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        row = await _fetch_owned(conn, skill_id, _actor_tenant(user))
    if row is None:
        raise HTTPException(404, "auto_distilled_skill not found")
    return row


@router.patch("/{skill_id}")
async def update_auto_skill(
    skill_id: str,
    body: AutoSkillUpdate,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    updates = body.model_dump(exclude_unset=True)
    expected_version = updates.pop("expected_version", None)
    if "skill_name" in updates and updates["skill_name"] is not None:
        updates["skill_name"] = _validate_skill_name(updates["skill_name"])
    if not updates:
        raise HTTPException(400, "no updates supplied")

    tenant_id = _actor_tenant(user)
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        current = await _fetch_owned(conn, skill_id, tenant_id)
        if current is None:
            raise HTTPException(404, "auto_distilled_skill not found")
        if current["status"] == "promoted":
            raise HTTPException(409, "promoted skills are immutable")
        if (
            expected_version is not None
            and expected_version != current["version"]
        ):
            raise HTTPException(409, "version conflict")

        values = []
        sets = []
        for column in ("skill_name", "source_task_id", "markdown_content"):
            if column in updates:
                values.append(updates[column])
                sets.append(f"{column} = ${len(values) + 2}")
        values.extend([skill_id, tenant_id])
        row = await conn.fetchrow(
            "UPDATE auto_distilled_skills SET "
            + ", ".join(sets)
            + ", version = version + 1 "
            "WHERE id = $1 AND tenant_id = $2 "
            "RETURNING id, tenant_id, skill_name, source_task_id, "
            "markdown_content, version, status, created_at",
            *values[-2:],
            *values[:-2],
        )
    return _row(row)


@router.delete("/{skill_id}")
async def delete_auto_skill(
    skill_id: str,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    tenant_id = _actor_tenant(user)
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM auto_distilled_skills "
            "WHERE id = $1 AND tenant_id = $2 AND status <> 'promoted' "
            "RETURNING id, status",
            skill_id,
            tenant_id,
        )
    if row is None:
        current = None
        async with get_pool().acquire() as conn:
            current = await _fetch_owned(conn, skill_id, tenant_id)
        if current is None:
            raise HTTPException(404, "auto_distilled_skill not found")
        raise HTTPException(409, "promoted skills cannot be deleted")
    return {"status": "deleted", "id": row["id"], "previous_status": row["status"]}


@router.post("/{skill_id}/review")
async def review_auto_skill(
    skill_id: str,
    body: AutoSkillUpdate | None = None,
    user: _au.User = Depends(_au.require_operator),
) -> dict:
    updates = body.model_dump(exclude_unset=True) if body else {}
    expected_version = updates.pop("expected_version", None)
    if "skill_name" in updates and updates["skill_name"] is not None:
        updates["skill_name"] = _validate_skill_name(updates["skill_name"])
    tenant_id = _actor_tenant(user)
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT id, tenant_id, skill_name, source_task_id, "
                "markdown_content, version, status, created_at "
                "FROM auto_distilled_skills "
                "WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                skill_id,
                tenant_id,
            )
            if current is None:
                raise HTTPException(404, "auto_distilled_skill not found")
            current_d = _row(current)
            if current_d["status"] != "draft":
                raise HTTPException(409, "only draft skills can be reviewed")
            if (
                expected_version is not None
                and expected_version != current_d["version"]
            ):
                raise HTTPException(409, "version conflict")

            values = [skill_id, tenant_id]
            sets = ["status = 'reviewed'"]
            for column in ("skill_name", "source_task_id", "markdown_content"):
                if column in updates:
                    values.append(updates[column])
                    sets.append(f"{column} = ${len(values)}")
            row = await conn.fetchrow(
                "UPDATE auto_distilled_skills SET "
                + ", ".join(sets)
                + ", version = version + 1 "
                "WHERE id = $1 AND tenant_id = $2 "
                "RETURNING id, tenant_id, skill_name, source_task_id, "
                "markdown_content, version, status, created_at",
                *values,
            )
    return _row(row)


@router.post("/{skill_id}/promote")
async def promote_auto_skill(
    skill_id: str,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    tenant_id = _actor_tenant(user)
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT id, tenant_id, skill_name, source_task_id, "
                "markdown_content, version, status, created_at "
                "FROM auto_distilled_skills "
                "WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                skill_id,
                tenant_id,
            )
            if current is None:
                raise HTTPException(404, "auto_distilled_skill not found")
            current_d = _row(current)
            if current_d["status"] != "reviewed":
                raise HTTPException(409, "only reviewed skills can be promoted")

            slug = _validate_skill_name(current_d["skill_name"])
            dest_dir = _SKILLS_LIVE / slug
            dest_file = dest_dir / "SKILL.md"
            if dest_file.exists() or dest_dir.exists():
                raise HTTPException(
                    status_code=409,
                    detail=f"skill {slug!r} already exists in live tree",
                )
            dest_dir.mkdir(parents=True, exist_ok=False)
            try:
                dest_file.write_text(
                    current_d["markdown_content"], encoding="utf-8",
                )
                row = await conn.fetchrow(
                    "UPDATE auto_distilled_skills SET "
                    "status = 'promoted', version = version + 1 "
                    "WHERE id = $1 AND tenant_id = $2 "
                    "RETURNING id, tenant_id, skill_name, source_task_id, "
                    "markdown_content, version, status, created_at",
                    skill_id,
                    tenant_id,
                )
            except Exception:
                dest_file.unlink(missing_ok=True)
                try:
                    dest_dir.rmdir()
                except OSError:
                    pass
                raise
    skill = _row(row)
    await _emit_promotion_audit(
        skill,
        path=dest_file,
        actor=getattr(user, "email", "operator"),
    )
    return {"skill": skill, "path": str(dest_file)}
