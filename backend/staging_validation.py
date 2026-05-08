"""OP-768 -- staging smoke suite + metric baseline comparator.

Runs after a staging deploy and before any production promotion can
proceed. The module is deliberately stdlib-only so the deploy script can
invoke it in the same minimal environment that already runs
``scripts/prod_smoke_test.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

from backend.agents.conflict_observations import (
    ConflictObservation,
    record_observation_sync,
)


SMOKE_TIMEOUT_SECONDS = 300
METRIC_OBSERVATION_SECONDS = 900
SMOKE_TRANSACTION_COUNT = 60

MetricName = str


@dataclass(frozen=True)
class SmokeTransaction:
    """One synthetic transaction in the post-deploy smoke suite."""

    name: str
    flow: str
    method: str
    path: str
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class MetricSample:
    """Current value and rolling 7-day median baseline."""

    name: MetricName
    current: float
    baseline: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline


@dataclass(frozen=True)
class RegressionDecision:
    """Comparator outcome. ``regressions`` is empty on pass."""

    passed: bool
    samples: tuple[MetricSample, ...]
    regressions: tuple[str, ...]


class PrometheusClient(Protocol):
    def query(self, promql: str) -> float:
        """Return a scalar value for *promql*."""


class UrlLibPrometheusClient:
    """Small Prometheus HTTP API client used by the deploy gate."""

    def __init__(self, base_url: str, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query(self, promql: str) -> float:
        params = urllib.parse.urlencode({"query": promql})
        url = f"{self.base_url}/api/v1/query?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {data!r}")
        result = data.get("data", {}).get("result", [])
        if not result:
            return 0.0
        value = result[0].get("value", [None, "0"])[1]
        return float(value)


def build_smoke_suite() -> tuple[SmokeTransaction, ...]:
    """Return 60 synthetic transactions covering the OP-768 flows."""

    flows = [
        ("auth", "GET", "/api/v1/health", 10),
        ("dashboard", "GET", "/api/v1/dashboard/summary", 10),
        ("agent_invoke", "POST", "/api/v1/invoke", 15),
        ("jira_pickup", "GET", "/api/v1/agents/jira/pickup/preview", 15),
        ("gerrit_push", "POST", "/api/v1/agents/gerrit/push/preview", 10),
    ]
    out: list[SmokeTransaction] = []
    for flow, method, path, count in flows:
        for idx in range(1, count + 1):
            payload = None
            if method == "POST":
                payload = {
                    "synthetic": True,
                    "dry_run": True,
                    "transaction": f"{flow}-{idx:02d}",
                }
            out.append(
                SmokeTransaction(
                    name=f"{flow}-{idx:02d}",
                    flow=flow,
                    method=method,
                    path=path,
                    payload=payload,
                )
            )
    return tuple(out)


def run_smoke_suite(
    base_url: str,
    *,
    timeout_seconds: int = SMOKE_TIMEOUT_SECONDS,
    opener: Callable[[SmokeTransaction], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, tuple[str, ...]]:
    """Run the synthetic suite and enforce the 5-minute deadline."""

    base_url = base_url.rstrip("/")
    deadline = monotonic() + timeout_seconds
    failures: list[str] = []
    for tx in build_smoke_suite():
        if monotonic() > deadline:
            failures.append("smoke_timeout_exceeded")
            break
        ok = opener(tx) if opener is not None else _run_http_transaction(base_url, tx)
        if not ok:
            failures.append(tx.name)
    return not failures, tuple(failures)


PROMQL_CURRENT = {
    "error_rate": (
        'sum(rate(http_requests_total{env="staging",status=~"5.."}[15m])) '
        '/ clamp_min(sum(rate(http_requests_total{env="staging"}[15m])), 1)'
    ),
    "p95_latency": (
        'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
        '{env="staging"}[15m])) by (le))'
    ),
    "request_rate": 'sum(rate(http_requests_total{env="staging"}[15m]))',
}

PROMQL_BASELINE = {
    "error_rate": (
        'quantile_over_time(0.5, (sum(rate(http_requests_total'
        '{env="staging",status=~"5.."}[15m])) / clamp_min(sum(rate('
        'http_requests_total{env="staging"}[15m])), 1))[7d:15m])'
    ),
    "p95_latency": (
        'quantile_over_time(0.5, histogram_quantile(0.95, sum(rate('
        'http_request_duration_seconds_bucket{env="staging"}[15m])) '
        'by (le))[7d:15m])'
    ),
    "request_rate": (
        'quantile_over_time(0.5, sum(rate(http_requests_total'
        '{env="staging"}[15m]))[7d:15m])'
    ),
}


def collect_metric_samples(client: PrometheusClient) -> tuple[MetricSample, ...]:
    """Pull current values and rolling 7-day medians from Prometheus."""

    samples: list[MetricSample] = []
    for name in ("error_rate", "p95_latency", "request_rate"):
        samples.append(
            MetricSample(
                name=name,
                current=client.query(PROMQL_CURRENT[name]),
                baseline=client.query(PROMQL_BASELINE[name]),
            )
        )
    return tuple(samples)


def compare_metrics(samples: Iterable[MetricSample]) -> RegressionDecision:
    """Apply OP-768 pass/fail thresholds."""

    by_name = {sample.name: sample for sample in samples}
    regressions: list[str] = []

    error_rate = by_name["error_rate"]
    if error_rate.current >= error_rate.baseline * 1.5:
        regressions.append(
            "error_rate current "
            f"{error_rate.current:.6g} >= baseline+50% "
            f"{error_rate.baseline * 1.5:.6g}"
        )

    p95 = by_name["p95_latency"]
    if p95.current >= p95.baseline + 0.100:
        regressions.append(
            "p95_latency current "
            f"{p95.current:.6g}s >= baseline+100ms "
            f"{p95.baseline + 0.100:.6g}s"
        )

    request_rate = by_name["request_rate"]
    lower = request_rate.baseline * 0.8
    upper = request_rate.baseline * 1.2
    if request_rate.current < lower or request_rate.current > upper:
        regressions.append(
            "request_rate current "
            f"{request_rate.current:.6g} outside +/-20% "
            f"[{lower:.6g}, {upper:.6g}]"
        )

    sample_tuple = tuple(
        by_name[name] for name in ("error_rate", "p95_latency", "request_rate")
    )
    return RegressionDecision(
        passed=not regressions,
        samples=sample_tuple,
        regressions=tuple(regressions),
    )


def emit_staging_regression_event(
    decision: RegressionDecision,
    *,
    ticket: str = "OP-768",
) -> bool:
    """Record a ``staging_regression`` row via OP-746's writer."""

    files = tuple(
        f"{sample.name}:current={sample.current:.6g}:baseline={sample.baseline:.6g}"
        for sample in decision.samples
    )
    return record_observation_sync(
        ConflictObservation(
            ts=datetime.now(timezone.utc),
            cause_category="staging_regression",
            files_in_conflict=files,
            ticket=ticket,
            pre_existing_open_count=len(decision.regressions),
        )
    )


