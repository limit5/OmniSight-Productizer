"""I10 — Redis-backed shared state for multi-worker uvicorn.

Provides atomic primitives (counters, key-value, pub/sub, lists) that work
across multiple uvicorn worker processes via Redis.  Falls back to in-memory
when OMNISIGHT_REDIS_URL is not set (single-worker dev mode).

All Redis operations are best-effort: if the connection drops mid-flight,
callers get the in-memory fallback value rather than an exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)

_redis_client = None
_redis_async_client = None
_redis_url: str = ""
_init_lock = threading.Lock()
_PREFIX = "omnisight:shared:"


def _get_redis_url() -> str:
    global _redis_url
    if not _redis_url:
        _redis_url = (os.environ.get("OMNISIGHT_REDIS_URL") or "").strip()
    return _redis_url


def get_sync_redis():
    """Return a synchronous Redis client, or None if unavailable."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = _get_redis_url()
    if not url:
        return None
    with _init_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            import redis as _redis
            _redis_client = _redis.Redis.from_url(url, decode_responses=True)
            _redis_client.ping()
            logger.info("I10: shared_state sync Redis connected")
            return _redis_client
        except Exception as exc:
            logger.warning("I10: sync Redis unavailable (%s)", exc)
            return None


def get_async_redis():
    """Return an async Redis client, or None if unavailable."""
    global _redis_async_client
    if _redis_async_client is not None:
        return _redis_async_client
    url = _get_redis_url()
    if not url:
        return None
    with _init_lock:
        if _redis_async_client is not None:
            return _redis_async_client
        try:
            import redis.asyncio as aioredis
            _redis_async_client = aioredis.Redis.from_url(url, decode_responses=True)
            logger.info("I10: shared_state async Redis connected")
            return _redis_async_client
        except Exception as exc:
            logger.warning("I10: async Redis unavailable (%s)", exc)
            return None


