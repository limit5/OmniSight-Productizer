"""KS.3.5 -- BYOG proxy heartbeat registry.

The customer-side ``omnisight-proxy`` posts a health heartbeat every 30
seconds. SaaS treats a proxy as disconnected once no heartbeat has been
observed for 60 seconds.

Module-global state audit
─────────────────────────
The production store coordinates through Redis TTL keys (SOP answer #2:
PG/Redis coordination). The in-memory fallback is intentionally
per-worker for local dev and unit tests only (SOP answer #3); production
must set ``OMNISIGHT_REDIS_URL`` before enabling BYOG tenants.

Read-after-write timing audit
─────────────────────────────
``record_heartbeat`` writes one JSON blob with a Redis ``EX`` TTL. A
subsequent read either sees the full previous heartbeat, the full new
heartbeat, or an expired/missing key; no torn state is observable.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from backend.shared_state import get_sync_redis

logger = logging.getLogger(__name__)


DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_STALE_THRESHOLD_SECONDS = 60
PROXY_HEARTBEAT_PREFIX = "omnisight:byog:proxy:"


@dataclass(frozen=True)
class ProxyHeartbeat:
    proxy_id: str
    tenant_id: str = ""
    status: str = "ok"
    service: str = "omnisight-proxy"
    provider_count: int = 0
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    recorded_at: float = 0.0


@dataclass(frozen=True)
class ProxyHealth:
    proxy_id: str
    connected: bool
    stale: bool
    stale_threshold_seconds: int
    last_heartbeat_at: float | None
    last_heartbeat_age_seconds: float | None
    heartbeat: ProxyHeartbeat | None = None


class ProxyHeartbeatStore(Protocol):
    def record(self, heartbeat: ProxyHeartbeat, *, ttl_s: int) -> None: ...
    def get(self, proxy_id: str) -> ProxyHeartbeat | None: ...


class InMemoryProxyHeartbeatStore:
    """Thread-safe dev/test store. Cross-worker production should use Redis."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._heartbeats: dict[str, ProxyHeartbeat] = {}

    def record(self, heartbeat: ProxyHeartbeat, *, ttl_s: int) -> None:
        with self._lock:
            self._heartbeats[heartbeat.proxy_id] = heartbeat

    def get(self, proxy_id: str) -> ProxyHeartbeat | None:
        with self._lock:
            return self._heartbeats.get(proxy_id)


class RedisProxyHeartbeatStore:
    """Redis TTL store shared by all SaaS workers."""

    def __init__(self, redis_client: Any | None = None) -> None:
        self._client = redis_client or get_sync_redis()
        if self._client is None:
            raise RuntimeError("RedisProxyHeartbeatStore requires OMNISIGHT_REDIS_URL")

    def _key(self, proxy_id: str) -> str:
        return f"{PROXY_HEARTBEAT_PREFIX}{proxy_id}:heartbeat"

    def record(self, heartbeat: ProxyHeartbeat, *, ttl_s: int) -> None:
        self._client.set(
            self._key(heartbeat.proxy_id),
            json.dumps(asdict(heartbeat), sort_keys=True),
            ex=ttl_s,
        )

    def get(self, proxy_id: str) -> ProxyHeartbeat | None:
        raw = self._client.get(self._key(proxy_id))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return _heartbeat_from_mapping(data)


_default_store: ProxyHeartbeatStore | None = None


def _get_default_store() -> ProxyHeartbeatStore:
    global _default_store
    if _default_store is not None:
        return _default_store
    try:
        if get_sync_redis() is not None:
            _default_store = RedisProxyHeartbeatStore()
            return _default_store
    except Exception as exc:
        logger.debug("BYOG proxy Redis heartbeat store unavailable: %s", exc)
    _default_store = InMemoryProxyHeartbeatStore()
    return _default_store


def set_proxy_heartbeat_store_for_tests(store: ProxyHeartbeatStore | None) -> None:
    global _default_store
    _default_store = store


def record_heartbeat(
    heartbeat: ProxyHeartbeat,
    *,
    store: ProxyHeartbeatStore | None = None,
    now: float | None = None,
    stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
) -> ProxyHeartbeat:
    timestamp = time.time() if now is None else now
    recorded = ProxyHeartbeat(
        proxy_id=heartbeat.proxy_id,
        tenant_id=heartbeat.tenant_id,
        status=heartbeat.status or "ok",
        service=heartbeat.service or "omnisight-proxy",
        provider_count=max(0, int(heartbeat.provider_count)),
        heartbeat_interval_seconds=max(1, int(heartbeat.heartbeat_interval_seconds)),
        recorded_at=timestamp,
    )
    (store or _get_default_store()).record(recorded, ttl_s=stale_threshold_seconds)
    return recorded


def get_proxy_health(
    proxy_id: str,
    *,
    store: ProxyHeartbeatStore | None = None,
    now: float | None = None,
    stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
) -> ProxyHealth:
    timestamp = time.time() if now is None else now
    heartbeat = (store or _get_default_store()).get(proxy_id)
    if heartbeat is None:
        return ProxyHealth(
            proxy_id=proxy_id,
            connected=False,
            stale=True,
            stale_threshold_seconds=stale_threshold_seconds,
            last_heartbeat_at=None,
            last_heartbeat_age_seconds=None,
            heartbeat=None,
        )
    age = max(0.0, timestamp - heartbeat.recorded_at)
    stale = age > stale_threshold_seconds
    return ProxyHealth(
        proxy_id=proxy_id,
        connected=not stale,
        stale=stale,
        stale_threshold_seconds=stale_threshold_seconds,
        last_heartbeat_at=heartbeat.recorded_at,
        last_heartbeat_age_seconds=age,
        heartbeat=heartbeat,
    )


def _heartbeat_from_mapping(data: dict[str, Any]) -> ProxyHeartbeat | None:
    proxy_id = str(data.get("proxy_id") or "").strip()
    if not proxy_id:
        return None
    try:
        return ProxyHeartbeat(
            proxy_id=proxy_id,
            tenant_id=str(data.get("tenant_id") or ""),
            status=str(data.get("status") or "ok"),
            service=str(data.get("service") or "omnisight-proxy"),
            provider_count=int(data.get("provider_count") or 0),
            heartbeat_interval_seconds=int(
                data.get("heartbeat_interval_seconds")
                or DEFAULT_HEARTBEAT_INTERVAL_SECONDS
            ),
            recorded_at=float(data.get("recorded_at") or 0.0),
        )
    except (TypeError, ValueError):
        return None
