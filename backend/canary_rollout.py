"""OP-771 -- progressive canary rollout controller.

This module is intentionally state-file based: Caddy reads the generated
snippet, operators inspect the JSON state next to it, and tests can
exercise the controller without Docker, Caddy, or a database.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol

from backend import ha_observability


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANARY_DIR = PROJECT_ROOT / "deploy" / "blue-green"
DEFAULT_STATE_PATH = DEFAULT_CANARY_DIR / "canary_state.json"
DEFAULT_SNIPPET_PATH = DEFAULT_CANARY_DIR / "canary-weighted.caddy"


@dataclass(frozen=True)
class CanaryStage:
    """One OP-771 ramp stage."""

    name: str
    canary_percent: int
    observe_seconds: int


CANARY_STAGES: tuple[CanaryStage, ...] = (
    CanaryStage(name="5", canary_percent=5, observe_seconds=10 * 60),
    CanaryStage(name="25", canary_percent=25, observe_seconds=15 * 60),
    CanaryStage(name="100", canary_percent=100, observe_seconds=0),
)


@dataclass(frozen=True)
class SloSnapshot:
    """D11 SLO decision input for one stage evaluation."""

    error_rate: float
    p95_latency_ms: float = 0.0
    availability: float = 1.0
    source: str = "d11"


@dataclass(frozen=True)
class SloThresholds:
    """Abort thresholds aligned with docs/ops/slo.md."""

    max_error_rate: float = 0.005
    max_p95_latency_ms: float = 1000.0
    min_availability: float = 0.99


class SloMonitor(Protocol):
    """Protocol implemented by D11 SLO monitor adapters."""

    def snapshot(self) -> SloSnapshot:
        """Return the current SLO snapshot."""


class RollingDeploySloMonitor:
    """D11 bridge backed by the in-process rolling 5xx SLI."""

    def snapshot(self) -> SloSnapshot:
        return SloSnapshot(
            error_rate=ha_observability.current_5xx_rate(),
            source="backend.ha_observability.current_5xx_rate",
        )


@dataclass(frozen=True)
class CanaryDecision:
    """Result of evaluating a rollout stage."""

    action: str
    stage: str
    reason: str
    snapshot: SloSnapshot | None = None


@dataclass(frozen=True)
class CanaryState:
    """Durable controller state stored beside the Caddy snippets."""

    rollout_id: str
    status: str
    stable_color: str
    canary_color: str
    stage_index: int
    stage_started_at: float
    updated_at: float
    reason: str = ""

    @property
    def stage(self) -> CanaryStage:
        return CANARY_STAGES[self.stage_index]


def _validate_color(color: str) -> str:
    if color not in {"blue", "green"}:
        raise ValueError(f"color must be blue or green, got {color!r}")
    return color


def stable_canary_assignment(
    tenant_id: str,
    canary_percent: int,
    *,
    salt: str = "omnisight-canary-v1",
) -> bool:
    """Return whether ``tenant_id`` belongs to the canary cohort.

    The SHA-256 bucket is deterministic across workers and process
    restarts; the same tenant stays pinned for a given percentage.
    """
    if not 0 <= canary_percent <= 100:
        raise ValueError("canary_percent must be between 0 and 100")
    if canary_percent == 0:
        return False
    if canary_percent == 100:
        return True
    key = f"{salt}:{tenant_id}".encode("utf-8")
    bucket = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100
    return bucket < canary_percent


class CaddyWeightWriter:
    """Render OP-771 weighted Caddy snippets atomically."""

    def __init__(
        self,
        snippet_path: Path | None = None,
        *,
        state_path: Path | None = None,
    ) -> None:
        self.snippet_path = snippet_path or DEFAULT_SNIPPET_PATH
        self.state_path = state_path or DEFAULT_STATE_PATH

    def write(self, state: CanaryState) -> None:
        self.snippet_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.snippet_path, self.render_snippet(state))
        _atomic_write_text(
            self.state_path,
            json.dumps(self._state_payload(state), indent=2, sort_keys=True) + "\n",
        )

    def _state_payload(self, state: CanaryState) -> dict[str, object]:
        payload = asdict(state)
        payload["stage"] = asdict(state.stage)
        return payload

    def render_snippet(self, state: CanaryState) -> str:
        stage = state.stage
        if state.status == "aborted":
            return _single_upstream_snippet(state.stable_color, reason=state.reason)
        if stage.canary_percent == 100:
            return _single_upstream_snippet(state.canary_color, reason="canary_100")

        blue_weight, green_weight = _weights_for(
            stable_color=state.stable_color,
            canary_color=state.canary_color,
            canary_percent=stage.canary_percent,
        )
        return (
            "# OP-771 canary weighted upstream snippet.\n"
            "# Import with: import canary_weighted_upstream_rp\n"
            "# Generated by backend.canary_rollout.CaddyWeightWriter.\n"
            "(canary_weighted_upstream_rp) {\n"
            "\treverse_proxy {$OMNISIGHT_UPSTREAM_A:backend-a:8000} "
            "{$OMNISIGHT_UPSTREAM_B:backend-b:8001} {\n"
            f"\t\tlb_policy weighted_round_robin {blue_weight} {green_weight}\n"
            "\t\thealth_uri /readyz\n"
            "\t}\n"
            "}\n"
        )


def _single_upstream_snippet(color: str, *, reason: str) -> str:
    upstream = (
        "{$OMNISIGHT_UPSTREAM_A:backend-a:8000}"
        if color == "blue"
        else "{$OMNISIGHT_UPSTREAM_B:backend-b:8001}"
    )
    return (
        "# OP-771 canary weighted upstream snippet.\n"
        f"# Single-upstream route: {color} ({reason}).\n"
        "(canary_weighted_upstream_rp) {\n"
        f"\treverse_proxy {upstream} {{\n"
        "\t\thealth_uri /readyz\n"
        "\t}\n"
        "}\n"
    )


def _weights_for(
    *,
    stable_color: str,
    canary_color: str,
    canary_percent: int,
) -> tuple[int, int]:
    _validate_color(stable_color)
    _validate_color(canary_color)
    if stable_color == canary_color:
        raise ValueError("stable_color and canary_color must differ")
    stable_weight = 100 - canary_percent
    canary_weight = canary_percent
    by_color = {
        stable_color: stable_weight,
        canary_color: canary_weight,
    }
    return by_color["blue"], by_color["green"]


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class CanaryController:
    """Stage controller for 5% -> 25% -> 100% canary rollout."""

    def __init__(
        self,
        *,
        writer: CaddyWeightWriter | None = None,
        monitor: SloMonitor | None = None,
        thresholds: SloThresholds | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.writer = writer or CaddyWeightWriter()
        self.monitor = monitor or RollingDeploySloMonitor()
        self.thresholds = thresholds or SloThresholds()
        self.clock = clock

    def start(
        self,
        *,
        rollout_id: str,
        stable_color: str,
        canary_color: str,
    ) -> CanaryState:
        stable = _validate_color(stable_color)
        canary = _validate_color(canary_color)
        if stable == canary:
            raise ValueError("stable_color and canary_color must differ")
        now = self.clock()
        state = CanaryState(
            rollout_id=rollout_id,
            status="running",
            stable_color=stable,
            canary_color=canary,
            stage_index=0,
            stage_started_at=now,
            updated_at=now,
        )
        self.writer.write(state)
        return state

    def evaluate(self, state: CanaryState) -> tuple[CanaryState, CanaryDecision]:
        if state.status != "running":
            decision = CanaryDecision(
                action=state.status,
                stage=state.stage.name,
                reason=f"rollout is {state.status}",
            )
            return state, decision

        snapshot = self.monitor.snapshot()
        breach_reason = self._breach_reason(snapshot)
        if breach_reason:
            aborted = self._replace(
                state,
                status="aborted",
                updated_at=self.clock(),
                reason=breach_reason,
            )
            self.writer.write(aborted)
            return aborted, CanaryDecision(
                action="abort",
                stage=state.stage.name,
                reason=breach_reason,
                snapshot=snapshot,
            )

        now = self.clock()
        if now - state.stage_started_at < state.stage.observe_seconds:
            return state, CanaryDecision(
                action="hold",
                stage=state.stage.name,
                reason="observe_window_open",
                snapshot=snapshot,
            )

        if state.stage_index == len(CANARY_STAGES) - 1:
            completed = self._replace(
                state,
                status="completed",
                updated_at=now,
                reason="canary_complete",
            )
            self.writer.write(completed)
            return completed, CanaryDecision(
                action="complete",
                stage=state.stage.name,
                reason="canary_complete",
                snapshot=snapshot,
            )

        advanced = self._replace(
            state,
            stage_index=state.stage_index + 1,
            stage_started_at=now,
            updated_at=now,
            reason="slo_passed",
        )
        self.writer.write(advanced)
        return advanced, CanaryDecision(
            action="advance",
            stage=advanced.stage.name,
            reason="slo_passed",
            snapshot=snapshot,
        )

    def manual_control(
        self,
        state: CanaryState,
        command: str,
        *,
        reason: str = "operator",
    ) -> CanaryState:
        now = self.clock()
        if command == "pause":
            new_state = self._replace(
                state, status="paused", updated_at=now, reason=reason
            )
        elif command == "resume":
            new_state = self._replace(
                state, status="running", stage_started_at=now,
                updated_at=now, reason=reason,
            )
        elif command == "advance":
            next_index = min(state.stage_index + 1, len(CANARY_STAGES) - 1)
            new_state = self._replace(
                state,
                status="running",
                stage_index=next_index,
                stage_started_at=now,
                updated_at=now,
                reason=reason,
            )
        elif command == "abort":
            new_state = self._replace(
                state, status="aborted", updated_at=now, reason=reason
            )
        else:
            raise ValueError("command must be pause, resume, advance, or abort")
        self.writer.write(new_state)
        return new_state

    def _breach_reason(self, snapshot: SloSnapshot) -> str:
        if snapshot.error_rate >= self.thresholds.max_error_rate:
            return f"slo_error_rate:{snapshot.error_rate:.4f}"
        if snapshot.p95_latency_ms > self.thresholds.max_p95_latency_ms:
            return f"slo_p95_latency:{snapshot.p95_latency_ms:.1f}"
        if snapshot.availability < self.thresholds.min_availability:
            return f"slo_availability:{snapshot.availability:.4f}"
        return ""

    def _replace(self, state: CanaryState, **changes: object) -> CanaryState:
        data = asdict(state)
        data.update(changes)
        return CanaryState(**data)


def load_state(path: Path | None = None) -> CanaryState:
    raw = json.loads((path or DEFAULT_STATE_PATH).read_text(encoding="utf-8"))
    raw.pop("stage", None)
    return CanaryState(**raw)
