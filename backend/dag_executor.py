"""DAG execution engine — Dim 2 / Phase 1: inert poll-loop skeleton.

This module is the *entrypoint scaffold* for the future DAG executor
(design doc §7, Dim 2). It is deliberately **inert**:

  * It is a **no-op unless ``OMNISIGHT_DAG_EXECUTOR_ENABLED=1``**. With the
    flag unset the process logs one line and exits 0 — it claims nothing,
    writes no heartbeat, and touches no state. This is what lets the
    systemd unit ship "default OFF": even if an operator starts the unit
    by accident, an un-armed executor does nothing.
  * When armed, it runs an async poll-loop that **only ticks + heartbeats**.
    There is intentionally NO task execution here: it does not claim a
    ``dag_task``, does not run an agent, and never marks a plan
    ``completed``/``failed``. Those land in a later phase. The loop body
    is the seam where that work will hook in.

Distinct identity namespace
---------------------------
Executor instances live under the ``dag-exec-*`` instance-id namespace
(vs. the worker pool's ``wkr-*``) so the two are never confused on the
operator surface. The heartbeat reuses :func:`backend.worker._get_default_store`
(Redis when ``OMNISIGHT_REDIS_URL`` is set, else the in-memory fallback)
but writes under a DISTINCT ``omnisight:dag-exec:`` key namespace — see
:class:`DagExecHeartbeat`. It must NOT pollute the worker registry
(``omnisight:worker:active``); the dag-exec set is separate.

CLI::

    OMNISIGHT_DAG_EXECUTOR_ENABLED=1 python -m backend.dag_executor \
        --poll-interval-s 2 --heartbeat-interval-s 15

Send SIGTERM (what the systemd unit + docker-compose use) for a clean
shutdown: the loop stops, the heartbeat key is cleared, exit 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

from backend.agents import runner_sandbox
from backend import dag_storage, metrics, worker
from backend.dag_schema import DAG, Task
from backend.env_contract import _truthy, canonical_env

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables / namespace constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: Arm flag. Unset / anything-but-"1" keeps the executor a no-op.
ENABLE_ENV = "OMNISIGHT_DAG_EXECUTOR_ENABLED"

#: Deployment kill-boundary (OP-1674): a comma-separated allowlist of dag_ids
#: the armed executor may auto-run. Empty / unset ⇒ NOTHING is auto-runnable —
#: an armed executor still ticks + heartbeats but claims no plan. This is one
#: half of the dual opt-in gate; the other is the per-run metadata consent.
ALLOW_DAG_IDS_ENV = "OMNISIGHT_DAG_EXECUTOR_ALLOW_DAG_IDS"

#: Project root the :class:`PlanWorkspaceBuilder` jails task-input globs to.
#: Unset ⇒ the executor SKIPS plan execution entirely (it cannot materialise
#: a task's inputs without a root to copy them from).
PROJECT_ROOT_ENV = "OMNISIGHT_DAG_PROJECT_ROOT"

#: Per-run consent key in ``workflow_run.metadata`` — the submission half of
#: the dual opt-in gate. Must be truthy for the executor to touch the plan.
#: ``scripts/prod_smoke_test.py`` stamps it ``True`` on the smoke DAG run.
OPT_IN_METADATA_KEY = "dag_executor_opt_in"

#: Distinct instance-id namespace so dag-exec instances never collide
#: with worker ids (``wkr-*``) on the operator surface.
DAG_EXEC_INSTANCE_PREFIX = "dag-exec"

#: Heartbeat key namespace — DISTINCT from worker's ``omnisight:worker:``
#: so the worker registry (``omnisight:worker:active``) is never polluted.
DAG_EXEC_HEARTBEAT_PREFIX = "omnisight:dag-exec:"

DEFAULT_POLL_INTERVAL_S = 2.0
# Mirror the worker's registry cadence (C4 audit 2026-04-19): refresh
# every 15 s, TTL 45 s (= 3x interval, so 2 missed pings == dead).
DEFAULT_HEARTBEAT_INTERVAL_S = 15.0
DEFAULT_HEARTBEAT_TTL_S = 45

#: Default dag_plans lease lifetime. Tracks the heartbeat TTL so the
#: lease a future executor phase holds expires on the same "2 missed
#: pings == dead" budget as the operator-surface heartbeat key. The
#: authoritative default lives in :mod:`backend.dag_storage`.
DEFAULT_LEASE_TTL_S = dag_storage.DEFAULT_LEASE_TTL_S


def is_enabled() -> bool:
    """True only when ``OMNISIGHT_DAG_EXECUTOR_ENABLED`` is exactly ``"1"``."""
    return os.environ.get(ENABLE_ENV, "").strip() == "1"


def parse_allow_dag_ids(raw: str | None) -> tuple[str, ...]:
    """Parse the comma-separated dag-id allowlist, order-preserving + deduped.

    Empty / unset ⇒ an empty tuple: the executor then auto-runs nothing, which
    is what keeps even an *armed* executor default-OFF until an operator opts a
    specific dag_id in (the deployment kill-boundary, :data:`ALLOW_DAG_IDS_ENV`).
    """
    if not raw:
        return ()
    seen: dict[str, None] = {}
    for part in raw.split(","):
        did = part.strip()
        if did:
            seen.setdefault(did, None)
    return tuple(seen)


def metadata_opt_in(metadata: dict[str, Any] | None) -> bool:
    """True when a run's metadata carries a truthy dual-opt-in consent.

    Accepts a JSON ``true`` (the ``prod_smoke_test`` submission shape) as well
    as the ``1/true/yes/on`` string forms (reusing the env-contract truthy
    semantics) so a hand-set string consent is honoured too. A missing key /
    falsey value / no metadata ⇒ NOT opted in (the plan is left untouched)."""
    if not metadata:
        return False
    val = metadata.get(OPT_IN_METADATA_KEY)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return _truthy(val)
    return bool(val)


def new_instance_id(prefix: str = DAG_EXEC_INSTANCE_PREFIX) -> str:
    host = socket.gethostname().split(".")[0]
    return f"{prefix}-{host}-{uuid.uuid4().hex[:8]}"


def ensure_dag_exec_namespace(instance_id: str) -> str:
    """Force an instance id into the ``dag-exec-*`` namespace.

    A bare / mis-prefixed id (e.g. an operator passing ``--instance-id 1``)
    is normalised to ``dag-exec-1`` so the heartbeat can never masquerade
    as a worker.
    """
    iid = (instance_id or "").strip()
    if not iid:
        return new_instance_id()
    if iid == DAG_EXEC_INSTANCE_PREFIX or iid.startswith(
        DAG_EXEC_INSTANCE_PREFIX + "-"
    ):
        return iid
    return f"{DAG_EXEC_INSTANCE_PREFIX}-{iid}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Heartbeat (distinct namespace over the shared store)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class DagExecHeartbeat:
    """Heartbeat writer for the DAG executor skeleton.

    Reuses :func:`backend.worker._get_default_store` (Redis when
    configured, else the in-memory fallback) but writes under the
    ``omnisight:dag-exec:`` namespace rather than calling the store's
    ``register``/``heartbeat`` methods — those are hard-wired to the
    worker prefix (``omnisight:worker:``) and the worker active set, so
    using them would pollute the worker registry. We instead drive the
    backing client directly (Redis) or stash a namespaced key in the
    in-memory store (single-process dev / tests).
    """

    def __init__(self, store: Any | None = None) -> None:
        self._store = store if store is not None else worker._get_default_store()
        # RedisHeartbeatStore exposes ``_client``; the memory store doesn't.
        self._client = getattr(self._store, "_client", None)

    # ─── key helpers ─────────────────────────────────────────────
    def _alive_key(self, instance_id: str) -> str:
        return f"{DAG_EXEC_HEARTBEAT_PREFIX}{instance_id}:alive"

    def _set_key(self) -> str:
        return f"{DAG_EXEC_HEARTBEAT_PREFIX}active"

    # ─── writes ──────────────────────────────────────────────────
    def beat(self, instance_id: str, info: dict[str, Any], ttl_s: int) -> None:
        """Set the alive key (with TTL) + add to the dag-exec active set."""
        if self._client is not None:
            payload = json.dumps(info, sort_keys=True, default=str)
            pipe = self._client.pipeline()
            pipe.set(self._alive_key(instance_id), payload, ex=ttl_s)
            pipe.sadd(self._set_key(), instance_id)
            pipe.execute()
        else:
            # In-memory fallback: the store keys by an opaque string, so a
            # fully-namespaced key keeps dag-exec entries from ever looking
            # like a worker id to ``worker list``.
            self._store.heartbeat(self._alive_key(instance_id), dict(info), ttl_s)

    def clear(self, instance_id: str) -> None:
        """Drop the alive key + remove from the active set (graceful exit)."""
        if self._client is not None:
            pipe = self._client.pipeline()
            pipe.delete(self._alive_key(instance_id))
            pipe.srem(self._set_key(), instance_id)
            pipe.execute()
        else:
            self._store.deregister(self._alive_key(instance_id))

    # ─── reads (operator / test surface) ─────────────────────────
    def is_alive(self, instance_id: str) -> bool:
        if self._client is not None:
            return bool(self._client.exists(self._alive_key(instance_id)))
        return self._store.get_info(self._alive_key(instance_id)) is not None

    def get_info(self, instance_id: str) -> dict[str, Any] | None:
        if self._client is not None:
            raw = self._client.get(self._alive_key(instance_id))
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
        return self._store.get_info(self._alive_key(instance_id))

    def list_active(self) -> list[str]:
        if self._client is not None:
            members = self._client.smembers(self._set_key()) or set()
            live: list[str] = []
            for m in members:
                iid = m.decode() if isinstance(m, bytes) else m
                if self._client.exists(self._alive_key(iid)):
                    live.append(iid)
                else:
                    self._client.srem(self._set_key(), iid)
            return sorted(live)
        # Memory store: recover instance ids from our namespaced keys only,
        # so worker ids living in the same dict are never reported here.
        prefix, suffix = DAG_EXEC_HEARTBEAT_PREFIX, ":alive"
        out: list[str] = []
        for key in self._store.list_active():
            if key.startswith(prefix) and key.endswith(suffix):
                out.append(key[len(prefix):-len(suffix)])
        return sorted(out)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config + result
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class DagExecutorConfig:
    instance_id: str
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    heartbeat_ttl_s: int = DEFAULT_HEARTBEAT_TTL_S
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S
    max_ticks: int | None = None  # tests / one-shot harness; None == forever

    def __post_init__(self) -> None:
        # Identity is load-bearing for the "never looks like a worker"
        # guarantee — normalise here so every construction path is safe.
        self.instance_id = ensure_dag_exec_namespace(self.instance_id)


@dataclass
class DagExecutorResult:
    """What :meth:`DagExecutor.run` returns — handy for tests + ExecStop."""

    enabled: bool
    ticks: int = 0
    heartbeats: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Executor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class DagExecutor:
    """Inert async poll-loop skeleton for the DAG executor (Phase 1).

    Lifecycle::

        ex = DagExecutor(config)
        ex.install_signal_handlers(loop)   # SIGTERM/SIGINT -> request_stop
        await ex.run()                     # no-op if not armed; else loops

    ``run()`` never executes DAG tasks and never transitions a plan; it
    only ticks and heartbeats while armed.
    """

    def __init__(
        self,
        config: DagExecutorConfig,
        *,
        heartbeat: DagExecHeartbeat | None = None,
        enabled: bool | None = None,
        storage=None,
        workflow=None,
        task_runner: Callable[[list[str], Path, float], tuple[int, str, str]]
        | None = None,
    ) -> None:
        self.config = config
        self.heartbeat = heartbeat if heartbeat is not None else DagExecHeartbeat()
        # ``enabled`` override is for tests; production reads the env flag.
        self._enabled = is_enabled() if enabled is None else enabled
        # ``storage`` / ``workflow`` / ``task_runner`` default to the real
        # modules / subprocess runner in production; they are injectable so the
        # run() loop seam can be exercised in-harness with in-memory doubles
        # (mirroring the test_dag_executor_terminal.py pattern). ``_workflow``
        # stays ``None`` until needed so the heavy workflow import is lazy.
        self._storage = storage if storage is not None else dag_storage
        self._workflow = workflow
        self._task_runner = task_runner
        self._stop = asyncio.Event()
        self._started_at = 0.0
        self._ticks = 0
        self._heartbeats = 0

    # ─── control ─────────────────────────────────────────────────
    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        """Wire SIGTERM / SIGINT to a graceful stop on the running loop."""
        loop = loop or asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                # Some runtimes (Windows, non-main thread) can't register
                # loop signal handlers — the systemd unit still delivers
                # SIGTERM; without a handler the default terminates us,
                # which for an inert skeleton is also clean.
                logger.debug("could not install signal handler for %s", sig)

    # ─── main loop ───────────────────────────────────────────────
    async def run(self) -> DagExecutorResult:
        if not self._enabled:
            logger.info(
                "dag-executor %s disabled (set %s=1 to arm) — no-op exit",
                self.config.instance_id, ENABLE_ENV,
            )
            return DagExecutorResult(enabled=False, ticks=0, heartbeats=0)

        self._started_at = time.time()
        logger.info(
            "dag-executor %s armed (poll=%.1fs, heartbeat=%.1fs/ttl=%ds) — "
            "SKELETON: ticks + heartbeat only, no task execution",
            self.config.instance_id, self.config.poll_interval_s,
            self.config.heartbeat_interval_s, self.config.heartbeat_ttl_s,
        )
        self._beat("starting")
        last_beat = time.monotonic()
        try:
            while not self._stop.is_set():
                if (self.config.max_ticks is not None
                        and self._ticks >= self.config.max_ticks):
                    break

                # ── SEAM (OP-1674): claim → execute → finalize ────
                # When armed, claim ONE ready 'executing' plan that clears the
                # dual opt-in gate, drive it through the EXISTING
                # record_and_finalize_plan, then release the lease — one plan
                # per tick. Stays a NO-OP whenever the deployment allowlist is
                # unset, OMNISIGHT_DAG_PROJECT_ROOT is unset, or no plan is
                # both ready AND opted-in: that is the default-OFF posture
                # (an armed-but-unconfigured executor still only ticks here).
                try:
                    await self._maybe_claim_and_run_one()
                except Exception:  # a single plan tick must never kill the loop
                    logger.exception(
                        "dag-executor %s: plan tick failed",
                        self.config.instance_id,
                    )
                self._ticks += 1

                now = time.monotonic()
                if now - last_beat >= self.config.heartbeat_interval_s:
                    self._beat("alive")
                    last_beat = now

                # Sleep poll_interval, but wake immediately on stop.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.config.poll_interval_s,
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self._clear()
            logger.info(
                "dag-executor %s stopped (ticks=%d, heartbeats=%d)",
                self.config.instance_id, self._ticks, self._heartbeats,
            )
        return DagExecutorResult(
            enabled=True, ticks=self._ticks, heartbeats=self._heartbeats,
        )

    # ─── helpers ─────────────────────────────────────────────────
    def _beat(self, status: str) -> None:
        try:
            self.heartbeat.beat(
                self.config.instance_id, self._info(status),
                self.config.heartbeat_ttl_s,
            )
            self._heartbeats += 1
        except Exception as exc:  # heartbeat is best-effort, never fatal
            logger.warning(
                "dag-executor %s heartbeat failed: %s",
                self.config.instance_id, exc,
            )
        # OP-1665: operator-surface heartbeat metrics. Kept INDEPENDENT of the
        # store write above (and itself best-effort) so the liveness gauge still
        # reflects a running executor through a Redis blip, and a metric error
        # can never break the poll loop.
        self._record_beat_metrics()

    def _record_beat_metrics(self) -> None:
        """Bump the Prometheus heartbeat surface for this instance (OP-1665)."""
        iid = self.config.instance_id
        try:
            metrics.dag_executor_up.labels(instance_id=iid).set(1)
            metrics.dag_executor_heartbeat_total.labels(instance_id=iid).inc()
            metrics.dag_executor_last_heartbeat_timestamp_seconds.labels(
                instance_id=iid,
            ).set(time.time())
        except Exception:  # the metric surface must never fault the loop
            logger.debug(
                "dag-executor %s heartbeat metric update failed", iid,
                exc_info=True,
            )

    def _clear(self) -> None:
        try:
            self.heartbeat.clear(self.config.instance_id)
        except Exception as exc:
            logger.warning(
                "dag-executor %s heartbeat clear failed: %s",
                self.config.instance_id, exc,
            )
        # OP-1665: flip the liveness gauge to 0 on graceful stop — independent
        # of the store clear above so a scrape never shows a stopped executor
        # as still up.
        try:
            metrics.dag_executor_up.labels(
                instance_id=self.config.instance_id,
            ).set(0)
        except Exception:
            logger.debug(
                "dag-executor %s heartbeat-down metric update failed",
                self.config.instance_id, exc_info=True,
            )

    def _info(self, status: str) -> dict[str, Any]:
        return {
            "instance_id": self.config.instance_id,
            "kind": "dag-executor",
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "status": status,
            "started_at": self._started_at,
            "ticks": self._ticks,
            "enabled": self._enabled,
        }

    # ─── dag_plans lease (OP-1656) ───────────────────────────────
    #
    # Thin owner-scoped wrappers over the compare-and-set primitives in
    # :mod:`backend.dag_storage`. The executor's ``instance_id`` (already
    # forced into the ``dag-exec-*`` namespace) is the lease owner, so a
    # claimed plan is attributable to the exact instance on the operator
    # surface.
    #
    # IMPORTANT: these are deliberately NOT called from :meth:`run`'s loop.
    # The Phase-1 executor stays inert — the SEAM in ``run()`` claims
    # nothing. These methods ship in the backend image as the primitive a
    # later (still-gated) phase will call at that seam; wiring them now lets
    # the lease be exercised by tests without arming task execution.

    async def claim_plan(
        self, plan_id: int, *, conn=None,
    ) -> Optional["dag_storage.PlanLease"]:
        """Try to claim ``plan_id`` for this executor instance.

        Returns the granted :class:`~backend.dag_storage.PlanLease`, or
        ``None`` if another live owner holds it (or the plan is gone).
        """
        return await self._storage.claim_plan(
            plan_id, self.config.instance_id,
            lease_ttl_s=self.config.lease_ttl_s, conn=conn,
        )

    async def renew_plan_lease(
        self, lease: "dag_storage.PlanLease", *, conn=None,
    ) -> Optional["dag_storage.PlanLease"]:
        """Heartbeat-renew a lease this instance holds.

        Returns the refreshed lease, or ``None`` if it was lost (lapsed or
        reclaimed) — the cue to stop driving that plan.
        """
        return await self._storage.renew_lease(
            lease.plan_id, lease.owner, lease.token,
            lease_ttl_s=self.config.lease_ttl_s, conn=conn,
        )

    async def release_plan_lease(
        self, lease: "dag_storage.PlanLease", *, conn=None,
    ) -> bool:
        """Release a lease this instance holds (idempotent)."""
        return await self._storage.release_lease(
            lease.plan_id, lease.owner, lease.token, conn=conn,
        )

    # ─── run() loop seam: claim → execute → finalize (OP-1674) ───
    #
    # The methods below are what :meth:`run`'s SEAM calls each tick once the
    # executor is armed. They ONLY wire the already-merged pieces — the OP-1656
    # lease (above), discovery via :func:`dag_storage.list_plans`,
    # :class:`PlanWorkspaceBuilder` + :class:`LocalTaskHandler`, and
    # :func:`record_and_finalize_plan` — behind the dual opt-in gate. There is
    # NO new claim / scheduling / handler / step-recording / terminal logic
    # here (a ticket MUST-NOT), and NO background lease-renewer: a build that
    # outlives the lease TTL is an accepted fast-smoke-MVP limitation.

    async def _maybe_claim_and_run_one(self) -> Optional["PlanTerminalResult"]:
        """Discover → dual-gate → claim → finalize → release ONE plan per tick.

        Returns the :class:`PlanTerminalResult` of the plan driven this tick,
        or ``None`` when nothing ran: the allowlist is unset, the project root
        is unset, no ready+opted-in plan exists, or the claim was lost to a
        live owner.

        Dual opt-in gate — a plan is TOUCHED only when BOTH hold:

          1. its ``dag_id`` is in :data:`ALLOW_DAG_IDS_ENV` (discovery iterates
             only those ids — the deployment kill-boundary), AND
          2. its run's ``metadata.dag_executor_opt_in`` is truthy (the per-
             submission consent).

        Either missing ⇒ the plan is left ENTIRELY untouched (never claimed),
        which is what stops a stale / bootstrap 'executing' row from ever being
        auto-run.
        """
        allow = parse_allow_dag_ids(os.environ.get(ALLOW_DAG_IDS_ENV))
        if not allow:
            return None  # no kill-boundary set ⇒ nothing is auto-runnable

        project_root = (os.environ.get(PROJECT_ROOT_ENV) or "").strip()
        if not project_root:
            logger.info(
                "dag-executor %s: %s unset — skipping plan execution this tick",
                self.config.instance_id, PROJECT_ROOT_ENV,
            )
            return None

        for dag_id in allow:
            plan = await self._first_executing_plan(dag_id)
            if plan is None:
                continue
            if not await self._run_opted_in(plan):
                logger.info(
                    "dag-executor %s: plan=%s dag=%s executing but not "
                    "opted-in — left untouched",
                    self.config.instance_id, plan.id, dag_id,
                )
                continue
            lease = await self.claim_plan(plan.id)
            if lease is None:
                logger.info(
                    "dag-executor %s: plan=%s dag=%s held by another live "
                    "owner — skipping", self.config.instance_id, plan.id, dag_id,
                )
                continue
            try:
                handler = self._build_handler(Path(project_root))
                result = await record_and_finalize_plan(
                    plan, handler=handler,
                    workflow=self._workflow, storage=self._storage,
                )
                logger.info(
                    "dag-executor %s: drove plan=%s dag=%s -> %s "
                    "(%d step(s) recorded)", self.config.instance_id,
                    plan.id, dag_id, result.status, len(result.recorded),
                )
                return result
            finally:
                # acquire-before / release-after, one plan per tick. No
                # background renewer: a build outliving the lease TTL is an
                # accepted fast-smoke limitation (ticket scope).
                await self.release_plan_lease(lease)
        return None

    async def _first_executing_plan(
        self, dag_id: str,
    ) -> Optional["dag_storage.StoredPlan"]:
        """The first 'executing' plan for ``dag_id`` (mutation-round order).

        Discovery is :func:`dag_storage.list_plans` (already round-ordered) +
        a Python ``status == 'executing'`` filter — deliberately NOT a new
        status-keyed query / ``list_executing_plans`` / status index (a ticket
        MUST-NOT)."""
        for plan in await self._storage.list_plans(dag_id):
            if plan.status == "executing":
                return plan
        return None

    async def _run_opted_in(self, plan: "dag_storage.StoredPlan") -> bool:
        """The per-run consent half of the dual gate: the plan's run carries a
        truthy ``metadata.dag_executor_opt_in``.

        A plan with no ``run_id``, a missing run row, or an absent/falsey flag
        is NOT opted in (returns ``False``)."""
        run_id = plan.run_id
        if not run_id:
            return False
        run = await self._wf().get_run(run_id)
        metadata = getattr(run, "metadata", None) if run is not None else None
        return metadata_opt_in(metadata)

    def _build_handler(self, project_root: Path) -> "LocalTaskHandler":
        """Bind the EXISTING :class:`LocalTaskHandler` over a
        :class:`PlanWorkspaceBuilder` jailed to ``project_root``
        (:data:`PROJECT_ROOT_ENV`).

        ``self._task_runner`` is ``None`` in production (→ the real
        :func:`_run_local` subprocess runner) and injectable for the in-harness
        double, mirroring ``LocalTaskHandler``'s own ``runner`` seam."""
        builder = PlanWorkspaceBuilder(project_root=project_root)
        return LocalTaskHandler(
            workspace_builder=builder, runner=self._task_runner,
        )

    def _wf(self):
        """The workflow module (or injected double) used for run-metadata reads.

        Lazy: production keeps ``self._workflow`` ``None`` and resolves the real
        module on first use, so importing this module never drags in the heavy
        :mod:`backend.workflow` graph."""
        return self._workflow if self._workflow is not None else _import_workflow()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Serial topological scheduler (OP-1657)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Once a plan is claimed (OP-1656 lease) a single executor drives it by
