"""OP-883 D11 -- continuous SLO monitor with auto-rollback on breach.

This is the **orchestrator-side** SLO monitor sitting one level above
:mod:`backend.slo_monitor` (the OP-772 deploy-window worker that wraps
Prometheus + ``docker compose``). The D11 monitor runs continuously in
production and decides between **D10 canary** rollback and **D9 full**
prod rollback when an SLO breach is sustained past the AC #3 window.

The five acceptance criteria from META OP-761 §Phase 3:

* **AC #1** — :meth:`SloMonitor.tick` samples the metric source every
  ``sample_interval_seconds`` (default 30s, set by
  ``config/slo_thresholds.yaml``). The daemon loop in
  :func:`run_forever` is a thin sleep-around-``tick`` wrapper so tests
  can drive ticks deterministically without touching wall time.
* **AC #2** — two SLOs are evaluated independently: error rate < 1%
  (1-min window) AND p95 latency < 500ms (5-min window). The
  :class:`MetricSource` returns both numbers per tick; the windows are
  the responsibility of the source (Prometheus ``rate``/``histogram_
  quantile`` queries take ``[1m]`` / ``[5m]`` respectively).
* **AC #3** — once a sustained breach (>2min, i.e. ``ceil(120/30)+1=5``
  consecutive sampling ticks under the default cadence) is detected the
  monitor emits ``slo.breach`` on the global event bus AND invokes the
  injected :class:`RollbackTrigger`. The trigger's :attr:`mode` field
  decides whether D10 canary abort or D9 full prod rollback fires --
  the production wiring (:func:`build_default_rollback`) picks canary
  whenever a ``canary_state.json`` exists and the rollout is still
  ``running``/``paused``; otherwise it falls back to the D9 full path.
* **AC #4** — after a rollback fires the monitor sets a 10-min
  cooldown gate. Any tick whose ``now < cooldown_until`` returns
  :attr:`TickAction.cooldown_skip` (logs and emits no SSE event so the
  dashboard isn't spammed mid-incident).
* **AC #5** — the operator override flag ``slo:monitor:suppress`` (a
  file in :data:`DEFAULT_SUPPRESS_FLAG_DIR` *or* the
  ``OMNISIGHT_SLO_MONITOR_SUPPRESS`` env var) short-circuits the
  evaluation. Suppress is intended for known transient incidents
  (planned partner outage etc.) and reads-fresh every tick so an
  operator can toggle it without restarting the daemon.

Error catalog (matches the ticket description verbatim):

* :class:`SLOBreachDetected` -- raised internally when sustained breach
  fires; surfaces to the operator notify + SSE event so the on-call
  page goes out alongside the auto-rollback.
* :class:`MetricSourceUnavailable` -- raised by :class:`MetricSource`
  when Prometheus is unreachable. **Fail-open**: the monitor logs a
  warning and returns :attr:`TickAction.metric_unavailable` -- we
  refuse to auto-rollback when our own measurement plane goes dark
  (a Prometheus outage is not evidence that the prod stack is bad).
* :class:`CooldownInEffect` -- raised when an operator manually
  invokes :meth:`SloMonitor.maybe_rollback` from the runbook while the
  cooldown window is still open; the loop path never sees this because
  it checks the gate before dispatching.

Wiring notes
------------
:class:`SloMonitor` accepts the metric source / override / trigger as
constructor injections so the test suite can pin the sequence without
touching Prometheus, the file system, or Docker. Production wiring
lives in :func:`build_default_monitor` and is intentionally lazy: the
expensive imports (Prometheus client, canary_rollout) only fire when
the daemon entry point asks for them.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

from backend import events


logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "slo_thresholds.yaml"
DEFAULT_SUPPRESS_FLAG_DIR = PROJECT_ROOT / "deploy" / "blue-green"
DEFAULT_SUPPRESS_FLAG_NAME = "slo_monitor_suppress.flag"
SUPPRESS_ENV_VAR = "OMNISIGHT_SLO_MONITOR_SUPPRESS"


# ─── Error catalog ────────────────────────────────────────────────────


class SloMonitorError(RuntimeError):
    """Base for the SLO monitor error catalog."""


class SLOBreachDetected(SloMonitorError):
    """Sustained breach observed -- pages operator and triggers rollback."""


class MetricSourceUnavailable(SloMonitorError):
    """Metric backend is unreachable -- fail-open (do NOT auto-rollback)."""


class CooldownInEffect(SloMonitorError):
    """Manual rollback attempt during the AC #4 cooldown window."""


