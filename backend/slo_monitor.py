"""OP-772 deploy SLO monitor and image-tag rollback helper.

Runs every 30 s during deploy and for 1 h post-deploy, detects three
consecutive SLO breaches per route, and rolls the production compose
stack back to the previous image tag when the previous tag is available.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import yaml

from backend.models import NotificationLevel, Severity

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "slos.yaml"
DEFAULT_COMPOSE_FILE = PROJECT_ROOT / "docker-compose.prod.yml"
APP_SERVICES = ("backend-a", "backend-b", "frontend")


@dataclass(frozen=True)
class RouteSlo:
    error_rate_lt: float
    p95_latency_ms_lt: float
    success_rate_gt: float


@dataclass(frozen=True)
class SloConfig:
    interval_seconds: int
    post_deploy_monitor_seconds: int
    breach_consecutive_windows: int
    rollback_max_seconds: int
    error_budget_seconds: int
    routes: Mapping[str, RouteSlo]

    def route_slo(self, route: str) -> RouteSlo:
        return self.routes.get(route, self.routes["*"])


@dataclass(frozen=True)
class RouteWindow:
    route: str
    error_rate: float
    p95_latency_ms: float
    success_rate: float


@dataclass(frozen=True)
class SloBreach:
    route: str
    metric: str
    observed: float
    threshold: float
    windows: int


@dataclass(frozen=True)
class RollbackResult:
    status: str
    previous_tag: str
    elapsed_seconds: float
    detail: str


class MetricWindowSource(Protocol):
    def fetch(self, window_seconds: int) -> Sequence[RouteWindow]:
        """Return the latest route windows from the metrics backend."""


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run one command and return its completed process."""


def load_slo_config(path: Path | str = DEFAULT_CONFIG_PATH) -> SloConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    routes = {
        route: RouteSlo(
            error_rate_lt=float(data["error_rate_lt"]),
            p95_latency_ms_lt=float(data["p95_latency_ms_lt"]),
            success_rate_gt=float(data["success_rate_gt"]),
        )
        for route, data in raw["routes"].items()
    }
    if "*" not in routes:
        raise ValueError("config/slos.yaml must define routes['*']")
    return SloConfig(
        interval_seconds=int(raw["interval_seconds"]),
        post_deploy_monitor_seconds=int(raw["post_deploy_monitor_seconds"]),
        breach_consecutive_windows=int(raw["breach_consecutive_windows"]),
        rollback_max_seconds=int(raw["rollback_max_seconds"]),
        error_budget_seconds=int(raw["error_budget"]["seconds"]),
        routes=routes,
    )


class PrometheusMetricSource:
    """Minimal Prometheus HTTP API client for route-level SLO windows."""

    def __init__(self, base_url: str, *, routes: Sequence[str] = ("*",)) -> None:
        self.base_url = base_url.rstrip("/")
        self.routes = tuple(routes) or ("*",)

    def fetch(self, window_seconds: int) -> Sequence[RouteWindow]:
        return [self._fetch_route(route, window_seconds) for route in self.routes]

    def _fetch_route(self, route: str, window_seconds: int) -> RouteWindow:
        selector = "" if route == "*" else f',route="{route}"'
        span = f"{window_seconds}s"
        total = f'sum(rate(http_requests_total{{{selector.lstrip(",")}}}[{span}]))'
        errors = f'sum(rate(http_requests_total{{status=~"5.."{selector}}}[{span}]))'
        success = f'sum(rate(http_requests_total{{status!~"5.."{selector}}}[{span}]))'
        latency = (
            "histogram_quantile(0.95, "
            f"sum(rate(http_request_duration_seconds_bucket{{{selector.lstrip(',')}}}[{span}])) by (le))"
            ") * 1000"
        )
        total_value = self._query(total)
        if total_value <= 0:
            return RouteWindow(route=route, error_rate=0.0, p95_latency_ms=0.0, success_rate=1.0)
        return RouteWindow(
            route=route,
            error_rate=self._query(errors) / total_value,
            p95_latency_ms=self._query(latency),
            success_rate=self._query(success) / total_value,
        )

    def _query(self, promql: str) -> float:
        url = self.base_url + "/api/v1/query?" + urllib.parse.urlencode({"query": promql})
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        results = payload.get("data", {}).get("result", [])
        if not results:
            return 0.0
        return float(results[0]["value"][1])