# walking ``dag.tasks`` in dependency order, SERIALLY — one task at a
# time, no concurrency. This is the ordering substrate later tickets hang
# real task dispatch off of; it deliberately ships with a no-op task
# handler so the scheduler can be merged **inert** (the seam in
# :meth:`DagExecutor.run` does not call it yet).
#
# The walk reuses the SAME in-degree-0 ready-set logic as
# :func:`backend.sandbox_prewarm.pick_prewarm_candidates` (declaration-order
# ready set → deterministic, stable replay during incident review), extended
# from "pick the first ``depth``" to "drain the whole graph". Dependencies
# pointing at unknown task ids are ignored (``if d in indeg``) exactly as the
# prewarm walk and :func:`backend.dag_validator._check_cycles` do — surfacing
# unknown deps is the validator's job, not the scheduler's.


class CycleError(RuntimeError):
    """Raised if a cyclic DAG reaches the scheduler.

    The validator (:func:`backend.dag_validator._check_cycles`, Kahn's
    algorithm) already blocks cycles before a plan is persisted as
    ``validated``, so a claimed plan should never be cyclic. This is the
    belt-and-braces guard: rather than silently dropping the unresolved
    tail of the walk, we refuse loudly so the bug is visible.
    """


def topological_order(dag: DAG) -> list[Task]:
    """Return ``dag.tasks`` in a deterministic serial topological order.

    Kahn's algorithm with a **declaration-order** ready set: among the tasks
    whose dependencies are all satisfied, the next one emitted is the one
    declared earliest in ``dag.tasks``. That determinism matters for stable
    replay during incident review (same plan → same execution order).

    Cycle-safe: if the walk cannot drain every task (a cycle the validator
    failed to catch), raises :class:`CycleError` naming the unresolved tasks
    rather than returning a truncated order.
    """
    by_id: dict[str, Task] = {t.task_id: t for t in dag.tasks}
    order_index: dict[str, int] = {t.task_id: i for i, t in enumerate(dag.tasks)}
    indeg: dict[str, int] = {t.task_id: 0 for t in dag.tasks}
    edges: dict[str, list[str]] = {t.task_id: [] for t in dag.tasks}
    for t in dag.tasks:
        for d in t.depends_on:
            if d in indeg:  # ignore deps to unknown ids — validator's job
                edges[d].append(t.task_id)
                indeg[t.task_id] += 1

    # Ready set kept sorted by declaration index so the walk is deterministic.
    ready: list[str] = sorted(
        (n for n, k in indeg.items() if k == 0), key=order_index.__getitem__
    )
    out: list[Task] = []
    while ready:
        n = ready.pop(0)
        out.append(by_id[n])
        newly: list[str] = []
        for nxt in edges[n]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                newly.append(nxt)
        if newly:
            ready.extend(newly)
            ready.sort(key=order_index.__getitem__)

    if len(out) != len(dag.tasks):
        unresolved = sorted(n for n, k in indeg.items() if k > 0)
        raise CycleError(
            f"cyclic dependency reached scheduler in dag {dag.dag_id!r}; "
            f"{len(unresolved)} task(s) unresolved: {unresolved[:10]}"
        )
    return out


