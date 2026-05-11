#!/usr/bin/env python3
"""OP-911 F13 -- Sprint F canary orchestrator for OMNISIGHT_PROJECT_STATE_INJECT.

Drives a 5% -> 25% -> 100% staged rollout of the F7 (OP-905) feature
flag ``OMNISIGHT_PROJECT_STATE_INJECT`` via the D12 (OP-884) feature
flag service. Each stage holds for 24h before the operator may advance;
the manual gate at every transition checks three signals and rolls
back (flag flip to ``state='disabled'``) on any breach:

  1. SLO snapshot (F14 dep, ``MetricsSource.slo_breach``).
  2. Runner success rate must not fall below baseline -5pp.
  3. No new ``[runner-no-commits-from-cli]`` revert-loop pattern since
     the stage started (OP-827 post-mortem signal).

D12 is the persistent state: the orchestrator re-derives the current
stage on every invocation from ``FlagClient.get(flag_name).rollout_pct``,
so a Python process restart between stages does not lose state. The
24h observe window is enforced against the flag row's ``updated_at``.

SSE events fire on every transition for the operator dashboard:

  * ``sprint_f.canary.stage.started`` -- after start() applies 5%.
  * ``sprint_f.canary.stage.transitioned`` -- after advance() promotes.
  * ``sprint_f.canary.gate.failed`` -- before auto-rollback fires.
  * ``sprint_f.canary.rolled_back`` -- after the rollback writer succeeds.
  * ``sprint_f.canary.completed`` -- after the terminal advance() at 100%.

Error catalog (per ticket):

  * :class:`CanaryGateFail` -- gate refused promotion; rollback issued.
  * :class:`RolloutPercentMisapplied` -- D12 returned a different pct
    than requested; rollback issued and operator files a follow-up.
  * :class:`RollbackFailed` -- D12 disable call itself failed; operator
    paged for manual flag flip.

Reusable model: this script intentionally mirrors the D10 (OP-882)
``CanaryOrchestrator`` shape so operators familiar with the
``deploy/canary-runbook.md`` flow can transfer their muscle memory.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


logger = logging.getLogger("sprint_f_canary")


FLAG_NAME = "OMNISIGHT_PROJECT_STATE_INJECT"
OBSERVE_SECONDS_PER_STAGE = 24 * 60 * 60  # 24h
RUNNER_SUCCESS_RATE_TOLERANCE = 0.05  # baseline - 5pp


# --- Error catalog --------------------------------------------------


class CanaryError(RuntimeError):
    """Base so the CLI / API handler can map errors uniformly."""


class CanaryGateFail(CanaryError):
    """Per-stage gate refused promotion; rollback was issued."""


class RolloutPercentMisapplied(CanaryError):
    """D12 returned a different rollout_pct than requested."""


class RollbackFailed(CanaryError):
    """The rollback flag-flip itself failed; operator must intervene."""


# --- Stages ---------------------------------------------------------


@dataclass(frozen=True)
class CanaryStage:
    name: str
    rollout_pct: int


STAGES: tuple[CanaryStage, ...] = (
    CanaryStage(name="5", rollout_pct=5),
    CanaryStage(name="25", rollout_pct=25),
    CanaryStage(name="100", rollout_pct=100),
)


# --- Flag client + metrics protocols --------------------------------


@dataclass(frozen=True)
class FlagState:
    rollout_pct: int
    state: str  # "enabled" | "disabled"
    updated_at: datetime | None


class FlagClient(Protocol):
    def get(self, name: str) -> FlagState: ...
    def set_rollout_pct(self, name: str, pct: int) -> FlagState: ...
    def disable(self, name: str) -> FlagState: ...


@dataclass(frozen=True)
class StageMetrics:
    """Snapshot the gate evaluates against."""

    slo_breach_reason: str | None
    runner_success_rate: float
    revert_loop_count: int


class MetricsSource(Protocol):
    def snapshot(self, since: datetime) -> StageMetrics: ...


# --- HTTP-backed flag client ----------------------------------------


@dataclass
class HttpFlagClient:
    """Default :class:`FlagClient` against the WP.7.8 PATCH endpoint.

    The orchestrator's prod wiring talks to the same
    ``/feature-flags/{name}`` route the operator UI uses, so canary
    writes share the audit-log trail and Redis fan-out the manual UI
    edits do.
    """

    base_url: str
    auth_header: str
    timeout_seconds: float = 5.0

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None) -> Any:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.auth_header)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise CanaryError(
                f"D12 {method} {path} -> HTTP {exc.code}: "
                f"{exc.read().decode('utf-8', errors='replace')[:240]}"
            ) from exc

    def _to_state(self, payload: Mapping[str, Any]) -> FlagState:
        updated_raw = payload.get("updated_at")
        updated_at: datetime | None = None
        if isinstance(updated_raw, str) and updated_raw:
            try:
                updated_at = datetime.fromisoformat(updated_raw)
            except ValueError:
                updated_at = None
        return FlagState(
            rollout_pct=int(payload.get("rollout_pct") or 0),
            state=str(payload.get("state") or "disabled"),
            updated_at=updated_at,
        )

    def get(self, name: str) -> FlagState:
        rows = self._request("GET", "/feature-flags", None)
        for raw in (rows or {}).get("feature_flags") or []:
            if str(raw.get("flag_name")) == name:
                return self._to_state(raw)
        raise CanaryError(f"D12 flag {name!r} not registered")

    def set_rollout_pct(self, name: str, pct: int) -> FlagState:
        body = {"state": "enabled", "rollout_pct": int(pct)}
        return self._to_state(self._request("PATCH", f"/feature-flags/{name}", body))

    def disable(self, name: str) -> FlagState:
        body = {"state": "disabled", "rollout_pct": 0}
        return self._to_state(self._request("PATCH", f"/feature-flags/{name}", body))


def _default_publish(event: str, data: Mapping[str, Any]) -> None:
    """Lazy import of the event bus so unit tests stay decoupled."""
    from backend import events

    events.bus.publish(event, dict(data))


# --- Orchestrator ---------------------------------------------------


@dataclass
class SprintFCanaryOrchestrator:
    rollout_id: str
    flag_client: FlagClient
    metrics: MetricsSource
    baseline_runner_success_rate: float
    flag_name: str = FLAG_NAME
    observe_seconds: int = OBSERVE_SECONDS_PER_STAGE
    clock: Callable[[], datetime] = field(
        default_factory=lambda: (lambda: datetime.now(timezone.utc))
    )
    publish: Callable[[str, Mapping[str, Any]], None] = field(default=_default_publish)

    def status(self) -> tuple[CanaryStage | None, FlagState]:
        """Re-derive the current stage from D12. ``None`` = not started."""
        state = self.flag_client.get(self.flag_name)
        if state.state != "enabled" or state.rollout_pct <= 0:
            return None, state
        for stage in STAGES:
            if stage.rollout_pct == state.rollout_pct:
                return stage, state
        # An off-grid pct (e.g. operator dialed 17%) is the same shape
        # as RolloutPercentMisapplied; the runbook treats it as such.
        return None, state

    def start(self) -> CanaryStage:
        stage, state = self.status()
        if stage is not None:
            raise CanaryError(
                f"rollout already at stage={stage.name} "
                f"(pct={state.rollout_pct}); call advance() or rollback()"
            )
        first = STAGES[0]
        applied = self.flag_client.set_rollout_pct(self.flag_name, first.rollout_pct)
        self._verify_pct(applied, first.rollout_pct)
        self._emit("sprint_f.canary.stage.started", first, snapshot=None)
        return first

    def advance(self) -> CanaryStage:
        stage, state = self.status()
        if stage is None:
            raise CanaryError("rollout not started; call start() first")

        # Observe window: enforced against the D12 row's updated_at.
        # When updated_at is missing (legacy row), the gate falls back
        # to the operator's judgement -- the runbook documents this.
        if state.updated_at is not None:
            elapsed = (self.clock() - state.updated_at).total_seconds()
            if elapsed < self.observe_seconds:
                raise CanaryGateFail(
                    f"observe window open at stage={stage.name}: "
                    f"elapsed={int(elapsed)}s < required={self.observe_seconds}s"
                )

        snapshot = self.metrics.snapshot(state.updated_at or self.clock())
        breach = self._evaluate_gate(snapshot)
        if breach:
            self._emit(
                "sprint_f.canary.gate.failed", stage,
                snapshot=snapshot, reason=breach,
            )
            self._rollback_after_breach(reason=f"gate_failed:{breach}")
            raise CanaryGateFail(f"stage={stage.name}: {breach}")

        if stage.rollout_pct >= STAGES[-1].rollout_pct:
            self._emit("sprint_f.canary.completed", stage, snapshot=snapshot)
            return stage

        idx = STAGES.index(stage)
        nxt = STAGES[idx + 1]
        applied = self.flag_client.set_rollout_pct(self.flag_name, nxt.rollout_pct)
        self._verify_pct(applied, nxt.rollout_pct)
        self._emit(
            "sprint_f.canary.stage.transitioned", nxt,
            snapshot=snapshot, from_stage=stage.name,
        )
        return nxt

    def rollback(self, *, reason: str) -> FlagState:
        try:
            disabled = self.flag_client.disable(self.flag_name)
        except CanaryError as exc:
            raise RollbackFailed(
                f"D12 disable({self.flag_name}) failed: {exc}"
            ) from exc
        self._emit(
            "sprint_f.canary.rolled_back",
            CanaryStage(name="rollback", rollout_pct=0),
            snapshot=None, reason=reason,
        )
        return disabled

    # -- internals ----------------------------------------------------

    def _verify_pct(self, applied: FlagState, expected: int) -> None:
        if applied.rollout_pct != expected:
            self._rollback_after_breach(
                reason=f"rollout_percent_misapplied:expected={expected},"
                f"applied={applied.rollout_pct}"
            )
            raise RolloutPercentMisapplied(
                f"D12 reported rollout_pct={applied.rollout_pct} "
                f"after PATCH(rollout_pct={expected}) -- rolled back"
            )

    def _evaluate_gate(self, snapshot: StageMetrics) -> str:
        if snapshot.slo_breach_reason:
            return f"slo_breach:{snapshot.slo_breach_reason}"
        floor = self.baseline_runner_success_rate - RUNNER_SUCCESS_RATE_TOLERANCE
        if snapshot.runner_success_rate < floor:
            return (
                f"runner_success_rate={snapshot.runner_success_rate:.4f} "
                f"< floor={floor:.4f} (baseline={self.baseline_runner_success_rate:.4f})"
            )
        if snapshot.revert_loop_count > 0:
            return (
                f"runner_no_commits_from_cli_revert_loop "
                f"count={snapshot.revert_loop_count}"
            )
        return ""

    def _rollback_after_breach(self, *, reason: str) -> None:
        try:
            self.rollback(reason=reason)
        except RollbackFailed as exc:
            raise RollbackFailed(
                f"gate failed AND rollback failed: gate={reason}; rollback={exc}"
            ) from exc

    def _emit(
        self,
        event: str,
        stage: CanaryStage,
        *,
        snapshot: StageMetrics | None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "rollout_id": self.rollout_id,
            "flag_name": self.flag_name,
            "stage": stage.name,
            "rollout_pct": stage.rollout_pct,
        }
        if snapshot is not None:
            payload["slo_breach_reason"] = snapshot.slo_breach_reason or ""
            payload["runner_success_rate"] = snapshot.runner_success_rate
            payload["revert_loop_count"] = snapshot.revert_loop_count
        payload.update(extra)
        try:
            self.publish(event, payload)
        except Exception:
            logger.exception("sprint_f canary SSE publish failed (%s)", event)


# --- CLI ------------------------------------------------------------


def _build_default_http_client() -> HttpFlagClient:
    base = os.environ.get("OMNISIGHT_API_BASE_URL", "http://localhost:8000")
    token = os.environ.get("OMNISIGHT_API_TOKEN", "")
    if not token:
        raise SystemExit(
            "OMNISIGHT_API_TOKEN env var is required for D12 PATCH calls"
        )
    auth = "Basic " + b64encode(f"operator:{token}".encode()).decode()
    return HttpFlagClient(base_url=base + "/api/v1", auth_header=auth)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("start", "advance", "rollback", "status"))
    parser.add_argument("--rollout-id", default="op-911-sprint-f-canary")
    parser.add_argument(
        "--baseline-runner-success-rate", type=float, default=0.95,
        help="captured pre-canary baseline; gate uses baseline - 5pp",
    )
    parser.add_argument("--reason", default="operator_initiated")
    args = parser.parse_args(argv)

    # The CLI's metrics source is a thin shim around the F14 SLO client
    # (when available) -- left as a sentinel that operators are expected
    # to inject from the runbook, since F14 is the formal dep.
    raise SystemExit(
        "CLI wiring requires a MetricsSource injection per "
        "docs/operations/sprint-f-canary-runbook.md -- import "
        "SprintFCanaryOrchestrator directly from a thin operator script "
        f"(action={args.action!r}, rollout_id={args.rollout_id!r})"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