def _key(name: str) -> str:
    return _PREFIX + name


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Atomic Counter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SharedCounter:
    """Process-safe integer counter backed by Redis INCR/DECR."""

    def __init__(self, name: str, initial: int = 0) -> None:
        self._name = name
        self._local = initial
        self._lock = threading.Lock()

    def _rkey(self) -> str:
        return _key(f"counter:{self._name}")

    def get(self) -> int:
        r = get_sync_redis()
        if r:
            try:
                val = r.get(self._rkey())
                return int(val) if val is not None else 0
            except Exception:
                pass
        with self._lock:
            return self._local

    def increment(self, delta: int = 1) -> int:
        r = get_sync_redis()
        if r:
            try:
                return r.incrby(self._rkey(), delta)
            except Exception:
                pass
        with self._lock:
            self._local += delta
            return self._local

    def decrement(self, delta: int = 1) -> int:
        r = get_sync_redis()
        if r:
            try:
                return r.decrby(self._rkey(), delta)
            except Exception:
                pass
        with self._lock:
            self._local = max(0, self._local - delta)
            return self._local

    def set(self, value: int) -> None:
        r = get_sync_redis()
        if r:
            try:
                r.set(self._rkey(), value)
                return
            except Exception:
                pass
        with self._lock:
            self._local = value


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared Key-Value Store
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SharedKV:
    """Simple key-value store backed by Redis hash."""

    def __init__(self, namespace: str) -> None:
        self._ns = namespace
        self._local: dict[str, str] = {}
        self._lock = threading.Lock()

    def _rkey(self) -> str:
        return _key(f"kv:{self._ns}")

    def get(self, field: str, default: str = "") -> str:
        r = get_sync_redis()
        if r:
            try:
                val = r.hget(self._rkey(), field)
                return val if val is not None else default
            except Exception:
                pass
        with self._lock:
            return self._local.get(field, default)

    def set(self, field: str, value: str) -> None:
        r = get_sync_redis()
        if r:
            try:
                r.hset(self._rkey(), field, value)
                return
            except Exception:
                pass
        with self._lock:
            self._local[field] = value

    def get_all(self) -> dict[str, str]:
        r = get_sync_redis()
        if r:
            try:
                return r.hgetall(self._rkey()) or {}
            except Exception:
                pass
        with self._lock:
            return dict(self._local)

    def delete(self, field: str) -> None:
        r = get_sync_redis()
        if r:
            try:
                r.hdel(self._rkey(), field)
                return
            except Exception:
                pass
        with self._lock:
            self._local.pop(field, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session Presence (Q.5 #299 — active device indicator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# SOP Step 1 module-global audit: ``session_presence`` is a module-level
# singleton of :class:`SessionPresence` (a :class:`SharedKV` subclass
# namespaced ``session_presence``). Cross-worker consistency is the
# SharedKV contract — Redis-backed when ``OMNISIGHT_REDIS_URL`` is set
# (fits rubric #2 "coordinate via Redis") so every uvicorn worker +
# replica sees the same presence hash; in-memory fallback is per-worker
# (rubric #3 "deliberately per-worker" for single-worker dev). No new
# shared-mutable state is introduced beyond what ``SharedKV`` already
# guarantees.

_PRESENCE_FIELD_SEP = "|"
_PRESENCE_DEFAULT_WINDOW_SECONDS = 60.0


class SessionPresence(SharedKV):
    """Per-session heartbeat tracker for the "active devices" indicator.

    Records ``(user_id, session_id) → last_heartbeat_at`` so that the
    presence endpoint (Q.5 follow-up) can answer "how many of this
    user's devices are currently online?" and surface a mini list.

    Field key: ``f"{user_id}|{session_id}"`` — ``|`` rather than ``:``
    because ``user_id`` may itself contain ``:`` for API-key users
    (``"apikey:<id>"`` — see ``backend/auth.py::current_user``).
    Session ids are SHA-256 hex prefixes from
    ``auth.session_id_from_token`` and never contain ``|``.

    Field value: ``f"{ts:.3f}"`` (unix epoch seconds, float formatted).
    """

    def __init__(self) -> None:
        super().__init__("session_presence")

    @staticmethod
    def _field(user_id: str, session_id: str) -> str:
        return f"{user_id}{_PRESENCE_FIELD_SEP}{session_id}"

    @staticmethod
    def _split_field(field: str) -> tuple[str, str] | None:
        sep_idx = field.rfind(_PRESENCE_FIELD_SEP)
        if sep_idx <= 0 or sep_idx == len(field) - 1:
            return None
        return field[:sep_idx], field[sep_idx + 1:]

    def record_heartbeat(
        self, user_id: str, session_id: str,
        *, ts: float | None = None,
    ) -> float:
        """Write the heartbeat timestamp. Returns the ts actually stored."""
        if not user_id or not session_id:
            return 0.0
        now = ts if ts is not None else time.time()
        self.set(self._field(user_id, session_id), f"{now:.3f}")
        return now

    def drop(self, user_id: str, session_id: str) -> None:
        """Forget a session — called on SSE disconnect / logout."""
        if not user_id or not session_id:
            return
        self.delete(self._field(user_id, session_id))

    def last_seen(self, user_id: str, session_id: str) -> float | None:
        raw = self.get(self._field(user_id, session_id))
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def active_sessions(
        self, user_id: str, *, window_seconds: float | None = None,
        now: float | None = None,
    ) -> list[tuple[str, float]]:
        """Return ``[(session_id, last_heartbeat_ts), ...]`` for the user
        whose heartbeat is within ``window_seconds`` of ``now`` (default
        60 s). Sorted by heartbeat desc (freshest first)."""
        cutoff_now = now if now is not None else time.time()
        window = (
            window_seconds if window_seconds is not None
            else _PRESENCE_DEFAULT_WINDOW_SECONDS
        )
        out: list[tuple[str, float]] = []
        for key, raw in self.get_all().items():
            split = self._split_field(key)
            if split is None:
                continue
            uid, sid = split
            if uid != user_id:
                continue
            try:
                ts = float(raw)
            except (TypeError, ValueError):
                continue
            if cutoff_now - ts <= window:
                out.append((sid, ts))
        out.sort(key=lambda entry: entry[1], reverse=True)
        return out

    def active_count(
        self, user_id: str, *, window_seconds: float | None = None,
        now: float | None = None,
    ) -> int:
        return len(
            self.active_sessions(
                user_id, window_seconds=window_seconds, now=now,
            ),
        )

    def prune_expired(
        self, *, window_seconds: float | None = None,
        now: float | None = None,
    ) -> int:
        """Delete entries older than ``window_seconds`` (default 60 s).
        Opportunistic housekeeping — safe to call from the presence
        endpoint since the hash is small (one field per device)."""
        cutoff_now = now if now is not None else time.time()
        window = (
            window_seconds if window_seconds is not None
            else _PRESENCE_DEFAULT_WINDOW_SECONDS
        )
        pruned = 0
        for key, raw in list(self.get_all().items()):
            try:
                ts = float(raw)
            except (TypeError, ValueError):
                self.delete(key)
                pruned += 1
                continue
            if cutoff_now - ts > window:
                self.delete(key)
                pruned += 1
        return pruned


# Singleton — one per worker; Redis coordinates across workers/replicas.
session_presence = SessionPresence()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared Flag (boolean)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SharedFlag:
    """Process-safe boolean flag backed by Redis."""

    def __init__(self, name: str, initial: bool = False) -> None:
        self._name = name
        self._local = initial
        self._lock = threading.Lock()

    def _rkey(self) -> str:
        return _key(f"flag:{self._name}")

    def get(self) -> bool:
        r = get_sync_redis()
        if r:
            try:
                val = r.get(self._rkey())
                return val == "1" if val is not None else False
            except Exception:
                pass
        with self._lock:
            return self._local

    def set(self, value: bool) -> None:
        r = get_sync_redis()
        if r:
            try:
                r.set(self._rkey(), "1" if value else "0")
                return
            except Exception:
                pass
        with self._lock:
            self._local = value


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Redis Pub/Sub for cross-worker events
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PUBSUB_CHANNEL = "omnisight:events"
_pubsub_listener_started = False
_pubsub_callbacks: list[Callable[[str, dict], None]] = []


def publish_cross_worker(event: str, data: dict[str, Any]) -> bool:
    """Publish an event to all workers via Redis Pub/Sub.

    Returns True if published to Redis, False if Redis unavailable
    (caller should fall back to local-only delivery).
    """
    r = get_sync_redis()
    if not r:
        return False
    try:
        payload = json.dumps({"event": event, "data": data})
        r.publish(_PUBSUB_CHANNEL, payload)
        return True
    except Exception as exc:
        logger.debug("I10: cross-worker publish failed: %s", exc)
        return False


def register_cross_worker_callback(cb: Callable[[str, dict], None]) -> None:
    """Register a callback to receive events from other workers."""
    _pubsub_callbacks.append(cb)


async def start_pubsub_listener() -> None:
    """Start listening to Redis Pub/Sub for cross-worker events.

    Call this once per worker during startup (lifespan).
    Runs forever in a background task.
    """
    global _pubsub_listener_started
    if _pubsub_listener_started:
        return
    _pubsub_listener_started = True

    r = get_async_redis()
    if not r:
        logger.info("I10: no Redis — cross-worker pub/sub disabled (single-worker mode)")
        return

    try:
        pubsub = r.pubsub()
        await pubsub.subscribe(_PUBSUB_CHANNEL)
        logger.info("I10: cross-worker pub/sub listener started")

        while True:
            try:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0,
                )
                if msg and msg["type"] == "message":
                    try:
                        payload = json.loads(msg["data"])
                        event = payload["event"]
                        data = payload["data"]
                        for cb in _pubsub_callbacks:
                            try:
                                cb(event, data)
                            except Exception as exc:
                                logger.debug("I10: pubsub callback error: %s", exc)
                    except (json.JSONDecodeError, KeyError) as exc:
                        logger.debug("I10: malformed pubsub message: %s", exc)
            except asyncio.CancelledError:
                await pubsub.unsubscribe(_PUBSUB_CHANNEL)
                raise
            except Exception as exc:
                logger.warning("I10: pubsub listener error, reconnecting: %s", exc)
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("I10: pubsub listener failed to start: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared Log Buffer (Redis list)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SharedLogBuffer:
    """Bounded log buffer backed by Redis list with in-memory fallback."""

    def __init__(self, name: str, maxlen: int = 200) -> None:
        self._name = name
        self._maxlen = maxlen
        self._local: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def _rkey(self) -> str:
        return _key(f"log:{self._name}")

    def append(self, entry: dict) -> None:
        r = get_sync_redis()
        if r:
            try:
                r.rpush(self._rkey(), json.dumps(entry))
                r.ltrim(self._rkey(), -self._maxlen, -1)
                return
            except Exception:
                pass
        with self._lock:
            self._local.append(entry)

    def get_all(self) -> list[dict]:
        r = get_sync_redis()
        if r:
            try:
                items = r.lrange(self._rkey(), 0, -1)
                return [json.loads(it) for it in items]
            except Exception:
                pass
        with self._lock:
            return list(self._local)

    def get_recent(self, n: int = 50) -> list[dict]:
        r = get_sync_redis()
        if r:
            try:
                items = r.lrange(self._rkey(), -n, -1)
                return [json.loads(it) for it in items]
            except Exception:
                pass
        with self._lock:
            return list(self._local)[-n:]

    def clear(self) -> None:
        r = get_sync_redis()
        if r:
            try:
                r.delete(self._rkey())
            except Exception:
                pass
        with self._lock:
            self._local.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared Token Usage (Redis hash of JSON)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# P7 Fix B (2026-04-20): canonical public shape for a token-usage entry —
# must match ``_token_usage`` in backend/routers/system.py and the
# ``TokenUsage`` interface in lib/api.ts. Pre-P7 Redis payloads used
# ``requests`` / ``avg_latency_ms`` and were missing ``total_tokens`` /
# ``last_used``; _normalize_entry below rewrites those in place so
# track()/get_all() always emit the canonical shape regardless of what
# earlier workers wrote to Redis.


def _fresh_token_entry(model: str) -> dict:
    return {
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
        "request_count": 0,
        "avg_latency": 0,
        "last_used": "",
        # Internal: sum of all observed latencies, used to recompute
        # the avg on each track(). Stripped from the dict returned to
        # callers by _strip_internal — see get_all().
        "_total_latency": 0.0,
    }


def _normalize_token_entry(entry: dict) -> dict:
    """Accept legacy + canonical field names, return canonical shape."""
    if "request_count" not in entry and "requests" in entry:
        entry["request_count"] = entry.pop("requests")
    if "avg_latency" not in entry and "avg_latency_ms" in entry:
        entry["avg_latency"] = int(entry.pop("avg_latency_ms"))
    entry.setdefault("model", "")
    entry.setdefault("input_tokens", 0)
    entry.setdefault("output_tokens", 0)
    entry.setdefault(
        "total_tokens", entry["input_tokens"] + entry["output_tokens"],
    )
    entry.setdefault("cost", 0.0)
    entry.setdefault("request_count", 0)
    entry.setdefault("avg_latency", 0)
    entry.setdefault("last_used", "")
    entry.setdefault("_total_latency", 0.0)
    return entry


def _apply_token_delta(
    entry: dict, inp: int, out: int, latency_ms: float, cost: float,
    now_hms: str,
) -> None:
    entry["input_tokens"] += inp
    entry["output_tokens"] += out
    entry["total_tokens"] = entry["input_tokens"] + entry["output_tokens"]
    entry["cost"] += cost
    entry["request_count"] += 1
    entry["_total_latency"] += latency_ms
    entry["avg_latency"] = (
        int(entry["_total_latency"] / entry["request_count"])
        if entry["request_count"] else 0
    )
    entry["last_used"] = now_hms


def _strip_internal(entry: dict) -> dict:
    entry.pop("_total_latency", None)
    return entry


class SharedTokenUsage:
    """Per-model token usage counters backed by Redis hash."""

    def __init__(self) -> None:
        self._local: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _rkey(self) -> str:
        return _key("token_usage")

    def track(self, model: str, input_tokens: int, output_tokens: int,
              latency_ms: float, cost: float) -> dict:
        """Atomically update usage for a model. Returns new totals."""
        now_hms = datetime.now().strftime("%H:%M:%S")
        r = get_sync_redis()
        if r:
            try:
                field_key = self._rkey()
                raw = r.hget(field_key, model)
                entry = (
                    _normalize_token_entry(json.loads(raw))
                    if raw else _fresh_token_entry(model)
                )
                _apply_token_delta(
                    entry, input_tokens, output_tokens, latency_ms, cost,
                    now_hms,
                )
                r.hset(field_key, model, json.dumps(entry))
                return _strip_internal(dict(entry))
            except Exception:
                pass

        with self._lock:
            entry = (
                _normalize_token_entry(self._local[model])
                if model in self._local else _fresh_token_entry(model)
            )
            _apply_token_delta(
                entry, input_tokens, output_tokens, latency_ms, cost, now_hms,
            )
            self._local[model] = entry
            return _strip_internal(dict(entry))

    def get_all(self) -> dict[str, dict]:
        r = get_sync_redis()
        if r:
            try:
                raw = r.hgetall(self._rkey())
                return {
                    k: _strip_internal(_normalize_token_entry(json.loads(v)))
                    for k, v in raw.items()
                }
            except Exception:
                pass
        with self._lock:
            return {
                k: _strip_internal(_normalize_token_entry(dict(v)))
                for k, v in self._local.items()
            }

    def total_cost(self) -> float:
        usage = self.get_all()
        return sum(v.get("cost", 0.0) for v in usage.values())

    def set_all(self, data: dict[str, dict]) -> None:
        """Restore usage from DB (startup)."""
        r = get_sync_redis()
        if r:
            try:
                pipe = r.pipeline()
                rk = self._rkey()
                pipe.delete(rk)
                for model, entry in data.items():
                    pipe.hset(rk, model, json.dumps(entry))
                pipe.execute()
                return
            except Exception:
                pass
        with self._lock:
            self._local = {k: dict(v) for k, v in data.items()}

    def clear(self) -> None:
        r = get_sync_redis()
        if r:
            try:
                r.delete(self._rkey())
            except Exception:
                pass
        with self._lock:
            self._local.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared Hourly Ledger (Redis sorted set)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SharedHourlyLedger:
    """Rolling-window cost ledger backed by Redis sorted set."""

    def __init__(self, window_seconds: float = 3600.0) -> None:
        self._window = window_seconds
        self._local: list[tuple[float, float]] = []
        self._lock = threading.Lock()

    def _rkey(self) -> str:
        return _key("hourly_ledger")

    def record(self, cost: float) -> None:
        now = time.time()
        r = get_sync_redis()
        if r:
            try:
                member = f"{now}:{cost}"
                r.zadd(self._rkey(), {member: now})
                cutoff = now - self._window
                r.zremrangebyscore(self._rkey(), "-inf", cutoff)
                r.expire(self._rkey(), int(self._window) + 120)
                return
            except Exception:
                pass
        with self._lock:
            cutoff = now - self._window
            self._local = [(t, c) for t, c in self._local if t > cutoff]
            self._local.append((now, cost))

    def total_in_window(self) -> float:
        now = time.time()
        cutoff = now - self._window
        r = get_sync_redis()
        if r:
            try:
                members = r.zrangebyscore(self._rkey(), cutoff, "+inf")
                return sum(float(m.split(":")[-1]) for m in members)
            except Exception:
                pass
        with self._lock:
            return sum(c for t, c in self._local if t > cutoff)

    def clear(self) -> None:
        r = get_sync_redis()
        if r:
            try:
                r.delete(self._rkey())
            except Exception:
                pass
        with self._lock:
            self._local.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Halt Flag (cross-worker emergency stop)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SharedHaltFlag:
    """Halt flag that propagates across workers via Redis + local Event."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._event = asyncio.Event()
        self._event.set()

    def _rkey(self) -> str:
        return _key(f"halt:{self._name}")

    def is_running(self) -> bool:
        r = get_sync_redis()
        if r:
            try:
                val = r.get(self._rkey())
                return val != "halted"
            except Exception:
                pass
        return self._event.is_set()

    def halt(self) -> None:
        self._event.clear()
        r = get_sync_redis()
        if r:
            try:
                r.set(self._rkey(), "halted", ex=3600)
            except Exception:
                pass
        publish_cross_worker("_halt", {"name": self._name})

    def resume(self) -> None:
        self._event.set()
        r = get_sync_redis()
        if r:
            try:
                r.delete(self._rkey())
            except Exception:
                pass
        publish_cross_worker("_resume", {"name": self._name})

    async def wait(self) -> None:
        """Wait until running (for use in loops that check halt state)."""
        if self.is_running():
            return
        self._event.clear()
        await self._event.wait()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cleanup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def close() -> None:
    """Shutdown Redis connections."""
    global _redis_client, _redis_async_client
    if _redis_async_client:
        try:
            await _redis_async_client.close()
        except Exception:
            pass
        _redis_async_client = None
    if _redis_client:
        try:
            _redis_client.close()
        except Exception:
            pass
        _redis_client = None


def reset_for_tests() -> None:
    """Clear all shared state — for test isolation only."""
    global _redis_client, _redis_async_client, _pubsub_listener_started
    _redis_client = None
    _redis_async_client = None
    _pubsub_listener_started = False
    _pubsub_callbacks.clear()
    try:
        # Drop in-memory presence entries so per-test reset is clean.
        # No Redis call — reset_for_tests is also the path that nulls
        # the Redis clients above, so any residual Redis state will be
        # re-read from the new connection on next access.
        session_presence._local.clear()
    except Exception:
        pass
