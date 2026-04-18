"""O3 (#266) — Stateless Agent Worker Pool.

A ``Worker`` is the small, long-lived process that turns CATC payloads
sitting on the queue (O2 / ``backend/queue_backend.py``) into actual
sandbox-executed agent runs that get committed and pushed to Gerrit
for human review.  Workers are stateless: any number can run on any
host, all draining the same shared queue.  Crash-safety comes from
upstream:

  * **Queue visibility timeout** (O2) — if the worker dies mid-task,
    the message is automatically re-queued by ``sweep_visibility()``.
  * **Distributed file-path lock** (O1) — held leases auto-expire if
    the worker stops sending heartbeats; another worker takes the
    file scope.
  * **Redis ``workers:active`` set + per-worker heartbeat key with
    TTL** — the operator surface (and, later, the orchestrator) can
    spot a dead worker within 45 s (post-C4 audit 2026-04-19; previously
    90 s). Note this is the *registry* TTL, not file-path dist-lock TTL.
    File-path locks still expire on their own 30-min schedule; a
    dead-worker lock cleanup cron is a follow-up row.

The runtime stays pluggable so unit tests don't need real Docker or
Gerrit:

  * ``AgentExecutor`` — runs the actual agent step inside a sandbox
    workspace and returns a ``AgentResult`` (commit message + files
    touched).  Default implementation is a stub that just records the
    invocation; production wires this to ``backend/container.py``.
  * ``SandboxRuntime`` — provisions a workspace directory whose
    bind-mount surface is restricted to the CATC card's
    ``impact_scope.allowed`` paths and tears it down after.  Default
    is the local-fs implementation (``LocalSandboxRuntime``); prod
    can swap in ``DockerSandboxRuntime`` (uses ``container.py``).
  * ``GerritPusher`` — pushes the resulting commit to
    ``refs/for/main`` and records the Change-Id.  Default is a
    no-op echo; prod uses ``backend/git_review.push_for_review``.

CLI::

    python -m backend.worker run \
        --capacity 2 \
        --tenant-filter t-acme \
        --capability-filter firmware,vision

Send SIGTERM to gracefully drain (stop claiming new work, finish
in-flight tasks, release locks, deregister) — same signal both the
systemd unit and the docker-compose profile use.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from backend import dist_lock, metrics, queue_backend
from backend.catc import TaskCard
from backend.queue_backend import (
    QueueMessage,
    TaskState,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_CAPACITY = 1
# C4 audit 2026-04-19: halved from 30s / 90s to 15s / 45s so a SIGKILL'd
# worker drops from the `workers:active` registry inside 45 s instead of
# 90 s. Refresh interval stays at TTL/3 (1.5x headroom for missed pings).
# Note: this is ONLY the registry heartbeat — file-path dist-locks still
# expire on DEFAULT_LOCK_TTL_S (30 min). A follow-up row should add a
# dead-worker lock-release cron that keys off this registry TTL so a
# SIGKILL'd worker's paths are reclaimable within ~45 s, not 30 min.
DEFAULT_HEARTBEAT_INTERVAL_S = 15      # refresh every 15 s
DEFAULT_HEARTBEAT_TTL_S = 45           # 3x interval — 2 missed pings = dead
DEFAULT_VISIBILITY_TIMEOUT_S = 5 * 60  # spec: workers ack within 5 min
DEFAULT_LOOP_IDLE_S = 1.0              # idle backoff when queue is empty
DEFAULT_LOCK_WAIT_S = 0.0              # don't block — re-queue and try later
DEFAULT_LOCK_TTL_S = 30 * 60           # mirror dist_lock.DEFAULT_TTL_S
GERRIT_PUSH_MAX_RETRIES = 3
GERRIT_PUSH_BACKOFF_S = (1.0, 4.0, 15.0)

WORKER_HEARTBEAT_KEY = "worker:{wid}:alive"
WORKER_REGISTRY_SET = "workers:active"
WORKER_HEARTBEAT_PREFIX = "omnisight:worker:"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Result dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class AgentResult:
    """What an ``AgentExecutor`` returns after running one task.

    ``ok=True`` means the agent produced a coherent commit; the worker
    will then ``commit + push`` it to Gerrit.  ``ok=False`` triggers a
    ``nack`` with the rendered ``reason``/``stack`` (so the queue's
    3-strike rule + DLQ policy stays the source of truth for "give up").
    """

    ok: bool
    commit_message: str = ""
    files_touched: list[str] = field(default_factory=list)
    reason: str = ""
    stack: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GerritPushResult:
    """Outcome of pushing a worker's commit to Gerrit."""

    ok: bool
    change_id: str = ""
    review_url: str = ""
    attempts: int = 1
    reason: str = ""


@dataclass
class WorkerTaskOutcome:
    """Per-message audit trail produced by a single ``Worker.handle()``."""

    message_id: str
    task_id: str
    jira_ticket: str
    status: str                          # acked | nacked | dlq | locked_skipped
    agent_result: AgentResult | None = None
    gerrit: GerritPushResult | None = None
    error: str | None = None
    elapsed_s: float = 0.0


