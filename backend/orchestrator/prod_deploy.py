"""OP-881 D9 -- prod deploy orchestrator with operator approval gate.

Single entrypoint (``POST /api/v1/prod/deploy``) that drives the four
production-deploy steps in order and aborts the whole attempt on any
failure:

    approval -> D2 image pull -> D3 prod secrets decrypt
             -> D7 smoke pre-check on staging mirror
             -> blue-green switch on prod

Operator approval is a dual signal -- the request body must carry a
signed webhook payload (HMAC-SHA256 over the canonical body bytes
keyed on ``OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET``) **and** a Slack DM
confirmation token issued out-of-band by the on-call rotation. Either
signal alone is insufficient: the webhook gate forbids a Slack-only
spoof and the Slack gate forbids a leaked-secret replay.

Idempotence (AC #4)
-------------------
``release_id`` is the deduplication key. The orchestrator records a
``running`` row in :class:`prod_deploy_audit` before any side effect
and rolls it forward to a terminal status (``completed`` or
``aborted_*``). A duplicate POST with the same ``release_id`` whose
audit row is already ``completed`` returns the previous result with
``idempotent_replay=true`` and emits no further SSE events. A
duplicate whose row is still ``running`` returns HTTP 409 so two
concurrent operators cannot race.

Wiring to the larger system
---------------------------
The four steps are exposed as injectable callables on
:class:`ProdDeployOrchestrator` so tests can pin the sequence without
shelling out to Docker / Slack / Vault and so the parent META
(OP-761) can swap concrete implementations from D6/D7/D8 as those
tickets land. Production wiring lives in :func:`build_default_steps`.

SSE contract
------------
Three event types are published on the bus:

* ``prod.deploy.started``  -- emitted before the approval check
* ``prod.deploy.completed`` -- emitted after blue-green switch returns
* ``prod.deploy.aborted``  -- emitted on any abort path; carries the
  exception class name in ``error_class`` and the canonical
  ``aborted_*`` status in ``status``.

Change-management compliance
----------------------------
Every attempt also writes to :mod:`backend.deploy_audit` (the hash-
chained 0204 table) -- ``started`` on entry, ``succeeded`` or
``failed`` on exit. The local :class:`prod_deploy_audit` ledger is
for the orchestrator's own idempotence; the chained log is what
compliance reviewers inspect.
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend import auth, deploy_audit, events


logger = logging.getLogger(__name__)


# ─── Error catalog ────────────────────────────────────────────────────


class ProdDeployError(RuntimeError):
    """Base class so the FastAPI handler can map to a single HTTP code."""

    http_status: int = 500
    abort_status: str = "aborted_error"


class OperatorApprovalRefused(ProdDeployError):
    http_status = 403
    abort_status = "aborted_approval_refused"


class PreCheckSmokeFailed(ProdDeployError):
    http_status = 412
    abort_status = "aborted_smoke_failed"


class DeployTimeoutExceeded(ProdDeployError):
    http_status = 504
    abort_status = "aborted_timeout"


class ConcurrentDeployInFlight(ProdDeployError):
    """Two POSTs raced for the same release_id; the loser sees 409."""

    http_status = 409
    abort_status = "aborted_error"


# ─── Step protocols ───────────────────────────────────────────────────


class ImagePullStep(Protocol):
    def __call__(self, image_tag: str) -> None: ...


class SecretsDecryptStep(Protocol):
    def __call__(self, release_id: str) -> dict[str, str]: ...


class SmokeCheckStep(Protocol):
    def __call__(self, image_tag: str) -> tuple[bool, tuple[str, ...]]: ...


class BlueGreenSwitchStep(Protocol):
    def __call__(self, image_tag: str, decrypted_secrets: dict[str, str]) -> None: ...


@dataclass(frozen=True)
class DeploySteps:
    image_pull: ImagePullStep
    secrets_decrypt: SecretsDecryptStep
    smoke_check: SmokeCheckStep
    blue_green_switch: BlueGreenSwitchStep


def build_default_steps() -> DeploySteps:
    """Wire up the production callables. Used when the router is mounted
    in the real app; tests inject fakes via the orchestrator constructor.
    """
    from backend import secret_store, staging_validation
    from backend.production_release import ProductionDeployOrchestrator

    legacy_orch = ProductionDeployOrchestrator()

    def _image_pull(image_tag: str) -> None:
        legacy_orch._run(
            ["docker", "pull", f"{legacy_orch.image_repository}:{image_tag}"],
            timeout=300.0,
            tag=image_tag,
        )

    def _secrets_decrypt(release_id: str) -> dict[str, str]:
        env_blob = os.environ.get("OMNISIGHT_PROD_SECRETS_ENC", "")
        if not env_blob:
            return {}
        return {"_blob": secret_store.decrypt(env_blob)}

    def _smoke_check(image_tag: str) -> tuple[bool, tuple[str, ...]]:
        base = os.environ.get(
            "OMNISIGHT_STAGING_MIRROR_URL",
            "http://staging-mirror.internal:8080",
        )
        return staging_validation.run_smoke_suite(base)

    def _blue_green_switch(
        image_tag: str, decrypted_secrets: dict[str, str]
    ) -> None:
        # delegate to the legacy production_release orchestrator which
        # already implements the compose-based blue/green dance.
        legacy_orch.ship(image_tag)

    return DeploySteps(
        image_pull=_image_pull,
        secrets_decrypt=_secrets_decrypt,
        smoke_check=_smoke_check,
        blue_green_switch=_blue_green_switch,
    )


# ─── Approval gate ────────────────────────────────────────────────────


def _webhook_secret() -> bytes:
    return os.environ.get("OMNISIGHT_PROD_DEPLOY_WEBHOOK_SECRET", "").encode("utf-8")


def verify_webhook_signature(body: bytes, signature_header: str) -> bool:
    """Constant-time HMAC-SHA256 check on the raw body."""
    secret = _webhook_secret()
    if not secret or not signature_header:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    # tolerate both ``sha256=<hex>`` and bare hex forms.
    provided = signature_header.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, provided)


SlackConfirmCheck = Callable[[str, str], bool]


def _default_slack_confirm(release_id: str, token: str) -> bool:
    """Verify the Slack DM confirmation token.

    Production binds this to the Slack bridge. The default impl reads
    a comma-separated allowlist from ``OMNISIGHT_PROD_DEPLOY_SLACK_TOKENS``
    so the gate can be exercised in environments without the bridge
    (e.g. sandboxed mirror DoD run).
    """
    if not release_id or not token:
        return False
    allow = os.environ.get("OMNISIGHT_PROD_DEPLOY_SLACK_TOKENS", "")
    valid = {t.strip() for t in allow.split(",") if t.strip()}
    return token in valid


# ─── Audit ledger ─────────────────────────────────────────────────────


_test_engine: sa.Engine | None = None
_prod_engine: sa.Engine | None = None


def set_engine_for_tests(engine: sa.Engine | None) -> None:
    global _test_engine
    _test_engine = engine


def _engine() -> sa.Engine:
    if _test_engine is not None:
        return _test_engine
    global _prod_engine
    if _prod_engine is None:
        url = os.environ.get("OMNISIGHT_DATABASE_URL", "sqlite:///prod_deploy_audit.db")
        _prod_engine = sa.create_engine(url, future=True)
    return _prod_engine


def _lookup_audit(release_id: str) -> dict[str, Any] | None:
    with _engine().begin() as conn:
        row = conn.execute(
            sa.text(
                "SELECT release_id, image_tag, actor, status, last_step, "
                "started_at, finished_at, elapsed_seconds, "
                "error_class, error_message "
                "FROM prod_deploy_audit WHERE release_id = :rid"
            ),
            {"rid": release_id},
        ).mappings().first()
    return dict(row) if row else None


def _insert_running(release_id: str, image_tag: str, actor: str) -> bool:
    """Insert the ``running`` row. Returns False if release_id collides."""
    try:
        with _engine().begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO prod_deploy_audit "
                    "(release_id, image_tag, actor, status, last_step) "
                    "VALUES (:rid, :tag, :actor, 'running', 'approval')"
                ),
                {"rid": release_id, "tag": image_tag, "actor": actor},
            )
        return True
    except sa.exc.IntegrityError:
        return False


def _update_step(release_id: str, step: str) -> None:
    with _engine().begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE prod_deploy_audit SET last_step = :s "
                "WHERE release_id = :rid AND status = 'running'"
            ),
            {"s": step, "rid": release_id},
        )


def _finalize(
    release_id: str,
    *,
    status: str,
    elapsed: float,
    error_class: str | None = None,
    error_message: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    finished = datetime.now(timezone.utc).isoformat()
    with _engine().begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE prod_deploy_audit "
                "SET status = :st, finished_at = :fin, elapsed_seconds = :el, "
                "    error_class = :ec, error_message = :em, "
                "    last_step = CASE WHEN :st = 'completed' "
                "                     THEN 'completed' ELSE last_step END, "
                "    context_json = :ctx "
                "WHERE release_id = :rid AND status = 'running'"
            ),
            {
                "st": status,
                "fin": finished,
                "el": elapsed,
                "ec": error_class,
                "em": error_message,
                "rid": release_id,
                "ctx": json.dumps(context) if context else None,
            },
        )


# ─── Orchestrator ─────────────────────────────────────────────────────


@dataclass
class ProdDeployOrchestrator:
    steps: DeploySteps
    deploy_timeout_seconds: float = 1800.0
    clock: Callable[[], float] = field(default=time.monotonic)
    slack_confirm: SlackConfirmCheck = field(default=_default_slack_confirm)
    audit_record: Callable[..., int] = field(default=deploy_audit.record)

    def execute(
        self,
        *,
        release_id: str,
        image_tag: str,
        actor: str,
        reason: str,
        approval_token: str,
        body_bytes: bytes,
        signature_header: str,
    ) -> dict[str, Any]:
        # Idempotence: short-circuit replay BEFORE any side effect.
        existing = _lookup_audit(release_id)
        if existing and existing["status"] == "completed":
            logger.info("prod-deploy idempotent replay release_id=%s", release_id)
            return {
                "release_id": release_id,
                "status": "completed",
                "idempotent_replay": True,
                "elapsed_seconds": existing["elapsed_seconds"],
            }
        if existing and existing["status"] == "running":
            raise ConcurrentDeployInFlight(
                f"prod-deploy already in flight for release_id={release_id}"
            )

        if not _insert_running(release_id, image_tag, actor):
            raise ConcurrentDeployInFlight(
                f"prod-deploy already in flight for release_id={release_id}"
            )

        started_monotonic = self.clock()
        events.bus.publish("prod.deploy.started", {
            "release_id": release_id,
            "image_tag": image_tag,
            "actor": actor,
        })
        self.audit_record(
            kind="deploy", status="started",
            tag=image_tag, actor=actor, reason=reason,
            context={"release_id": release_id, "source": "prod_deploy_orchestrator"},
        )

        try:
            # ── 0. approval gate
            if not verify_webhook_signature(body_bytes, signature_header):
                raise OperatorApprovalRefused(
                    "webhook signature missing or invalid"
                )
            if not self.slack_confirm(release_id, approval_token):
                raise OperatorApprovalRefused(
                    "Slack DM confirmation token missing or unknown"
                )

            # ── 1. D2 image pull
            _update_step(release_id, "image_pull")
            self._guard_timeout(started_monotonic)
            self.steps.image_pull(image_tag)

            # ── 2. D3 secrets decrypt
            _update_step(release_id, "secrets_decrypt")
            self._guard_timeout(started_monotonic)
            secrets = self.steps.secrets_decrypt(release_id)

            # ── 3. D7 smoke pre-check on staging mirror
            _update_step(release_id, "smoke_pre_check")
            self._guard_timeout(started_monotonic)
            ok, failures = self.steps.smoke_check(image_tag)
            if not ok:
                raise PreCheckSmokeFailed(
                    "staging-mirror smoke failed: " + ",".join(failures)
                )

            # ── 4. blue-green switch on prod
            _update_step(release_id, "blue_green_switch")
            self._guard_timeout(started_monotonic)
            self.steps.blue_green_switch(image_tag, secrets)

        except ProdDeployError as exc:
            self._abort(release_id, image_tag, actor, started_monotonic, exc)
            raise
        except Exception as exc:  # pragma: no cover -- defensive
            self._abort(release_id, image_tag, actor, started_monotonic, exc)
            raise

        elapsed = self.clock() - started_monotonic
        # Final timeout check: if the orchestrated steps collectively
        # blew past the budget, treat the attempt as a timeout abort
        # even though every step returned OK -- "DeployTimeoutExceeded
        # auto-rollback" per the OP-881 error catalog.
        if elapsed >= self.deploy_timeout_seconds:
            exc = DeployTimeoutExceeded(
                f"prod-deploy exceeded {self.deploy_timeout_seconds:.0f}s"
            )
            self._abort(release_id, image_tag, actor, started_monotonic, exc)
            raise exc
        _finalize(release_id, status="completed", elapsed=elapsed)
        events.bus.publish("prod.deploy.completed", {
            "release_id": release_id,
            "image_tag": image_tag,
            "elapsed_seconds": elapsed,
        })
        self.audit_record(
            kind="deploy", status="succeeded",
            tag=image_tag, actor=actor, reason=reason,
            elapsed_seconds=elapsed,
            context={"release_id": release_id},
        )
        return {
            "release_id": release_id,
            "status": "completed",
            "idempotent_replay": False,
            "elapsed_seconds": elapsed,
        }

    def _guard_timeout(self, started_monotonic: float) -> None:
        if self.clock() - started_monotonic >= self.deploy_timeout_seconds:
            raise DeployTimeoutExceeded(
                f"prod-deploy exceeded {self.deploy_timeout_seconds:.0f}s"
            )

    def _abort(
        self,
        release_id: str,
        image_tag: str,
        actor: str,
        started_monotonic: float,
        exc: BaseException,
    ) -> None:
        elapsed = self.clock() - started_monotonic
        status = getattr(exc, "abort_status", "aborted_error")
        _finalize(
            release_id,
            status=status,
            elapsed=elapsed,
            error_class=exc.__class__.__name__,
            error_message=str(exc),
        )
        events.bus.publish("prod.deploy.aborted", {
            "release_id": release_id,
            "image_tag": image_tag,
            "status": status,
            "error_class": exc.__class__.__name__,
            "error_message": str(exc),
        })
        try:
            self.audit_record(
                kind="deploy", status="failed",
                tag=image_tag, actor=actor,
                reason=f"prod-deploy aborted: {exc.__class__.__name__}",
                elapsed_seconds=elapsed,
                context={"release_id": release_id, "abort_status": status},
            )
        except Exception:  # pragma: no cover -- audit must not mask abort
            logger.exception("deploy_audit.record failed during abort")


# ─── FastAPI router ───────────────────────────────────────────────────


class DeployRequest(BaseModel):
    release_id: str = Field(min_length=1, max_length=128)
    image_tag: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)
    approval_token: str = Field(min_length=1, max_length=256)


_orchestrator_factory: Callable[[], ProdDeployOrchestrator] | None = None


def set_orchestrator_factory(
    factory: Callable[[], ProdDeployOrchestrator] | None,
) -> None:
    """Tests inject a factory that returns a stubbed orchestrator."""
    global _orchestrator_factory
    _orchestrator_factory = factory


def _resolve_orchestrator() -> ProdDeployOrchestrator:
    if _orchestrator_factory is not None:
        return _orchestrator_factory()
    return ProdDeployOrchestrator(steps=build_default_steps())


router = APIRouter(prefix="/prod", tags=["prod-deploy"])


@router.post("/deploy")
async def post_deploy(
    request: Request,
    actor: auth.User = Depends(auth.require_admin),
) -> dict[str, Any]:
    body_bytes = await request.body()
    try:
        body = DeployRequest.model_validate_json(body_bytes)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    signature = request.headers.get("X-Prod-Deploy-Signature", "")

    orch = _resolve_orchestrator()
    try:
        result = await asyncio.to_thread(
            orch.execute,
            release_id=body.release_id,
            image_tag=body.image_tag,
            actor=actor.email,
            reason=body.reason,
            approval_token=body.approval_token,
            body_bytes=body_bytes,
            signature_header=signature,
        )
    except ProdDeployError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={
                "error_class": exc.__class__.__name__,
                "message": str(exc),
                "abort_status": exc.abort_status,
            },
        ) from exc
    return result