class ConsecutiveBreachDetector:
    def __init__(self, config: SloConfig) -> None:
        self.config = config
        self._counts: dict[tuple[str, str], int] = {}

    def observe(self, windows: Sequence[RouteWindow]) -> list[SloBreach]:
        breaches: list[SloBreach] = []
        seen: set[tuple[str, str]] = set()
        for window in windows:
            slo = self.config.route_slo(window.route)
            checks = (
                ("error_rate", window.error_rate, slo.error_rate_lt, window.error_rate >= slo.error_rate_lt),
                (
                    "p95_latency_ms",
                    window.p95_latency_ms,
                    slo.p95_latency_ms_lt,
                    window.p95_latency_ms >= slo.p95_latency_ms_lt,
                ),
                (
                    "success_rate",
                    window.success_rate,
                    slo.success_rate_gt,
                    window.success_rate <= slo.success_rate_gt,
                ),
            )
            for metric, observed, threshold, failed in checks:
                key = (window.route, metric)
                seen.add(key)
                self._counts[key] = self._counts.get(key, 0) + 1 if failed else 0
                if self._counts[key] == self.config.breach_consecutive_windows:
                    breaches.append(
                        SloBreach(
                            route=window.route,
                            metric=metric,
                            observed=observed,
                            threshold=threshold,
                            windows=self._counts[key],
                        )
                    )
        for key in set(self._counts) - seen:
            self._counts[key] = 0
        return breaches