@dataclass
class TaskWorkspace:
    """A prepared per-(plan, task) workspace directory.

    ``path`` follows the ``workdir_root/{plan_id}-{task_id}`` convention.
    ``copied`` lists the workspace-relative paths materialised into the task's
    scratch from its ``inputs`` — whether copied from the per-plan staging
    area (an upstream task's ``expected_output``) or from ``project_root``
    (a source seed / ``external:`` / ``user:`` input). Empty when neither
    source yields a match.
    """

    plan_id: int
    task_id: str
    path: Path
    copied: list[str] = field(default_factory=list)


#: Subtree under ``workdir_root`` where each plan's successful task outputs are
#: staged (OP-1676) so a downstream task can materialise an upstream-output
#: input by exact path. The leading underscore keeps it from ever colliding
#: with a per-task ``{plan_id}-{task_id}`` scratch dir (which starts with a
#: digit) under a shared workdir root.
PLAN_OUTPUTS_SUBDIR = "_plan_outputs"

#: Declared-input prefixes the ``dag_validator`` accepts for caller-provided
#: seeds (``external:<path>`` / ``user:<path>``). ``prepare()`` strips the
#: prefix and copies the remainder from ``project_root`` with the normal glob
#: behaviour — the raw literal (e.g. ``"external:CMakeLists.txt"``) matched no
#: glob before this fix. Mirrors ``dag_validator._INPUT_EXTERNAL_RE`` but
#: captures the path tail.
_EXTERNAL_INPUT_RE = re.compile(r"^(?:external|user):(.+)$")

#: ``expected_output`` entities that are NOT filesystem paths (the validator
#: also accepts ``git:<sha>`` / ``issue:<id>``). These have no artifact to
#: stage, so :meth:`PlanWorkspaceBuilder.stage_output` skips them.
_NON_FILE_OUTPUT_RE = re.compile(r"^(?:git|issue):")