@dataclass
class WorkerConfig:
    """All knobs a ``Worker`` accepts.  Construct from CLI or fixtures."""

    worker_id: str
    capacity: int = DEFAULT_CAPACITY
    tenant_filter: list[str] = field(default_factory=list)
    capability_filter: list[str] = field(default_factory=list)
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    heartbeat_ttl_s: int = DEFAULT_HEARTBEAT_TTL_S
    visibility_timeout_s: float = DEFAULT_VISIBILITY_TIMEOUT_S
    loop_idle_s: float = DEFAULT_LOOP_IDLE_S
    lock_wait_s: float = DEFAULT_LOCK_WAIT_S
    lock_ttl_s: float = DEFAULT_LOCK_TTL_S
    max_messages: int | None = None     # tests / one-shot drain
    pull_count: int | None = None       # default = capacity
    project_root: Path = field(default_factory=lambda: Path.cwd())

    def effective_pull_count(self) -> int:
        return max(1, self.pull_count if self.pull_count is not None
                   else self.capacity)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pluggable runtime protocols
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SandboxHandle:
    """Opaque per-task handle returned from ``SandboxRuntime.start``.

    Carries the workspace directory + any runtime-specific bookkeeping
    needed for ``execute`` / ``stop``.
    """

    def __init__(self, workspace: Path, runtime_id: str = "",
                 metadata: dict[str, Any] | None = None) -> None:
        self.workspace = workspace
        self.runtime_id = runtime_id
        self.metadata: dict[str, Any] = metadata or {}


class SandboxRuntime(Protocol):
    """Workspace + execution sandbox for a single task.

    Implementations MUST honour ``allowed_paths``: nothing outside that
    set should be physically reachable from inside the sandbox.  The
    default ``LocalSandboxRuntime`` enforces this by copying-only the
    allowed paths into a tmp workspace and refusing the launch when an
    allowed glob escapes the project root.
    """

    def start(self, *, worker_id: str, task_id: str,
              card: TaskCard, project_root: Path) -> SandboxHandle: ...

    def commit(self, handle: SandboxHandle, *,
               commit_message: str) -> str: ...

    def stop(self, handle: SandboxHandle) -> None: ...


class AgentExecutor(Protocol):
    """Runs the agent step inside a prepared sandbox workspace."""

    def run(self, *, handle: SandboxHandle, card: TaskCard,
            worker_id: str) -> AgentResult: ...


class GerritPusher(Protocol):
    """Pushes a sandbox commit out for review."""

    def push(self, *, handle: SandboxHandle, card: TaskCard,
             commit_sha: str, change_id: str,
             worker_id: str) -> GerritPushResult: ...


class HeartbeatStore(Protocol):
    """Backs ``worker:<id>:alive`` + ``workers:active`` set.

    The default implementation prefers Redis when ``OMNISIGHT_REDIS_URL``
    is set; otherwise falls back to an in-memory dict so single-process
    tests / dev runs work without Redis.
    """

    def register(self, worker_id: str, info: dict[str, Any],
                 ttl_s: int) -> None: ...

    def heartbeat(self, worker_id: str, info: dict[str, Any],
                  ttl_s: int) -> bool: ...

    def deregister(self, worker_id: str) -> None: ...

    def list_active(self) -> list[str]: ...

    def get_info(self, worker_id: str) -> dict[str, Any] | None: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Default runtime implementations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _StubAgentExecutor:
    """Default executor — records the invocation, no real agent call.

    Used by the integration test suite and by ``--dry-run`` mode so
    the worker can be exercised end-to-end without an LLM key.  Real
    deployments inject the production executor at construction time.
    """

    def run(self, *, handle: SandboxHandle, card: TaskCard,
            worker_id: str) -> AgentResult:
        marker = handle.workspace / ".omnisight-stub-agent.json"
        marker.write_text(json.dumps({
            "worker_id": worker_id,
            "ticket": card.jira_ticket,
            "entry_point": card.navigation.entry_point,
        }, indent=2))
        return AgentResult(
            ok=True,
            commit_message=(
                f"stub-agent: {card.jira_ticket}\n\n"
                f"{card.acceptance_criteria}\n"
            ),
            files_touched=[str(marker.relative_to(handle.workspace))],
        )


class LocalSandboxRuntime:
    """Filesystem sandbox.  No Docker required.

    ``start`` materialises a fresh workspace directory + clones a thin
    tree containing **only** the files matched by the CATC card's
    ``impact_scope.allowed`` globs.  Anything else (other source tree,
    secrets, sibling repos) is physically unreachable from the
    workspace because it was never copied in.

    The worker uses ``git`` inside the workspace to commit; ``commit``
    runs ``git add -A && git commit -m ...`` in the workspace dir and
    returns the resulting SHA.  This keeps the local backend trivially
    testable while preserving the spec's "bind-mount only allowed
    paths" guarantee (the implementation IS the bind, just via copy).
    """

    def __init__(self, *, workdir_root: Path | None = None,
                 init_git: bool = True) -> None:
        self._root = Path(workdir_root or
                          os.environ.get("OMNISIGHT_WORKER_WORKDIR")
                          or Path.cwd() / ".artifacts" / "worker_sandboxes")
        self._init_git = init_git
        self._root.mkdir(parents=True, exist_ok=True)

    def start(self, *, worker_id: str, task_id: str,
              card: TaskCard, project_root: Path) -> SandboxHandle:
        ws = self._root / f"{worker_id}-{task_id}"
        if ws.exists():
            _rmtree(ws)
        ws.mkdir(parents=True, exist_ok=True)

        allowed = list(card.navigation.impact_scope.allowed)
        copied: list[str] = []
        for glob in allowed:
            for src in _resolve_glob(project_root, glob):
                rel = src.relative_to(project_root)
                dst = ws / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    _copytree(src, dst)
                else:
                    _copyfile(src, dst)
                copied.append(str(rel))

        if self._init_git:
            _run_git(ws, ["init", "-q", "-b", "main"])
            _run_git(ws, ["config", "user.name", f"omnisight-worker-{worker_id}"])
            _run_git(ws, ["config", "user.email",
                          f"{worker_id}@omnisight.local"])
            if copied:
                _run_git(ws, ["add", "-A"])
                _run_git(ws, ["commit", "--allow-empty", "-q",
                              "-m", f"baseline: {card.jira_ticket}"])

        return SandboxHandle(
            workspace=ws,
            runtime_id="local",
            metadata={"copied": copied, "task_id": task_id},
        )

    def commit(self, handle: SandboxHandle, *,
               commit_message: str) -> str:
        if not self._init_git:
            return ""
        _run_git(handle.workspace, ["add", "-A"])
        rc, out, err = _run_git(handle.workspace,
                                ["commit", "--allow-empty", "-q",
                                 "-m", commit_message])
        if rc != 0:
            raise RuntimeError(
                f"sandbox commit failed: {err.strip() or out.strip()}"
            )
        rc, sha, _ = _run_git(handle.workspace, ["rev-parse", "HEAD"])
        if rc != 0:
            raise RuntimeError("sandbox commit succeeded but rev-parse failed")
        return sha.strip()

    def stop(self, handle: SandboxHandle) -> None:
        try:
            _rmtree(handle.workspace)
        except OSError as exc:
            logger.warning("sandbox cleanup failed for %s: %s",
                           handle.workspace, exc)


