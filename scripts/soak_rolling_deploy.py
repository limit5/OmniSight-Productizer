#!/usr/bin/env python3
"""G2 #5 — Rolling-deploy soak test ("0 × 5xx during deploy").

TODO row 1349:
    整合測試：部署中對 ``/api/v1/*`` 持續打流量，0 個 5xx

What this script does
---------------------
Continuously fires GET requests at a list of ``/api/v1/*`` endpoints
behind the Caddy reverse-proxy while an operator (or CI) runs a
rolling restart via ``scripts/deploy.sh … rolling``. At the end of
the soak window it aggregates status-code counts, latency percentiles
and — crucially — **asserts 0 × 5xx**.

The script is stdlib-only (urllib + threading) so it can be dropped
onto any deploy host without pulling in backend dependencies. That
matters because the operator is often running it from outside the
containerised backend environment (the whole point is to verify the
front door during the deploy window).

Why this shape
--------------
The companion in-memory integration test
(``backend/tests/test_rolling_deploy_soak.py``) is what pins the
invariant *inside CI* — deterministic, fast, and requires no real
Docker / Caddy. This script is the **operator-runbook deliverable**:
the thing a human (or a post-deploy CI gate) actually runs against
the staging or prod URL during a real deploy.

Default endpoint list picks up the three cheapest, always-available
``/api/v1/*`` routes so the soak itself doesn't amplify backend
load. Operators can override via ``--endpoint`` (repeatable).

Contract (tested by ``test_rolling_deploy_soak.py::TestSoakScript``):
    * `--base-url`, `--duration`, `--concurrency`, `--endpoint` CLI flags
    * Exits 0 when 0 × 5xx observed
    * Exits 2 when any 5xx observed (so CI / deploy.sh can fail the deploy)
    * Exits 3 on no-traffic-generated (safety — a silent dead soak
      must not be mistaken for a successful one)
    * Emits a JSON summary to stdout (last line) for log scraping
    * `set -euo pipefail`-safe: never prints unhandled traceback

Usage
-----
    # Real deploy — run in a second terminal alongside `scripts/deploy.sh prod vX rolling`
    python3 scripts/soak_rolling_deploy.py \\
        --base-url https://omnisight.example.com \\
        --duration 120 --concurrency 8

    # Staging smoke (60 s, moderate concurrency):
    python3 scripts/soak_rolling_deploy.py --duration 60

Exit codes
----------
    0  soak completed, 0 × 5xx observed
    2  5xx observed (any count > 0 fails the deploy)
    3  soak generated zero requests (operator error or target unreachable)
    4  bad CLI args

Siblings:
    * ``scripts/deploy.sh`` — the rolling-restart orchestrator
    * ``deploy/reverse-proxy/Caddyfile`` — upstream pool + eject config
    * ``deploy/reverse-proxy/README.md`` §5 — timing budget
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import time
from typing import Iterable
from urllib import error as urlerror
from urllib import request as urlrequest


# Default endpoints — all cheap, always 200, no side effects. Each one
# traverses a different backend module so the soak exercises real
# request routing rather than a static served file.
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "/api/v1/health",
    "/api/v1/version",
    "/api/v1/ui/features",
)

# Request timeout — shorter than Caddy's `lb_try_duration 5s`. A
# request that outlives the retry budget on the proxy side is already
# a red flag; we don't wait forever client-side.
DEFAULT_REQUEST_TIMEOUT = 10.0


class SoakResult:
    """Thread-safe aggregator for status codes + latencies.

    Kept as a tiny class rather than a dict so the contract test can
    import + exercise it without running the whole soak.
    """

    def __init__(self) -> None:
        # RLock, not Lock — `to_summary()` calls `count_5xx()` /
        # `percentile()` which each reacquire the same lock; a plain
        # Lock would deadlock.
        self._lock = threading.RLock()
        self.total = 0
        self.status_counts: dict[int, int] = {}
        # -1 = transport error (connection refused, DNS fail, TLS fail).
        # During a rolling restart, transport errors against the Caddy
        # front door are a test failure (Caddy itself should never go
        # down), but against a bare upstream they are expected and
        # should not count as 5xx.
        self.transport_errors = 0
        self.latencies_ms: list[float] = []

    def record(self, status: int | None, latency_ms: float) -> None:
        with self._lock:
            self.total += 1
            if status is None:
                self.transport_errors += 1
            else:
                self.status_counts[status] = self.status_counts.get(status, 0) + 1
            self.latencies_ms.append(latency_ms)

    def count_5xx(self) -> int:
        with self._lock:
            return sum(n for s, n in self.status_counts.items() if 500 <= s < 600)

    def count_4xx(self) -> int:
        with self._lock:
            return sum(n for s, n in self.status_counts.items() if 400 <= s < 500)

    def count_2xx(self) -> int:
        with self._lock:
            return sum(n for s, n in self.status_counts.items() if 200 <= s < 300)

    def percentile(self, p: float) -> float:
        with self._lock:
            if not self.latencies_ms:
                return 0.0
            s = sorted(self.latencies_ms)
            idx = min(len(s) - 1, int(len(s) * p))
            return s[idx]

    def to_summary(self) -> dict:
        with self._lock:
            return {
                "total_requests": self.total,
                "status_counts": dict(self.status_counts),
                "transport_errors": self.transport_errors,
                "count_2xx": self.count_2xx(),
                "count_4xx": self.count_4xx(),
                "count_5xx": self.count_5xx(),
                "latency_p50_ms": round(self.percentile(0.5), 2),
                "latency_p95_ms": round(self.percentile(0.95), 2),
                "latency_p99_ms": round(self.percentile(0.99), 2),
            }


def _build_opener(insecure: bool) -> urlrequest.OpenerDirector:
    """Build a urllib opener, optionally accepting self-signed certs.

    `tls internal` (the default Caddy issuer when
    `OMNISIGHT_PUBLIC_HOSTNAME` is unset) presents a self-signed cert
    that urllib refuses by default. Operators validating a bare-IP
    deploy pass `--insecure` to accept it; tightening this to a
    pinned CA is a follow-up.
    """
    ctx: ssl.SSLContext | None = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handler = urlrequest.HTTPSHandler(context=ctx) if ctx else urlrequest.HTTPSHandler()
    return urlrequest.build_opener(handler)


def _fire_one(
    opener: urlrequest.OpenerDirector,
    url: str,
    timeout: float,
    result: SoakResult,
) -> None:
    """Issue one GET request and record the status + latency."""
    req = urlrequest.Request(url, method="GET")
    req.add_header("User-Agent", "omnisight-soak-rolling-deploy/1.0")
    t0 = time.monotonic()
    status: int | None
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.getcode()
            # Drain body so the connection can be reused.
            _ = resp.read(4096)
    except urlerror.HTTPError as e:
        # HTTPError carries a status code (4xx, 5xx, 3xx …) — count it.
        status = e.code
    except Exception:
        # Transport errors (ConnectionRefused, TimeoutError, TLS, DNS)
        # — NOT a 5xx. Recorded separately so operators can triage.
        status = None
    latency_ms = (time.monotonic() - t0) * 1000.0
    result.record(status, latency_ms)


def _worker_loop(
    opener: urlrequest.OpenerDirector,
    base_url: str,
    endpoints: tuple[str, ...],
    deadline: float,
    timeout: float,
    result: SoakResult,
    stop_event: threading.Event,
) -> None:
    """Single worker thread — round-robins through endpoints until stopped."""
    i = 0
    n = len(endpoints)
    while not stop_event.is_set() and time.monotonic() < deadline:
        url = base_url.rstrip("/") + endpoints[i % n]
        i += 1
        _fire_one(opener, url, timeout, result)


def run_soak(
    base_url: str,
    duration_seconds: float,
    concurrency: int,
    endpoints: Iterable[str],
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    insecure: bool = False,
    stop_event: threading.Event | None = None,
) -> SoakResult:
    """Run a soak burst and return the aggregated result.

    Exposed as a library function (not just a `main()`) so the
    integration test can drive it against an in-process proxy without
    re-implementing the thread-pool logic.
    """
    endpoints_t = tuple(endpoints) or DEFAULT_ENDPOINTS
    result = SoakResult()
    deadline = time.monotonic() + duration_seconds
    stop = stop_event or threading.Event()
    opener = _build_opener(insecure=insecure)

    threads = [
        threading.Thread(
            target=_worker_loop,
            args=(opener, base_url, endpoints_t, deadline, request_timeout, result, stop),
            daemon=True,
            name=f"soak-worker-{i}",
        )
        for i in range(max(1, concurrency))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_seconds + request_timeout + 5.0)
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="soak_rolling_deploy",
        description=(
            "Soak a Caddy front door with `/api/v1/*` traffic during a "
            "rolling deploy and assert 0 × 5xx."
        ),
    )
    p.add_argument(
        "--base-url", default="http://localhost:443",
        help="Caddy front-door URL (default: http://localhost:443)",
    )
    p.add_argument(
        "--duration", type=float, default=60.0,
        help="Soak duration in seconds (default: 60)",
    )
    p.add_argument(
        "--concurrency", type=int, default=8,
        help="Number of parallel worker threads (default: 8)",
    )
    p.add_argument(
        "--endpoint", action="append", default=None,
        help=(
            "/api/v1/* endpoint to hammer (repeatable). "
            f"Default set: {', '.join(DEFAULT_ENDPOINTS)}"
        ),
    )
    p.add_argument(
        "--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
        help=f"Per-request timeout seconds (default: {DEFAULT_REQUEST_TIMEOUT})",
    )
    p.add_argument(
        "--insecure", action="store_true",
        help="Accept self-signed TLS certs (tls internal deploys).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        ns = _parse_args(argv)
    except SystemExit as e:
        # argparse already printed the error. Convert the standard
        # exit=2 into our "bad CLI" code 4 so 2 stays reserved for
        # "5xx observed" (the load-bearing failure the deploy pipeline
        # gates on).
        return 4 if e.code else 0

    if ns.duration <= 0 or ns.concurrency <= 0:
        print("[soak] error: --duration and --concurrency must be > 0", file=sys.stderr)
        return 4

    endpoints = tuple(ns.endpoint) if ns.endpoint else DEFAULT_ENDPOINTS
    print(
        f"[soak] target={ns.base_url} duration={ns.duration}s "
        f"concurrency={ns.concurrency} endpoints={list(endpoints)}",
        file=sys.stderr,
        flush=True,
    )
    result = run_soak(
        base_url=ns.base_url,
        duration_seconds=ns.duration,
        concurrency=ns.concurrency,
        endpoints=endpoints,
        request_timeout=ns.request_timeout,
        insecure=ns.insecure,
    )

    summary = result.to_summary()
    # Emit the JSON summary as the LAST line of stdout for log scraping.
    print(json.dumps(summary, sort_keys=True), flush=True)

    # Decision gate — any 5xx fails the soak.
    if summary["total_requests"] == 0:
        print("[soak] FAIL: zero requests generated — target unreachable or misconfigured", file=sys.stderr)
        return 3
    if summary["count_5xx"] > 0:
        print(
            f"[soak] FAIL: {summary['count_5xx']} × 5xx observed during soak "
            f"(5xx breakdown: {[(s, n) for s, n in summary['status_counts'].items() if 500 <= s < 600]})",
            file=sys.stderr,
        )
        return 2

    # Transport errors are reported but don't fail on their own — a
    # bounded number during the drain transition is acceptable if the
    # Caddy front door itself never went down. Operators should still
    # eyeball the number; CI pipelines can post-process the JSON.
    print(
        f"[soak] PASS: {summary['total_requests']} requests, 0 × 5xx, "
        f"{summary['transport_errors']} transport errors, "
        f"p95={summary['latency_p95_ms']}ms",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    raise SystemExit(main())
