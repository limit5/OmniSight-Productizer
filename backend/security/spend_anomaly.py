"""KS.1.6 -- per-tenant token-rate anomaly detector.

This module mirrors ``backend.agents.cost_guard``: callers configure a
tenant threshold, record observed token usage, receive a decision, and
an alert sink fans out notifications when the threshold trips. KS.1.10
owns the eventual ``spend_thresholds`` table; this row keeps the hot-path
contract schema-free and lets production inject a PG-backed store later.

Module-global state audit
─────────────────────────
No module-level mutable detector singleton is created. Store instances
are caller-owned. ``SharedKVSpendAnomalyStore`` coordinates rolling
usage, throttle markers, thresholds, and alerts through Redis when
``OMNISIGHT_REDIS_URL`` is set; without Redis it deliberately falls back
to per-worker in-memory state for single-worker dev/test deployments.

Read-after-write timing audit
─────────────────────────────
The detector's only read-after-write path is the rolling token total
returned by ``record_usage``. The Redis store computes write + prune +
sum in one Lua script, so multi-worker production sees one atomic value.
The in-memory fallback is intentionally per-worker and thread-locked.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from backend.shared_state import SharedKV, get_sync_redis

logger = logging.getLogger(__name__)


AlertAction = Literal["notify", "throttle"]

DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_THROTTLE_SECONDS = 300.0


@dataclass(frozen=True)
class SpendThreshold:
    """Per-tenant token-rate threshold for KS.1.6."""

    tenant_id: str
    token_rate_limit: int
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    throttle_seconds: float = DEFAULT_THROTTLE_SECONDS
    enabled: bool = True


@dataclass(frozen=True)
class TokenUsageEvent:
    """One observed LLM usage event."""

    tenant_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    request_id: str | None = None
    user_id: str | None = None
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        return max(
            0,
            int(self.input_tokens)
            + int(self.output_tokens)
            + int(self.cache_read_tokens)
            + int(self.cache_creation_tokens),
        )


@dataclass(frozen=True)
class SpendAnomalyAlert:
    """A fired spend-anomaly alert."""

    alert_id: str
    tenant_id: str
    threshold_tokens: int
    observed_tokens: int
    window_seconds: float
    throttle_until: datetime
    action: AlertAction
    fired_at: datetime
    request_id: str | None = None
    user_id: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class SpendDecision:
    """Outcome returned to the LLM call site."""

    allowed: bool
    tenant_id: str
    observed_tokens: int = 0
    threshold_tokens: int = 0
    retry_after_seconds: float = 0.0
    reason: str = ""
    alert: SpendAnomalyAlert | None = None


class SpendAnomalyStore(Protocol):
    """Persistence surface for KS.1.6 thresholds, windows, and alerts."""

    async def upsert_threshold(self, threshold: SpendThreshold) -> None: ...
    async def get_threshold(self, tenant_id: str) -> SpendThreshold | None: ...
    async def record_usage(
        self,
        tenant_id: str,
        total_tokens: int,
        *,
        now: float,
        window_seconds: float,
    ) -> int: ...
    async def throttle_until(self, tenant_id: str, *, now: float) -> float | None: ...
    async def set_throttle(
        self,
        tenant_id: str,
        until: float,
        *,
        now: float,
    ) -> None: ...
    async def save_alert(self, alert: SpendAnomalyAlert) -> None: ...
    async def list_alerts(self, tenant_id: str | None = None) -> list[SpendAnomalyAlert]: ...
    async def clear(self) -> None: ...


class InMemorySpendAnomalyStore:
    """Dev / test implementation. Cross-worker production should use Redis."""

    def __init__(self) -> None:
        self._thresholds: dict[str, SpendThreshold] = {}
        self._usage: dict[str, list[tuple[float, int]]] = {}
        self._throttles: dict[str, float] = {}
        self._alerts: list[SpendAnomalyAlert] = []
        self._lock = threading.Lock()

    async def upsert_threshold(self, threshold: SpendThreshold) -> None:
        with self._lock:
            self._thresholds[threshold.tenant_id] = threshold

    async def get_threshold(self, tenant_id: str) -> SpendThreshold | None:
        with self._lock:
            return self._thresholds.get(tenant_id)

    async def record_usage(
        self,
        tenant_id: str,
        total_tokens: int,
        *,
        now: float,
        window_seconds: float,
    ) -> int:
        cutoff = now - window_seconds
        with self._lock:
            events = [
                (ts, tokens)
                for ts, tokens in self._usage.get(tenant_id, [])
                if ts >= cutoff
            ]
            events.append((now, max(0, int(total_tokens))))
            self._usage[tenant_id] = events
            return sum(tokens for _ts, tokens in events)

    async def throttle_until(self, tenant_id: str, *, now: float) -> float | None:
        with self._lock:
            until = self._throttles.get(tenant_id)
            if until is None:
                return None
            if until <= now:
                self._throttles.pop(tenant_id, None)
                return None
            return until

    async def set_throttle(
        self,
        tenant_id: str,
        until: float,
        *,
        now: float,
    ) -> None:
        del now
        with self._lock:
            self._throttles[tenant_id] = until

    async def save_alert(self, alert: SpendAnomalyAlert) -> None:
        with self._lock:
            self._alerts.append(alert)

    async def list_alerts(self, tenant_id: str | None = None) -> list[SpendAnomalyAlert]:
        with self._lock:
            items = list(self._alerts)
        if tenant_id is not None:
            items = [a for a in items if a.tenant_id == tenant_id]
        return items

    async def clear(self) -> None:
        with self._lock:
            self._thresholds.clear()
            self._usage.clear()
            self._throttles.clear()
            self._alerts.clear()


_RECORD_USAGE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local tokens = tonumber(ARGV[3])
local member = ARGV[4]
local ttl = tonumber(ARGV[5])

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, ttl)

local rows = redis.call('ZRANGEBYSCORE', key, cutoff, '+inf')
local total = 0
for _, row in ipairs(rows) do
    local tok = string.match(row, '|([0-9]+)$')
    if tok ~= nil then
        total = total + tonumber(tok)
    end
end
return total
"""


