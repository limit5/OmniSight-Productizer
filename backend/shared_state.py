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
    """Simple key-value store backed by Redis hash.

    In-memory fallback uses class-level storage keyed by namespace so
    that multiple ``SharedKV("same-ns")`` instances within the same
    process share the same data — mirroring Redis behaviour.  This
    matters for the ``incr()`` counter pattern used by Z.6.5's Ollama
    fallback: the function that writes the counter and the endpoint
    that reads it create separate instances; without shared in-memory
    storage the counts would always read as zero when Redis is absent.

    Cross-worker consistency answer (SOP Step 1):
      Redis path → answer #2 (coordinated via Redis).
      In-memory path → answer #3 (故意每 worker 獨立; acceptable for
      observability counters like ollama_tool_failures where per-replica
      drift is tolerable and operators can sum across replicas).
    """

    # Per-field TTL is implemented by wrapping the user's payload in a
    # small envelope ``{_TTL_DATA_KEY: <value>, _TTL_EXPIRY_KEY: epoch}``
    # and lazy-pruning on read. This mirrors ``SessionPresence``'s
    # housekeeping rather than relying on Redis 7.4 ``HEXPIRE`` (which
    # production may not have) or ``r.expire`` on the whole hash (which
    # would cross-contaminate TTLs between fields — touching one
    # provider entry would refresh every other provider's expiry).
    _TTL_DATA_KEY = "_data"
    _TTL_EXPIRY_KEY = "_expires_at"

    # Class-level in-memory store: namespace → {field: value}.
    # Shared across all instances of the same namespace so that reads
    # and writes from different code sites see the same data (Redis-like).
    _mem: dict[str, dict[str, str]] = {}
    _mem_lock: threading.Lock = threading.Lock()

    def __init__(self, namespace: str) -> None:
        self._ns = namespace
        # Ensure namespace bucket exists; grab the per-namespace lock ref.
        with self._mem_lock:
            if namespace not in self._mem:
                self._mem[namespace] = {}
        # Use the class-level lock for all ops (simpler than per-ns locks).
        self._lock = self._mem_lock

    @property
    def _local(self) -> dict[str, str]:
        """Return the shared in-memory dict for this namespace."""
        return self._mem[self._ns]

    @_local.setter
    def _local(self, value: dict[str, str]) -> None:
        """Allow test fixtures to reset the namespace bucket via assignment."""
        with self._mem_lock:
            self._mem[self._ns] = value

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

    def incr(self, field: str, delta: int = 1) -> int:
        """Atomically increment a numeric counter stored at *field*.

        Backed by Redis HINCRBY (cross-worker consistent when Redis is
        available); falls back to the class-level in-memory store when
        Redis is absent (per process — see class docstring).
        """
        r = get_sync_redis()
        if r:
            try:
                return int(r.hincrby(self._rkey(), field, delta))
            except Exception:
                pass
        with self._lock:
            current = int(self._local.get(field, "0"))
            new_val = current + delta
            self._local[field] = str(new_val)
            return new_val

    def set_with_ttl(
        self, field: str, value: Any, ttl_seconds: float,
        *, now: float | None = None,
    ) -> None:
        """Store ``value`` with a per-field lazy-prune TTL.

        ``value`` may be any JSON-serialisable type (dict, list, str,
        int, float, bool, None). The stored payload is
        ``{_data: value, _expires_at: <epoch>}`` serialised to JSON;
        :meth:`get_with_ttl` / :meth:`get_all_with_ttl` unwrap and prune
        on read.

        ``ttl_seconds`` must be > 0. ``now`` is injectable for tests so
        the expiry clock can be frozen without monkey-patching
        ``time.time`` globally — default resolves lazily to ``time.time``
        so a ``monkeypatch.setattr(time, "time", ...)`` at the call site
        still takes effect.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        _now = now if now is not None else time.time()
        envelope = {
            self._TTL_DATA_KEY: value,
            self._TTL_EXPIRY_KEY: _now + float(ttl_seconds),
        }
        self.set(field, json.dumps(envelope))

    def get_with_ttl(
        self, field: str, *, now: float | None = None,
    ) -> Any:
        """Return the unwrapped payload, or ``None`` if absent / expired.

        Expired entries are deleted in-place (lazy prune) so the hash
        does not grow without bound when callers rotate through
        provider names faster than they re-read them.

        Malformed entries (missing envelope, non-JSON, non-dict,
        missing expiry) are treated as absent and also pruned — keeps
        the store self-healing if a prior writer used a different
        encoding.
        """
        raw = self.get(field, default="")
        if not raw:
            return None
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            self.delete(field)
            return None
        if (
            not isinstance(envelope, dict)
            or self._TTL_EXPIRY_KEY not in envelope
        ):
            self.delete(field)
            return None
        expires_at = envelope.get(self._TTL_EXPIRY_KEY)
        try:
            expires_at_f = float(expires_at)
        except (TypeError, ValueError):
            self.delete(field)
            return None
        _now = now if now is not None else time.time()
        if _now >= expires_at_f:
            self.delete(field)
            return None
        return envelope.get(self._TTL_DATA_KEY)

    def get_all_with_ttl(
        self, *, now: float | None = None,
    ) -> dict[str, Any]:
        """Return ``{field: unwrapped_value}`` for all non-expired
        entries. Expired + malformed entries are lazy-pruned."""
        _now = now if now is not None else time.time()
        out: dict[str, Any] = {}
        for field, raw in list(self.get_all().items()):
            if not raw:
                self.delete(field)
                continue
            try:
                envelope = json.loads(raw)
            except (TypeError, ValueError):
                self.delete(field)
                continue
            if (
                not isinstance(envelope, dict)
                or self._TTL_EXPIRY_KEY not in envelope
            ):
                self.delete(field)
                continue
            try:
                expires_at_f = float(envelope.get(self._TTL_EXPIRY_KEY))
            except (TypeError, ValueError):
                self.delete(field)
                continue
            if _now >= expires_at_f:
                self.delete(field)
                continue
            out[field] = envelope.get(self._TTL_DATA_KEY)
        return out


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
        # ZZ.A1 (#303-1, 2026-04-24): prompt-cache observability.
        # NULL-by-default on pre-ZZ Redis payloads (see
        # _normalize_token_entry); fresh entries start at 0 because
        # once a worker on the ZZ-capable code path writes an entry
        # its cache counters are authoritative (absent cache data
        # from the provider is normalised to 0 in the LLM callback).
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "cache_hit_ratio": 0.0,
        # ZZ.A3 (#303-3, 2026-04-24): per-turn LLM-compute boundary
        # stamps in ISO-8601 UTC. These are *last-turn snapshots* (not
        # accumulated) — each ``track()`` overwrites with the current
        # turn's ``on_llm_start`` / ``on_llm_end`` wall-clock. The
        # difference ``turn_ended_at - turn_started_at`` is pure LLM
        # compute; the difference between consecutive turns'
        # ``turn_started_at`` and the prior turn's ``turn_ended_at`` is
        # the inter-turn gap (tool execution + event-bus scheduling +
        # context-gather wait) that ZZ.A3's dashboard surfaces. Fresh
        # ZZ rows start "" and get populated on the first ``track()``;
        # legacy pre-ZZ rows loaded from Redis/PG preserve NULL via
        # _normalize_token_entry so the UI can render "—" vs a real 0.
        "turn_started_at": "",
        "turn_ended_at": "",
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
    # ZZ.A1 (#303-1): cache fields are absent on pre-ZZ payloads.
    # Preserve ``None`` explicitly rather than setdefault-ing to 0 so
    # the DB/UI can distinguish "legacy row, no data" from "ZZ-era row
    # that saw zero cache hits". Only fresh entries (via
    # _fresh_token_entry) start at 0.
    if "cache_read_tokens" not in entry:
        entry["cache_read_tokens"] = None
    if "cache_create_tokens" not in entry:
        entry["cache_create_tokens"] = None
    if "cache_hit_ratio" not in entry:
        entry["cache_hit_ratio"] = None
    # ZZ.A3 (#303-3): same NULL-vs-genuine-zero contract as cache
    # fields above — pre-ZZ rows had no per-turn boundary stamps, so
    # their absence is preserved as None and the dashboard renders
    # "—" instead of a fabricated gap. Fresh ZZ rows go through
    # _fresh_token_entry (starts "") and get populated on first
    # track().
    if "turn_started_at" not in entry:
        entry["turn_started_at"] = None
    if "turn_ended_at" not in entry:
        entry["turn_ended_at"] = None
    entry.setdefault("_total_latency", 0.0)
    return entry


def _apply_token_delta(
    entry: dict, inp: int, out: int, latency_ms: float, cost: float,
    now_hms: str, cache_read: int = 0, cache_create: int = 0,
    turn_started_at: str | None = None,
    turn_ended_at: str | None = None,
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
    # ZZ.A1 (#303-1): accumulate cache counters. Whenever track() is
    # invoked on the ZZ code path the cache_* fields become non-NULL
    # regardless of whether the prior stored value was NULL (legacy
    # row) — the first track() call "upgrades" the row to ZZ-era
    # semantics. Hit ratio is recomputed from lifetime totals so it
    # stays stable under concurrent increments (would drift if we
    # kept only the last-turn snapshot).
    prev_read = entry.get("cache_read_tokens") or 0
    prev_create = entry.get("cache_create_tokens") or 0
    entry["cache_read_tokens"] = prev_read + int(cache_read)
    entry["cache_create_tokens"] = prev_create + int(cache_create)
    denom = entry["input_tokens"] + entry["cache_read_tokens"]
    entry["cache_hit_ratio"] = (
        round(entry["cache_read_tokens"] / denom, 6) if denom > 0 else 0.0
    )
    # ZZ.A3 (#303-3): last-turn snapshot — overwrite with the current
    # turn's wall-clock boundaries. Callers that didn't capture
    # timestamps (e.g. legacy test fixtures) pass None and the stored
    # value is left alone, so partial-knowledge callers don't erase a
    # previously-populated value. First ZZ-era track() on a legacy row
    # upgrades NULL → string the same way cache fields do.
    if turn_started_at is not None:
        entry["turn_started_at"] = turn_started_at
    if turn_ended_at is not None:
        entry["turn_ended_at"] = turn_ended_at


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
              latency_ms: float, cost: float,
              cache_read_tokens: int = 0,
              cache_create_tokens: int = 0,
              turn_started_at: str | None = None,
              turn_ended_at: str | None = None) -> dict:
        """Atomically update usage for a model. Returns new totals.

        ZZ.A1 (#303-1): ``cache_read_tokens`` / ``cache_create_tokens``
        are additive counters accumulated across all calls; the
        resulting entry carries lifetime totals plus a recomputed
        ``cache_hit_ratio = cache_read / (input + cache_read)`` (0.0
        when the denominator is zero — avoids ZeroDivisionError on
        the first call of a fresh model that saw no cache). Both
        default to 0 so existing callers keep compiling without
        change; the ZZ code path plumbs real values through the LLM
        callback.

        ZZ.A3 (#303-3): ``turn_started_at`` / ``turn_ended_at`` are
        ISO-8601 UTC strings captured in the LLM callback at
        ``on_llm_start`` / ``on_llm_end`` respectively. Stored as
        last-turn snapshots (overwrite semantics, not accumulation)
        so the dashboard can compute (a) per-turn LLM compute time
        via ``end - start`` of the same row and (b) the inter-turn
        gap — tool execution + event-bus scheduling + context-gather
        wait — via ``this_turn.start - last_turn.end``. Callers that
        didn't capture stamps pass ``None`` and the stored field is
        left untouched, so back-compat test fixtures keep working
        without fabricating wall-clock values.
        """
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
                    now_hms, cache_read_tokens, cache_create_tokens,
                    turn_started_at=turn_started_at,
                    turn_ended_at=turn_ended_at,
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
                cache_read_tokens, cache_create_tokens,
                turn_started_at=turn_started_at,
                turn_ended_at=turn_ended_at,
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
