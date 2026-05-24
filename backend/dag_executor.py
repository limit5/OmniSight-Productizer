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
import signal
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from backend import dag_storage, worker
from backend.dag_schema import DAG, Task

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables / namespace constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: Arm flag. Unset / anything-but-"1" keeps the executor a no-op.
ENABLE_ENV = "OMNISIGHT_DAG_EXECUTOR_ENABLED"

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
    ) -> None:
        self.config = config
        self.heartbeat = heartbeat if heartbeat is not None else DagExecHeartbeat()
        # ``enabled`` override is for tests; production reads the env flag.
        self._enabled = is_enabled() if enabled is None else enabled
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

                # ── SEAM ──────────────────────────────────────────
                # A future phase claims one ready plan here and drives it —
                # via :meth:`claim_plan` / renew / release (OP-1656) and the
                # :class:`SerialPlanScheduler` topological walk (OP-1657).
                # The Phase-1 skeleton deliberately does NOTHING: no claim,
                # no scheduler run, no terminal transition. The scheduler +
                # lease primitives ship in the image so a later (still-gated)
                # phase can wire them here without re-touching this loop.
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

    def _clear(self) -> None:
        try:
            self.heartbeat.clear(self.config.instance_id)
        except Exception as exc:
            logger.warning(
                "dag-executor %s heartbeat clear failed: %s",
                self.config.instance_id, exc,
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
        return await dag_storage.claim_plan(
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
        return await dag_storage.renew_lease(
            lease.plan_id, lease.owner, lease.token,
            lease_ttl_s=self.config.lease_ttl_s, conn=conn,
        )

    async def release_plan_lease(
        self, lease: "dag_storage.PlanLease", *, conn=None,
    ) -> bool:
        """Release a lease this instance holds (idempotent)."""
        return await dag_storage.release_lease(
            lease.plan_id, lease.owner, lease.token, conn=conn,
        )


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
    ``copied`` lists the project-relative paths materialised from the task's
    ``inputs`` globs (empty when no project root is bound).
    """

    plan_id: int
    task_id: str
    path: Path
    copied: list[str] = field(default_factory=list)


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
        """Create ``workdir_root/{plan_id}-{task_id}`` and copy in inputs."""
        ws = self._root / f"{plan_id}-{task.task_id}"
        if ws.exists():
            worker._rmtree(ws)
        ws.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        if self._project_root is not None:
            for glob in task.inputs:
                for src in worker._resolve_glob(self._project_root, glob):
                    rel = src.relative_to(self._project_root)
                    dst = ws / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if src.is_dir():
                        worker._copytree(src, dst)
                    else:
                        worker._copyfile(src, dst)
                    copied.append(str(rel))
        return TaskWorkspace(
            plan_id=plan_id, task_id=task.task_id, path=ws, copied=copied,
        )

    def cleanup(self, ws: TaskWorkspace) -> None:
        """Remove a prepared workspace (best-effort, mirrors sandbox.stop)."""
        try:
            worker._rmtree(ws.path)
        except OSError as exc:
            logger.warning(
                "dag plan workspace cleanup failed for %s: %s", ws.path, exc,
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

#: Toolchains this local slice knows how to run. Anything else FAILS the
#: task (not a crash) — per the ticket MUST-NOT (no cross-compile / flash /
#: remote / publish here).
LOCAL_TOOLCHAINS: tuple[str, ...] = ("cmake", "make", "python3")

#: The only tier this slice executes. ``networked`` / ``t3`` tasks FAIL as
#: out-of-slice rather than running on the local host.
LOCAL_TIER = "t1"

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
    ) -> None:
        self.workspace_builder = workspace_builder
        self.timeout_s = timeout_s
        self._runner = runner or _run_local

    # ─── public API ──────────────────────────────────────────────
    async def run(self, plan_id: int, task: Task) -> LocalTaskResult:
        """Run ``task`` locally for ``plan_id``; return a result, never raise."""
        # ── gate 1: tier (this slice is t1/local only) ─────────────
        if task.required_tier != LOCAL_TIER:
            return self._fail(
                plan_id, task, workspace=None,
                reason=(
                    f"required_tier {task.required_tier!r} is not local "
                    f"({LOCAL_TIER}-only slice — networked/t3 out of slice)"
                ),
            )
        # ── gate 2: known local toolchain ─────────────────────────
        if task.toolchain not in LOCAL_TOOLCHAINS:
            return self._fail(
                plan_id, task, workspace=None,
                reason=(
                    f"unknown/nonlocal toolchain {task.toolchain!r} "
                    f"(local handlers: {', '.join(LOCAL_TOOLCHAINS)})"
                ),
            )

        # ── build the per-plan scratch workspace (handler owns creation) ──
        ws = self.workspace_builder.prepare(plan_id, task)

        try:
            commands = self._command_sequence(task, ws)
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
    def _command_sequence(self, task: Task, ws: TaskWorkspace) -> list[list[str]]:
        """Map a local toolchain to the command(s) to run in the workspace.

        cmake → configure + build (two commands, run in order);
        make  → ``make`` (default target);
        python3 → run the first ``.py`` in ``inputs`` with this interpreter.
        """
        tc = task.toolchain
        if tc == "cmake":
            return [
                ["cmake", "-S", ".", "-B", CMAKE_BUILD_DIR],
                ["cmake", "--build", CMAKE_BUILD_DIR],
            ]
        if tc == "make":
            return [["make"]]
        if tc == "python3":
            return [[sys.executable, self._python_entry(task)]]
        # Unreachable: run() already gated on LOCAL_TOOLCHAINS.
        raise _LocalHandlerError(f"no command mapping for toolchain {tc!r}")

    @staticmethod
    def _python_entry(task: Task) -> str:
        """The ``.py`` script a python3 task runs: first such entry in inputs.

        No ``.py`` input → :class:`_LocalHandlerError` (the task FAILS) — we
        do not guess an entry point.
        """
        for inp in task.inputs:
            if inp.strip().endswith(".py"):
                return inp.strip()
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
    "SerialPlanScheduler",
    "TaskHandler",
    "TaskRun",
    "TaskWorkspace",
    "LOCAL_TOOLCHAINS",
    "LOCAL_TIER",
    "DEFAULT_TASK_TIMEOUT_S",
    "CMAKE_BUILD_DIR",
    "LocalTaskHandler",
    "LocalTaskResult",
    "ensure_dag_exec_namespace",
    "is_enabled",
    "main",
    "new_instance_id",
    "topological_order",
]
