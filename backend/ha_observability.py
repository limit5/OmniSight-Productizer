"""G7 (HA-07) — helpers that drive the four HA-observability metrics.

This module sits behind the Prometheus metric primitives in
``backend/metrics.py``: it knows *when* to set / increment the gauges
and counters and computes the in-process rolling 5xx rate that we
export as a convenience gauge.

Responsibilities
----------------

1. **instance_up** — a 1/0 gauge (per replica) set to 1 the moment
   the Starlette app boots and cleared to 0 when the lifecycle
   coordinator flips to draining.
2. **rolling_deploy 5xx tracking** — a Starlette middleware observes
   every HTTP response, increments the per-status-class counter, and
   updates an in-memory 60 s ring-buffer from which the rolling 5xx
   share (0..1) is published as ``omnisight_rolling_deploy_5xx_rate``.
3. **replica_lag_seconds** — a thin setter a pg_ha sampler can call.
4. **readyz_latency_seconds** — a context manager the /readyz handler
   wraps its body in.

Everything here is deliberately side-effect-free at import time so
tests can exercise individual pieces without booting the whole app.

Contract: when prometheus_client is absent the metrics are `_NoOp`
stubs and every function here still returns sensibly (zeros, no
exceptions). That means test environments without prometheus still
run.
"""

from __future__ import annotations

