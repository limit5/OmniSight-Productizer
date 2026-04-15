"""Phase 56-DAG-D — Mode A endpoint.

Caller-as-orchestrator path: operator submits a pre-written DAG JSON,
the backend validates + persists + (optionally) runs the mutation
loop. Mode B (auto-plan inside chat router) is deliberately deferred
to a follow-up phase so this endpoint can ship as a standalone unit
without touching the hot chat path.

Permissions (Phase 54 RBAC):
  * POST /api/v1/dag             — require_operator
  * GET  /api/v1/dag/plans/{id}  — require_operator
  * GET  /api/v1/dag/runs/{id}/plan — require_operator

Mutation mode: when `mutate=true` and the initial DAG fails validation,
we call `dag_planner.run_mutation_loop` with whatever `ask_fn` is
provided by `iq_runner.live_ask_fn`-alike wiring. If no orchestrator
is configured (no LLM provider), we short-circuit to status=failed
and let the operator fix manually.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import dag_storage as _ds
from backend import dag_validator as _dv
from backend.dag_schema import DAG

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dag", tags=["dag"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Request model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DAGSubmitRequest(BaseModel):
    """Submit a hand-written DAG plan for validation + execution link."""
    dag: dict = Field(..., description="DAG JSON matching DAG schema_version=1")
    mutate: bool = Field(
        False,
        description=(
            "If true and validation fails, invoke the Orchestrator "
            "mutation loop (Phase 56-DAG-C) to auto-fix up to 3 rounds."
        ),
    )
    metadata: Optional[dict] = Field(
        default=None,
        description="Optional metadata stored on the workflow_run",
    )
    target_platform: Optional[str] = Field(
        default=None,
        description=(
            "Phase 64-C-LOCAL S4 — platform profile name (e.g. 'host_native', "
            "'aarch64'). When supplied, the validator asks the T3 resolver "
            "whether this target can run on the host and relaxes tier_violation "
            "for t3 tasks accordingly. None = fall back to the global default "
            "(hardware_manifest.yaml → host_native)."
        ),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Target-platform resolver (Phase 64-C-LOCAL S4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Pipeline of sources, first non-empty wins:
#   1. The request's explicit `target_platform` field.
#   2. `configs/hardware_manifest.yaml`'s `target_platform` key.
#   3. Default — `host_native` profile (T1-A default).
#
# Returns the parsed profile dict or None if none can be loaded; the
# validator's `target_profile=None` path preserves pre-64-C behaviour.

def _resolve_target_profile(explicit: str | None) -> dict | None:
    from pathlib import Path
    import yaml as _yaml
    from backend.sdk_provisioner import _validate_platform_name, _platform_profile

    # 1. Explicit request field.
    if explicit:
        name = explicit.strip()
    else:
        name = ""

    # 2. Fall back to hardware_manifest.
    if not name:
        manifest = Path("configs") / "hardware_manifest.yaml"
        if manifest.is_file():
            try:
                m = _yaml.safe_load(manifest.read_text()) or {}
                name = (
                    (m.get("project") or {}).get("target_platform")
                    or m.get("target_platform")
                    or ""
                ).strip()
            except Exception as exc:
                logger.debug("hardware_manifest parse failed: %s", exc)

    # 3. Final default matches T1-A.
    if not name:
        name = "host_native"

    if not _validate_platform_name(name):
        logger.debug("target_platform %r rejected by validator", name)
        return None
    profile_path = _platform_profile(name)
    if profile_path is None or not profile_path.exists():
        logger.debug("target_platform %r: no profile YAML", name)
        return None
    try:
        return _yaml.safe_load(profile_path.read_text()) or {}
    except Exception as exc:
        logger.debug("profile %s parse failed: %s", name, exc)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestrator hook — opt-in
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _default_ask_fn(system: str, user: str) -> tuple[str, int]:
    """Wire Mode A's `mutate=true` to the live LLM layer via
    `iq_runner.live_ask_fn` (lazy import keeps LangChain off this
    router's import graph for the 90% path)."""
    try:
        from backend.iq_runner import live_ask_fn
        from backend.config import settings as _s
        # Combine system + user into one prompt — live_ask_fn only
        # takes (model, prompt). Prefix the system instructions.
        combined = f"{system}\n\n---\n\n{user}"
        model = f"{_s.llm_provider}/{_s.get_model_name()}"
        return await live_ask_fn(model, combined)
    except Exception as exc:
        logger.warning("default ask_fn failed: %s", exc)
        return ("", 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 67-C hooks — speculative pre-warm
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _prewarm_enabled() -> bool:
    """Pre-warm is opt-in. Default OFF because v1 cannot yet mount the
    per-agent workspace into the pre-warmed container (that requires
    agent→task assignment which happens LATER). Operators that run a
    shared workspace layout can turn this on experimentally."""
    import os as _os
    raw = (_os.environ.get("OMNISIGHT_PREWARM_ENABLED") or "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def _prewarm_in_background(dag: DAG) -> None:
    """Fire-and-forget hook. Always swallows exceptions — pre-warm
    failure must not surface to the DAG submit caller."""
    try:
        from pathlib import Path
        from backend.workspace import _WORKSPACES_ROOT  # type: ignore[attr-defined]
        from backend import sandbox_prewarm as _pw
        shared = Path(_WORKSPACES_ROOT) / "_prewarm"
        shared.mkdir(parents=True, exist_ok=True)
        await _pw.prewarm_for(dag, shared)
    except Exception as exc:
        logger.debug("prewarm hook swallowed error: %s", exc)


async def _cancel_prewarm(reason: str) -> None:
    """Mutation / abort path — drop stale pre-warms so their lifetime
    budget isn't burned waiting for a task that'll never consume."""
    try:
        from backend import sandbox_prewarm as _pw
        n = await _pw.cancel_all(reason=reason)
        if n:
            logger.info("prewarm: cancelled %d container(s) on %s", n, reason)
    except Exception as exc:
        logger.debug("prewarm cancel swallowed error: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /api/v1/dag/validate — Phase 56-DAG-E
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Dry-run validation for the authoring UI. Pure function: parses the
# payload against the Pydantic schema, then runs the 7-rule validator.
# Nothing is persisted and no workflow_run is created — the editor's
# live-validate loop can hit this on every keystroke without polluting
# storage or burning a plan id. Mutation loop is NOT invoked here; the
# operator fixes the DAG themselves via the UI.

class DAGValidateRequest(BaseModel):
    dag: dict = Field(..., description="DAG JSON to validate")
    target_platform: Optional[str] = Field(
        default=None,
        description=(
            "Phase 64-C-LOCAL S4 — platform profile name (e.g. 'host_native', "
            "'aarch64'). Drives the T3 resolver's LOCAL/BUNDLE decision, "
            "which in turn relaxes tier_violation for t3 tasks when the "
            "host can natively run the target. None = hardware_manifest → "
            "host_native fallback."
        ),
    )


@router.post("/validate")
async def validate_dag(req: DAGValidateRequest,
                       _user=Depends(_au.require_operator)) -> dict:
    """Run validator without storing. Returns either
    `{ok: true}` or `{ok: false, stage: schema|semantic, errors: [...]}`.
    Each semantic error carries `rule`, `task_id` (or null), `message`.

    Phase 64-C-LOCAL S4 additions in the response:
      * `t3_runner` — "local" / "bundle" / ... — what the resolver
        would pick for every t3 task in this DAG. Lets the editor UI
        render per-node ⚡ (LOCAL) vs 🔗 (remote bundle) chips.
      * `target_platform` — the profile name the resolver used, so the
        UI can display it alongside the chip."""
    # Stage 1: Pydantic schema (shape).
    try:
        dag = DAG.model_validate(req.dag)
    except Exception as exc:
        return {
            "ok": False,
            "stage": "schema",
            "errors": [{"rule": "schema", "task_id": None, "message": str(exc)}],
        }

    # Resolve once — validator + UI-hint both want the same answer.
    profile = _resolve_target_profile(req.target_platform)
    from backend.t3_resolver import resolve_from_profile
    resolution = resolve_from_profile(profile)

    # Stage 2: semantic rules with tier-relaxation awareness.
    result = _dv.validate(dag, target_profile=profile)
    return {
        "ok": result.ok,
        "stage": "semantic",
        "errors": [
            {"rule": e.rule, "task_id": e.task_id, "message": e.message}
            for e in result.errors
        ],
        "task_count": len(dag.tasks),
        "t3_runner": resolution.kind.value,
        "target_platform": (profile or {}).get("platform") or req.target_platform or "host_native",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /api/v1/dag
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.post("")
async def submit_dag(req: DAGSubmitRequest,
                     _user=Depends(_au.require_operator)) -> dict:
    """Submit a DAG. Validates, optionally mutates, persists, links
    to a new workflow_run. Returns the run_id + plan_id + status."""
    # 1. Pydantic schema on the raw JSON — reject shape errors early.
    try:
        dag = DAG.model_validate(req.dag)
    except Exception as exc:
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"DAG schema validation failed: {exc}",
                "stage": "schema",
            },
        )

    # 2. Persist + validate + link via workflow.start.
    from backend import workflow as wf
    metadata = dict(req.metadata or {})
    metadata.setdefault("source", "api:dag-submit")
    metadata.setdefault("user", getattr(_user, "email", "operator"))

    # Phase 64-C-LOCAL S4: resolve target_platform once and thread it
    # through so wf.start's internal validator + downstream dispatcher
    # get the same answer. Stash the profile name in metadata for
    # post-hoc debugging of "why did this plan validate / not validate".
    target_profile = _resolve_target_profile(req.target_platform)
    if target_profile is not None:
        metadata.setdefault(
            "target_platform", target_profile.get("platform") or req.target_platform or "host_native",
        )

    # First attempt — workflow.start itself runs the validator.
    run = await wf.start("invoke", dag=dag, metadata=metadata, target_profile=target_profile)
    plan = await _ds.get_plan_by_run(run.id)
    if plan is None:
        # Plan persistence failed inside workflow.start — return best-
        # effort so the caller can retry.
        return JSONResponse(
            status_code=500,
            content={"detail": "plan persistence failed",
                     "run_id": run.id},
        )

    # 3. If the plan failed validation AND caller opted into mutate,
    #    kick the Orchestrator loop.
    if plan.status == "failed" and req.mutate:
        from backend import dag_planner as dp
        # Before we attempt to mutate, drop any pre-warms that were
        # speculatively launched against the prior draft — the
        # replanned DAG will have different in-degree-0 tasks.
        await _cancel_prewarm(reason="dag_mutated")
        loop = await dp.run_mutation_loop(dag, ask_fn=_default_ask_fn)
        if loop.ok:
            # Open a successor run with the recovered DAG.
            successor = await wf.mutate_workflow(
                run.id, loop.final_dag, mutation_round=1,
            )
            succ_plan = await _ds.get_plan_by_run(successor.id)
            return {
                "run_id": successor.id,
                "plan_id": succ_plan.id if succ_plan else None,
                "status": succ_plan.status if succ_plan else "unknown",
                "mutation_rounds": len(loop.attempts),
                "supersedes_run_id": run.id,
                "validation_errors": [],
            }
        # Exhausted — return the failed plan as-is; DE `dag/exhausted`
        # proposal already fired inside run_mutation_loop.
        return JSONResponse(
            status_code=422,
            content={
                "run_id": run.id,
                "plan_id": plan.id,
                "status": plan.status,
                "mutation_rounds": len(loop.attempts),
                "mutation_status": loop.status,
                "validation_errors": plan.errors(),
                "stage": "mutation_exhausted",
            },
        )

    # 4. Normal path — plain submit, no mutation. Return whatever
    #    status we landed at (validated / executing / failed).
    status_code = 200 if plan.status != "failed" else 422

    # Phase 67-C hook: only fire pre-warm on validated plans AND when
    # opt-in. We fire-and-forget with asyncio.create_task so the
    # response returns immediately; pre-warm continues in background.
    if plan.status == "executing" and _prewarm_enabled():
        import asyncio as _asyncio
        _asyncio.create_task(_prewarm_in_background(dag))

    return JSONResponse(
        status_code=status_code,
        content={
            "run_id": run.id,
            "plan_id": plan.id,
            "status": plan.status,
            "validation_errors": plan.errors(),
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /api/v1/dag/plans/{plan_id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/plans/{plan_id}")
async def get_plan(plan_id: int,
                   _user=Depends(_au.require_operator)) -> dict:
    try:
        plan = await _ds.get_plan(plan_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"no dag_plan id={plan_id}")
    return {
        "id": plan.id,
        "dag_id": plan.dag_id,
        "run_id": plan.run_id,
        "parent_plan_id": plan.parent_plan_id,
        "status": plan.status,
        "mutation_round": plan.mutation_round,
        "validation_errors": plan.errors(),
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /api/v1/dag/runs/{run_id}/plan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/runs/{run_id}/plan")
async def get_plan_for_run(run_id: str,
                           _user=Depends(_au.require_operator)) -> dict:
    plan = await _ds.get_plan_by_run(run_id)
    if plan is None:
        raise HTTPException(
            status_code=404, detail=f"no dag_plan for run {run_id}",
        )
    return {
        "id": plan.id,
        "dag_id": plan.dag_id,
        "run_id": plan.run_id,
        "parent_plan_id": plan.parent_plan_id,
        "status": plan.status,
        "mutation_round": plan.mutation_round,
        "validation_errors": plan.errors(),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /api/v1/dag/plans/by-dag/{dag_id}  — full mutation chain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/plans/by-dag/{dag_id}")
async def list_plan_chain(dag_id: str,
                          _user=Depends(_au.require_operator)) -> dict:
    plans = await _ds.list_plans(dag_id)
    return {
        "dag_id": dag_id,
        "count": len(plans),
        "plans": [
            {
                "id": p.id,
                "run_id": p.run_id,
                "parent_plan_id": p.parent_plan_id,
                "status": p.status,
                "mutation_round": p.mutation_round,
                "created_at": p.created_at,
            }
            for p in plans
        ],
    }