class PlanWorkspaceBuilder:
    """Thin per-(plan, task) workspace helper for the serial scheduler.

    Reuses :mod:`backend.worker`'s module-level ``_rmtree`` / ``_copytree`` /
    ``_copyfile`` / ``_resolve_glob`` to materialise a fresh workspace at
    ``workdir_root/{plan_id}-{task_id}`` and copy in the files matched by the
    task's ``inputs`` globs (project-root jailed via ``_resolve_glob``).

    It deliberately does **NOT** call :meth:`LocalSandboxRuntime.start`: that
    path is TaskCard / git-coupled (the path-b worker flow) and would need a
    fake ``PROJECT-NUMBER`` jira_ticket + a CATC card. The serial scheduler
    only needs the thin "copy allowed inputs into a jailed workdir" behaviour,
    which is exactly the low-level helpers, minus the git/TaskCard coupling.
    """

    def __init__(
        self,
        *,
        workdir_root: Path | None = None,
        project_root: Path | None = None,
    ) -> None:
        # Distinct default subtree from the worker's ``_sandboxes`` so a
        # dag-exec plan workspace ({plan_id}-{task_id}) never collides with a
        # worker sandbox ({worker_id}-{task_id}) under a shared workdir root.
        self._root = Path(
            workdir_root
            or os.environ.get("OMNISIGHT_DAG_WORKDIR")
            or Path.cwd() / ".artifacts" / "dag_workspaces"
        )
        self._project_root = Path(project_root) if project_root is not None else None
        self._root.mkdir(parents=True, exist_ok=True)

    def prepare(self, plan_id: int, task: Task) -> TaskWorkspace:
        """Create ``workdir_root/{plan_id}-{task_id}`` and materialise inputs.

        Each declared input is honoured per the FULL dep-closure contract the
        ``dag_validator`` accepts (OP-1676):

          * an ``external:<path>`` / ``user:<path>`` seed — the prefix is
            stripped and the path copied from ``project_root`` (the normal
            glob behaviour; the raw literal matched nothing before);
          * an UPSTREAM-OUTPUT input (an input equal to an upstream task's
            ``expected_output``) — materialised by EXACT path from this plan's
            staging area (populated by :meth:`stage_output`). Staging WINS over
            ``project_root`` on a path collision;
          * any other input — copied from ``project_root`` (source seed) via
            the existing glob, exactly as before.

        Known limitation (OP-1676, scoped to first-run): on an idempotent
        re-claim a prior ``done`` task's output may already be cleaned, so the
        staging copy can be absent — the upstream-output input is then simply
        not materialised (no crash). Durable artifact restore across re-claim
        is a separate follow-up.
        """
        ws = self._root / f"{plan_id}-{task.task_id}"
        if ws.exists():
            worker._rmtree(ws)
        ws.mkdir(parents=True, exist_ok=True)

        staging_root = self._plan_staging_root(plan_id)
        copied: list[str] = []
        for inp in task.inputs:
            ext = _EXTERNAL_INPUT_RE.match(inp)
            if ext is not None:
                # external:/user: — caller-provided seed from project_root.
                copied.extend(self._copy_from_project_root(ws, ext.group(1)))
                continue

            # Upstream-output input first (staging wins on collision); then
            # fall back to project_root for any rel path staging did not cover.
            staged = self._copy_from_staging(ws, staging_root, inp)
            copied.extend(staged)
            copied.extend(
                self._copy_from_project_root(ws, inp, skip=set(staged))
            )
        return TaskWorkspace(
            plan_id=plan_id, task_id=task.task_id, path=ws, copied=copied,
        )

    # ─── input materialisation helpers ───────────────────────────
    def _plan_staging_root(self, plan_id: int) -> Path:
        """The per-plan executor-owned staging dir for upstream outputs."""
        return self._root / PLAN_OUTPUTS_SUBDIR / str(plan_id)

    def _copy_from_staging(
        self, ws: Path, staging_root: Path, input_path: str,
    ) -> list[str]:
        """Materialise an upstream-output input by EXACT path from staging.

        Returns the rel path(s) copied — empty when staging holds no such
        artifact (the producing task hasn't run yet on this builder, or its
        staged output was already cleaned on a re-claim: the documented
        first-run-only limitation). A ``..`` segment never escapes the jail.
        """
        rel = input_path.lstrip("/")
        if not rel or any(part == ".." for part in Path(rel).parts):
            return []
        src = staging_root / rel
        if not src.exists():
            return []
        self._copy_one(src, ws / rel)
        return [str(Path(rel))]

    def _copy_from_project_root(
        self, ws: Path, glob: str, *, skip: set[str] = frozenset(),  # type: ignore[assignment]
    ) -> list[str]:
        """Copy ``project_root`` glob matches into ``ws`` (project-root jailed).

        ``skip`` is the set of rel paths an upstream-staging copy already won,
        so staging takes precedence on a collision. No-op when no project root
        is bound.
        """
        if self._project_root is None:
            return []
        out: list[str] = []
        for src in worker._resolve_glob(self._project_root, glob):
            rel_str = str(src.relative_to(self._project_root))
            if rel_str in skip:
                continue  # staging already materialised this path (it wins)
            self._copy_one(src, ws / src.relative_to(self._project_root))
            out.append(rel_str)
        return out

    @staticmethod
    def _copy_one(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            worker._copytree(src, dst)
        else:
            worker._copyfile(src, dst)

    def stage_output(
        self, plan_id: int, expected_output: str, artifact: Path | None,
    ) -> Optional[Path]:
        """Stage a successful task's ``expected_output`` into the plan staging
        area, preserving its artifact-relative path (OP-1676).

        Called by :func:`record_and_finalize_plan` AFTER a task succeeds and
        BEFORE cleanup, so a downstream :meth:`prepare` can materialise a
        matching declared input by exact path. ``expected_output`` is ONE
        concrete file/dir path; a non-file io entity (``git:`` / ``issue:``)
        or a missing artifact is a no-op (returns ``None``). The downstream
        task reads this plan-level COPY — never the upstream scratch directly,
        so per-task scratch isolation is preserved.
        """
        rel = (expected_output or "").strip()
        if not rel or _NON_FILE_OUTPUT_RE.match(rel):
            return None  # non-file io entity → nothing to stage
        if any(part == ".." for part in Path(rel).parts):
            return None  # defensive: never stage outside the staging root
        if artifact is None or not artifact.exists():
            return None
        dst = self._plan_staging_root(plan_id) / rel
        self._copy_one(artifact, dst)
        return dst

    def cleanup(self, ws: TaskWorkspace) -> None:
        """Remove a prepared workspace (best-effort, mirrors sandbox.stop)."""
        try:
            worker._rmtree(ws.path)
        except OSError as exc:
            logger.warning(
                "dag plan workspace cleanup failed for %s: %s", ws.path, exc,
            )

    def cleanup_plan_staging(self, plan_id: int) -> None:
        """Remove a plan's staging area (best-effort), AFTER all tasks ran.

        Safe to call once the serial walk has finished: every downstream
        ``prepare`` that needed an upstream output has already read its COPY.
        """
        staging = self._plan_staging_root(plan_id)
        try:
            worker._rmtree(staging)
        except OSError as exc:
            logger.warning(
                "dag plan staging cleanup failed for %s: %s", staging, exc,
            )


@dataclass
class TaskRun:
    """The scheduler's per-task ordering record.

    ``order_index`` is the 0-based position the task occupied in the serial
    walk — the load-bearing "ordering state later tickets need". ``status``
    is ``"ran"`` under the default no-op handler; real terminal states land
    in a later phase.
    """

    task_id: str
    order_index: int
    workspace: Optional[Path] = None
    status: str = "ran"


@dataclass
class PlanRunResult:
    """Outcome of one serial topological walk of a plan."""

    plan_id: int
    dag_id: str
    order: list[str]  # task_ids in execution order
    runs: list[TaskRun] = field(default_factory=list)


#: A task handler runs one task inside its prepared workspace. The default
#: (:func:`_noop_task_handler`) does nothing — the scheduler ships inert and a
#: later ticket injects the real dispatcher here. ``workspace`` is ``None``
#: when the scheduler runs without a :class:`PlanWorkspaceBuilder`.
TaskHandler = Callable[[Task, Optional[TaskWorkspace]], Awaitable[Any]]


async def _noop_task_handler(task: Task, workspace: Optional[TaskWorkspace]) -> None:
    """Default handler: record-ordering only, no execution (inert merge)."""
    logger.debug("dag scheduler: ordered task %s (no-op handler)", task.task_id)
    return None


class SerialPlanScheduler:
    """Drive a claimed plan by walking its DAG in dependency order, serially.

    Construction is cheap and side-effect-free; :meth:`run_plan` does the
    walk. Execution is strictly **serial** — each task's handler is awaited to
    completion before the next task starts (no ``gather`` / no concurrency, an
    explicit MUST-NOT for this ticket). Tasks are emitted in the deterministic
    :func:`topological_order`.

    The default ``task_handler`` is a no-op, so a default-constructed scheduler
    only computes + records ordering — which is what lets OP-1657 merge inert.
    Later tickets inject a real handler (and a :class:`PlanWorkspaceBuilder`)
    without changing the ordering contract.
    """

    def __init__(
        self,
        *,
        workspace_builder: PlanWorkspaceBuilder | None = None,
        task_handler: TaskHandler | None = None,
    ) -> None:
        self.workspace_builder = workspace_builder
        self.task_handler = task_handler or _noop_task_handler

    async def run_plan(
        self, plan_id: int, dag: DAG, *, cleanup: bool = False,
    ) -> PlanRunResult:
        """Walk ``dag`` in topological order, running each task serially.

        For each task, in dependency order: optionally prepare its workspace
        (when a builder is bound), await the handler, then record the
        ordering. ``cleanup=True`` removes each workspace after its handler
        returns (handy for tests / disk-tight hosts); the default keeps them.
        """
        order = topological_order(dag)
        result = PlanRunResult(
            plan_id=plan_id, dag_id=dag.dag_id,
            order=[t.task_id for t in order],
        )
        logger.info(
            "dag scheduler: plan=%s dag=%s order=%s (serial, %d task(s))",
            plan_id, dag.dag_id, result.order, len(order),
        )
        for idx, task in enumerate(order):
            ws: Optional[TaskWorkspace] = None
            if self.workspace_builder is not None:
                ws = self.workspace_builder.prepare(plan_id, task)
            try:
                await self.task_handler(task, ws)
            finally:
                if ws is not None and cleanup and self.workspace_builder is not None:
                    self.workspace_builder.cleanup(ws)
            result.runs.append(TaskRun(
                task_id=task.task_id, order_index=idx,
                workspace=(ws.path if ws is not None else None),
            ))
        return result

    async def run_stored_plan(
        self, plan: "dag_storage.StoredPlan", *, cleanup: bool = False,
    ) -> PlanRunResult:
        """Convenience: walk a claimed :class:`~backend.dag_storage.StoredPlan`.

        Re-hydrates the DAG from the stored JSON and forwards to
        :meth:`run_plan`. This is the shape the (still-gated) executor seam
        will call after :meth:`DagExecutor.claim_plan` grants the lease.
        """
        return await self.run_plan(plan.id, plan.dag(), cleanup=cleanup)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Local task handlers (cmake / make / python3) — OP-1658
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Design doc §7 (codex Q7). This slice gives the serial scheduler a REAL
# task handler for the three **local** toolchains — and ONLY those three.
# Each task runs in its own per-plan scratch workspace
# (``workdir_root/{plan_id}-{task_id}`` via :class:`PlanWorkspaceBuilder`,
# materialised from ``Task.inputs`` using worker.py's low-level helpers —
# NOT ``LocalSandboxRuntime.start`` which is TaskCard/git-coupled). We
# capture the process ``rc`` + ``stdout``/``stderr`` and the artifact that
# landed at the task's ``expected_output``.
#
# Fail-closed scope rules (each FAILS the task — never crashes):
#   * a non-``t1`` ``required_tier`` (``networked`` / ``t3``) — out of this
#     local-only slice;
#   * an unknown / non-local ``toolchain`` (anything but cmake/make/python3);
#   * an ``expected_output`` that escapes the workspace;
#   * the toolchain exiting non-zero, or not producing the artifact.
#
# Workspace lifecycle: the handler CREATES + RETURNS the workspace on the
# result; the CALLER (a later step-recording / terminal ticket) owns
# cleanup so the artifact can be read first. We never delete it here. The
# fail-closed side-effect guard for nonlocal toolchains beyond "just FAIL"
# is a SEPARATE ticket's job.

@dataclass(frozen=True)
class ToolchainSpec:
    """Registry entry for a DAG task toolchain."""

    command_seq: tuple[tuple[str, ...], ...] | None
    slice: str
    env_requires: tuple[str, ...] = ()

#: The only tier this slice executes. ``networked`` / ``t3`` tasks FAIL as
#: out-of-slice rather than running on the local host.
LOCAL_TIER = "t1"

#: The only non-local slice this ticket enables: Android gradle builds inside
#: the existing host sandbox with network explicitly allowed.
BUILD_NETWORKED_SLICE = "build-networked"

#: Toolchain registry. Unknown entries FAIL the task (not a crash) — per the
#: ticket MUST-NOT (no cross-compile / flash / remote / publish here).
TOOLCHAIN_REGISTRY: dict[str, ToolchainSpec] = {
    "cmake": ToolchainSpec(command_seq=None, slice="local"),
    "make": ToolchainSpec(command_seq=(("make",),), slice="local"),
    "python3": ToolchainSpec(command_seq=None, slice="local"),
    "gradle": ToolchainSpec(
        command_seq=(("gradle", "assembleDebug", "test"),),
        slice=BUILD_NETWORKED_SLICE,
        env_requires=("ANDROID_HOME",),
    ),
}

#: Toolchains this local slice knows how to run. Kept as a compatibility
#: export for the OP-1658 tests and callers.
LOCAL_TOOLCHAINS: tuple[str, ...] = tuple(
    name for name, spec in TOOLCHAIN_REGISTRY.items() if spec.slice == "local"
)

#: Per-task subprocess wall-clock budget (seconds). Mirrors the
#: ``build_adapters._run`` default; injectable per-handler for tests.
DEFAULT_TASK_TIMEOUT_S = 600

#: Sub-directory cmake configures + builds into, relative to the workspace.
CMAKE_BUILD_DIR = "build"


class _LocalHandlerError(Exception):
    """Internal — a pre-flight handler problem that FAILS the task.

    Used for prepare-time conditions (e.g. a ``python3`` task with no ``.py``
    input) so :meth:`LocalTaskHandler.run` can translate them into a FAILED
    :class:`LocalTaskResult` instead of raising out of the handler.
    """


@dataclass
class LocalTaskResult:
    """Outcome of running one task through a local toolchain handler.

    ``status`` is ``"ok"`` only when the toolchain exited 0 **and** the
    declared artifact landed at ``expected_output`` inside the workspace;
    otherwise ``"failed"`` with a human-readable ``reason``. ``workspace``
    is the scratch dir the handler created (``None`` only when the task
    FAILED a gate before any workspace was built) — the caller owns its
    cleanup. ``artifact`` is the resolved ``expected_output`` path when it
    was produced, else ``None``.
    """

    task_id: str
    plan_id: int
    toolchain: str
    status: str                       # "ok" | "failed"
    rc: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    workspace: Optional[Path] = None
    artifact: Optional[Path] = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _run_local(
    argv: list[str], cwd: Path, timeout_s: float,
) -> tuple[int, str, str]:
    """Run one local command in ``cwd``; return ``(rc, stdout, stderr)``.

    Never raises: a missing binary maps to rc 127 and a timeout to rc 124
    (mirroring :func:`backend.build_adapters._run`) so the handler can fold
    either into a FAILED task rather than an exception.
    """
    import subprocess

    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or ""), f"timeout after {timeout_s}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def _resolve_expected_output(workspace: Path, expected_output: str) -> Path:
    """Resolve a workspace-relative ``expected_output`` inside ``workspace``.

    ``expected_output`` is workspace-relative by contract. An absolute path,
    a ``..`` segment, or anything that resolves outside the workspace raises
    :class:`ValueError` — the caller turns that into a FAILED task (a path
    escaping the workspace must never let a task read/write outside its
    jail). Mirrors the jail check in :func:`backend.worker._resolve_glob`.
    """
    rel = (expected_output or "").strip()
    p = Path(rel)
    if not rel or p.is_absolute() or any(part == ".." for part in p.parts):
        raise ValueError(
            f"expected_output {expected_output!r} escapes the workspace"
        )
    candidate = workspace / rel
    try:
        candidate.resolve().relative_to(workspace.resolve())
    except (ValueError, OSError):
        raise ValueError(
            f"expected_output {expected_output!r} escapes the workspace"
        )
    return candidate