def run_gate(
    *,
    base_url: str,
    prometheus_url: str,
    observe_seconds: int = METRIC_OBSERVATION_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> RegressionDecision:
    """Run smoke immediately, wait 15 min, then compare metrics."""

    smoke_ok, failures = run_smoke_suite(base_url)
    if not smoke_ok:
        samples = (
            MetricSample("error_rate", 1.0, 0.0),
            MetricSample("p95_latency", 0.0, 0.0),
            MetricSample("request_rate", 0.0, 0.0),
        )
        decision = RegressionDecision(
            passed=False,
            samples=samples,
            regressions=tuple(f"smoke_failed:{failure}" for failure in failures),
        )
        emit_staging_regression_event(decision)
        return decision

    sleep(observe_seconds)
    decision = compare_metrics(
        collect_metric_samples(UrlLibPrometheusClient(prometheus_url))
    )
    if not decision.passed:
        emit_staging_regression_event(decision)
    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.staging_validation",
        description="Run OP-768 staging smoke + metric baseline gate",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OMNISIGHT_STAGING_BASE_URL", "http://localhost:8001"),
    )
    parser.add_argument(
        "--prometheus-url",
        default=os.environ.get("OMNISIGHT_PROMETHEUS_URL", "http://localhost:9090"),
    )
    parser.add_argument(
        "--observe-seconds",
        type=int,
        default=int(
            os.environ.get(
                "OMNISIGHT_STAGING_OBSERVE_SECONDS",
                str(METRIC_OBSERVATION_SECONDS),
            )
        ),
    )
    ns = parser.parse_args(argv)
    decision = run_gate(
        base_url=ns.base_url,
        prometheus_url=ns.prometheus_url,
        observe_seconds=ns.observe_seconds,
    )
    print(json.dumps({
        "passed": decision.passed,
        "samples": [sample.__dict__ for sample in decision.samples],
        "regressions": list(decision.regressions),
    }, sort_keys=True))
    return 0 if decision.passed else 7


def _run_http_transaction(base_url: str, tx: SmokeTransaction) -> bool:
    headers = {
        "Accept": "application/json",
        "User-Agent": "OmniSight-StagingSmoke/1.0",
    }
    data = None
    if tx.payload is not None:
        data = json.dumps(tx.payload).encode()
        headers["Content-Type"] = "application/json"
    token = os.environ.get("OMNISIGHT_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base_url}{tx.path}",
        data=data,
        headers=headers,
        method=tx.method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, TimeoutError):
        return False


if __name__ == "__main__":
    sys.exit(main())