class StubGerritPusher:
    """Default pusher — generates a Change-Id, doesn't talk to Gerrit.

    Production wires ``GerritCommandPusher`` instead (below); the stub
    is what the test suite uses so unit tests don't need an SSH key.
    """

    def __init__(self) -> None:
        self.pushed: list[dict[str, Any]] = []

    def push(self, *, handle: SandboxHandle, card: TaskCard,
             commit_sha: str, change_id: str,
             worker_id: str) -> GerritPushResult:
        self.pushed.append({
            "task_id": handle.metadata.get("task_id", ""),
            "ticket": card.jira_ticket,
            "commit": commit_sha,
            "change_id": change_id,
            "worker_id": worker_id,
        })
        return GerritPushResult(
            ok=True,
            change_id=change_id,
            review_url=f"local://gerrit/changes/{change_id}",
        )


class GerritCommandPusher:
    """Production pusher — uses the local ``git`` CLI to push to
    ``refs/for/main`` (Gerrit's "review" magic ref).

    The class is intentionally thin: it shells out to ``git push``
    against the workspace's git directory so the real git protocol
    handles auth (SSH key on the agent host).  Retries are bounded
    (``GERRIT_PUSH_MAX_RETRIES``) with backoff — Gerrit transient
    errors (network, 503) shouldn't kill the queue ack.
    """

    def __init__(self, *, remote: str = "origin",
                 ref: str = "refs/for/main",
                 max_retries: int = GERRIT_PUSH_MAX_RETRIES,
                 backoff_s: tuple[float, ...] = GERRIT_PUSH_BACKOFF_S,
                 runner: Callable[..., tuple[int, str, str]] | None = None
                 ) -> None:
        self._remote = remote
        self._ref = ref
        self._max_retries = max(1, max_retries)
        self._backoff = backoff_s
        # Injectable so tests can simulate transient failures.
        self._runner = runner or _run_git

    def push(self, *, handle: SandboxHandle, card: TaskCard,
             commit_sha: str, change_id: str,
             worker_id: str) -> GerritPushResult:
        last_err = ""
        for attempt in range(1, self._max_retries + 1):
            rc, out, err = self._runner(
                handle.workspace,
                ["push", self._remote, f"HEAD:{self._ref}"],
            )
            if rc == 0:
                return GerritPushResult(
                    ok=True,
                    change_id=change_id,
                    review_url=_extract_review_url(out + "\n" + err),
                    attempts=attempt,
                )
            last_err = (err or out).strip()
            logger.warning(
                "Gerrit push attempt %d/%d for %s failed: %s",
                attempt, self._max_retries, card.jira_ticket, last_err,
            )
            if attempt < self._max_retries:
                idx = min(attempt - 1, len(self._backoff) - 1)
                time.sleep(self._backoff[idx])
        return GerritPushResult(
            ok=False, change_id=change_id, attempts=self._max_retries,
            reason=last_err or "unknown push failure",
        )