def _run_command(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=str(PROJECT_ROOT),
        env=dict(env) if env is not None else None,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def image_refs_for_tag(namespace: str, tag: str) -> tuple[str, str]:
    return (
        f"ghcr.io/{namespace}/omnisight-backend:{tag}",
        f"ghcr.io/{namespace}/omnisight-frontend:{tag}",
    )


@dataclass
class ComposeRollbackExecutor:
    previous_tag: str
    namespace: str
    compose_file: Path = DEFAULT_COMPOSE_FILE
    runner: CommandRunner = _run_command
    env: Mapping[str, str] = field(default_factory=lambda: os.environ.copy())

    def previous_images_available(self) -> tuple[bool, str]:
        if not self.previous_tag:
            return False, "previous image tag is not configured"
        missing: list[str] = []
        for image in image_refs_for_tag(self.namespace, self.previous_tag):
            result = self.runner(
                ("docker", "manifest", "inspect", image),
                env=self.env,
                timeout=30,
            )
            if result.returncode != 0:
                missing.append(image)
        if missing:
            return False, "missing previous image(s): " + ", ".join(missing)
        return True, "previous images available"

    def rollback(self, timeout_seconds: int) -> RollbackResult:
        start = time.monotonic()
        ok, detail = self.previous_images_available()
        if not ok:
            return RollbackResult(
                status="halted",
                previous_tag=self.previous_tag,
                elapsed_seconds=time.monotonic() - start,
                detail=detail,
            )

        env = dict(self.env)
        env["OMNISIGHT_IMAGE_TAG"] = self.previous_tag
        pull = self.runner(
            ("docker", "compose", "-f", str(self.compose_file), "pull", *APP_SERVICES),
            env=env,
            timeout=timeout_seconds,
        )
        if pull.returncode != 0:
            return RollbackResult(
                status="failed",
                previous_tag=self.previous_tag,
                elapsed_seconds=time.monotonic() - start,
                detail=pull.stderr.strip() or pull.stdout.strip() or "compose pull failed",
            )

        elapsed = time.monotonic() - start
        remaining = max(1, int(timeout_seconds - elapsed))
        up = self.runner(
            (
                "docker",
                "compose",
                "-f",
                str(self.compose_file),
                "up",
                "-d",
                "--no-deps",
                *APP_SERVICES,
            ),
            env=env,
            timeout=remaining,
        )
        return RollbackResult(
            status="rolled_back" if up.returncode == 0 else "failed",
            previous_tag=self.previous_tag,
            elapsed_seconds=time.monotonic() - start,
            detail=up.stderr.strip() or up.stdout.strip() or "compose rollback completed",
        )


async def notify_slo_breach(breach: SloBreach) -> None:
    from backend.notifications import notify

    await notify(
        NotificationLevel.warning,
        "SLO breach detected",
        (
            f"route={breach.route} metric={breach.metric} "
            f"observed={breach.observed:.6g} threshold={breach.threshold:.6g} "
            f"windows={breach.windows}"
        ),
        source="slo-monitor",
    )


async def notify_rollback(result: RollbackResult) -> None:
    from backend.notifications import notify

    if result.status == "rolled_back":
        await notify(
            NotificationLevel.critical,
            "SLO rollback completed",
            (
                f"previous_tag={result.previous_tag} elapsed={result.elapsed_seconds:.1f}s "
                f"detail={result.detail}"
            ),
            source="slo-monitor",
            severity=Severity.P1,
        )
        return
    await notify(
        NotificationLevel.critical,
        "SLO rollback halted",
        (
            f"previous_tag={result.previous_tag} status={result.status} "
            f"elapsed={result.elapsed_seconds:.1f}s detail={result.detail}"
        ),
        source="slo-monitor",
        severity=Severity.P1,
    )


class SloMonitor:
    def __init__(
        self,
        *,
        config: SloConfig,
        source: MetricWindowSource,
        rollback: ComposeRollbackExecutor,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.config = config
        self.source = source
        self.rollback = rollback
        self.detector = ConsecutiveBreachDetector(config)
        self.clock = clock
        self.sleeper = sleeper

    def run_for(self, duration_seconds: int) -> RollbackResult | None:
        deadline = self.clock() + duration_seconds
        while self.clock() < deadline:
            breaches = self.detector.observe(self.source.fetch(self.config.interval_seconds))
            if breaches:
                asyncio.run(notify_slo_breach(breaches[0]))
                result = self.rollback.rollback(self.config.rollback_max_seconds)
                asyncio.run(notify_rollback(result))
                return result
            self.sleeper(self.config.interval_seconds)
        return None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OP-772 deploy SLO monitor")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--prometheus-url", default=os.environ.get("OMNISIGHT_PROMETHEUS_URL", "http://localhost:9090"))
    parser.add_argument("--routes", default=os.environ.get("OMNISIGHT_SLO_ROUTES", "*"))
    parser.add_argument("--previous-tag", default=os.environ.get("OMNISIGHT_PREVIOUS_IMAGE_TAG", ""))
    parser.add_argument("--namespace", default=os.environ.get("OMNISIGHT_GHCR_NAMESPACE", "your-org"))
    parser.add_argument("--duration-seconds", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = load_slo_config(args.config)
    duration = args.duration_seconds or config.post_deploy_monitor_seconds
    source = PrometheusMetricSource(
        args.prometheus_url,
        routes=tuple(route.strip() for route in args.routes.split(",") if route.strip()),
    )
    rollback = ComposeRollbackExecutor(
        previous_tag=args.previous_tag,
        namespace=args.namespace,
    )
    result = SloMonitor(config=config, source=source, rollback=rollback).run_for(duration)
    if result is None:
        logger.info("SLO monitor finished without breach")
        return 0
    return 0 if result.status == "rolled_back" else 2


if __name__ == "__main__":
    raise SystemExit(main())
