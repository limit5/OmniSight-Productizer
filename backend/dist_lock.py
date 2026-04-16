"""O1 (#264) — Redis distributed file-path mutex lock.

Stateless agent workers (O3) must serialise writes to the same file-path
namespace across processes / hosts.  The CATC card's
``navigation.impact_scope.allowed`` list (O0) defines the set of globs
a task intends to mutate; before a worker opens the sandbox, it calls
``acquire_paths(task_id, paths, ttl_s)`` to take exclusive leases on
every element of that set.

Contract:
  * Lock granularity is a single path string (file OR directory).  The
    CATC glob vocabulary is flattened upstream — callers pass concrete
    paths, and we only normalise slashes.
  * Acquisition is **all-or-nothing** (atomic MULTI/EXEC-via-Lua): if
    any path is held by another task, nothing is taken and the caller
    gets the conflict set back.
  * Paths are taken in lexicographic order inside the Lua script — this
    makes acquisition order deterministic and is the primary defence
    against AB / BA deadlocks.
  * Every lease has a TTL (default 30 min).  A worker calls
    ``extend_lease`` from its heartbeat loop (every 60 s).  If the
    worker dies, Redis key expiry auto-revokes the lease and a new
    worker can claim.
  * **Deadlock detection** is still required — even with sorted-acquire
    a two-task pattern can deadlock when they hold different held-sets
    and each requests the other's.  ``detect_deadlock_cycle`` runs on
    the wait-for graph; the lowest-priority participant is killed and
    an audit row is written.
  * **Preemption**: when a lock has been held for more than ``TTL × 2``
    (i.e. heartbeat is clearly dead-stuck), a higher-priority task can
    force-acquire it (pair with DRF scheduling in O6).

Backend selection:
  * If ``OMNISIGHT_REDIS_URL`` is set and ``redis`` imports, the Redis
    backend is used (shared across workers / hosts).
  * Otherwise the in-memory backend is used — single-process only, but
    keeps unit tests and dev runs working without Redis.  The two
    backends implement the same observable semantics; integration tests
    that assert cross-process behaviour must target the Redis backend.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from backend import metrics

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_TTL_S = 30 * 60           # 30 minutes — matches spec
HEARTBEAT_INTERVAL_S = 60         # worker calls extend_lease every 60 s
PREEMPTION_MULTIPLIER = 2         # stale_for > TTL × 2 → preemptable
KEY_PREFIX = "omnisight:dist_lock:"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class LockResult:
    """Return value of ``acquire_paths`` / ``preempt``.

    ``ok=True``  → every requested path is now held by ``task_id`` until
                   ``expires_at`` (unix seconds).
    ``ok=False`` → nothing was taken; ``conflicts`` maps each contested
                   path to the task_id currently holding it.
    """

    ok: bool
    task_id: str
    acquired: list[str] = field(default_factory=list)
    conflicts: dict[str, str] = field(default_factory=dict)
    expires_at: float = 0.0
    wait_seconds: float = 0.0

    def __bool__(self) -> bool:  # ergonomic: `if acquire_paths(...):`
        return self.ok


@dataclass
class LockEntry:
    """Server-side view of a single held path."""

    path: str
    task_id: str
    acquired_at: float
    expires_at: float


@dataclass
class DeadlockSweepResult:
    """Output of a single ``run_deadlock_sweep`` cycle."""

    cycles_found: list[list[str]]
    killed_task_ids: list[str]
    elapsed_seconds: float


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Path normalisation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _normalise(path: str) -> str:
    """Normalise a path so two equivalent strings collide on the same key.

    Keeps cross-platform semantics simple: collapses ``\\`` → ``/``, strips
    leading / trailing slashes, and rejects empty strings.  We do **not**
    resolve symlinks or ``..`` — callers must pass repo-relative paths
    (the CATC gate already enforces that).
    """
    if not isinstance(path, str):
        raise TypeError(f"lock path must be str, got {type(path).__name__}")
    p = path.replace("\\", "/").strip()
    while "//" in p:
        p = p.replace("//", "/")
    p = p.strip("/")
    if not p:
        raise ValueError("lock path must be non-empty after normalisation")
    return p


def _normalise_many(paths: Iterable[str]) -> list[str]:
    """Normalise + dedupe + sort.  Sorting is the deadlock-avoidance knob."""
    return sorted({_normalise(p) for p in paths})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Backend protocol
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _LockBackend(Protocol):
    def acquire(self, task_id: str, paths: list[str], ttl_s: float,
                priority: int, preempt_after_s: float | None) -> LockResult: ...
    def release(self, task_id: str) -> int: ...
    def extend(self, task_id: str, ttl_s: float) -> bool: ...
    def get_holder(self, path: str) -> str | None: ...
    def get_paths(self, task_id: str) -> list[str]: ...
    def all_entries(self) -> list[LockEntry]: ...
    def waiters(self) -> dict[str, list[tuple[str, int]]]: ...
    def record_wait(self, task_id: str, paths: list[str], priority: int) -> None: ...
    def clear_waits(self, task_id: str) -> None: ...
    def clear_all(self) -> None: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  In-memory backend
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class InMemoryLockBackend:
    """Thread-safe dict-backed lock table.

    Structure:
      _holders:   path        -> LockEntry
      _by_task:   task_id     -> set[path]
      _waiters:   path        -> list[(task_id, priority, requested_at)]
      _priority:  task_id     -> int
    """

    def __init__(self) -> None:
        self._holders: dict[str, LockEntry] = {}
        self._by_task: dict[str, set[str]] = {}
        self._waiters: dict[str, list[tuple[str, int, float]]] = {}
        self._priority: dict[str, int] = {}
        self._lock = threading.Lock()

    # --- internal --------------------------------------------------

    def _expire_locked(self, now: float) -> None:
        dead = [p for p, e in self._holders.items() if e.expires_at <= now]
        for p in dead:
            entry = self._holders.pop(p)
            paths = self._by_task.get(entry.task_id)
            if paths:
                paths.discard(p)
                if not paths:
                    self._by_task.pop(entry.task_id, None)
                    self._priority.pop(entry.task_id, None)

    def _find_conflicts(self, task_id: str, paths: list[str],
                        now: float,
                        preempt_after_s: float | None,
                        my_priority: int) -> dict[str, str]:
        conflicts: dict[str, str] = {}
        for p in paths:
            entry = self._holders.get(p)
            if entry is None or entry.task_id == task_id:
                continue
            # TTL-based preemption: stale lease + higher priority takes over.
            if preempt_after_s is not None:
                age = now - entry.acquired_at
                holder_prio = self._priority.get(entry.task_id, 0)
                if age >= preempt_after_s and my_priority > holder_prio:
                    # caller wants preemption; we'll handle eviction
                    # inside ``acquire`` — here we just don't flag it.
                    continue
            conflicts[p] = entry.task_id
        return conflicts

    # --- API -------------------------------------------------------

    def acquire(self, task_id: str, paths: list[str], ttl_s: float,
                priority: int, preempt_after_s: float | None) -> LockResult:
        now = time.time()
        with self._lock:
            self._expire_locked(now)
            conflicts = self._find_conflicts(
                task_id, paths, now, preempt_after_s, priority,
            )
            if conflicts:
                return LockResult(ok=False, task_id=task_id, conflicts=conflicts)

            # Preemption: evict any stale holder we out-prioritise.
            if preempt_after_s is not None:
                for p in paths:
                    entry = self._holders.get(p)
                    if entry is None or entry.task_id == task_id:
                        continue
                    age = now - entry.acquired_at
                    holder_prio = self._priority.get(entry.task_id, 0)
                    if age >= preempt_after_s and priority > holder_prio:
                        held = self._by_task.get(entry.task_id)
                        if held is not None:
                            held.discard(p)
                            if not held:
                                self._by_task.pop(entry.task_id, None)
                                self._priority.pop(entry.task_id, None)
                        del self._holders[p]

            expires_at = now + ttl_s
            for p in paths:
                self._holders[p] = LockEntry(
                    path=p, task_id=task_id,
                    acquired_at=now, expires_at=expires_at,
                )
                self._by_task.setdefault(task_id, set()).add(p)
            self._priority[task_id] = priority

            # Clear wait records now that we succeeded.
            for pl in list(self._waiters.values()):
                pl[:] = [t for t in pl if t[0] != task_id]

        return LockResult(
            ok=True, task_id=task_id,
            acquired=list(paths), expires_at=expires_at,
        )

    def release(self, task_id: str) -> int:
        with self._lock:
            paths = self._by_task.pop(task_id, set())
            for p in paths:
                entry = self._holders.get(p)
                if entry is not None and entry.task_id == task_id:
                    del self._holders[p]
            self._priority.pop(task_id, None)
            for pl in list(self._waiters.values()):
                pl[:] = [t for t in pl if t[0] != task_id]
            return len(paths)

    def extend(self, task_id: str, ttl_s: float) -> bool:
        now = time.time()
        with self._lock:
            paths = self._by_task.get(task_id)
            if not paths:
                return False
            new_expiry = now + ttl_s
            for p in paths:
                entry = self._holders.get(p)
                if entry is None or entry.task_id != task_id:
                    continue
                entry.expires_at = new_expiry
            return True

    def get_holder(self, path: str) -> str | None:
        now = time.time()
        with self._lock:
            self._expire_locked(now)
            entry = self._holders.get(_normalise(path))
            return entry.task_id if entry else None

    def get_paths(self, task_id: str) -> list[str]:
        with self._lock:
            return sorted(self._by_task.get(task_id, set()))

    def all_entries(self) -> list[LockEntry]:
        now = time.time()
        with self._lock:
            self._expire_locked(now)
            return [
                LockEntry(
                    path=e.path, task_id=e.task_id,
                    acquired_at=e.acquired_at, expires_at=e.expires_at,
                )
                for e in self._holders.values()
            ]

    def waiters(self) -> dict[str, list[tuple[str, int]]]:
        with self._lock:
            return {
                p: [(t, pr) for (t, pr, _ts) in pl]
                for p, pl in self._waiters.items() if pl
            }

    def record_wait(self, task_id: str, paths: list[str], priority: int) -> None:
        now = time.time()
        with self._lock:
            self._priority[task_id] = priority
            for p in paths:
                entry = self._holders.get(p)
                if entry is None or entry.task_id == task_id:
                    continue
                pl = self._waiters.setdefault(p, [])
                if not any(t[0] == task_id for t in pl):
                    pl.append((task_id, priority, now))

    def clear_waits(self, task_id: str) -> None:
        with self._lock:
            for pl in list(self._waiters.values()):
                pl[:] = [t for t in pl if t[0] != task_id]

    def clear_all(self) -> None:
        with self._lock:
            self._holders.clear()
            self._by_task.clear()
            self._waiters.clear()
            self._priority.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Redis backend
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# The Redis schema mirrors the in-memory one:
#
#   omnisight:dist_lock:holder:<path>       → hash {task, acq, exp}  (PEXPIRE-aligned)
#   omnisight:dist_lock:task:<task_id>      → set of paths held
#   omnisight:dist_lock:task_prio:<task_id> → int priority            (EXPIRE tracked too)
#   omnisight:dist_lock:waiters:<path>      → zset  member=task_id, score=-priority*1e6+ts
#
# The Lua script below is the O1 atomicity contract: it checks every path
# in KEYS, and if ANY is held by a different task (and cannot be
# preempted), nothing is written.  This is cheaper and race-free vs
# MULTI/EXEC + WATCH loops, because the script runs single-threaded on
# the Redis server.

_ACQUIRE_LUA = """
-- ARGV: task_id, now, ttl_s, priority, preempt_after_s (-1 if no preempt)
local task_id         = ARGV[1]
local now             = tonumber(ARGV[2])
local ttl_s           = tonumber(ARGV[3])
local priority        = tonumber(ARGV[4])
local preempt_after_s = tonumber(ARGV[5])

