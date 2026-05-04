"""KS.3.5/KS.3.7 -- BYOG proxy heartbeat and metadata audit registry.

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
``record_heartbeat`` writes one JSON blob with a Redis ``EX`` TTL.
``record_audit_metadata`` appends one metadata-only JSON blob. A
subsequent read either sees complete entries or none; no prompt/response
payload is accepted by the SaaS-side API.
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
PROXY_AUDIT_METADATA_LIMIT = 100


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


@dataclass(frozen=True)
class ProxyAuditMetadata:
    proxy_id: str
    tenant_id: str = ""
    provider: str = ""
    method: str = ""
    path: str = ""
    status_code: int = 0
    model: str = ""
    token_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    recorded_at: str = ""
    received_at: float = 0.0


class ProxyHeartbeatStore(Protocol):
    def record(self, heartbeat: ProxyHeartbeat, *, ttl_s: int) -> None: ...
    def get(self, proxy_id: str) -> ProxyHeartbeat | None: ...


class ProxyAuditMetadataStore(Protocol):
    def record_metadata(self, metadata: ProxyAuditMetadata, *, limit: int) -> None: ...
    def list_metadata(self, proxy_id: str) -> list[ProxyAuditMetadata]: ...


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


class InMemoryProxyAuditMetadataStore:
    """Thread-safe dev/test store. Cross-worker production should use Redis."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metadata: dict[str, list[ProxyAuditMetadata]] = {}

    def record_metadata(self, metadata: ProxyAuditMetadata, *, limit: int) -> None:
        with self._lock:
            entries = self._metadata.setdefault(metadata.proxy_id, [])
            entries.insert(0, metadata)
            del entries[limit:]

    def list_metadata(self, proxy_id: str) -> list[ProxyAuditMetadata]:
        with self._lock:
            return list(self._metadata.get(proxy_id, []))


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


class RedisProxyAuditMetadataStore:
    """Redis list store shared by all SaaS workers; stores metadata only."""

    def __init__(self, redis_client: Any | None = None) -> None:
        self._client = redis_client or get_sync_redis()
        if self._client is None:
            raise RuntimeError(
                "RedisProxyAuditMetadataStore requires OMNISIGHT_REDIS_URL"
            )

    def _key(self, proxy_id: str) -> str:
        return f"{PROXY_HEARTBEAT_PREFIX}{proxy_id}:audit_metadata"

    def record_metadata(self, metadata: ProxyAuditMetadata, *, limit: int) -> None:
        key = self._key(metadata.proxy_id)
        self._client.lpush(key, json.dumps(asdict(metadata), sort_keys=True))
        self._client.ltrim(key, 0, limit - 1)

    def list_metadata(self, proxy_id: str) -> list[ProxyAuditMetadata]:
        raw_entries = self._client.lrange(self._key(proxy_id), 0, -1)
        entries: list[ProxyAuditMetadata] = []
        for raw in raw_entries:
            if isinstance(raw, bytes):
                raw = raw.decode()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                metadata = _audit_metadata_from_mapping(data)
                if metadata is not None:
                    entries.append(metadata)
        return entries


_default_store: ProxyHeartbeatStore | None = None
_default_audit_metadata_store: ProxyAuditMetadataStore | None = None


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


def _get_default_audit_metadata_store() -> ProxyAuditMetadataStore:
    global _default_audit_metadata_store
    if _default_audit_metadata_store is not None:
        return _default_audit_metadata_store
    try:
        if get_sync_redis() is not None:
            _default_audit_metadata_store = RedisProxyAuditMetadataStore()
            return _default_audit_metadata_store
    except Exception as exc:
        logger.debug("BYOG proxy Redis audit metadata store unavailable: %s", exc)
    _default_audit_metadata_store = InMemoryProxyAuditMetadataStore()
    return _default_audit_metadata_store


def set_proxy_audit_metadata_store_for_tests(
    store: ProxyAuditMetadataStore | None,
) -> None:
    global _default_audit_metadata_store
    _default_audit_metadata_store = store


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


def record_audit_metadata(
    metadata: ProxyAuditMetadata,
    *,
    store: ProxyAuditMetadataStore | None = None,
    now: float | None = None,
) -> ProxyAuditMetadata:
    timestamp = time.time() if now is None else now
    recorded = ProxyAuditMetadata(
        proxy_id=metadata.proxy_id,
        tenant_id=metadata.tenant_id,
        provider=metadata.provider,
        method=metadata.method,
        path=metadata.path,
        status_code=int(metadata.status_code),
        model=metadata.model,
        token_count=max(0, int(metadata.token_count)),
        prompt_tokens=max(0, int(metadata.prompt_tokens)),
        completion_tokens=max(0, int(metadata.completion_tokens)),
        total_tokens=max(0, int(metadata.total_tokens)),
        recorded_at=metadata.recorded_at,
        received_at=timestamp,
    )
    (store or _get_default_audit_metadata_store()).record_metadata(
        recorded,
        limit=PROXY_AUDIT_METADATA_LIMIT,
    )
    return recorded


def list_audit_metadata(
    proxy_id: str,
    *,
    store: ProxyAuditMetadataStore | None = None,
) -> list[ProxyAuditMetadata]:
    return (store or _get_default_audit_metadata_store()).list_metadata(proxy_id)


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


def _audit_metadata_from_mapping(data: dict[str, Any]) -> ProxyAuditMetadata | None:
    proxy_id = str(data.get("proxy_id") or "").strip()
    if not proxy_id:
        return None
    try:
        return ProxyAuditMetadata(
            proxy_id=proxy_id,
            tenant_id=str(data.get("tenant_id") or ""),
            provider=str(data.get("provider") or ""),
            method=str(data.get("method") or ""),
            path=str(data.get("path") or ""),
            status_code=int(data.get("status_code") or 0),
            model=str(data.get("model") or ""),
            token_count=int(data.get("token_count") or 0),
            prompt_tokens=int(data.get("prompt_tokens") or 0),
            completion_tokens=int(data.get("completion_tokens") or 0),
            total_tokens=int(data.get("total_tokens") or 0),
            recorded_at=str(data.get("recorded_at") or ""),
            received_at=float(data.get("received_at") or 0.0),
        )
    except (TypeError, ValueError):
        return None