class LocalTaskHandler:
    """Toolchain → handler dispatch for the three **local** toolchains.

    Construct with a :class:`PlanWorkspaceBuilder` (the per-plan scratch
    bridge) and call :meth:`run` per task. The handler:

      1. gates on ``required_tier == "t1"`` and a known local ``toolchain``
         (else FAIL — not a crash);
      2. prepares + RETURNS a fresh ``{plan_id}-{task_id}`` workspace
         (materialised from ``Task.inputs``);
      3. runs the toolchain's command sequence serially in that workspace,
         capturing ``rc`` / ``stdout`` / ``stderr``;
      4. resolves the workspace-relative ``expected_output`` (escape → FAIL)
         and requires it to exist for ``status == "ok"``.

    It never cleans the workspace up — the caller owns that so artifacts can
    be read first.

    ``runner`` is injectable (``(argv, cwd, timeout_s) -> (rc, out, err)``)
    so unit tests can exercise the dispatch / gating / artifact logic
    without spawning real ``cmake`` / ``make`` processes.
    """

    def __init__(
        self,
        *,
        workspace_builder: "PlanWorkspaceBuilder",
        timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
        runner: Callable[[list[str], Path, float], tuple[int, str, str]]
        | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.workspace_builder = workspace_builder
        self.timeout_s = timeout_s
        self._runner = runner or _run_local
        self._env = os.environ if env is None else env

    # ─── public API ──────────────────────────────────────────────
    async def run(self, plan_id: int, task: Task) -> LocalTaskResult:
        """Run ``task`` locally for ``plan_id``; return a result, never raise."""
        # ── gate 1: known toolchain ───────────────────────────────
        spec = TOOLCHAIN_REGISTRY.get(task.toolchain)
        if spec is None:
            return self._fail(
                plan_id, task, workspace=None,
                reason=(
                    f"unknown toolchain {task.toolchain!r} "
                    f"(registered: {', '.join(TOOLCHAIN_REGISTRY)})"
                ),
            )
        # ── gate 2: tier/slice ────────────────────────────────────
        expected_tier = LOCAL_TIER if spec.slice == "local" else spec.slice
        if task.required_tier != expected_tier:
            return self._fail(
                plan_id, task, workspace=None,
                reason=(
                    f"required_tier {task.required_tier!r} does not match "
                    f"{task.toolchain!r} slice {spec.slice!r} "
                    f"(expected {expected_tier!r})"
                ),
            )
        # ── gate 3: required environment ──────────────────────────
        missing = [
            name for name in spec.env_requires
            if not str(self._env.get(name, "")).strip()
        ]
        if missing:
            return self._fail(
                plan_id, task, workspace=None,
                reason=(
                    f"{task.toolchain} requires env var(s): "
                    f"{', '.join(missing)}"
                ),
            )

        # ── build the per-plan scratch workspace (handler owns creation) ──
        ws = self.workspace_builder.prepare(plan_id, task)

        try:
            commands = self._command_sequence(task, ws, spec)
        except _LocalHandlerError as exc:
            return self._fail(plan_id, task, workspace=ws.path, reason=str(exc))

        # ── run the command sequence serially; stop at first non-zero ──
        rc = 0
        out_parts: list[str] = []
        err_parts: list[str] = []
        for argv in commands:
            rc, out, err = await asyncio.to_thread(
                self._runner, argv, ws.path, self.timeout_s,
            )
            out_parts.append(out)
            err_parts.append(err)
            if rc != 0:
                break
        stdout = "".join(out_parts)
        stderr = "".join(err_parts)

        if rc != 0:
            return self._fail(
                plan_id, task, workspace=ws.path, rc=rc,
                stdout=stdout, stderr=stderr,
                reason=f"{task.toolchain} exited rc={rc}",
            )

        # ── resolve + verify the declared artifact ─────────────────
        try:
            artifact = _resolve_expected_output(ws.path, task.expected_output)
        except ValueError as exc:
            return self._fail(
                plan_id, task, workspace=ws.path, rc=rc,
                stdout=stdout, stderr=stderr, reason=str(exc),
            )
        if not artifact.exists():
            return self._fail(
                plan_id, task, workspace=ws.path, rc=rc,
                stdout=stdout, stderr=stderr,
                reason=(
                    f"expected_output {task.expected_output!r} not produced "
                    f"by {task.toolchain}"
                ),
            )

        logger.info(
            "dag local handler: plan=%s task=%s toolchain=%s OK -> %s",
            plan_id, task.task_id, task.toolchain, task.expected_output,
        )
        return LocalTaskResult(
            task_id=task.task_id, plan_id=plan_id, toolchain=task.toolchain,
            status="ok", rc=rc, stdout=stdout, stderr=stderr,
            workspace=ws.path, artifact=artifact,
        )

    # ─── command derivation ──────────────────────────────────────
    def _command_sequence(
        self, task: Task, ws: TaskWorkspace, spec: ToolchainSpec,
    ) -> list[list[str]]:
        """Map a local toolchain to the command(s) to run in the workspace.

        cmake → configure + build (two commands, run in order);
        make  → ``make`` (default target);
        python3 → run the first ``.py`` in ``inputs`` with this interpreter.
        gradle → wrap ``gradle assembleDebug test`` in the host sandbox
        with network explicitly allowed.
        """
        tc = task.toolchain
        if tc == "cmake":
            return [
                ["cmake", "-S", ".", "-B", CMAKE_BUILD_DIR],
                ["cmake", "--build", CMAKE_BUILD_DIR],
            ]
        if tc == "make":
            return [list(argv) for argv in spec.command_seq or ()]
        if tc == "python3":
            return [[sys.executable, self._python_entry(task)]]
        if tc == "gradle":
            return [
                runner_sandbox.wrap_in_bubblewrap(
                    list(argv),
                    worktree_path=ws.path,
                    ticket_key=f"dag-plan-{ws.plan_id}-{ws.task_id}",
                    network=True,
                    env=self._env,
                )
                for argv in spec.command_seq or ()
            ]
        # Unreachable: run() already gated on TOOLCHAIN_REGISTRY.
        raise _LocalHandlerError(f"no command mapping for toolchain {tc!r}")

    @staticmethod
    def _python_entry(task: Task) -> str:
        """The ``.py`` script a python3 task runs: first such entry in inputs.

        The returned path must match the filename :meth:`PlanWorkspaceBuilder.
        prepare` materialised into the scratch. ``prepare`` STRIPS the
        ``external:``/``user:`` prefix off a declared input (via
        ``_EXTERNAL_INPUT_RE``) and copies the bare tail, so a declared
        ``external:run_test.py`` lands as ``run_test.py``. We mirror that same
        prefix-strip here before selecting/returning the ``.py`` entry —
        otherwise we'd run ``python3 external:run_test.py`` against a scratch
        that only holds ``run_test.py`` → ``can't open file`` → rc=2 (OP-1677).

        No ``.py`` input → :class:`_LocalHandlerError` (the task FAILS) — we
        do not guess an entry point.
        """
        for inp in task.inputs:
            ext = _EXTERNAL_INPUT_RE.match(inp.strip())
            entry = ext.group(1) if ext is not None else inp.strip()
            if entry.endswith(".py"):
                return entry
        raise _LocalHandlerError(
            "python3 toolchain requires a .py file in inputs to run"
        )

    # ─── result helper ───────────────────────────────────────────
    @staticmethod
    def _fail(
        plan_id: int, task: Task, *,
        workspace: Optional[Path], reason: str,
        rc: Optional[int] = None, stdout: str = "", stderr: str = "",
    ) -> LocalTaskResult:
        logger.info(
            "dag local handler: plan=%s task=%s toolchain=%s FAIL — %s",
            plan_id, task.task_id, task.toolchain, reason,
        )
        return LocalTaskResult(
            task_id=task.task_id, plan_id=plan_id, toolchain=task.toolchain,
            status="failed", rc=rc, stdout=stdout, stderr=stderr,
            workspace=workspace, reason=reason,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Step recording + terminal wiring (OP-1659)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Design doc §7 (codex E5/E6). This slice closes the executor loop ON TOP of
# the OP-1657 serial walk + OP-1658 local handlers:
#
#   * one IDEMPOTENT ``workflow.step`` per task, carrying ``dag_task_id``;
#   * ALL tasks ok   -> set_status(plan,'completed') + workflow.finish(run,'completed');
#   * ANY task fails -> set_status(plan,'failed')    + workflow.finish(run,'failed');
#   * the executor OWNS per-plan workspace cleanup, AFTER the steps are recorded
#     (so a task's artifact is recorded before its scratch is removed).
#
# Idempotent re-claim
# -------------------
# A plan already in a terminal state ('completed' / 'failed') was fully
# recorded by a prior run — we short-circuit and touch nothing (no duplicate
# steps, no illegal re-transition). A plan still 'executing' (a mid-flight
# crash, then re-claim) is RESUMED: each task whose step is already *done* is
# skipped (cache-hit, handler not re-run); a task with a prior *failed* step
# is honoured as a failure (no retry); the rest run for the first time.
#
# MUST-NOT (ticket scope): no new export / billing / skills / retry /
# side-effect behaviour. We only record per-task steps + terminal statuses
# and clean the scratch. This function is NOT wired into :meth:`DagExecutor.run`'s
# loop seam — like every prior slice it ships INERT (a still-gated phase calls
# it after a lease is granted), which is the "merged inert" Go-Live contract.

#: Plan statuses that mean "already fully recorded" — a re-claim of one of
#: these is a no-op (re-transitioning out of them is illegal anyway).
_TERMINAL_PLAN_STATUSES = frozenset({"completed", "failed"})

#: Run-metadata marker stamped on every workflow_run the DAG executor
#: finalizes (OP-1661). Executor runs newly surface as status='completed'
#: once the executor lands; this marker is the signal
#: :mod:`backend.finetune_export` uses to exclude them from the fine-tune
#: corpus. ``finetune_export._EXECUTOR_RUN_MARKER`` MUST equal this string
#: (a cross-module test asserts it) — they are kept as separate literals so
#: the lightweight exporter need not import this module's heavy graph.
EXECUTOR_RUN_METADATA_MARKER = "dag_executor_run"


def dag_step_key(task_id: str) -> str:
    """The ``workflow_steps.idempotency_key`` for a task: ``dag-task:{id}``.

    Stable per (run, task) so a re-claim looks the recorded step back up by
    the same key — that lookup is the "skip already-done" resume hinge.
    """
    return f"dag-task:{task_id}"


@dataclass
class PlanTerminalResult:
    """Outcome of recording a plan's steps + wiring its terminal status.

    ``status`` is the terminal plan/run status this invocation drove the plan
    to (``"completed"`` / ``"failed"``); ``recorded`` is the ``dag_task_id``s
    that have a step (newly recorded this invocation **or** skipped because
    already done). ``results`` holds the :class:`LocalTaskResult` for tasks
    actually run here (empty on a re-claim no-op). ``already_terminal`` is
    True when the plan was found already in a terminal state and nothing ran.
    """

    plan_id: int
    run_id: str
    status: str                       # "completed" | "failed"
    recorded: list[str] = field(default_factory=list)
    results: list["LocalTaskResult"] = field(default_factory=list)
    already_terminal: bool = False


def _step_output(result: "LocalTaskResult") -> dict[str, Any]:
    """The JSON-able step payload cached for an ok task (audit / replay)."""
    return {
        "toolchain": result.toolchain,
        "rc": result.rc,
        "artifact": str(result.artifact) if result.artifact is not None else None,
        "status": result.status,
    }


def _skipped_step_output(task: Task) -> dict[str, Any]:
    """The step payload for a task skip-cascaded by an upstream continue-failure.

    Carries ``status="skipped"`` as the durable truth — the task never ran
    because a (transitive) ``depends_on`` upstream failed under
    ``on_failure="continue"`` and its output is gone (OP-1832). Recorded
    WITHOUT an ``error`` so the step reads as :attr:`StepRecord.is_done`: a
    re-claim treats it as already-accounted-for, not as a fresh failure.
    """
    return {
        "toolchain": task.toolchain,
        "rc": None,
        "artifact": None,
        "status": "skipped",
    }


def _transitive_dependents(dag: DAG, task_id: str) -> set[str]:
    """All task_ids that (transitively) depend on ``task_id`` (excludes it).

    Follows ``depends_on`` edges FORWARD: every task reachable downstream of
    ``task_id`` has lost a (transitive) input, so when ``task_id`` fails under
    ``on_failure="continue"`` the whole set is skip-cascaded (OP-1832).
    """
    children: dict[str, list[str]] = {t.task_id: [] for t in dag.tasks}
    for t in dag.tasks:
        for d in t.depends_on:
            if d in children:
                children[d].append(t.task_id)
    out: set[str] = set()
    stack = list(children.get(task_id, []))
    while stack:
        n = stack.pop()
        if n in out:
            continue
        out.add(n)
        stack.extend(children.get(n, []))
    return out


def _cleanup_plan_workspaces(
    handler: "LocalTaskHandler", results: list["LocalTaskResult"],
) -> None:
    """Best-effort removal of each task's scratch, AFTER steps are recorded.

    Reuses the handler's :class:`PlanWorkspaceBuilder` so cleanup goes through
    the same ``worker._rmtree`` path the rest of the module uses. A task that
    failed a gate before any workspace was built (``workspace is None``) has
    nothing to clean.
    """
    builder = getattr(handler, "workspace_builder", None)
    for r in results:
        if r.workspace is None:
            continue
        ws = TaskWorkspace(plan_id=r.plan_id, task_id=r.task_id, path=r.workspace)
        if builder is not None:
            builder.cleanup(ws)
        else:  # pragma: no cover - handler always carries a builder
            try:
                worker._rmtree(r.workspace)
            except OSError as exc:
                logger.warning(
                    "dag plan workspace cleanup failed for %s: %s",
                    r.workspace, exc,
                )


def _stage_task_output(
    handler: "LocalTaskHandler", plan_id: int, task: Task,
    result: "LocalTaskResult",
) -> None:
    """Stage an ok task's ``expected_output`` into the per-plan staging area so
    a downstream task's :meth:`PlanWorkspaceBuilder.prepare` can materialise it
    as a declared input (OP-1676).

    Routes through the handler's :class:`PlanWorkspaceBuilder` so the staged
    COPY lives under the same workdir root the downstream prepare() reads. A
    non-file io entity / missing artifact is a no-op; staging failures are
    logged but never fault the walk (a downstream task simply won't find the
    input, which it then surfaces as its own failure).
    """
    builder = getattr(handler, "workspace_builder", None)
    if builder is None or result.artifact is None:
        return
    try:
        builder.stage_output(plan_id, task.expected_output, result.artifact)
    except OSError as exc:
        logger.warning(
            "dag plan output staging failed for plan=%s task=%s: %s",
            plan_id, task.task_id, exc,
        )


def _cleanup_plan_staging(handler: "LocalTaskHandler", plan_id: int) -> None:
    """Best-effort removal of a plan's staging area, AFTER the walk + steps."""
    builder = getattr(handler, "workspace_builder", None)
    if builder is not None:
        builder.cleanup_plan_staging(plan_id)


async def record_and_finalize_plan(
    plan: "dag_storage.StoredPlan",
    *,
    handler: "LocalTaskHandler",
    workflow=None,
    storage=None,
    cleanup: bool = True,
) -> PlanTerminalResult:
    """Run ``plan``'s tasks, record one idempotent step each, wire the terminal
    plan status + ``workflow.finish``, then clean each task's scratch.

    The tasks are walked in :func:`topological_order` and run SERIALLY through
    ``handler`` (OP-1658's local toolchain handler). For each task a single
    idempotent ``workflow.record_dag_step`` is written carrying its
    ``dag_task_id``. A task failure branches on ``task.on_failure`` (OP-1832):

      * ``"abort"`` (default): the walk stops, the plan + run land ``failed`` —
        unchanged from OP-1659, so existing plans are unaffected.
      * ``"continue"``: the failure is recorded, the failed task's TRANSITIVE
        dependents are recorded ``skipped`` (their upstream output is gone), and
        the walk CONTINUES with the tasks outside that subtree.

    The plan lands ``failed`` iff an ``abort``-failure occurred; otherwise it
    ``completed`` — continue-failures and their skipped dependents are carried
    by the per-task step records, not the terminal status. Workspace cleanup
    runs LAST, after every step is durably recorded.

    Idempotent re-claim: if ``plan`` is already terminal nothing runs (no-op);
    if it is still ``executing`` (a mid-flight crash) already-done tasks are
    skipped and a prior failed task is honoured without re-running.

    ``workflow`` / ``storage`` default to :mod:`backend.workflow` /
    :mod:`backend.dag_storage`; both are injectable so the wiring can be
    exercised in-harness without a database.
    """
    wf = workflow if workflow is not None else _import_workflow()
    ds = storage if storage is not None else dag_storage

    run_id = plan.run_id
    if not run_id:
        raise ValueError(
            f"plan {plan.id} has no run_id — cannot record steps / finish a run"
        )
    dag = plan.dag()

    # ── idempotent re-claim: a terminal plan was already fully recorded ──
    current = await ds.get_plan(plan.id)
    if current.status in _TERMINAL_PLAN_STATUSES:
        recorded = [
            s.dag_task_id for s in await wf.list_steps(run_id) if s.dag_task_id
        ]
        logger.info(
            "dag finalize: plan=%s already terminal (%s) — re-claim no-op",
            plan.id, current.status,
        )
        return PlanTerminalResult(
            plan_id=plan.id, run_id=run_id, status=current.status,
            recorded=recorded, already_terminal=True,
        )

    order = topological_order(dag)
    results: list[LocalTaskResult] = []
    recorded: list[str] = []
    # Tasks transitively downstream of an ``on_failure="continue"`` failure:
    # their upstream output is gone, so they are recorded ``skipped`` and never
    # run (OP-1832). Populated lazily as continue-failures are observed; because
    # the walk is topological, a task is always added here BEFORE it is reached.
    skipped: set[str] = set()
    # The plan only lands ``failed`` when an ``on_failure="abort"`` task failed.
    # A continue-failure (and its skip-cascade) is recorded per-task but leaves
    # the plan free to complete — the step records carry the truth.
    abort_failed = False
    for task in order:
        key = dag_step_key(task.task_id)
        prior = await wf.get_step(run_id, key)

        if task.task_id in skipped:
            # Skip-cascaded by an upstream continue-failure: record the skip
            # once (idempotent on re-claim) and move on — never run the handler.
            if prior is None:
                await wf.record_dag_step(
                    run_id, key, dag_task_id=task.task_id,
                    output=_skipped_step_output(task),
                )
            recorded.append(task.task_id)
            logger.info(
                "dag finalize: plan=%s task=%s skipped — upstream "
                "continue-failure", plan.id, task.task_id,
            )
            continue

        if prior is not None:
            # Resume: this task already has a recorded outcome — honour it
            # without re-running (no retry). Done == ok/skip; not-done == a
            # prior failure, whose effect now depends on ``on_failure``.
            recorded.append(task.task_id)
            if prior.is_done:
                logger.info(
                    "dag finalize: plan=%s task=%s step already done — skip",
                    plan.id, task.task_id,
                )
                continue
            if task.on_failure == "continue":
                logger.info(
                    "dag finalize: plan=%s task=%s prior failed step — "
                    "continue (skip-cascade dependents)",
                    plan.id, task.task_id,
                )
                skipped |= _transitive_dependents(dag, task.task_id)
                continue
            logger.info(
                "dag finalize: plan=%s task=%s has a prior failed step — "
                "plan fails (no retry)", plan.id, task.task_id,
            )
            abort_failed = True
            break

        result = await handler.run(plan.id, task)
        results.append(result)
        if result.ok:
            await wf.record_dag_step(
                run_id, key, dag_task_id=task.task_id,
                output=_step_output(result),
            )
            recorded.append(task.task_id)
            # Stage the produced artifact so a downstream task's prepare() can
            # materialise it as an upstream-output input (OP-1676). AFTER the
            # step is durably recorded, BEFORE cleanup runs at the end.
            _stage_task_output(handler, plan.id, task, result)
        else:
            await wf.record_dag_step(
                run_id, key, dag_task_id=task.task_id,
                error=(result.reason or "task failed")[:512],
            )
            recorded.append(task.task_id)
            if task.on_failure == "continue":
                # Non-critical subsystem: record the failure, skip the tasks
                # downstream of it, and keep executing the rest of the walk.
                logger.info(
                    "dag finalize: plan=%s task=%s failed (on_failure=continue)"
                    " — skip-cascade dependents, continue walk",
                    plan.id, task.task_id,
                )
                skipped |= _transitive_dependents(dag, task.task_id)
                continue
            abort_failed = True
            break  # on_failure=abort (default): stop the walk at this failure

    terminal = "failed" if abort_failed else "completed"
    # ── tag the run as executor-produced BEFORE finishing it, so the
    #    'completed' row finetune_export sees already carries the marker
    #    (OP-1661 — executor runs must not pollute the fine-tune corpus).
    await _mark_executor_run(wf, run_id)
    # ── terminal wiring: plan status first, then finish the run ──
    await ds.set_status(plan.id, terminal)
    await wf.finish(run_id, terminal)
    logger.info(
        "dag finalize: plan=%s run=%s -> %s (%d step(s) recorded)",
        plan.id, run_id, terminal, len(recorded),
    )

    # ── own per-plan workspace cleanup, AFTER the steps are recorded ──
    if cleanup:
        _cleanup_plan_workspaces(handler, results)
        # The plan staging area (OP-1676) is read by downstream prepare() during
        # the walk above; with the walk done it is safe to drop too.
        _cleanup_plan_staging(handler, plan.id)

    return PlanTerminalResult(
        plan_id=plan.id, run_id=run_id, status=terminal,
        recorded=recorded, results=results,
    )


async def _mark_executor_run(workflow, run_id: str) -> None:
    """Stamp :data:`EXECUTOR_RUN_METADATA_MARKER` into the run's metadata so
    :mod:`backend.finetune_export` excludes it from the training corpus
    (OP-1661).

    Best-effort + idempotent: if the run already carries the marker we touch
    nothing, and a transient optimistic-lock version race is re-read and
    retried once. A failure here is logged but never blocks the terminal
    wiring — the exporter's ``dag_task_id`` backstop keeps the run out of the
    corpus even if this stamp is missed.
    """
    for _attempt in range(2):
        run = await workflow.get_run(run_id)
        if run is None:
            return
        md = getattr(run, "metadata", None) or {}
        if md.get(EXECUTOR_RUN_METADATA_MARKER):
            return  # already tagged (idempotent re-claim / prior attempt)
        try:
            await workflow.update_run_metadata(
                run_id, run.version, {EXECUTOR_RUN_METADATA_MARKER: True},
            )
            return
        except Exception as exc:  # VersionConflict or similar — re-read + retry
            logger.debug(
                "executor-run marker write retry for run=%s: %s", run_id, exc,
            )
    logger.warning(
        "could not stamp executor-run marker on run=%s — relying on the "
        "finetune_export dag_task_id backstop", run_id,
    )


def _import_workflow():
    """Lazy import of :mod:`backend.workflow` (avoids a heavy import at module
    load — workflow pulls in the db pool / billing surface)."""
    from backend import workflow
    return workflow


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fail-closed side-effect guard (OP-1660)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Design doc §7 Dim 6 + codex E8. Modelled on :mod:`backend.env_contract`'s
# fail-closed style: a gate the (still-gated) NONLOCAL task handlers MUST
# call *before* performing an irreversible / outward-facing side effect —
# pushing to Gerrit, publishing to a registry, opening an SSH session,
# flashing hardware, or writing a prod database.
#
# Fail-closed contract — the side effect is REFUSED unless BOTH:
#   1. ``OMNISIGHT_ENV`` canonicalises to ``"prod"``
#      (reusing :func:`backend.env_contract.canonical_env`), AND
#   2. the explicit per-capability allow flag ``OMNISIGHT_ALLOW_<CAPABILITY>``
#      is truthy (reusing the same ``1/true/yes/on`` semantics as the
#      env-DB contract via :func:`backend.env_contract._truthy`).
# Anything else — dev/staging/unset env, a missing/false allow flag, or an
# unrecognised capability — raises :class:`SideEffectBlocked`, aborting the
# task BEFORE any network/action call is made.
#
# Unlike the env-DB contract there is deliberately NO test/CI carveout: the
# whole point of this guard is that fake creds present on a dev/staging host
# (or inside a test) can NEVER push. Tests exercise the *allowed* path by
# passing ``env="prod"`` + setting the allow flag, never by bypassing.

#: The guarded side-effect capabilities. Each names an irreversible /
#: outward-facing action a future nonlocal handler could take.
GERRIT_PUSH = "gerrit_push"
REGISTRY_PUBLISH = "registry_publish"
SSH = "ssh"
HARDWARE_FLASH = "hardware_flash"
PROD_DB_WRITE = "prod_db_write"

SIDE_EFFECT_CAPABILITIES: frozenset[str] = frozenset({
    GERRIT_PUSH, REGISTRY_PUBLISH, SSH, HARDWARE_FLASH, PROD_DB_WRITE,
})

#: Prefix of the explicit per-capability allow-flag env var. The full var
#: is ``OMNISIGHT_ALLOW_<CAPABILITY-UPPERCASED>`` (e.g.
#: ``OMNISIGHT_ALLOW_GERRIT_PUSH``).
SIDE_EFFECT_ALLOW_PREFIX = "OMNISIGHT_ALLOW_"


def allow_flag_env(capability: str) -> str:
    """The env var that must be truthy (in prod) to permit ``capability``."""
    return f"{SIDE_EFFECT_ALLOW_PREFIX}{capability.upper()}"


class SideEffectBlocked(RuntimeError):
    """Raised (fail-closed) when a guarded side effect is refused.

    A task-level abort, NOT a process exit (contrast
    :class:`backend.env_contract.EnvContractViolation`, which exits 78): it
    propagates out of the handler so the scheduler fails that one task while
    the executor keeps running. ``capability`` / ``env`` / ``source`` are
    preserved as attributes for structured handling and assertions.
    """

    def __init__(
        self, capability: str, *, source: str, env: str | None, reason: str,
    ) -> None:
        self.capability = capability
        self.source = source
        self.env = env
        self.reason = reason
        super().__init__(
            f"side-effect {capability!r} blocked "
            f"(source={source}, env={env!r}): {reason}"
        )
        logger.critical(
            "SideEffectBlocked: capability=%s source=%s env=%r — %s",
            capability, source, env, reason,
        )


def guard_side_effect(
    capability: str, *, source: str, env: str | None = None,
) -> None:
    """Fail closed unless ``OMNISIGHT_ENV=prod`` AND the per-capability flag.

    Call this BEFORE any network/action call. Returns ``None`` when the side
    effect is permitted; otherwise raises :class:`SideEffectBlocked` (so the
    guarded action never runs). ``source`` is a short call-site label for the
    error/log message; ``env`` overrides ``OMNISIGHT_ENV`` (used by tests).
    """
    canon = canonical_env(
        env if env is not None else os.environ.get("OMNISIGHT_ENV")
    )

    # Unknown capability → refuse (fail-closed on an unrecognised gate; a
    # typo'd capability must never silently fall through to "allowed").
    if capability not in SIDE_EFFECT_CAPABILITIES:
        raise SideEffectBlocked(
            capability, source=source, env=canon,
            reason=(
                "unknown side-effect capability — the guarded set is "
                f"{sorted(SIDE_EFFECT_CAPABILITIES)}"
            ),
        )

    # Gate 1: only a prod process may EVER perform a guarded side effect.
    if canon != "prod":
        raise SideEffectBlocked(
            capability, source=source, env=canon,
            reason=(
                f"refused outside prod (OMNISIGHT_ENV={canon!r}) — a guarded "
                "side effect requires OMNISIGHT_ENV=prod"
            ),
        )

    # Gate 2: prod still needs the explicit per-capability allow flag.
    flag = allow_flag_env(capability)
    if not _truthy(os.environ.get(flag)):
        raise SideEffectBlocked(
            capability, source=source, env=canon,
            reason=(
                f"prod env but allow flag {flag} is not set — set {flag}=1 to "
                "explicitly permit this capability"
            ),
        )

    logger.warning(
        "side-effect %s PERMITTED (source=%s, env=prod, %s set) — proceeding",
        capability, source, flag,
    )


def perform_side_effect(
    capability: str,
    action: Callable[[], Any],
    *,
    source: str,
    env: str | None = None,
) -> Any:
    """Guard, then run ``action`` (the network/side-effecting call).

    ``action`` is a zero-arg callable invoked ONLY after
    :func:`guard_side_effect` passes, so a blocked capability aborts the task
    BEFORE any network call. Returns whatever ``action`` returns. This is the
    seam a nonlocal handler wraps its real push/publish/flash call in.
    """
    guard_side_effect(capability, source=source, env=env)
    return action()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI entry-point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backend.dag_executor",
        description=(
            "OmniSight DAG executor (inert Phase-1 skeleton). No-op unless "
            f"{ENABLE_ENV}=1."
        ),
    )
    p.add_argument("--instance-id", default=None,
                   help="override the auto-generated dag-exec-* instance id")
    p.add_argument("--poll-interval-s", type=float,
                   default=DEFAULT_POLL_INTERVAL_S)
    p.add_argument("--heartbeat-interval-s", type=float,
                   default=DEFAULT_HEARTBEAT_INTERVAL_S)
    p.add_argument("--heartbeat-ttl-s", type=int,
                   default=DEFAULT_HEARTBEAT_TTL_S)
    p.add_argument("--max-ticks", type=int, default=None,
                   help="stop after N ticks (tests / one-shot)")
    p.add_argument("--log-level", default="INFO")
    return p


async def _run_main(executor: DagExecutor) -> int:
    loop = asyncio.get_running_loop()
    executor.install_signal_handlers(loop)
    await executor.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    cfg = DagExecutorConfig(
        instance_id=args.instance_id or new_instance_id(),
        poll_interval_s=args.poll_interval_s,
        heartbeat_interval_s=args.heartbeat_interval_s,
        heartbeat_ttl_s=args.heartbeat_ttl_s,
        max_ticks=args.max_ticks,
    )
    executor = DagExecutor(cfg)
    return asyncio.run(_run_main(executor))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "ENABLE_ENV",
    "ALLOW_DAG_IDS_ENV",
    "PROJECT_ROOT_ENV",
    "OPT_IN_METADATA_KEY",
    "parse_allow_dag_ids",
    "metadata_opt_in",
    "DAG_EXEC_INSTANCE_PREFIX",
    "DAG_EXEC_HEARTBEAT_PREFIX",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_HEARTBEAT_TTL_S",
    "DEFAULT_LEASE_TTL_S",
    "DagExecHeartbeat",
    "DagExecutor",
    "DagExecutorConfig",
    "DagExecutorResult",
    "CycleError",
    "PlanRunResult",
    "PlanWorkspaceBuilder",
    "PLAN_OUTPUTS_SUBDIR",
    "SerialPlanScheduler",
    "TaskHandler",
    "TaskRun",
    "TaskWorkspace",
    "ToolchainSpec",
    "TOOLCHAIN_REGISTRY",
    "LOCAL_TOOLCHAINS",
    "LOCAL_TIER",
    "BUILD_NETWORKED_SLICE",
    "DEFAULT_TASK_TIMEOUT_S",
    "CMAKE_BUILD_DIR",
    "LocalTaskHandler",
    "LocalTaskResult",
    "PlanTerminalResult",
    "EXECUTOR_RUN_METADATA_MARKER",
    "dag_step_key",
    "record_and_finalize_plan",
    "GERRIT_PUSH",
    "REGISTRY_PUBLISH",
    "SSH",
    "HARDWARE_FLASH",
    "PROD_DB_WRITE",
    "SIDE_EFFECT_CAPABILITIES",
    "SIDE_EFFECT_ALLOW_PREFIX",
    "allow_flag_env",
    "guard_side_effect",
    "perform_side_effect",
    "SideEffectBlocked",
    "ensure_dag_exec_namespace",
    "is_enabled",
    "main",
    "new_instance_id",
    "topological_order",
]