-- First pass: detect conflicts.
local conflicts = {}
for i = 1, #KEYS do
    local holder_key = KEYS[i]
    local h_task = redis.call('HGET', holder_key, 'task')
    if h_task and h_task ~= task_id then
        if preempt_after_s >= 0 then
            local h_acq = tonumber(redis.call('HGET', holder_key, 'acq')) or now
            local age = now - h_acq
            local h_prio_raw = redis.call('GET', 'OMNISIGHT_PRIO:' .. h_task)
            local h_prio = tonumber(h_prio_raw) or 0
            if age < preempt_after_s or priority <= h_prio then
                conflicts[#conflicts + 1] = holder_key
                conflicts[#conflicts + 1] = h_task
            end
        else
            conflicts[#conflicts + 1] = holder_key
            conflicts[#conflicts + 1] = h_task
        end
    end
end

if #conflicts > 0 then
    return {0, conflicts}
end

-- Second pass: evict preempted stale holders (we already know we can).
if preempt_after_s >= 0 then
    for i = 1, #KEYS do
        local holder_key = KEYS[i]
        local h_task = redis.call('HGET', holder_key, 'task')
        if h_task and h_task ~= task_id then
            -- we only reach here if allowed (conflicts is empty)
            redis.call('SREM', 'OMNISIGHT_TASK:' .. h_task, holder_key)
            redis.call('DEL', holder_key)
        end
    end
end

-- Third pass: write ownership.
local ttl_ms = math.floor(ttl_s * 1000)
local expires_at = now + ttl_s
for i = 1, #KEYS do
    local holder_key = KEYS[i]
    redis.call('HSET', holder_key,
               'task', task_id,
               'acq', tostring(now),
               'exp', tostring(expires_at))
    redis.call('PEXPIRE', holder_key, ttl_ms)
    redis.call('SADD', 'OMNISIGHT_TASK:' .. task_id, holder_key)
end
redis.call('SET', 'OMNISIGHT_PRIO:' .. task_id, tostring(priority), 'EX',
           math.max(1, math.ceil(ttl_s * 4)))
redis.call('EXPIRE', 'OMNISIGHT_TASK:' .. task_id, math.max(1, math.ceil(ttl_s * 4)))

return {1, tostring(expires_at)}
"""


_EXTEND_LUA = """
-- ARGV: task_id, ttl_s
local task_id = ARGV[1]
local ttl_s   = tonumber(ARGV[2])
local ttl_ms  = math.floor(ttl_s * 1000)
local now     = tonumber(ARGV[3])
local expires_at = now + ttl_s

local set_key = 'OMNISIGHT_TASK:' .. task_id
local paths   = redis.call('SMEMBERS', set_key)
local alive   = 0
for i = 1, #paths do
    local holder_key = paths[i]
    local h_task = redis.call('HGET', holder_key, 'task')
    if h_task == task_id then
        redis.call('HSET', holder_key, 'exp', tostring(expires_at))
        redis.call('PEXPIRE', holder_key, ttl_ms)
        alive = alive + 1
    else
        redis.call('SREM', set_key, holder_key)
    end
end
redis.call('EXPIRE', set_key, math.max(1, math.ceil(ttl_s * 4)))
return alive
"""


_RELEASE_LUA = """
local task_id = ARGV[1]
local set_key = 'OMNISIGHT_TASK:' .. task_id
local paths   = redis.call('SMEMBERS', set_key)
local removed = 0
for i = 1, #paths do
    local holder_key = paths[i]
    local h_task = redis.call('HGET', holder_key, 'task')
    if h_task == task_id then
        redis.call('DEL', holder_key)
        removed = removed + 1
    end
end
redis.call('DEL', set_key)
redis.call('DEL', 'OMNISIGHT_PRIO:' .. task_id)
return removed
"""


class RedisLockBackend:
    """Redis-backed lock.  Uses Lua for atomic multi-key operations."""

    def __init__(self, redis_url: str) -> None:
        import redis as _redis
        self._pool = _redis.ConnectionPool.from_url(redis_url, decode_responses=True)
        self._client = _redis.Redis(connection_pool=self._pool)
        # Bake prefix substitutions into the Lua source once.
        self._acquire = self._client.register_script(
            _ACQUIRE_LUA
            .replace("OMNISIGHT_TASK:", KEY_PREFIX + "task:")
            .replace("OMNISIGHT_PRIO:", KEY_PREFIX + "task_prio:")
        )
        self._extend = self._client.register_script(
            _EXTEND_LUA.replace("OMNISIGHT_TASK:", KEY_PREFIX + "task:")
        )
        self._release = self._client.register_script(
            _RELEASE_LUA
            .replace("OMNISIGHT_TASK:", KEY_PREFIX + "task:")
            .replace("OMNISIGHT_PRIO:", KEY_PREFIX + "task_prio:")
        )

    def _holder_key(self, path: str) -> str:
        return f"{KEY_PREFIX}holder:{path}"

    def _waiter_key(self, path: str) -> str:
        return f"{KEY_PREFIX}waiters:{path}"

    def acquire(self, task_id: str, paths: list[str], ttl_s: float,
                priority: int, preempt_after_s: float | None) -> LockResult:
        now = time.time()
        keys = [self._holder_key(p) for p in paths]
        preempt_arg = preempt_after_s if preempt_after_s is not None else -1.0
        res = self._acquire(
            keys=keys,
            args=[task_id, now, ttl_s, priority, preempt_arg],
        )
        ok = int(res[0]) == 1
        if ok:
            return LockResult(
                ok=True, task_id=task_id,
                acquired=list(paths), expires_at=float(res[1]),
            )
        conflicts: dict[str, str] = {}
        flat = res[1]
        for i in range(0, len(flat), 2):
            holder_key, holder_task = flat[i], flat[i + 1]
            # Strip prefix to get path back.
            p = holder_key.removeprefix(f"{KEY_PREFIX}holder:")
            conflicts[p] = holder_task
        return LockResult(ok=False, task_id=task_id, conflicts=conflicts)

    def release(self, task_id: str) -> int:
        return int(self._release(keys=[], args=[task_id]))

    def extend(self, task_id: str, ttl_s: float) -> bool:
        alive = int(self._extend(keys=[], args=[task_id, ttl_s, time.time()]))
        return alive > 0

    def get_holder(self, path: str) -> str | None:
        val = self._client.hget(self._holder_key(_normalise(path)), "task")
        return val if val else None

    def get_paths(self, task_id: str) -> list[str]:
        set_key = f"{KEY_PREFIX}task:{task_id}"
        members = self._client.smembers(set_key) or set()
        prefix = f"{KEY_PREFIX}holder:"
        return sorted(m.removeprefix(prefix) for m in members)

    def all_entries(self) -> list[LockEntry]:
        prefix = f"{KEY_PREFIX}holder:"
        entries: list[LockEntry] = []
        cursor = 0
        while True:
            cursor, keys = self._client.scan(
                cursor=cursor, match=f"{prefix}*", count=500,
            )
            for k in keys:
                data = self._client.hgetall(k)
                if not data:
                    continue
                entries.append(LockEntry(
                    path=k.removeprefix(prefix),
                    task_id=data.get("task", ""),
                    acquired_at=float(data.get("acq", "0")),
                    expires_at=float(data.get("exp", "0")),
                ))
            if cursor == 0:
                break
        return entries

    def waiters(self) -> dict[str, list[tuple[str, int]]]:
        prefix = f"{KEY_PREFIX}waiters:"
        out: dict[str, list[tuple[str, int]]] = {}
        cursor = 0
        while True:
            cursor, keys = self._client.scan(
                cursor=cursor, match=f"{prefix}*", count=500,
            )
            for k in keys:
                path = k.removeprefix(prefix)
                members = self._client.zrange(k, 0, -1, withscores=True) or []
                out[path] = []
                for task_id, score in members:
                    # score packing: -priority * 1e6 + ts ; priority = -(score - ts) / 1e6
                    # easier: keep per-task priority in a hash
                    prio_raw = self._client.get(f"{KEY_PREFIX}task_prio:{task_id}")
                    out[path].append((task_id, int(prio_raw) if prio_raw else 0))
            if cursor == 0:
                break
        return out

    def record_wait(self, task_id: str, paths: list[str], priority: int) -> None:
        now = time.time()
        pipe = self._client.pipeline(transaction=False)
        pipe.set(f"{KEY_PREFIX}task_prio:{task_id}", priority, ex=3600 * 4)
        for p in paths:
            # score encodes (-priority, ts) so lower-score = higher priority and earlier.
            score = -priority * 1e6 + now
            pipe.zadd(self._waiter_key(p), {task_id: score})
            pipe.expire(self._waiter_key(p), 3600 * 4)
        pipe.execute()

    def clear_waits(self, task_id: str) -> None:
        prefix = f"{KEY_PREFIX}waiters:"
        cursor = 0
        while True:
            cursor, keys = self._client.scan(
                cursor=cursor, match=f"{prefix}*", count=500,
            )
            if keys:
                pipe = self._client.pipeline(transaction=False)
                for k in keys:
                    pipe.zrem(k, task_id)
                pipe.execute()
            if cursor == 0:
                break

    def clear_all(self) -> None:
        cursor = 0
        while True:
            cursor, keys = self._client.scan(
                cursor=cursor, match=f"{KEY_PREFIX}*", count=500,
            )
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Singleton selection + public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_backend: _LockBackend | None = None
_backend_lock = threading.Lock()


def _get_backend() -> _LockBackend:
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        url = (os.environ.get("OMNISIGHT_REDIS_URL") or "").strip()
        if url:
            try:
                _backend = RedisLockBackend(url)
                logger.info("O1 dist_lock: using Redis backend at %s",
                            _redacted(url))
            except Exception as exc:
                logger.warning(
                    "O1 dist_lock: Redis unavailable (%s), "
                    "falling back to in-memory", exc,
                )
                _backend = InMemoryLockBackend()
        else:
            _backend = InMemoryLockBackend()
            logger.info("O1 dist_lock: using in-memory backend")
        return _backend


def _redacted(url: str) -> str:
    if "@" in url:
        scheme, _, rest = url.partition("://")
        _, _, hostpart = rest.partition("@")
        return f"{scheme}://***@{hostpart}"
    return url


def set_backend_for_tests(backend: _LockBackend | None) -> None:
    """Test helper — replace (or reset) the active backend.

    Passing ``None`` re-triggers auto-selection on next call.  Production
    code must not call this.
    """
    global _backend
    with _backend_lock:
        _backend = backend


def acquire_paths(task_id: str, paths: Iterable[str], ttl_s: float = DEFAULT_TTL_S,
                  priority: int = 100,
                  wait_timeout_s: float = 0.0,
                  poll_interval_s: float = 0.2) -> LockResult:
    """Atomically acquire leases on every path in ``paths`` for ``task_id``.

    Path semantics:
      * Strings are normalised (slash form) then sorted — deterministic
        acquisition order avoids AB/BA deadlocks between tasks that share
        the same sub-set of paths.
      * An empty list returns ``ok=True`` immediately.

    Concurrency semantics:
      * All-or-nothing.  If any path conflicts, no leases are taken and
        ``conflicts`` lists each blocking ``task_id``.
      * If ``wait_timeout_s > 0`` the caller polls until either the lock
        clears or the deadline passes.  During the wait we record the
        wait intent in the waiter map so the deadlock detector can see
        it (this is the wait-for edge in the graph).

    Metrics:
      * ``omnisight_dist_lock_wait_seconds`` observed on every call.
      * ``omnisight_dist_lock_held_total`` bumped when ok=True.
    """
    if not task_id or not isinstance(task_id, str):
        raise ValueError("task_id must be a non-empty string")

    path_list = _normalise_many(paths)
    if not path_list:
        return LockResult(ok=True, task_id=task_id, acquired=[], expires_at=0.0)

    backend = _get_backend()
    started = time.time()
    deadline = started + max(0.0, wait_timeout_s)

    while True:
        res = backend.acquire(task_id, path_list, ttl_s, priority, None)
        if res.ok:
            res.wait_seconds = time.time() - started
            metrics.dist_lock_wait_seconds.labels(outcome="acquired").observe(res.wait_seconds)
            metrics.dist_lock_held_total.labels(outcome="acquired").inc(len(res.acquired))
            backend.clear_waits(task_id)
            try:
                from backend.orchestration_observability import emit_lock_acquired
                emit_lock_acquired(
                    task_id=task_id,
                    paths=res.acquired,
                    priority=priority,
                    wait_seconds=res.wait_seconds,
                    expires_at=res.expires_at,
                )
            except Exception as exc:                              # pragma: no cover
                logger.debug("emit_lock_acquired failed: %s", exc)
            return res

        if wait_timeout_s <= 0 or time.time() >= deadline:
            res.wait_seconds = time.time() - started
            metrics.dist_lock_wait_seconds.labels(outcome="conflict").observe(res.wait_seconds)
            metrics.dist_lock_held_total.labels(outcome="conflict").inc()
            backend.record_wait(task_id, path_list, priority)
            return res

        backend.record_wait(task_id, path_list, priority)
        time.sleep(min(poll_interval_s, max(0.05, deadline - time.time())))


def release_paths(task_id: str) -> int:
    """Release every path currently held by ``task_id``.  Idempotent."""
    if not task_id:
        raise ValueError("task_id must be non-empty")
    backend = _get_backend()
    n = backend.release(task_id)
    if n > 0:
        metrics.dist_lock_held_total.labels(outcome="released").inc(n)
        try:
            from backend.orchestration_observability import emit_lock_released
            emit_lock_released(task_id=task_id, released_count=n)
        except Exception as exc:                                 # pragma: no cover
            logger.debug("emit_lock_released failed: %s", exc)
    return n


def extend_lease(task_id: str, ttl_s: float = DEFAULT_TTL_S) -> bool:
    """Heartbeat — push every held path's expiry out by ``ttl_s``.

    Returns True if at least one lease was refreshed.  A False return
    means the worker's lease has already been revoked (crash + timeout,
    or force-killed by deadlock resolver) and it should abort.
    """
    if not task_id:
        raise ValueError("task_id must be non-empty")
    backend = _get_backend()
    return backend.extend(task_id, ttl_s)


def preempt_paths(task_id: str, paths: Iterable[str], ttl_s: float = DEFAULT_TTL_S,
                  priority: int = 100) -> LockResult:
    """Take ``paths`` away from any stale, lower-priority holder.

    "Stale" = held for at least ``TTL × PREEMPTION_MULTIPLIER`` seconds
    (the heartbeat interval is much shorter than this, so a stale lease
    is evidence that the worker is lost).  Used by the higher-priority
    task dispatcher once DRF (O6) signals preemption is allowed.
    """
    path_list = _normalise_many(paths)
    if not path_list:
        return LockResult(ok=True, task_id=task_id, acquired=[], expires_at=0.0)
    backend = _get_backend()
    res = backend.acquire(
        task_id, path_list, ttl_s, priority,
        preempt_after_s=ttl_s * PREEMPTION_MULTIPLIER,
    )
    if res.ok:
        metrics.dist_lock_held_total.labels(outcome="preempted").inc(len(res.acquired))
    return res


def get_lock_holder(path: str) -> str | None:
    return _get_backend().get_holder(path)


def get_locked_paths(task_id: str) -> list[str]:
    return _get_backend().get_paths(task_id)


def all_entries() -> list[LockEntry]:
    return _get_backend().all_entries()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Deadlock detection (wait-for graph + cycle detection)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def build_wait_graph() -> dict[str, set[str]]:
    """Construct the wait-for graph: task_id → set of task_ids it waits on.

    An edge ``A → B`` exists if A is currently queued for a path that B
    holds.  This is rebuilt fresh on every sweep — no persistent state.
    """
    backend = _get_backend()
    entries = {e.path: e for e in backend.all_entries()}
    waiters = backend.waiters()
    graph: dict[str, set[str]] = {}
    for path, pending in waiters.items():
        holder_entry = entries.get(path)
        if holder_entry is None:
            continue
        holder = holder_entry.task_id
        for waiter_task, _prio in pending:
            if waiter_task == holder:
                continue
            graph.setdefault(waiter_task, set()).add(holder)
    return graph


def detect_deadlock_cycles(graph: dict[str, set[str]] | None = None) -> list[list[str]]:
    """Return the list of cycles in the wait-for graph (Tarjan's SCC).

    Each returned cycle is a list of task_ids in traversal order; only
    SCCs of size ≥ 2 count (self-loops are skipped — they're invariably
    caused by a task calling ``acquire_paths`` twice in parallel threads
    which is the caller's bug, not a deadlock).
    """
    g = graph if graph is not None else build_wait_graph()

    # Tarjan's algorithm — iterative to avoid recursion depth.
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(v: str) -> None:
        call_stack = [(v, iter(g.get(v, set())))]
        index[v] = lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        while call_stack:
            node, it = call_stack[-1]
            try:
                w = next(it)
                if w not in index:
                    index[w] = lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    call_stack.append((w, iter(g.get(w, set()))))
                elif w in on_stack:
                    lowlink[node] = min(lowlink[node], index[w])
            except StopIteration:
                call_stack.pop()
                if call_stack:
                    parent, _ = call_stack[-1]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
                if lowlink[node] == index[node]:
                    component: list[str] = []
                    while True:
                        u = stack.pop()
                        on_stack.discard(u)
                        component.append(u)
                        if u == node:
                            break
                    if len(component) >= 2:
                        result.append(component)

    for v in list(g.keys()):
        if v not in index:
            strongconnect(v)
    return result


def _task_priority(task_id: str) -> int:
    """Best-effort priority lookup from either backend."""
    backend = _get_backend()
    if isinstance(backend, InMemoryLockBackend):
        return backend._priority.get(task_id, 0)
    try:
        import redis  # noqa: F401 — optional
    except ImportError:
        return 0
    try:
        raw = backend._client.get(f"{KEY_PREFIX}task_prio:{task_id}")  # type: ignore[attr-defined]
        return int(raw) if raw else 0
    except Exception:
        return 0


def _kill_task(task_id: str, reason: str) -> None:
    """Forcibly release all leases for a deadlocked task and emit audit.

    The Orchestrator will see ``release_paths`` via the event bus and
    mark the task's CATC back to Failed/Requeue.  This function only
    touches the lock layer — it does not interrupt the worker process;
    the worker's next ``extend_lease`` call will return False and the
    worker aborts itself.
    """
    try:
        release_paths(task_id)
    except Exception as exc:
        logger.warning("O1 dist_lock: release_paths(%s) during kill failed: %s",
                       task_id, exc)
    metrics.dist_lock_deadlock_kills_total.labels(reason=reason).inc()
    logger.warning(
        "O1 dist_lock: killed task_id=%s reason=%s — emitting audit row",
        task_id, reason,
    )
    try:
        from backend import audit
        audit.log_sync(
            action="dist_lock.deadlock_kill",
            entity_kind="task",
            entity_id=task_id,
            after={"reason": reason},
            actor="dist_lock_detector",
        )
    except Exception as exc:
        logger.warning("O1 dist_lock: audit.log_sync failed: %s", exc)


def run_deadlock_sweep() -> DeadlockSweepResult:
    """Detect + resolve deadlocks once.

    Policy: within every cycle, kill the lowest-priority task (ties
    broken by lexicographic task_id so the choice is deterministic).
    """
    started = time.time()
    graph = build_wait_graph()
    cycles = detect_deadlock_cycles(graph)
    killed: list[str] = []
    for cycle in cycles:
        victim = min(cycle, key=lambda t: (_task_priority(t), t))
        _kill_task(victim, reason=f"deadlock_cycle_size={len(cycle)}")
        killed.append(victim)
    return DeadlockSweepResult(
        cycles_found=cycles,
        killed_task_ids=killed,
        elapsed_seconds=time.time() - started,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Background sweep task (thread-based for simplicity)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_sweep_thread: threading.Thread | None = None
_sweep_stop: threading.Event | None = None


def start_deadlock_sweep(interval_s: float = 30.0) -> None:
    """Spawn a daemon thread that runs ``run_deadlock_sweep`` every
    ``interval_s`` seconds.  Idempotent — a second call is a no-op.
    """
    global _sweep_thread, _sweep_stop
    if _sweep_thread is not None and _sweep_thread.is_alive():
        return
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval_s):
            try:
                run_deadlock_sweep()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("O1 dist_lock: deadlock sweep failed: %s", exc)

    _sweep_stop = stop
    _sweep_thread = threading.Thread(
        target=_loop, name="dist_lock_deadlock_sweep", daemon=True,
    )
    _sweep_thread.start()
    logger.info("O1 dist_lock: deadlock sweep started (interval=%.1fs)", interval_s)


def stop_deadlock_sweep() -> None:
    global _sweep_thread, _sweep_stop
    if _sweep_stop is not None:
        _sweep_stop.set()
    if _sweep_thread is not None:
        _sweep_thread.join(timeout=5.0)
    _sweep_thread = None
    _sweep_stop = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Convenience: unique task-id generator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def new_task_id(prefix: str = "task") -> str:
    """Return a fresh unique task id the worker can pass into acquire_paths."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


__all__ = [
    "DEFAULT_TTL_S",
    "HEARTBEAT_INTERVAL_S",
    "PREEMPTION_MULTIPLIER",
    "LockResult",
    "LockEntry",
    "DeadlockSweepResult",
    "InMemoryLockBackend",
    "RedisLockBackend",
    "set_backend_for_tests",
    "acquire_paths",
    "release_paths",
    "extend_lease",
    "preempt_paths",
    "get_lock_holder",
    "get_locked_paths",
    "all_entries",
    "build_wait_graph",
    "detect_deadlock_cycles",
    "run_deadlock_sweep",
    "start_deadlock_sweep",
    "stop_deadlock_sweep",
    "new_task_id",
]
