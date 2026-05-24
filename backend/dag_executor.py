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
from dataclasses import dataclass
from typing import Any, Optional

from backend import dag_storage, worker

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
                # A future phase claims one ready dag_task here and
                # dispatches it — via :meth:`claim_plan` / renew / release
                # (OP-1656). The Phase-1 skeleton deliberately does NOTHING:
                # no claim, no agent run, no terminal transition.
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
    "ensure_dag_exec_namespace",
    "is_enabled",
    "main",
    "new_instance_id",
]
