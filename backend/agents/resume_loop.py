"""U6-0 GAP-5c-loop sub-leaf C: wire the dormant execution machinery into the supervised driver loop (default-OFF).

run_supervised_resume_loop builds the fully-wired drive_once -- run_resume_job with the real dispatch executor
(make_dispatch_executor over the workspace-root-fd resolver) + the default-deny authorizer + the id/receipt factories --
and runs it under the drive_resume_jobs supervisor.  DEFAULT-OFF: unless OMNISIGHT_U6_RESUME_LOOP_ENABLED is truthy it
returns immediately WITHOUT touching the pool or building the executor.

Enabling it (loop flag on) still causes NO GOVERNED-ACTION / file side effect, via TWO independent execution-time gates
at DIFFERENT layers: (1) the authorizer is default-deny (needs OMNISIGHT_U6_EXECUTE + a non-empty allowlist), so
claim_and_execute policy-denies at the AUTHORIZE step and the real executor is NEVER invoked; (2) even if execution were
enabled, the workspace resolver is fail-closed (empty OMNISIGHT_U6_WORKSPACE_FD_ROOTS), so the Write executor -- which
DOES run the resolver -- returns DNA before touching the filesystem.  Either gate alone blocks a governed file effect.
It is NOT a pure no-op, though: it opens the DB pool and, for any PRE-EXISTING queued job, advances durable resume-queue
CONTROL-PLANE state to a terminal outcome (depending on the job's recovery mode -- a pending grant is policy-denied and
marked failed; an executing sink-idempotent / non-replayable job resolves to 'manual').  T11 enforce is a SEPARATE
UPSTREAM gate: it governs whether the guard CREATES a NEW challenge -- not an execution-time switch, and it is not
re-checked when a PRE-EXISTING challenge is confirmed into a grant, so it is not an absolute queue freeze; in prod there
is simply no challenge backlog, so the queue stays empty and drive_once just idles.  NOTHING auto-runs this: only the
operator-run scripts/run_u6_resume_loop.py calls it (no lifespan hook, no timer, no cron), mirroring the shipped P5
'manual, NO timer' posture.
"""
from __future__ import annotations

import os
from collections.abc import Callable

from backend.agents import execution_gate
from backend.agents.execution_service import run_resume_job
from backend.agents.executor_workspace_write import make_dispatch_executor
from backend.agents.resume_driver import DriveStats
from backend.agents.resume_driver import drive_resume_jobs
from backend.agents.workspace_root_resolver import resolve_root_fd as _default_resolve_root_fd

_ENABLE_ENV = "OMNISIGHT_U6_RESUME_LOOP_ENABLED"
_DISABLED = "loop_disabled"


def resume_loop_enabled() -> bool:
    """The loop-enable switch (default OFF; same predicate as execution_gate.execution_enabled)."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


async def run_supervised_resume_loop(
    pool,
    *,
    worker_id: str,
    lease_ttl_seconds: int,
    poll_interval_s: float,
    error_backoff_s: float,
    max_consecutive_errors: int,
    drain_when_idle: bool,
    should_continue: Callable[[], bool],
    max_ticks: "int | None" = None,
    resolve_root_fd: Callable[[str], "int | None"] = _default_resolve_root_fd,
    on_outcome: "Callable[[str], None] | None" = None,
) -> DriveStats:
    """Run the supervised resume loop IFF OMNISIGHT_U6_RESUME_LOOP_ENABLED is set; otherwise a no-op DriveStats.

    Disabled: NO side effect (no pool use, no executor build).  Enabled: the real executor stays gated by the
    default-deny authorizer + the fail-closed resolver, so no governed-action / file effect occurs; any queued job is
    only driven to a terminal control-plane outcome.
    """
    if not resume_loop_enabled():
        return DriveStats(0, 0, 0, 0, _DISABLED)

    executor = make_dispatch_executor(resolve_root_fd)

    async def drive_once() -> str:
        return await run_resume_job(
            pool,
            worker_id=worker_id,
            lease_ttl_seconds=lease_ttl_seconds,
            executor=executor,
            authorizer=execution_gate.authorizer,
            attempt_id_factory=execution_gate.mint_attempt_id,
            result_of=execution_gate.result_of,
        )

    return await drive_resume_jobs(
        drive_once,
        poll_interval_s=poll_interval_s,
        error_backoff_s=error_backoff_s,
        max_consecutive_errors=max_consecutive_errors,
        drain_when_idle=drain_when_idle,
        should_continue=should_continue,
        max_ticks=max_ticks,
        on_outcome=on_outcome,
    )