# ─── Data types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SloThresholds:
    """AC #1 + #2 + #3 + #4 numbers, loaded from
    ``config/slo_thresholds.yaml``."""

    error_rate_max: float = 0.01           # AC #2: error rate < 1%
    p95_latency_ms_max: float = 500.0      # AC #2: p95 < 500ms
    project_state_api_p95_ms_max: float = 2000.0
    cognee_query_p95_ms_max: float = 800.0
    graphiti_query_p95_ms_max: float = 600.0
    failure_recall_p95_ms_max: float = 500.0
    error_rate_window_seconds: int = 60    # AC #2: 1-min window
    p95_window_seconds: int = 300          # AC #2: 5-min window
    sample_interval_seconds: int = 30      # AC #1: sample every 30s
    breach_sustain_seconds: int = 120      # AC #3: >2min
    cooldown_seconds: int = 600            # AC #4: 10min cooldown


@dataclass(frozen=True)
class SloSample:
    """One reading from the metric source."""

    error_rate: float
    p95_latency_ms: float
    observed_at: float
    project_state_api_p95_ms: float = 0.0
    cognee_query_p95_ms: float = 0.0
    graphiti_query_p95_ms: float = 0.0
    failure_recall_p95_ms: float = 0.0


@dataclass(frozen=True)
class RollbackOutcome:
    """Outcome of a triggered rollback. ``mode`` is ``"canary"`` (D10
    abort) or ``"full"`` (D9 prod rollback) so the SSE payload can tell
    the dashboard which path fired."""

    mode: str
    status: str
    detail: str


class TickAction:
    """Enumerated tick outcomes. Strings (not Enum) so they serialise
    flat into SSE payloads and log lines."""

    ok = "ok"
    breach_pending = "breach_pending"
    rollback_triggered = "rollback_triggered"
    cooldown_skip = "cooldown_skip"
    suppressed = "suppressed"
    metric_unavailable = "metric_unavailable"


@dataclass(frozen=True)
class TickResult:
    """What :meth:`SloMonitor.tick` decided this iteration."""

    action: str
    sample: SloSample | None = None
    breached_metrics: tuple[str, ...] = ()
    rollback: RollbackOutcome | None = None
    axis_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    detail: str = ""


CROSS_TASK_SLOS: tuple[tuple[str, str, str], ...] = (
    (
        "project_state_api_p95",
        "project_state_api_p95_ms",
        "project_state_api_p95_ms_max",
    ),
    ("cognee_query_p95", "cognee_query_p95_ms", "cognee_query_p95_ms_max"),
    (
        "graphiti_query_p95",
        "graphiti_query_p95_ms",
        "graphiti_query_p95_ms_max",
    ),
    (
        "failure_recall_p95",
        "failure_recall_p95_ms",
        "failure_recall_p95_ms_max",
    ),
)


# ─── Protocols (injected at construction) ─────────────────────────────


class MetricSource(Protocol):
    def fetch(
        self,
        *,
        error_rate_window_seconds: int,
        p95_window_seconds: int,
    ) -> SloSample:
        """Return the current SLO sample.

        Raises :class:`MetricSourceUnavailable` when the backend is
        unreachable so the monitor can fail-open.
        """


class OverrideFlagSource(Protocol):
    def is_suppressed(self) -> bool:
        """True when the operator has set the ``slo:monitor:suppress`` flag."""


class RollbackTrigger(Protocol):
    @property
    def mode(self) -> str:
        """``"canary"`` (D10) or ``"full"`` (D9)."""

    def trigger(self, reason: str) -> RollbackOutcome:
        """Fire the rollback. May raise on unrecoverable failure."""


# ─── Config loader ────────────────────────────────────────────────────