def _extract_review_url(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("remote:") and "http" in line:
            for tok in line.split():
                if tok.startswith("http"):
                    return tok
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Heartbeat / registration store
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _MemoryHeartbeatStore:
    """Single-process store — used when Redis isn't available."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, dict[str, Any]] = {}
        self._expiry: dict[str, float] = {}

    def _gc_locked(self) -> None:
        now = time.time()
        for wid in [w for w, exp in self._expiry.items() if exp <= now]:
            self._workers.pop(wid, None)
            self._expiry.pop(wid, None)

    def register(self, worker_id: str, info: dict[str, Any],
                 ttl_s: int) -> None:
        with self._lock:
            self._workers[worker_id] = dict(info)
            self._expiry[worker_id] = time.time() + ttl_s

    def heartbeat(self, worker_id: str, info: dict[str, Any],
                  ttl_s: int) -> bool:
        with self._lock:
            self._workers[worker_id] = dict(info)
            self._expiry[worker_id] = time.time() + ttl_s
            return True

    def deregister(self, worker_id: str) -> None:
        with self._lock:
            self._workers.pop(worker_id, None)
            self._expiry.pop(worker_id, None)

    def list_active(self) -> list[str]:
        with self._lock:
            self._gc_locked()
            return sorted(self._workers.keys())

    def get_info(self, worker_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._gc_locked()
            d = self._workers.get(worker_id)
            return dict(d) if d else None


class RedisHeartbeatStore:
    """Redis-backed heartbeat + registry.

    Uses two keys per worker:

      * ``omnisight:worker:<wid>:alive`` — JSON blob with EX TTL.
      * ``omnisight:workers:active``     — SADD on register, SREM on
        graceful shutdown.  ``list_active()`` cross-checks set members
        against live ``alive`` keys so dead workers (TTL expired)
        don't haunt the operator surface.
    """

    def __init__(self, redis_client: Any | None = None) -> None:
        self._client = redis_client
        if self._client is None:
            from backend.shared_state import get_sync_redis
            self._client = get_sync_redis()
        if self._client is None:
            raise RuntimeError(
                "RedisHeartbeatStore requires OMNISIGHT_REDIS_URL"
            )

    def _alive_key(self, wid: str) -> str:
        return f"{WORKER_HEARTBEAT_PREFIX}{wid}:alive"

    def _set_key(self) -> str:
        return f"{WORKER_HEARTBEAT_PREFIX}active"

    def register(self, worker_id: str, info: dict[str, Any],
                 ttl_s: int) -> None:
        payload = json.dumps(info, sort_keys=True, default=str)
        pipe = self._client.pipeline()
        pipe.set(self._alive_key(worker_id), payload, ex=ttl_s)
        pipe.sadd(self._set_key(), worker_id)
        pipe.execute()

    def heartbeat(self, worker_id: str, info: dict[str, Any],
                  ttl_s: int) -> bool:
        payload = json.dumps(info, sort_keys=True, default=str)
        return bool(self._client.set(
            self._alive_key(worker_id), payload, ex=ttl_s,
        ))

    def deregister(self, worker_id: str) -> None:
        pipe = self._client.pipeline()
        pipe.delete(self._alive_key(worker_id))
        pipe.srem(self._set_key(), worker_id)
        pipe.execute()

    def list_active(self) -> list[str]:
        members = self._client.smembers(self._set_key()) or set()
        live: list[str] = []
        for m in members:
            wid = m.decode() if isinstance(m, bytes) else m
            if self._client.exists(self._alive_key(wid)):
                live.append(wid)
            else:
                # Stale registry entry — drop it.
                self._client.srem(self._set_key(), wid)
        return sorted(live)

    def get_info(self, worker_id: str) -> dict[str, Any] | None:
        raw = self._client.get(self._alive_key(worker_id))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


_default_store: HeartbeatStore | None = None


def _get_default_store() -> HeartbeatStore:
    global _default_store
    if _default_store is not None:
        return _default_store
    try:
        from backend.shared_state import get_sync_redis
        if get_sync_redis() is not None:
            _default_store = RedisHeartbeatStore()
            return _default_store
    except Exception as exc:
        logger.debug("Redis heartbeat store unavailable: %s", exc)
    _default_store = _MemoryHeartbeatStore()
    return _default_store


def set_heartbeat_store_for_tests(store: HeartbeatStore | None) -> None:
    global _default_store
    _default_store = store


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Filesystem helpers (used by LocalSandboxRuntime)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _rmtree(path: Path) -> None:
    import shutil
    if path.exists():
        shutil.rmtree(path, ignore_errors=False)


def _copyfile(src: Path, dst: Path) -> None:
    import shutil
    shutil.copy2(src, dst)


def _copytree(src: Path, dst: Path) -> None:
    import shutil
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _resolve_glob(root: Path, glob: str) -> list[Path]:
    """Resolve a CATC ``impact_scope.allowed`` glob into existing paths.

    Honours the project-root jail: any path resolving outside ``root``
    is rejected (and the worker will refuse the task) — this is the
    "physically unreachable" guarantee in the spec.
    """
    glob = glob.lstrip("/")
    if not glob:
        return []
    # Reject ".." segments before letting Python expand the glob.
    if any(part == ".." for part in Path(glob).parts):
        raise ValueError(
            f"impact_scope.allowed glob escapes project root: {glob!r}"
        )
    matches: list[Path] = []
    for raw in root.glob(glob):
        try:
            raw_resolved = raw.resolve()
            root_resolved = root.resolve()
            raw_resolved.relative_to(root_resolved)
        except (ValueError, OSError):
            raise ValueError(
                f"impact_scope.allowed glob escapes project root: {glob!r}"
            )
        matches.append(raw)
    if not matches:
        # If the literal path doesn't exist yet (e.g. the agent will
        # create it), still allow the worker to proceed by recording
        # an empty match — the workspace will be empty until commit.
        return []
    return matches


def _run_git(cwd: Path, args: list[str]) -> tuple[int, str, str]:
    import subprocess
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Worker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def new_worker_id(prefix: str = "wkr") -> str:
    host = socket.gethostname().split(".")[0]
    return f"{prefix}-{host}-{uuid.uuid4().hex[:8]}"


class Worker:
    """One stateless worker process.

    Lifecycle::

        w = Worker(config, sandbox_runtime, agent_executor, gerrit_pusher)
        w.start()       # registers, starts heartbeat thread
        try:
            w.run()     # blocks pulling + processing tasks
        finally:
            w.stop()    # graceful: stop claiming, finish in-flight,
                        # release locks, deregister
    """

    def __init__(self,
                 config: WorkerConfig,
                 *,
                 sandbox_runtime: SandboxRuntime | None = None,
                 agent_executor: AgentExecutor | None = None,
                 gerrit_pusher: GerritPusher | None = None,
                 heartbeat_store: HeartbeatStore | None = None,
                 ) -> None:
        self.config = config
        self.sandbox = sandbox_runtime or LocalSandboxRuntime()
        self.executor = agent_executor or _StubAgentExecutor()
        self.gerrit = gerrit_pusher or StubGerritPusher()
        self.store = heartbeat_store or _get_default_store()

        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._inflight: dict[str, str] = {}        # message_id -> task_id
        self._inflight_lock = threading.Lock()
        self._pending = 0                          # submitted-but-not-started
        self._pending_lock = threading.Lock()
        self._processed: list[WorkerTaskOutcome] = []
        self._processed_lock = threading.Lock()
        self._executor_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._started_at: float = 0.0
        self._signal_handlers_installed = False

    # ─── lifecycle ───────────────────────────────────────────────

    def start(self) -> None:
        self._started_at = time.time()
        info = self._info_snapshot(status="starting")
        self.store.register(self.config.worker_id, info,
                            self.config.heartbeat_ttl_s)
        _bump_workers_active(self.store)
        self._executor_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, self.config.capacity),
            thread_name_prefix=f"wkr-{self.config.worker_id}",
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"worker-heartbeat-{self.config.worker_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        logger.info(
            "worker %s started (capacity=%d, tenant_filter=%s, "
            "capability_filter=%s)",
            self.config.worker_id, self.config.capacity,
            self.config.tenant_filter or "*",
            self.config.capability_filter or "*",
        )
        try:
            metrics.worker_lifecycle_total.labels(event="start").inc()
        except Exception:
            pass

    def stop(self, *, timeout_s: float = 60.0) -> None:
        """Graceful shutdown.

        After this returns, ``stop_event`` is set, the heartbeat
        thread has exited, no in-flight task is still claimed, and
        the worker is deregistered from the active set.
        """
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        # Wait for in-flight + pending tasks to drain (run() loop does the work).
        deadline = time.time() + timeout_s
        while True:
            with self._inflight_lock:
                inflight = len(self._inflight)
            pending = self._pending_count()
            if inflight == 0 and pending == 0:
                break
            if time.time() >= deadline:
                logger.warning(
                    "worker %s stop: %d in-flight task(s) still running "
                    "after %.1fs — abandoning",
                    self.config.worker_id, len(self._inflight), timeout_s,
                )
                # Best-effort: nack each in-flight to release the
                # visibility timeout faster + release any locks.
                with self._inflight_lock:
                    in_flight = dict(self._inflight)
                for mid, tid in in_flight.items():
                    self._abandon(mid, tid, "worker stop timeout")
                break
            time.sleep(0.05)

        if self._executor_pool is not None:
            # We've already drained in-flight tasks above; this is just
            # to release the pool's worker threads.
            self._executor_pool.shutdown(wait=True, cancel_futures=False)
            self._executor_pool = None
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5.0)
        try:
            self.store.deregister(self.config.worker_id)
        except Exception as exc:
            logger.warning("worker %s deregister failed: %s",
                           self.config.worker_id, exc)
        _bump_workers_active(self.store)
        try:
            metrics.worker_lifecycle_total.labels(event="stop").inc()
        except Exception:
            pass
        logger.info("worker %s stopped (processed=%d)",
                    self.config.worker_id, len(self._processed))

    def install_signal_handlers(self) -> None:
        """Wire SIGTERM / SIGINT to ``stop()`` for systemd / docker."""
        if self._signal_handlers_installed:
            return
        if threading.current_thread() is not threading.main_thread():
            # signal.signal must be called from main thread.
            return

        def _handler(signum: int, _frame: Any) -> None:
            logger.info("worker %s caught signal %d — graceful shutdown",
                        self.config.worker_id, signum)
            self._stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # In some embedded runtimes setting signals raises.
                pass
        self._signal_handlers_installed = True

    # ─── main loop ───────────────────────────────────────────────

    def run(self) -> list[WorkerTaskOutcome]:
        """Block pulling + processing tasks until ``stop_event`` set
        or ``max_messages`` reached.

        Returns the per-task outcome list — useful for tests + for
        the systemd ExecStop hook to log a summary.
        """
        n = 0
        while not self._stop_event.is_set():
            if (self.config.max_messages is not None
                    and n >= self.config.max_messages):
                break
            free = self.config.capacity - max(
                self._inflight_count(), self._pending_count(),
            )
            if free <= 0:
                time.sleep(self.config.loop_idle_s)
                continue
            pull_n = min(self.config.effective_pull_count(), free)
            try:
                msgs = queue_backend.pull(
                    self.config.worker_id, count=pull_n,
                    visibility_timeout_s=self.config.visibility_timeout_s,
                )
            except Exception as exc:
                logger.exception("worker %s pull failed: %s",
                                 self.config.worker_id, exc)
                time.sleep(self.config.loop_idle_s)
                continue

            if not msgs:
                time.sleep(self.config.loop_idle_s)
                continue

            for msg in msgs:
                if self._stop_event.is_set():
                    # Re-queue immediately by returning the message
                    # to Queued; visibility timeout would do this
                    # eventually but we want the next worker to take
                    # it now (graceful shutdown spec).
                    self._return_to_queue(msg, "worker shutting down")
                    self._record_outcome(self._filter_outcome(
                        msg, "shutdown_returned",
                        "worker shutting down",
                    ))
                    n += 1
                    continue
                if not self._matches_filters(msg):
                    self._return_to_queue(
                        msg, "filter mismatch (tenant/capability)",
                    )
                    self._record_outcome(self._filter_outcome(
                        msg, "nacked",
                        "filter mismatch (tenant/capability)",
                    ))
                    n += 1
                    continue
                # Dispatch into the thread pool so the worker can have
                # up to ``capacity`` tasks in-flight at once (spec:
                # ``--capacity N``).  ``handle()`` never raises so we
                # don't bother chaining a future callback.
                if self.config.capacity == 1 or self._executor_pool is None:
                    self._record_outcome(self.handle(msg))
                else:
                    with self._pending_lock:
                        self._pending += 1
                    self._executor_pool.submit(
                        self._handle_and_record, msg,
                    )
                n += 1

        # Wait for any remaining submitted tasks (capacity > 1 case).
        # ``_pending`` is bumped at submit() and decremented in
        # ``_handle_and_record`` AFTER the outcome is appended, so
        # waiting on it == waiting for every outcome to be in
        # ``self._processed``.
        if self._executor_pool is not None:
            deadline = time.time() + 60.0
            while self._pending_count() > 0 and time.time() < deadline:
                time.sleep(0.02)
        return list(self._processed)

    def _handle_and_record(self, msg: QueueMessage) -> None:
        try:
            self._record_outcome(self.handle(msg))
        except Exception as exc:                       # pragma: no cover
            logger.exception("handle(%s) crashed: %s", msg.message_id, exc)
        finally:
            with self._pending_lock:
                self._pending -= 1

    def _pending_count(self) -> int:
        with self._pending_lock:
            return self._pending

    def _record_outcome(self, outcome: WorkerTaskOutcome) -> None:
        with self._processed_lock:
            self._processed.append(outcome)

    # ─── single-task processing ──────────────────────────────────

    def handle(self, msg: QueueMessage) -> WorkerTaskOutcome:
        """Process one ``QueueMessage`` end-to-end.

        Returns a ``WorkerTaskOutcome`` audit row.  Never raises:
        every failure path is caught and translated into ``nack``
        (queue handles 3-strike + DLQ) so the worker loop can keep
        going.
        """
        started = time.time()
        task_id = _task_id_for(msg)
        outcome = WorkerTaskOutcome(
            message_id=msg.message_id,
            task_id=task_id,
            jira_ticket=_safe_ticket(msg),
            status="acked",
        )
        # O10 (#273): reject forged / tampered queue payloads *before*
        # touching the sandbox.  When no HMAC key is configured the call
        # is a no-op and we fall back to pre-O10 behaviour.
        try:
            queue_backend.verify_pulled_message(msg)
        except Exception as exc:
            outcome.status = "nacked"
            outcome.error = f"O10 HMAC verify failed: {exc}"
            # Immediate DLQ — a bad signature is not a transient fault and
            # re-queueing it would just poison the next puller.  We NACK
            # MAX_DELIVERIES times to trigger DLQ in one call.
            try:
                for _ in range(queue_backend.MAX_DELIVERIES):
                    queue_backend.nack(
                        msg.message_id, outcome.error,
                        queue_backend.format_exc(exc),
                    )
            except Exception:
                pass
            outcome.elapsed_s = time.time() - started
            try:
                metrics.worker_task_total.labels(outcome="hmac_rejected").inc()
            except Exception:
                pass
            return outcome
        try:
            card = msg.task_card()
        except Exception as exc:
            outcome.status = "nacked"
            outcome.error = f"corrupt CATC payload: {exc}"
            queue_backend.nack(msg.message_id,
                               outcome.error,
                               queue_backend.format_exc(exc))
            outcome.elapsed_s = time.time() - started
            return outcome

        # ─── distributed lock on impact_scope.allowed ───────────
        allowed_paths = list(card.navigation.impact_scope.allowed)
        lock_res = dist_lock.acquire_paths(
            task_id, allowed_paths, ttl_s=self.config.lock_ttl_s,
            wait_timeout_s=self.config.lock_wait_s,
        )
        if not lock_res.ok:
            outcome.status = "locked_skipped"
            outcome.error = (
                f"file-path lock conflict: "
                f"{','.join(sorted(lock_res.conflicts))}"
            )
            try:
                queue_backend.set_state(
                    msg.message_id, TaskState.Blocked_by_Mutex,
                )
            except Exception:
                pass
            self._return_to_queue(msg, outcome.error)
            outcome.elapsed_s = time.time() - started
            try:
                metrics.worker_task_total.labels(outcome="locked").inc()
            except Exception:
                pass
            return outcome

        self._track_inflight(msg.message_id, task_id)
        try:
            try:
                queue_backend.set_state(msg.message_id, TaskState.Running)
            except Exception:
                pass

            handle = self.sandbox.start(
                worker_id=self.config.worker_id,
                task_id=task_id,
                card=card,
                project_root=self.config.project_root,
            )
            try:
                agent_result = self.executor.run(
                    handle=handle, card=card,
                    worker_id=self.config.worker_id,
                )
                outcome.agent_result = agent_result
                if not agent_result.ok:
                    raise WorkerTaskFailed(
                        agent_result.reason or "agent reported failure",
                        agent_result.stack,
                    )

                change_id = _new_change_id()
                full_msg = _build_commit_message(
                    card=card,
                    base=agent_result.commit_message,
                    change_id=change_id,
                    worker_id=self.config.worker_id,
                )
                commit_sha = self.sandbox.commit(
                    handle, commit_message=full_msg,
                )
                push_result = self.gerrit.push(
                    handle=handle, card=card, commit_sha=commit_sha,
                    change_id=change_id,
                    worker_id=self.config.worker_id,
                )
                outcome.gerrit = push_result
                if not push_result.ok:
                    raise WorkerTaskFailed(
                        f"gerrit push failed: {push_result.reason}",
                    )
                # O5 — notify intent_bridge so the sub-task in the
                # tracker flips to "In Review" while humans + AI +2.
                _notify_intent_bridge_gerrit_pushed(
                    task_id=task_id,
                    card=card,
                    change_id=change_id,
                    review_url=push_result.review_url,
                )
                queue_backend.ack(msg.message_id)
                outcome.status = "acked"
                try:
                    metrics.worker_task_total.labels(outcome="acked").inc()
                except Exception:
                    pass
            finally:
                try:
                    self.sandbox.stop(handle)
                except Exception as exc:
                    logger.warning("sandbox stop failed: %s", exc)
        except WorkerTaskFailed as exc:
            outcome.status = "nacked"
            outcome.error = str(exc)
            queue_backend.nack(
                msg.message_id, str(exc), exc.stack,
            )
            try:
                metrics.worker_task_total.labels(outcome="nacked").inc()
            except Exception:
                pass
        except Exception as exc:
            outcome.status = "nacked"
            outcome.error = f"unexpected error: {exc}"
            queue_backend.nack(
                msg.message_id, str(exc),
                queue_backend.format_exc(exc),
            )
            try:
                metrics.worker_task_total.labels(outcome="error").inc()
            except Exception:
                pass
        finally:
            self._untrack_inflight(msg.message_id)
            try:
                dist_lock.release_paths(task_id)
            except Exception as exc:
                logger.warning(
                    "release_paths(%s) failed: %s", task_id, exc,
                )

            # If the message has gone to DLQ this returns None — fine,
            # we just want to flip the visible state if it's still alive.
            if outcome.status == "acked":
                try:
                    metrics.worker_task_seconds.observe(time.time() - started)
                except Exception:
                    pass

        outcome.elapsed_s = time.time() - started
        return outcome

    # ─── helpers ─────────────────────────────────────────────────

    def _matches_filters(self, msg: QueueMessage) -> bool:
        if not self.config.tenant_filter and not self.config.capability_filter:
            return True
        tenant = _msg_tenant(msg)
        if self.config.tenant_filter and tenant not in self.config.tenant_filter:
            return False
        if self.config.capability_filter:
            caps = _msg_capabilities(msg)
            if not any(c in self.config.capability_filter for c in caps):
                return False
        return True

    def _filter_outcome(self, msg: QueueMessage, status: str,
                        reason: str) -> WorkerTaskOutcome:
        return WorkerTaskOutcome(
            message_id=msg.message_id,
            task_id=_task_id_for(msg),
            jira_ticket=_safe_ticket(msg),
            status=status,
            error=reason,
        )

    def _return_to_queue(self, msg: QueueMessage, reason: str) -> None:
        """Best-effort: bump delivery_count back without consuming a
        retry budget by re-queueing via state transition.

        We can't truly "un-claim" a message in O2's contract — but the
        next ``sweep_visibility`` will do it for us once the deadline
        passes.  For graceful shutdown / filter mismatch we still
        prefer to nack with a benign reason so the queue can re-deliver
        immediately, AT THE COST of one delivery_count tick.  3-strike
        rule applies — operators shouldn't loop a worker between
        filters that always reject.
        """
        try:
            queue_backend.nack(msg.message_id, reason)
        except Exception as exc:
            logger.warning("return_to_queue(%s) failed: %s",
                           msg.message_id, exc)

    def _abandon(self, message_id: str, task_id: str, reason: str) -> None:
        try:
            queue_backend.nack(message_id, reason)
        except Exception:
            pass
        try:
            dist_lock.release_paths(task_id)
        except Exception:
            pass

    def _track_inflight(self, message_id: str, task_id: str) -> None:
        with self._inflight_lock:
            self._inflight[message_id] = task_id
        try:
            metrics.worker_inflight.set(self._inflight_count())
        except Exception:
            pass

    def _untrack_inflight(self, message_id: str) -> None:
        with self._inflight_lock:
            self._inflight.pop(message_id, None)
        try:
            metrics.worker_inflight.set(self._inflight_count())
        except Exception:
            pass

    def _inflight_count(self) -> int:
        with self._inflight_lock:
            return len(self._inflight)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.config.heartbeat_interval_s):
            try:
                self.store.heartbeat(
                    self.config.worker_id,
                    self._info_snapshot(status="alive"),
                    self.config.heartbeat_ttl_s,
                )
                try:
                    metrics.worker_heartbeat_total.inc()
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(
                    "worker %s heartbeat failed: %s",
                    self.config.worker_id, exc,
                )

    def _info_snapshot(self, *, status: str) -> dict[str, Any]:
        snap = {
            "worker_id": self.config.worker_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "status": status,
            "capacity": self.config.capacity,
            "tenant_filter": list(self.config.tenant_filter),
            "capability_filter": list(self.config.capability_filter),
            "started_at": self._started_at,
            "inflight": self._inflight_count(),
            "processed": len(self._processed),
        }
        # O10 (#273): advertise the TLS certificate fingerprint so the
        # orchestrator / dashboard can pin it — if the cert rotates
        # without an ops approval, the next attestation will fail.
        fp = os.environ.get("OMNISIGHT_WORKER_TLS_FP", "").strip()
        if fp:
            snap["tls_cert_fingerprint"] = fp
        return snap


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers shared between Worker + tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class WorkerTaskFailed(Exception):
    """Internal — wraps an executor / push failure for ``handle()``."""

    def __init__(self, reason: str, stack: str | None = None) -> None:
        super().__init__(reason)
        self.stack = stack


def _task_id_for(msg: QueueMessage) -> str:
    """Stable, dist_lock-friendly task_id derived from message_id."""
    return f"task-{msg.message_id}"


def _safe_ticket(msg: QueueMessage) -> str:
    return str(msg.payload.get("jira_ticket", ""))


def _msg_tenant(msg: QueueMessage) -> str:
    return str(msg.payload.get("domain_context", "")) or ""


def _msg_capabilities(msg: QueueMessage) -> list[str]:
    """Best-effort capability label extraction.

    The CATC schema (O0) doesn't yet have a dedicated ``capabilities``
    field — for now the worker reads a comma-list out of
    ``handoff_protocol`` (each entry is treated as a capability tag)
    + a fallback to a leading ``cap:foo`` token in ``domain_context``.
    Capability scoping graduates to a first-class field when O5 lands.
    """
    caps: list[str] = []
    proto = msg.payload.get("handoff_protocol") or []
    if isinstance(proto, list):
        for item in proto:
            if isinstance(item, str) and item.startswith("cap:"):
                caps.append(item[4:])
    dc = str(msg.payload.get("domain_context", ""))
    for tok in dc.split():
        if tok.startswith("cap:"):
            caps.append(tok[4:])
    return caps


def _new_change_id() -> str:
    """Fresh Gerrit Change-Id (40-hex SHA preceded by ``I``)."""
    return "I" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _build_commit_message(*, card: TaskCard, base: str, change_id: str,
                          worker_id: str) -> str:
    """Append Gerrit + CATC trailers to the agent's commit message."""
    body = base.rstrip()
    if not body:
        body = f"agent: {card.jira_ticket}"
    trailers = [
        f"Change-Id: {change_id}",
        f"CATC-Ticket: {card.jira_ticket}",
        f"Worker-Id: {worker_id}",
    ]
    return body + "\n\n" + "\n".join(trailers) + "\n"


def _bump_workers_active(store: HeartbeatStore) -> None:
    try:
        metrics.worker_active.set(len(store.list_active()))
    except Exception:
        pass


def _notify_intent_bridge_gerrit_pushed(*, task_id: str, card: TaskCard,
                                       change_id: str,
                                       review_url: str) -> None:
    """Best-effort async dispatch to ``intent_bridge.on_worker_gerrit_pushed``.

    The worker runs on a synchronous thread pool, so we schedule the
    coroutine onto the running loop if there is one; otherwise create
    a disposable loop for this single call.  Errors are swallowed — the
    Gerrit push itself has already succeeded.
    """
    try:
        pass
    except Exception as exc:
        logger.debug("intent_bridge import failed in worker: %s", exc)
        return
    # The orchestrator recorded the parent under ``card.jira_ticket``
    # only when the card is itself the parent; for CATCs (sub-tasks)
    # we need to find the parent.  The bridge's record lookup does
    # that by CATC task_id + sub-task ticket so pass both.
    from backend import intent_bridge as _ib

    # Derive parent ticket: strip the trailing index we appended in
    # ``_subtask_key`` (PROJ-7001 → PROJ-7).  When we can't recover
    # the parent, pass an empty string — the bridge will still update
    # the sub-task's own status.
    subtask = card.jira_ticket
    parent = _derive_parent_from_subtask(subtask)

    async def _go() -> None:
        try:
            await _ib.on_worker_gerrit_pushed(
                task_id=task_id,
                jira_ticket=subtask,
                parent=parent,
                change_id=change_id,
                review_url=review_url,
                vendor=None,
            )
        except Exception as exc:
            logger.debug("intent_bridge gerrit_pushed hook failed: %s", exc)

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (worker default) — run to completion
            # synchronously on a disposable loop.
            asyncio.run(_go())
            return
        loop.create_task(_go())
    except Exception as exc:
        logger.debug("intent_bridge gerrit_pushed dispatch failed: %s", exc)


def _derive_parent_from_subtask(subtask: str) -> str:
    """Reverse the ``_subtask_key`` encoding in orchestrator_gateway.

    ``PROJ-7001`` → ``PROJ-7`` (last three digits were the 1-based
    CATC index).  When the encoding doesn't match, fall back to
    searching the bridge registry.
    """
    import re as _re
    m = _re.match(r"^([A-Z][A-Z0-9_]*)-(\d+)$", subtask or "")
    if not m:
        return ""
    prefix, num_s = m.group(1), m.group(2)
    num = int(num_s)
    if num < 1001:
        return ""
    parent_num, _remainder = divmod(num, 1000)
    # Index 1 of parent 7 becomes 7001; reverse: 7001 // 1000 = 7.
    return f"{prefix}-{parent_num}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI entry-point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backend.worker",
        description="OmniSight stateless agent worker (O3)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run a worker process")
    run.add_argument("--worker-id", default=None,
                     help="override the auto-generated worker id")
    run.add_argument("--capacity", type=int, default=DEFAULT_CAPACITY,
                     help="max concurrent in-flight tasks (default 1)")
    run.add_argument("--tenant-filter", default="",
                     help="comma-list of tenant ids; empty = all")
    run.add_argument("--capability-filter", default="",
                     help="comma-list of capability tags; empty = all")
    run.add_argument("--heartbeat-interval-s", type=float,
                     default=DEFAULT_HEARTBEAT_INTERVAL_S)
    run.add_argument("--heartbeat-ttl-s", type=int,
                     default=DEFAULT_HEARTBEAT_TTL_S)
    run.add_argument("--visibility-timeout-s", type=float,
                     default=DEFAULT_VISIBILITY_TIMEOUT_S)
    run.add_argument("--max-messages", type=int, default=None,
                     help="stop after N processed messages (tests/one-shots)")
    run.add_argument("--project-root", default=None,
                     help="project root (default cwd)")
    run.add_argument("--log-level", default="INFO")

    sub.add_parser("list", help="list active workers (registry view)")

    return p


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    log_level = getattr(args, "log_level", "INFO")
    logging.basicConfig(
        level=getattr(logging, str(log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if args.cmd == "list":
        store = _get_default_store()
        for wid in store.list_active():
            info = store.get_info(wid) or {}
            print(json.dumps({"worker_id": wid, "info": info}, indent=2))
        return 0

    cfg = WorkerConfig(
        worker_id=args.worker_id or new_worker_id(),
        capacity=args.capacity,
        tenant_filter=_parse_csv(args.tenant_filter),
        capability_filter=_parse_csv(args.capability_filter),
        heartbeat_interval_s=args.heartbeat_interval_s,
        heartbeat_ttl_s=args.heartbeat_ttl_s,
        visibility_timeout_s=args.visibility_timeout_s,
        max_messages=args.max_messages,
        project_root=Path(args.project_root or os.getcwd()),
    )
    worker = Worker(cfg)
    worker.install_signal_handlers()
    worker.start()
    try:
        worker.run()
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_HEARTBEAT_TTL_S",
    "DEFAULT_VISIBILITY_TIMEOUT_S",
    "GERRIT_PUSH_MAX_RETRIES",
    "AgentResult",
    "AgentExecutor",
    "GerritPushResult",
    "GerritPusher",
    "GerritCommandPusher",
    "HeartbeatStore",
    "LocalSandboxRuntime",
    "RedisHeartbeatStore",
    "SandboxHandle",
    "SandboxRuntime",
    "StubGerritPusher",
    "Worker",
    "WorkerConfig",
    "WorkerTaskFailed",
    "WorkerTaskOutcome",
    "main",
    "new_worker_id",
    "set_heartbeat_store_for_tests",
]
