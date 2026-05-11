"""OP-882 (D10 / META OP-761) — canary rollout 5% → 25% → 100%.

Drives a staged traffic shift via Caddy ``weighted_load_balancing``
with a manual gate at every transition, a per-stage SLO check
(``error_rate < 0.5%`` and p95 latency within ±20% of the captured
baseline), and an auto-rollback path back to the prior 100% stable
allocation on gate failure.

Wiring contract
---------------
* The :class:`CanaryOrchestrator` is the single entrypoint operators
  drive from the runbook. ``start()`` applies the 5% stage and emits
  ``canary.stage.started``. ``advance()`` is the manual gate — the
  operator calls it once per transition; the orchestrator verifies
  the observe window has elapsed, pulls a fresh SLO snapshot, and
  promotes to the next stage on success or rolls back on breach.
* SLO snapshots are sourced from an injected :class:`SloMonitor`. The
  D11 monitor (per ticket dependency) may not be ready yet — in that
  case operators wire :class:`_StubMonitor` from the runbook and the
  manual gate falls back to the operator's judgement (the stub always
  reports a healthy snapshot, so the SLO half of the gate becomes a
  no-op while the observe-window half still enforces dwell time).
* Traffic shifts go through :class:`TrafficShiftWriter`, which
  renders the checked-in ``deploy/caddy/canary-template.caddy`` with
  the active stage's weights and atomically replaces
  ``deploy/caddy/canary.caddy``. The writer stamps the snippet with a
  ``# canary-owner: <rollout_id>`` marker so a clobber by a second,
  concurrent orchestrator surfaces as
  :class:`TrafficShiftRaceCondition` rather than silently winning the
  race. The orchestrator retries once before giving up.

Error catalog
-------------
* :class:`CanaryGateFailed` — SLO breach detected at the manual gate;
  the orchestrator has already issued the rollback and emitted
  ``canary.gate.failed`` + ``canary.rolled_back``. The operator is
  alerted via the SSE channel.
* :class:`TrafficShiftRaceCondition` — the canary snippet was
  clobbered between read and write. ``_apply_stage`` retries once
  before re-raising so the operator can re-issue.
* :class:`RollbackFailed` — the rollback path itself could not write
  the snippet (two races back-to-back). Operator is paged for manual
  override.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from backend import events


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "deploy" / "caddy" / "canary-template.caddy"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "deploy" / "caddy" / "canary.caddy"
_OWNER_MARKER = "# canary-owner: "


# ─── Error catalog ────────────────────────────────────────────────────


class CanaryError(RuntimeError):
    """Base class so the API handler can map errors uniformly."""


class CanaryGateFailed(CanaryError):
    """Per-stage SLO check refused promotion; rollback was issued."""


class TrafficShiftRaceCondition(CanaryError):
    """Snippet was clobbered between read and write."""


class RollbackFailed(CanaryError):
    """Rollback could not restore prior 100% stable allocation."""


# ─── Stages ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CanaryStage:
    name: str
    canary_percent: int
    observe_seconds: int


STAGES: tuple[CanaryStage, ...] = (
    CanaryStage(name="5", canary_percent=5, observe_seconds=5 * 60),
    CanaryStage(name="25", canary_percent=25, observe_seconds=10 * 60),
    CanaryStage(name="100", canary_percent=100, observe_seconds=0),
)
_ROLLBACK_STAGE = CanaryStage(name="rollback", canary_percent=0, observe_seconds=0)


# ─── SLO snapshot + monitor ──────────────────────────────────────────


@dataclass(frozen=True)
class SloBaseline:
    p95_latency_ms: float


@dataclass(frozen=True)
class SloSnapshot:
    error_rate: float
    p95_latency_ms: float
    source: str = "stub"


class SloMonitor(Protocol):
    def snapshot(self) -> SloSnapshot: ...


class _StubMonitor:
    """D11 dependency stub — always reports a healthy snapshot."""

    def snapshot(self) -> SloSnapshot:
        return SloSnapshot(error_rate=0.0, p95_latency_ms=100.0, source="stub")


# ─── Traffic shift writer ────────────────────────────────────────────


class TrafficShiftWriter:
    def __init__(
        self,
        *,
        template_path: Path | None = None,
        output_path: Path | None = None,
    ) -> None:
        self.template_path = template_path or DEFAULT_TEMPLATE_PATH
        self.output_path = output_path or DEFAULT_OUTPUT_PATH

    def write(self, *, rollout_id: str, stage: CanaryStage) -> None:
        if self.output_path.exists():
            head = self.output_path.read_text(encoding="utf-8").splitlines()[:1]
            if head and head[0].startswith(_OWNER_MARKER):
                owner = head[0][len(_OWNER_MARKER):].strip()
                if owner and owner != rollout_id:
                    raise TrafficShiftRaceCondition(
                        f"canary.caddy owned by {owner!r}, "
                        f"requested by {rollout_id!r}"
                    )
        template = self.template_path.read_text(encoding="utf-8")
        rendered = (
            template
            .replace("{{STAGE_NAME}}", stage.name)
            .replace("{{CANARY_PERCENT}}", str(stage.canary_percent))
            .replace("{{STABLE_WEIGHT}}", str(100 - stage.canary_percent))
            .replace("{{CANARY_WEIGHT}}", str(stage.canary_percent))
        )
        body = (
            f"{_OWNER_MARKER}{rollout_id}\n"
            f"# canary-stage: {stage.name}\n"
            f"{rendered}"
        )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.output_path.with_suffix(self.output_path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self.output_path)


# ─── Orchestrator ────────────────────────────────────────────────────


@dataclass
class CanaryOrchestrator:
    rollout_id: str
    stable_color: str
    canary_color: str
    monitor: SloMonitor = field(default_factory=_StubMonitor)
    writer: TrafficShiftWriter = field(default_factory=TrafficShiftWriter)
    baseline: SloBaseline = field(
        default_factory=lambda: SloBaseline(p95_latency_ms=100.0)
    )
    max_error_rate: float = 0.005
    p95_tolerance: float = 0.20
    clock: Callable[[], float] = field(default=time.monotonic)
    publish: Callable[..., None] = field(default=events.bus.publish)

    _stage_index: int = field(init=False, default=-1)
    _stage_started_at: float = field(init=False, default=0.0)
    _completed: bool = field(init=False, default=False)

    def start(self) -> CanaryStage:
        if self._stage_index >= 0:
            raise CanaryError("rollout already started")
        stage = STAGES[0]
        self._apply_stage(stage)
        self._stage_index = 0
        self._stage_started_at = self.clock()
        self._emit("canary.stage.started", stage)
        return stage

    def advance(self) -> CanaryStage:
        """Manual gate: verify observe window + SLO, then promote."""
        if self._stage_index < 0:
            raise CanaryError("rollout not started")
        if self._completed:
            raise CanaryError("rollout already completed")

        current = STAGES[self._stage_index]
        elapsed = self.clock() - self._stage_started_at
        if elapsed < current.observe_seconds:
            raise CanaryGateFailed(
                f"observe window open: elapsed={elapsed:.0f}s "
                f"< required={current.observe_seconds}s"
            )

        snapshot = self.monitor.snapshot()
        breach = self._evaluate_slo(snapshot)
        if breach:
            self._emit(
                "canary.gate.failed", current,
                snapshot=snapshot, reason=breach,
            )
            self._rollback_after_breach(breach)
            raise CanaryGateFailed(breach)

        if self._stage_index >= len(STAGES) - 1:
            self._completed = True
            self._emit("canary.completed", current, snapshot=snapshot)
            return current

        self._stage_index += 1
        next_stage = STAGES[self._stage_index]
        self._apply_stage(next_stage)
        self._stage_started_at = self.clock()
        self._emit(
            "canary.stage.transitioned", next_stage,
            snapshot=snapshot, from_stage=current.name,
        )
        return next_stage

    def rollback(self, *, reason: str) -> None:
        """Restore prior 100% stable allocation."""
        try:
            self._apply_stage(_ROLLBACK_STAGE)
        except TrafficShiftRaceCondition as exc:
            raise RollbackFailed(
                f"rollback could not write snippet: {exc}"
            ) from exc
        self._emit("canary.rolled_back", _ROLLBACK_STAGE, reason=reason)

    # ── internals ──────────────────────────────────────────────────

    def _apply_stage(self, stage: CanaryStage) -> None:
        try:
            self.writer.write(rollout_id=self.rollout_id, stage=stage)
            return
        except TrafficShiftRaceCondition as exc:
            logger.warning(
                "canary traffic-shift race; retrying once (stage=%s): %s",
                stage.name, exc,
            )
        # one retry, then surface
        self.writer.write(rollout_id=self.rollout_id, stage=stage)

    def _evaluate_slo(self, snapshot: SloSnapshot) -> str:
        if snapshot.error_rate >= self.max_error_rate:
            return (
                f"error_rate={snapshot.error_rate:.4f} "
                f">= max={self.max_error_rate:.4f}"
            )
        lower = self.baseline.p95_latency_ms * (1 - self.p95_tolerance)
        upper = self.baseline.p95_latency_ms * (1 + self.p95_tolerance)
        if not lower <= snapshot.p95_latency_ms <= upper:
            return (
                f"p95_latency_ms={snapshot.p95_latency_ms:.1f} "
                f"outside baseline ±{int(self.p95_tolerance * 100)}% "
                f"[{lower:.1f}, {upper:.1f}]"
            )
        return ""

    def _rollback_after_breach(self, breach: str) -> None:
        try:
            self.rollback(reason=f"gate_failed:{breach}")
        except RollbackFailed as exc:
            # Re-raise the rollback failure so the operator is paged
            # (RollbackFailed maps to "emergency manual override" in
            # the runbook).
            raise RollbackFailed(
                f"canary gate failed AND rollback failed: "
                f"gate={breach}; rollback={exc}"
            ) from exc

    def _emit(
        self,
        event_name: str,
        stage: CanaryStage,
        *,
        snapshot: SloSnapshot | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "rollout_id": self.rollout_id,
            "stage": stage.name,
            "canary_percent": stage.canary_percent,
            "stable_color": self.stable_color,
            "canary_color": self.canary_color,
        }
        if snapshot is not None:
            payload["error_rate"] = snapshot.error_rate
            payload["p95_latency_ms"] = snapshot.p95_latency_ms
            payload["snapshot_source"] = snapshot.source
        payload.update(extra)
        try:
            self.publish(event_name, payload)
        except Exception:
            logger.exception("canary SSE publish failed (%s)", event_name)