class SharedKVSpendAnomalyStore(InMemorySpendAnomalyStore):
    """Redis-coordinated store with in-memory fallback.

    Thresholds and alerts use ``SharedKV`` envelopes. Rolling usage uses
    a Redis sorted-set Lua script so prune + write + sum is atomic across
    workers. If Redis is unavailable, the inherited in-memory paths are
    used deliberately for single-worker dev/test mode.
    """

    _prefix = "omnisight:ks:spend:"

    def __init__(self) -> None:
        super().__init__()
        self._threshold_kv = SharedKV("ks_spend_thresholds")
        self._throttle_kv = SharedKV("ks_spend_throttles")
        self._alert_kv = SharedKV("ks_spend_alerts")

    async def upsert_threshold(self, threshold: SpendThreshold) -> None:
        self._threshold_kv.set(threshold.tenant_id, json.dumps(threshold.__dict__))
        await super().upsert_threshold(threshold)

    async def get_threshold(self, tenant_id: str) -> SpendThreshold | None:
        raw = self._threshold_kv.get(tenant_id)
        if raw:
            try:
                data = json.loads(raw)
                return SpendThreshold(
                    tenant_id=str(data["tenant_id"]),
                    token_rate_limit=int(data["token_rate_limit"]),
                    window_seconds=float(data.get("window_seconds", DEFAULT_WINDOW_SECONDS)),
                    throttle_seconds=float(data.get("throttle_seconds", DEFAULT_THROTTLE_SECONDS)),
                    enabled=bool(data.get("enabled", True)),
                )
            except (KeyError, TypeError, ValueError):
                self._threshold_kv.delete(tenant_id)
        return await super().get_threshold(tenant_id)

    async def record_usage(
        self,
        tenant_id: str,
        total_tokens: int,
        *,
        now: float,
        window_seconds: float,
    ) -> int:
        r = get_sync_redis()
        if not r:
            return await super().record_usage(
                tenant_id, total_tokens, now=now, window_seconds=window_seconds,
            )
        key = self._prefix + "usage:" + tenant_id
        cutoff = now - window_seconds
        member = f"{now:.6f}|{uuid.uuid4().hex}|{max(0, int(total_tokens))}"
        ttl = max(1, int(window_seconds) + 60)
        try:
            return int(r.eval(_RECORD_USAGE_LUA, 1, key, now, cutoff, total_tokens, member, ttl))
        except Exception as exc:
            logger.warning("KS.1.6 Redis usage write failed for %s: %s", tenant_id, exc)
            return await super().record_usage(
                tenant_id, total_tokens, now=now, window_seconds=window_seconds,
            )

    async def throttle_until(self, tenant_id: str, *, now: float) -> float | None:
        val = self._throttle_kv.get_with_ttl(tenant_id, now=now)
        if val is not None:
            try:
                until = float(val)
                return until if until > now else None
            except (TypeError, ValueError):
                self._throttle_kv.delete(tenant_id)
        return await super().throttle_until(tenant_id, now=now)

    async def set_throttle(
        self,
        tenant_id: str,
        until: float,
        *,
        now: float,
    ) -> None:
        ttl = max(1.0, until - now)
        self._throttle_kv.set_with_ttl(tenant_id, until, ttl, now=now)
        await super().set_throttle(tenant_id, until, now=now)

    async def save_alert(self, alert: SpendAnomalyAlert) -> None:
        self._alert_kv.set(
            alert.alert_id,
            json.dumps({
                "alert_id": alert.alert_id,
                "tenant_id": alert.tenant_id,
                "threshold_tokens": alert.threshold_tokens,
                "observed_tokens": alert.observed_tokens,
                "window_seconds": alert.window_seconds,
                "throttle_until": alert.throttle_until.isoformat(),
                "action": alert.action,
                "fired_at": alert.fired_at.isoformat(),
                "request_id": alert.request_id,
                "user_id": alert.user_id,
                "model": alert.model,
            }),
        )
        await super().save_alert(alert)

    async def list_alerts(self, tenant_id: str | None = None) -> list[SpendAnomalyAlert]:
        items: list[SpendAnomalyAlert] = []
        for raw in self._alert_kv.get_all().values():
            try:
                data = json.loads(raw)
                alert = SpendAnomalyAlert(
                    alert_id=str(data["alert_id"]),
                    tenant_id=str(data["tenant_id"]),
                    threshold_tokens=int(data["threshold_tokens"]),
                    observed_tokens=int(data["observed_tokens"]),
                    window_seconds=float(data["window_seconds"]),
                    throttle_until=datetime.fromisoformat(data["throttle_until"]),
                    action=data.get("action", "throttle"),
                    fired_at=datetime.fromisoformat(data["fired_at"]),
                    request_id=data.get("request_id"),
                    user_id=data.get("user_id"),
                    model=data.get("model"),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if tenant_id is None or alert.tenant_id == tenant_id:
                items.append(alert)
        if items:
            items.sort(key=lambda alert: alert.fired_at)
            return items
        return await super().list_alerts(tenant_id)

    async def clear(self) -> None:
        for kv in (self._threshold_kv, self._throttle_kv, self._alert_kv):
            for field in list(kv.get_all()):
                kv.delete(field)
        await super().clear()


AlertSink = Callable[[SpendAnomalyAlert], Awaitable[None]]


class SpendAnomalyDetector:
    """KS.1.6 token-rate guard.

    ``record_and_check()`` is intended to run after a provider returns
    usage. When a tenant exceeds its configured token rate for the
    current window, the detector writes a throttle marker and returns
    ``allowed=False`` so the next caller can map it to HTTP 429 or an
    LLM-submit refusal.
    """

    def __init__(
        self,
        store: SpendAnomalyStore | None = None,
        *,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self.store = store or SharedKVSpendAnomalyStore()
        self.alert_sink = alert_sink or send_spend_anomaly_notification

    async def configure_threshold(
        self,
        tenant_id: str,
        *,
        token_rate_limit: int,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
        enabled: bool = True,
    ) -> SpendThreshold:
        if token_rate_limit <= 0:
            raise ValueError("token_rate_limit must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if throttle_seconds <= 0:
            raise ValueError("throttle_seconds must be > 0")
        threshold = SpendThreshold(
            tenant_id=tenant_id,
            token_rate_limit=token_rate_limit,
            window_seconds=window_seconds,
            throttle_seconds=throttle_seconds,
            enabled=enabled,
        )
        await self.store.upsert_threshold(threshold)
        return threshold

    async def record_and_check(
        self,
        event: TokenUsageEvent,
        *,
        now: datetime | None = None,
    ) -> SpendDecision:
        now_dt = now or datetime.now(timezone.utc)
        now_ts = now_dt.timestamp()
        threshold = await self.store.get_threshold(event.tenant_id)
        if threshold is None or not threshold.enabled:
            return SpendDecision(allowed=True, tenant_id=event.tenant_id)

        existing_throttle = await self.store.throttle_until(
            event.tenant_id, now=now_ts,
        )
        if existing_throttle is not None:
            retry_after = max(0.0, existing_throttle - now_ts)
            return SpendDecision(
                allowed=False,
                tenant_id=event.tenant_id,
                threshold_tokens=threshold.token_rate_limit,
                retry_after_seconds=retry_after,
                reason="Tenant token spend is temporarily throttled",
            )

        total_tokens = event.total_tokens
        if total_tokens <= 0:
            return SpendDecision(allowed=True, tenant_id=event.tenant_id)

        observed = await self.store.record_usage(
            event.tenant_id,
            total_tokens,
            now=now_ts,
            window_seconds=threshold.window_seconds,
        )
        if observed <= threshold.token_rate_limit:
            return SpendDecision(
                allowed=True,
                tenant_id=event.tenant_id,
                observed_tokens=observed,
                threshold_tokens=threshold.token_rate_limit,
            )

        throttle_until_ts = now_ts + threshold.throttle_seconds
        await self.store.set_throttle(
            event.tenant_id, throttle_until_ts, now=now_ts,
        )
        alert = SpendAnomalyAlert(
            alert_id=f"spend_alert_{uuid.uuid4().hex[:12]}",
            tenant_id=event.tenant_id,
            threshold_tokens=threshold.token_rate_limit,
            observed_tokens=observed,
            window_seconds=threshold.window_seconds,
            throttle_until=datetime.fromtimestamp(throttle_until_ts, timezone.utc),
            action="throttle",
            fired_at=now_dt,
            request_id=event.request_id,
            user_id=event.user_id,
            model=event.model,
        )
        await self.store.save_alert(alert)
        await self._emit_alert(alert)
        return SpendDecision(
            allowed=False,
            tenant_id=event.tenant_id,
            observed_tokens=observed,
            threshold_tokens=threshold.token_rate_limit,
            retry_after_seconds=threshold.throttle_seconds,
            reason=(
                "Tenant token rate exceeded: "
                f"{observed} > {threshold.token_rate_limit} tokens "
                f"in {threshold.window_seconds:g}s"
            ),
            alert=alert,
        )

    async def _emit_alert(self, alert: SpendAnomalyAlert) -> None:
        if self.alert_sink is None:
            return
        try:
            await self.alert_sink(alert)
        except Exception:
            logger.exception("KS.1.6 alert sink raised for %s", alert.alert_id)

    async def alerts_since(self, tenant_id: str | None = None) -> list[SpendAnomalyAlert]:
        return await self.store.list_alerts(tenant_id)


async def send_spend_anomaly_notification(alert: SpendAnomalyAlert) -> None:
    """Fan out KS.1.6 alerts through the existing Slack + email legs."""

    from backend.notifications import send_notification
    from backend.severity import L1_LOG_EMAIL, L2_IM_WEBHOOK

    await send_notification(
        tier={L2_IM_WEBHOOK, L1_LOG_EMAIL},
        severity="P2",
        payload={
            "level": "action",
            "title": f"KS.1.6 spend anomaly for tenant {alert.tenant_id}",
            "message": (
                f"Observed {alert.observed_tokens} tokens in "
                f"{alert.window_seconds:g}s; threshold is "
                f"{alert.threshold_tokens}. Auto-throttle active until "
                f"{alert.throttle_until.isoformat()}."
            ),
            "source": "ks.spend_anomaly",
            "action_label": "Review tenant spend",
        },
    )


__all__ = [
    "DEFAULT_THROTTLE_SECONDS",
    "DEFAULT_WINDOW_SECONDS",
    "InMemorySpendAnomalyStore",
    "SharedKVSpendAnomalyStore",
    "SpendAnomalyAlert",
    "SpendAnomalyDetector",
    "SpendAnomalyStore",
    "SpendDecision",
    "SpendThreshold",
    "TokenUsageEvent",
    "send_spend_anomaly_notification",
]