def load_thresholds(path: Path | str = DEFAULT_CONFIG_PATH) -> SloThresholds:
    """Parse ``config/slo_thresholds.yaml`` into :class:`SloThresholds`.

    Missing keys fall back to the dataclass defaults so a partial YAML
    keeps booting; an unparseable file raises so we don't silently run
    on stale numbers.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = SloThresholds()
    return SloThresholds(
        error_rate_max=float(raw.get("error_rate_max", defaults.error_rate_max)),
        p95_latency_ms_max=float(
            raw.get("p95_latency_ms_max", defaults.p95_latency_ms_max)
        ),
        project_state_api_p95_ms_max=float(
            raw.get(
                "project_state_api_p95_ms_max",
                defaults.project_state_api_p95_ms_max,
            )
        ),
        cognee_query_p95_ms_max=float(
            raw.get("cognee_query_p95_ms_max", defaults.cognee_query_p95_ms_max)
        ),
        graphiti_query_p95_ms_max=float(
            raw.get(
                "graphiti_query_p95_ms_max",
                defaults.graphiti_query_p95_ms_max,
            )
        ),
        failure_recall_p95_ms_max=float(
            raw.get(
                "failure_recall_p95_ms_max",
                defaults.failure_recall_p95_ms_max,
            )
        ),
        error_rate_window_seconds=int(
            raw.get(
                "error_rate_window_seconds", defaults.error_rate_window_seconds
            )
        ),
        p95_window_seconds=int(
            raw.get("p95_window_seconds", defaults.p95_window_seconds)
        ),
        sample_interval_seconds=int(
            raw.get(
                "sample_interval_seconds", defaults.sample_interval_seconds
            )
        ),
        breach_sustain_seconds=int(
            raw.get("breach_sustain_seconds", defaults.breach_sustain_seconds)
        ),
        cooldown_seconds=int(
            raw.get("cooldown_seconds", defaults.cooldown_seconds)
        ),
    )


# ─── Default override flag source ─────────────────────────────────────


@dataclass
class FileOrEnvOverrideSource:
    """AC #5 override: a flag file *or* an env var.

    The flag file path is read fresh every tick so an operator can
    ``touch`` / ``rm`` it without restarting the daemon. The env var
    is a convenience for CI / sandbox runs.
    """

    flag_path: Path = field(
        default_factory=lambda: DEFAULT_SUPPRESS_FLAG_DIR
        / DEFAULT_SUPPRESS_FLAG_NAME
    )
    env_var: str = SUPPRESS_ENV_VAR

    def is_suppressed(self) -> bool:
        if os.environ.get(self.env_var, "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            return True
        try:
            return self.flag_path.is_file()
        except OSError:
            return False


# ─── SloMonitor ───────────────────────────────────────────────────────


@dataclass
class SloMonitor:
    """Continuous monitor + auto-rollback decision engine.

    The monitor is single-tenant and not thread-safe; production runs
    one instance per worker. Use :meth:`tick` from a scheduler or
    :meth:`run_forever` for a self-pacing daemon loop. ``clock`` and
    ``sleeper`` are injectable so tests can advance time deterministically.
    """

    thresholds: SloThresholds
    source: MetricSource
    override_source: OverrideFlagSource
    rollback: RollbackTrigger
    clock: Callable[[], float] = field(default=time.monotonic)
    sleeper: Callable[[float], Any] = field(default=time.sleep)
    notify: Callable[[str, str], None] | None = None

    _first_breach_at: float | None = field(default=None, init=False, repr=False)
    _cooldown_until: float = field(default=0.0, init=False, repr=False)

    # ── public ──

    def tick(self) -> TickResult:
        """Run one evaluation cycle. Returns the decision taken so a
        scheduler can log / surface it."""
        now = self.clock()

        # AC #4: cooldown gate -- short-circuit BEFORE touching the
        # override or the metric source so the post-rollback recovery
        # window is silent on the dashboard.
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            return TickResult(
                action=TickAction.cooldown_skip,
                detail=f"cooldown_remaining_s={remaining:.0f}",
            )

        # AC #5: operator override -- evaluated fresh every tick.
        if self.override_source.is_suppressed():
            # Suppression also clears any in-flight breach streak so
            # the monitor doesn't fire the second the operator removes
            # the flag mid-incident (that would defeat the point of
            # the override).
            self._first_breach_at = None
            return TickResult(action=TickAction.suppressed)

        # AC #1: pull a sample.
        try:
            sample = self.source.fetch(
                error_rate_window_seconds=self.thresholds.error_rate_window_seconds,
                p95_window_seconds=self.thresholds.p95_window_seconds,
            )
        except MetricSourceUnavailable as exc:
            # Error catalog: fail-open + log warning. We also reset the
            # breach streak: we cannot confirm the breach is still
            # happening if our measurement plane is dark, and we must
            # not let "monitor outage" graduate into "auto-rollback".
            logger.warning("SLO monitor metric source unavailable: %s", exc)
            self._first_breach_at = None
            return TickResult(
                action=TickAction.metric_unavailable, detail=str(exc),
            )

        # AC #2: evaluate each SLO independently.
        breached = self._breached_metrics(sample)
        axis_status = self._axis_status(sample)
        self._publish_status(sample=sample, axis_status=axis_status)

        if not breached:
            # Healthy tick -- clear any in-flight streak so a transient
            # spike doesn't accumulate across recoveries.
            self._first_breach_at = None
            return TickResult(
                action=TickAction.ok,
                sample=sample,
                axis_status=axis_status,
            )

        # AC #3: track sustain window. The streak starts at the FIRST
        # breaching sample, and we trigger only once ``now -
        # first_breach_at`` reaches ``breach_sustain_seconds``.
        if self._first_breach_at is None:
            self._first_breach_at = now

        elapsed_breach = now - self._first_breach_at
        if elapsed_breach < self.thresholds.breach_sustain_seconds:
            return TickResult(
                action=TickAction.breach_pending,
                sample=sample,
                breached_metrics=tuple(breached),
                axis_status=axis_status,
                detail=(
                    f"breach_for_s={elapsed_breach:.0f} "
                    f"sustain_threshold_s={self.thresholds.breach_sustain_seconds}"
                ),
            )

        # Sustained breach -- fire SSE + invoke the trigger.
        outcome = self._fire_rollback(sample=sample, breached=tuple(breached))
        # AC #4: arm cooldown regardless of trigger outcome -- the
        # only safe move after an auto-rollback attempt is to wait.
        self._cooldown_until = now + self.thresholds.cooldown_seconds
        self._first_breach_at = None
        return TickResult(
            action=TickAction.rollback_triggered,
            sample=sample,
            breached_metrics=tuple(breached),
            rollback=outcome,
            axis_status=axis_status,
        )

    def maybe_rollback(self, reason: str) -> RollbackOutcome:
        """Operator-driven entrypoint (called from the runbook script).

        Respects the cooldown gate -- raises :class:`CooldownInEffect`
        when invoked inside the AC #4 window so two operators can't
        race a manual rollback against the auto path.
        """
        now = self.clock()
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            raise CooldownInEffect(
                f"cooldown active for {remaining:.0f}s more"
            )
        outcome = self.rollback.trigger(reason=reason)
        self._cooldown_until = now + self.thresholds.cooldown_seconds
        return outcome

    def run_forever(self, *, max_iterations: int | None = None) -> None:
        """Daemon loop. ``max_iterations`` is for the daemonised tests
        that don't want a true infinite loop."""
        iterations = 0
        while True:
            try:
                self.tick()
            except Exception:  # pragma: no cover -- defensive
                logger.exception("SLO monitor tick raised")
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            self.sleeper(self.thresholds.sample_interval_seconds)

    # ── internal ──

    def _axis_status(self, sample: SloSample) -> dict[str, dict[str, Any]]:
        status: dict[str, dict[str, Any]] = {}
        for name, sample_attr, threshold_attr in CROSS_TASK_SLOS:
            value = float(getattr(sample, sample_attr))
            threshold = float(getattr(self.thresholds, threshold_attr))
            status[name] = {
                "value_ms": value,
                "threshold_ms": threshold,
                "ok": value < threshold,
            }
        return status

    def _breached_metrics(self, sample: SloSample) -> list[str]:
        breached: list[str] = []
        if sample.error_rate >= self.thresholds.error_rate_max:
            breached.append("error_rate")
        if sample.p95_latency_ms >= self.thresholds.p95_latency_ms_max:
            breached.append("p95_latency_ms")
        for name, sample_attr, threshold_attr in CROSS_TASK_SLOS:
            if (
                float(getattr(sample, sample_attr))
                >= float(getattr(self.thresholds, threshold_attr))
            ):
                breached.append(name)
        return breached

    def _publish_status(
        self, *, sample: SloSample, axis_status: dict[str, dict[str, Any]],
    ) -> None:
        events.bus.publish(
            "slo.status",
            {
                "error_rate": sample.error_rate,
                "p95_latency_ms": sample.p95_latency_ms,
                "error_rate_threshold": self.thresholds.error_rate_max,
                "p95_latency_ms_threshold": self.thresholds.p95_latency_ms_max,
                "axes": axis_status,
            },
            broadcast_scope="global",
        )

    def _fire_rollback(
        self, *, sample: SloSample, breached: tuple[str, ...],
    ) -> RollbackOutcome:
        reason = (
            "SLO breach sustained >"
            f"{self.thresholds.breach_sustain_seconds}s on "
            + ",".join(breached)
        )
        mode = self.rollback.mode
        # SSE first so the dashboard sees the breach even if the
        # rollback hangs on a Docker/Caddy retry. Payload mirrors the
        # OP-761 §AC field set so the React side can render without
        # peeking at the rollback subevent.
        events.bus.publish(
            "slo.breach",
            {
                "error_rate": sample.error_rate,
                "p95_latency_ms": sample.p95_latency_ms,
                "error_rate_threshold": self.thresholds.error_rate_max,
                "p95_latency_ms_threshold": self.thresholds.p95_latency_ms_max,
                "axes": self._axis_status(sample),
                "breached_metrics": list(breached),
                "rollback_mode": mode,
                "reason": reason,
            },
            broadcast_scope="global",
        )
        # Page operator (best-effort -- never block the rollback).
        if self.notify is not None:
            try:
                self.notify(
                    "SLO breach detected -- auto-rollback firing",
                    reason,
                )
            except Exception:  # pragma: no cover -- defensive
                logger.exception("SLO monitor notify hook raised")
        try:
            outcome = self.rollback.trigger(reason=reason)
        except Exception as exc:  # pragma: no cover -- defensive
            logger.exception("SLO monitor rollback trigger failed")
            return RollbackOutcome(
                mode=mode, status="failed", detail=str(exc),
            )
        return outcome


