"""Z.7.7 — GET/POST /runtime/live-test-status.

Surface the nightly LLM live integration test result from the
``SharedKV("llm_live_test_status")`` namespace so the dashboard chip
(``<LiveTestStatusChip>``) can display "Last live-test pass: Xh ago"
without polling GitHub Actions directly.

Two endpoints:

* ``GET /runtime/live-test-status`` — read-only, requires normal session
  auth (same as all other ``/runtime/*`` routes). Returns the last status
  written by the nightly CI run, or a ``never_run`` envelope when the key
  is absent.

* ``POST /runtime/live-test-status`` — write from CI. Requires
  ``Authorization: Bearer <token>`` where the token matches the
  ``OMNISIGHT_REPORTER_TOKEN`` env var (static pre-shared secret for the
  GitHub Actions runner; not tied to the user session table). The
  ``current_user`` dependency is bypassed here so no interactive login is
  needed from the workflow.

Module-global audit (SOP Step 1, 2026-04-21 rule)
──────────────────────────────────────────────────
No module-globals introduced. Both endpoints use a fresh
``SharedKV("llm_live_test_status")`` instance per call. Cross-worker
consistency is Redis-backed (qualified answer #2) or intentionally
per-worker in-memory fallback (qualified answer #3 — per-worker drift
is tolerable for an observability chip that shows "last nightly pass").

Read-after-write audit
──────────────────────
Writer: the CI POST endpoint. Reader: GET endpoint + frontend chip.
No concurrent-write concern (one nightly runner + manual workflow_dispatch;
single writer per run). ``SharedKV.set`` is atomic per its own docstring.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _auth
from backend.shared_state import SharedKV

router = APIRouter(
    prefix="/runtime",
    tags=["runtime"],
)

_KV_NS = "llm_live_test_status"

# Fields stored in the SharedKV hash.
_FIELD_STATUS = "status"
_FIELD_TIMESTAMP = "timestamp"
_FIELD_RUN_ID = "run_id"
_FIELD_PROVIDERS = "providers"
_FIELD_ESTIMATED_COST = "estimated_cost_usd"
_FIELD_TESTS_RUN = "tests_run"
_FIELD_TESTS_PASSED = "tests_passed"
_FIELD_TESTS_SKIPPED = "tests_skipped"


def _reporter_token() -> str:
    """Return OMNISIGHT_REPORTER_TOKEN or empty string when not configured."""
    return os.environ.get("OMNISIGHT_REPORTER_TOKEN", "").strip()


# ─── Response schemas ────────────────────────────────────────────────────────

class ProviderResult(BaseModel):
    status: str = Field(..., description="pass | fail | skip")
    tests_run: int = 0
    tests_passed: int = 0
    tests_skipped: int = 0


class LiveTestStatusResponse(BaseModel):
    status: str = Field(..., description="pass | fail | unknown | never_run")
    timestamp: str | None = None
    run_id: str | None = None
    providers: dict[str, ProviderResult] | None = None
    estimated_cost_usd: float | None = None
    tests_run: int | None = None
    tests_passed: int | None = None
    tests_skipped: int | None = None


class LiveTestStatusWriteRequest(BaseModel):
    status: str = Field(..., description="pass | fail")
    run_id: str | None = None
    providers: dict[str, dict] | None = None
    estimated_cost_usd: float | None = None
    tests_run: int | None = None
    tests_passed: int | None = None
    tests_skipped: int | None = None


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get(
    "/live-test-status",
    response_model=LiveTestStatusResponse,
    dependencies=[Depends(_auth.current_user)],
    summary="Read last LLM live integration test result",
)
def get_live_test_status() -> LiveTestStatusResponse:
    """Return the most recent nightly live-test result from SharedKV.

    Returns ``status: never_run`` when the CI workflow has not yet written
    any result (fresh deploy / first run not completed).
    """
    kv = SharedKV(_KV_NS)
    raw_status = kv.get(_FIELD_STATUS, "")
    if not raw_status:
        return LiveTestStatusResponse(status="never_run")

    providers: dict[str, ProviderResult] | None = None
    raw_providers = kv.get(_FIELD_PROVIDERS, "")
    if raw_providers:
        try:
            p_dict = json.loads(raw_providers)
            providers = {k: ProviderResult(**v) for k, v in p_dict.items()}
        except Exception:
            providers = None

    raw_cost = kv.get(_FIELD_ESTIMATED_COST, "")
    estimated_cost: float | None = None
    if raw_cost:
        try:
            estimated_cost = float(raw_cost)
        except ValueError:
            pass

    def _int_or_none(field: str) -> int | None:
        v = kv.get(field, "")
        try:
            return int(v) if v else None
        except ValueError:
            return None

    return LiveTestStatusResponse(
        status=raw_status,
        timestamp=kv.get(_FIELD_TIMESTAMP, "") or None,
        run_id=kv.get(_FIELD_RUN_ID, "") or None,
        providers=providers,
        estimated_cost_usd=estimated_cost,
        tests_run=_int_or_none(_FIELD_TESTS_RUN),
        tests_passed=_int_or_none(_FIELD_TESTS_PASSED),
        tests_skipped=_int_or_none(_FIELD_TESTS_SKIPPED),
    )


@router.post(
    "/live-test-status",
    status_code=204,
    summary="Write LLM live integration test result (CI reporter)",
)
def post_live_test_status(
    body: LiveTestStatusWriteRequest,
    authorization: str | None = Header(default=None),
) -> None:
    """Write nightly live-test results from the GitHub Actions runner.

    Auth: ``Authorization: Bearer <OMNISIGHT_REPORTER_TOKEN>`` — a static
    pre-shared secret stored in GitHub Actions repo secrets. The standard
    user-session auth is intentionally skipped so CI does not need an
    operator account.

    If ``OMNISIGHT_REPORTER_TOKEN`` is unset/empty, the endpoint is
    disabled (returns 503) to prevent accidental open writes.
    """
    expected = _reporter_token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="OMNISIGHT_REPORTER_TOKEN not configured — reporter endpoint disabled",
        )

    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[len("bearer "):].strip()

    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid reporter token")

    if body.status not in ("pass", "fail"):
        raise HTTPException(status_code=422, detail="status must be 'pass' or 'fail'")

    kv = SharedKV(_KV_NS)
    ts = datetime.now(timezone.utc).isoformat()
    kv.set(_FIELD_STATUS, body.status)
    kv.set(_FIELD_TIMESTAMP, ts)
    if body.run_id is not None:
        kv.set(_FIELD_RUN_ID, body.run_id)
    if body.providers is not None:
        kv.set(_FIELD_PROVIDERS, json.dumps(body.providers))
    if body.estimated_cost_usd is not None:
        kv.set(_FIELD_ESTIMATED_COST, str(body.estimated_cost_usd))
    if body.tests_run is not None:
        kv.set(_FIELD_TESTS_RUN, str(body.tests_run))
    if body.tests_passed is not None:
        kv.set(_FIELD_TESTS_PASSED, str(body.tests_passed))
    if body.tests_skipped is not None:
        kv.set(_FIELD_TESTS_SKIPPED, str(body.tests_skipped))