import collections
import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from backend import metrics as _metrics

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  Instance identity
# ─────────────────────────────────────────────────────────────────
def get_instance_id() -> str:
    """Return a stable identifier for this backend replica.

    Resolution order:
      1. ``OMNISIGHT_INSTANCE_ID`` env var — the deployment tool
         (helm / compose) sets this per pod/container.
      2. ``HOSTNAME`` env var — populated by k8s + docker by default.
      3. ``socket.gethostname()`` — last-resort OS fallback.
    """
    for env_name in ("OMNISIGHT_INSTANCE_ID", "HOSTNAME"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


def mark_instance_up() -> None:
    """Flip ``omnisight_backend_instance_up`` to 1 for this replica."""
    _metrics.backend_instance_up.labels(instance_id=get_instance_id()).set(1)


def mark_instance_down() -> None:
    """Flip ``omnisight_backend_instance_up`` to 0 for this replica.

    Called from the lifecycle coordinator the moment SIGTERM / draining
    begins so the reverse-proxy / k8s service removes the pod from
    rotation.
    """
    _metrics.backend_instance_up.labels(instance_id=get_instance_id()).set(0)


# ─────────────────────────────────────────────────────────────────
#  Rolling 5xx tracker
# ─────────────────────────────────────────────────────────────────
# 60-second rolling window, 1-second resolution. Two deques of equal
# length — one for 5xx counts, one for total counts — keyed by the
# integer epoch second. On every observation we advance the buckets
# and accumulate.

_WINDOW_SECONDS = 60
_lock = threading.Lock()
_total_bucket: collections.deque[tuple[int, int]] = collections.deque()
_fivexx_bucket: collections.deque[tuple[int, int]] = collections.deque()


def _status_class(status_code: int) -> str:
    """Map an HTTP status code to the label value '2xx'..'5xx'.

    Status codes outside the 100-599 range are bucketed as '5xx' —
    they shouldn't happen, but if they do they're clearly a bug the
    operator wants to see in the error counter rather than silently
    dropped.
    """
    if 200 <= status_code < 300:
        return "2xx"
    if 300 <= status_code < 400:
        return "3xx"
    if 400 <= status_code < 500:
        return "4xx"
    return "5xx"


def _prune_locked(now_s: int) -> None:
    """Drop samples older than _WINDOW_SECONDS. Caller holds _lock."""
    cutoff = now_s - _WINDOW_SECONDS
    while _total_bucket and _total_bucket[0][0] <= cutoff:
        _total_bucket.popleft()
    while _fivexx_bucket and _fivexx_bucket[0][0] <= cutoff:
        _fivexx_bucket.popleft()


def record_http_response(status_code: int, *, now: float | None = None) -> None:
    """Bump the per-status-class counter + slide the 5xx rolling window.

    Safe to call from both sync and async contexts (uses a plain
    ``threading.Lock``). The ``now`` parameter exists for tests that
    want to pin the wall-clock time.
    """
    # Counter bump — always; unconditionally exported.
    cls = _status_class(status_code)
    _metrics.rolling_deploy_responses_total.labels(status_class=cls).inc()

    # Rolling window — only tracks 5xx share vs. total.
    now_s = int(now if now is not None else time.time())
    with _lock:
        _prune_locked(now_s)
        # Append or coalesce into the current-second bucket.
        if _total_bucket and _total_bucket[-1][0] == now_s:
            _total_bucket[-1] = (now_s, _total_bucket[-1][1] + 1)
        else:
            _total_bucket.append((now_s, 1))
        if cls == "5xx":
            if _fivexx_bucket and _fivexx_bucket[-1][0] == now_s:
                _fivexx_bucket[-1] = (now_s, _fivexx_bucket[-1][1] + 1)
            else:
                _fivexx_bucket.append((now_s, 1))
        rate = _current_rate_locked()
    _metrics.rolling_deploy_5xx_rate.set(rate)


def _current_rate_locked() -> float:
    total = sum(c for _, c in _total_bucket)
    if total == 0:
        return 0.0
    fivexx = sum(c for _, c in _fivexx_bucket)
    return fivexx / total


def current_5xx_rate(*, now: float | None = None) -> float:
    """Return the rolling 5xx rate (0..1) without recording a response."""
    now_s = int(now if now is not None else time.time())
    with _lock:
        _prune_locked(now_s)
        rate = _current_rate_locked()
    _metrics.rolling_deploy_5xx_rate.set(rate)
    return rate


def reset_rolling_window() -> None:
    """Clear the in-memory rolling window. Used by tests."""
    with _lock:
        _total_bucket.clear()
        _fivexx_bucket.clear()
    _metrics.rolling_deploy_5xx_rate.set(0.0)


# ─────────────────────────────────────────────────────────────────
#  Replica lag
# ─────────────────────────────────────────────────────────────────
def update_replica_lag(replica: str, lag_seconds: float) -> None:
    """Publish the current streaming-replication lag for a standby.

    The caller is typically a background sampler querying
    ``pg_stat_replication`` every few seconds.
    """
    if lag_seconds < 0:
        # A negative lag is meaningless; clamp to 0 so Grafana stays
        # honest but we don't silently drop the sample.
        lag_seconds = 0.0
    _metrics.replica_lag_seconds.labels(replica=replica).set(lag_seconds)


# ─────────────────────────────────────────────────────────────────
#  /readyz latency
# ─────────────────────────────────────────────────────────────────
@contextmanager
def observe_readyz_latency(outcome: str = "ready") -> Iterator[None]:
    """Context manager that observes the /readyz handler's wall-clock.

    ``outcome`` must be one of ``ready | not_ready | draining`` — it
    shows up as a label on the histogram so Grafana can split the
    distribution by success/failure class.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        _metrics.readyz_latency_seconds.labels(outcome=outcome).observe(elapsed)


# ─────────────────────────────────────────────────────────────────
#  Middleware registration
# ─────────────────────────────────────────────────────────────────
def register_middleware(app) -> None:
    """Attach the HTTP middleware that feeds the 5xx rolling window.

    Called from ``backend/main.py`` once at import time. Kept here so
    tests can build a minimal ASGI app without dragging in the full
    OmniSight bootstrap.
    """

    @app.middleware("http")
    async def _rolling_deploy_5xx_middleware(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception is a 5xx by contract. Record it
            # then re-raise so Starlette's exception handlers still
            # fire and the client sees the 500 payload it expects.
            record_http_response(500)
            raise
        record_http_response(response.status_code)
        return response