# ─── Production wiring (lazy) ─────────────────────────────────────────


class _PrometheusMetricSource:
    """Minimal Prometheus HTTP source -- delegates to the OP-772 client
    when the daemon is wired in. Lives here as a thin adapter so the
    OP-883 monitor doesn't import the heavier OP-772 worker at module
    import time (which would drag in ``yaml``-only config + ``docker``
    side effects)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def fetch(
        self,
        *,
        error_rate_window_seconds: int,
        p95_window_seconds: int,
    ) -> SloSample:
        import json
        import urllib.error
        import urllib.parse
        import urllib.request

        def _query(promql: str) -> float:
            url = (
                self.base_url
                + "/api/v1/query?"
                + urllib.parse.urlencode({"query": promql})
            )
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise MetricSourceUnavailable(
                    f"Prometheus query failed: {exc}"
                ) from exc
            results = payload.get("data", {}).get("result", [])
            if not results:
                return 0.0
            return float(results[0]["value"][1])

        err_span = f"{error_rate_window_seconds}s"
        lat_span = f"{p95_window_seconds}s"
        total = f"sum(rate(http_requests_total[{err_span}]))"
        errors = (
            'sum(rate(http_requests_total{status=~"5.."}'
            f"[{err_span}]))"
        )
        latency = (
            "histogram_quantile(0.95, "
            "sum(rate(http_request_duration_seconds_bucket"
            f"[{lat_span}])) by (le)) * 1000"
        )
        project_state_api_p95 = "project_state_api_p95_ms"
        cognee_query_p95 = "cognee_query_p95_ms"
        graphiti_query_p95 = "graphiti_query_p95_ms"
        failure_recall_p95 = "failure_recall_p95_ms"
        total_value = _query(total)
        error_rate = 0.0 if total_value <= 0 else _query(errors) / total_value
        return SloSample(
            error_rate=error_rate,
            p95_latency_ms=_query(latency),
            observed_at=time.time(),
            project_state_api_p95_ms=_query(project_state_api_p95),
            cognee_query_p95_ms=_query(cognee_query_p95),
            graphiti_query_p95_ms=_query(graphiti_query_p95),
            failure_recall_p95_ms=_query(failure_recall_p95),
        )


@dataclass
class _CompositeRollbackTrigger:
    """Picks D10 canary abort when a canary rollout is active, else D9
    full prod rollback. The two delegates are injected so the
    integration plumbing can be tested without Docker/Caddy."""

    canary_trigger: Callable[[str], RollbackOutcome]
    full_trigger: Callable[[str], RollbackOutcome]
    canary_active: Callable[[], bool]

    @property
    def mode(self) -> str:
        return "canary" if self.canary_active() else "full"

    def trigger(self, reason: str) -> RollbackOutcome:
        if self.canary_active():
            return self.canary_trigger(reason)
        return self.full_trigger(reason)


def build_default_rollback() -> RollbackTrigger:
    """Wire D10 canary abort + D9 full prod rollback.

    The canary side defers to :mod:`backend.canary_rollout` (its
    ``manual_control(state, "abort")``); the full side defers to
    :mod:`backend.production_release`. Both are kept inside the closure
    so importing this module from tests does not eagerly load the
    deploy plumbing.
    """

    def _canary_active() -> bool:
        from backend import canary_rollout as cr
        try:
            state = cr.load_state()
        except FileNotFoundError:
            return False
        except Exception:  # pragma: no cover -- corrupt state -> no canary
            logger.exception("canary state load failed; assuming no canary")
            return False
        return state.status in {"running", "paused"}

    def _canary_trigger(reason: str) -> RollbackOutcome:
        from backend import canary_rollout as cr
        state = cr.load_state()
        controller = cr.CanaryController()
        new_state = controller.manual_control(
            state, "abort", reason=reason,
        )
        return RollbackOutcome(
            mode="canary",
            status="aborted",
            detail=f"rollout_id={new_state.rollout_id} status={new_state.status}",
        )

    def _full_trigger(reason: str) -> RollbackOutcome:
        from backend import production_release
        tag = os.environ.get(
            "OMNISIGHT_PROD_CURRENT_IMAGE_TAG", "current",
        )
        orch = production_release.ProductionDeployOrchestrator()
        orch._rollback(tag)  # noqa: SLF001 -- intentional: D9 hook
        return RollbackOutcome(
            mode="full", status="rolled_back",
            detail=f"tag={tag} reason={reason}",
        )

    return _CompositeRollbackTrigger(
        canary_trigger=_canary_trigger,
        full_trigger=_full_trigger,
        canary_active=_canary_active,
    )


def build_default_monitor(
    *, config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> SloMonitor:
    """Construct the monitor that the prod daemon will run."""
    thresholds = load_thresholds(config_path)
    prom_url = os.environ.get(
        "OMNISIGHT_PROMETHEUS_URL", "http://localhost:9090",
    )

    def _notify(title: str, message: str) -> None:
        # Lazy import: keep notifications optional in headless contexts.
        try:
            import asyncio

            from backend.models import NotificationLevel, Severity
            from backend.notifications import notify

            async def _send() -> None:
                await notify(
                    NotificationLevel.critical,
                    title,
                    message,
                    source="slo-monitor",
                    severity=Severity.P1,
                )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(_send())
            else:
                loop.create_task(_send())
        except Exception:  # pragma: no cover -- notify must not block
            logger.exception("SLO monitor notify dispatch failed")

    return SloMonitor(
        thresholds=thresholds,
        source=_PrometheusMetricSource(prom_url),
        override_source=FileOrEnvOverrideSource(),
        rollback=build_default_rollback(),
        notify=_notify,
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- daemon
    import argparse

    parser = argparse.ArgumentParser(description="OP-883 D11 SLO monitor")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--max-iterations", type=int, default=None,
        help="Stop after N ticks (for soak tests). Default: run forever.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    monitor = build_default_monitor(config_path=args.config)
    monitor.run_forever(max_iterations=args.max_iterations)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
