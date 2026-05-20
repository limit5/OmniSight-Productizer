"""Coordinator chaos + decision-log validation tests.

Two layers, both about surviving a ``kill -9`` without leaving a mess:

* **Real-process watchdog recovery (OP-1548).** Spawns a real coordinator
  process, kills it with SIGKILL, then uses the watchdog's fake-systemctl
  seam to restart a clean replacement based only on heartbeat staleness.

* **N=50 random-kill chaos + decision-log schema validation (OP-1008 /
  AUDIT-29f-10).** Drives 50 random crash/recover cycles through the real
  cold-start orchestrator (L6 replay + Startup-3 sweep) against a stateful
  JIRA-label world, then asserts the three coordinator-correctness
  invariants — *zero orphan ``claim:*`` labels, zero double-relabels, every
  decision in the log* — and feeds the produced log through the
  ``scripts/validate-decision-log.py`` schema gate (the same gate CI runs).

A pure mid-tick ``kill -9`` leaves no drain marker at all, so the log just
ends on a tick; a kill *during* the L5 drain leaves a ``shutdown_began``
with no ``shutdown_complete`` (the crash signature L6 keys on). The chaos
loop fires both kinds at random so both recovery paths are exercised.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.agents.pipeline_coordinator import (
    CoordinatorConfig,
    InfraAuditResult,
    InterruptedTicket,
    PipelineCoordinator,
    StaleSweepPlan,
)
from backend.agents.pipeline_coordinator_watchdog import (
    PipelineCoordinatorWatchdog,
    RestartResult,
    WatchdogConfig,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VALIDATOR = _REPO_ROOT / "scripts" / "validate-decision-log.py"


_COORDINATOR_SNIPPET = """
from pathlib import Path
from backend.agents.pipeline_coordinator import CoordinatorConfig, PipelineCoordinator

base = Path(__import__("os").environ["OP1548_COORDINATOR_DIR"])
cfg = CoordinatorConfig(
    config_dir=base,
    heartbeat_path=base / "heartbeat",
    decision_log_dir=base / "decision-log",
    heartbeat_interval_seconds=0.2,
    tick_interval_seconds=0.2,
)
PipelineCoordinator(cfg).run_forever(install_signals=False)
"""


class RestartingSystemctl:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.calls: list[str] = []
        self.processes: list[subprocess.Popen] = []

    def restart(self, service_name: str) -> RestartResult:
        self.calls.append(service_name)
        self.processes.append(_spawn_coordinator(self.config_dir))
        return RestartResult(ok=True)


def _spawn_coordinator(config_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["OP1548_COORDINATOR_DIR"] = str(config_dir)
    return subprocess.Popen(
        [sys.executable, "-c", _COORDINATOR_SNIPPET],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _read_heartbeat(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for_pid(path: Path, *, exclude: int | None = None,
                  timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pid = int(_read_heartbeat(path)["pid"])
            if exclude is None or pid != exclude:
                return pid
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(f"heartbeat pid did not refresh: {last_error!r}")


def _heartbeat_age(path: Path) -> float:
    payload = _read_heartbeat(path)
    ts = datetime.fromisoformat(payload["ts"]).astimezone(timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def test_watchdog_recovers_after_sigkill_without_orphan_state(tmp_path: Path) -> None:
    """Exercised AC: kill -9 → watchdog restart → fresh pid, no orphan."""
    config_dir = tmp_path / "coordinator"
    heartbeat = config_dir / "heartbeat"
    first = _spawn_coordinator(config_dir)
    fake_systemctl = RestartingSystemctl(config_dir)

    try:
        old_pid = _wait_for_pid(heartbeat)
        assert old_pid == first.pid

        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=2)
        assert first.returncode == -signal.SIGKILL

        time.sleep(0.35)
        watchdog = PipelineCoordinatorWatchdog(
            WatchdogConfig(
                heartbeat_path=heartbeat,
                decision_log_dir=config_dir / "decision-log",
                stale_after_seconds=0.2,
                redie_window_seconds=300.0,
            ),
            systemctl=fake_systemctl,
        )
        outcome = watchdog.run_once()

        assert outcome.action == "restart"
        assert fake_systemctl.calls == ["pipeline-coordinator.service"]
        new_pid = _wait_for_pid(heartbeat, exclude=old_pid)
        assert new_pid != old_pid
        assert fake_systemctl.processes[0].pid == new_pid
        assert fake_systemctl.processes[0].poll() is None
        assert _heartbeat_age(heartbeat) < 1.0
    finally:
        _terminate(first)
        for proc in fake_systemctl.processes:
            _terminate(proc)


# ══════════════════════════════════════════════════════════════════════
# OP-1008 / AUDIT-29f-10 — N=50 random-kill chaos + decision-log validator
# ══════════════════════════════════════════════════════════════════════


# ── decision-log schema validator, loaded from its hyphenated path ────

def _load_validator() -> Any:
    """Import ``scripts/validate-decision-log.py`` as a module.

    The filename is hyphenated (it is a CLI gate, not an importable
    package), so the chaos loop shells out to it like CI does; these
    record-level unit checks load it via importlib for direct assertions.
    """
    spec = importlib.util.spec_from_file_location("validate_decision_log", _VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_validator(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_VALIDATOR), *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )


# ── A stateful JIRA-label world for the chaos loop ────────────────────


class FakeClock:
    """Pinned, advanceable UTC clock (chaos loop advances it 1h/cycle so the
    day-partitioned log spans multiple files and L6's 24h window ages out)."""

    def __init__(self) -> None:
        self.t = datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class ChaosGateway:
    """In-memory :class:`ColdStartGateway` modelling a JIRA ``claim:*`` world.

    A dead runner leaves an orphan ``claim:*`` label on a ticket; the
    coordinator's Startup-3 sweep is supposed to drop exactly those labels
    (the plan is derived *from* the live label set, so it only ever asks to
    remove a label that is currently present). Two things are recorded for
    the chaos assertions:

      * ``world`` — the live label set per ticket (a real orphan that the
        sweep failed to clear shows up here at the end).
      * ``double_relabels`` — any ``remove_label`` call for a label that is
        *not* currently present (i.e. removed twice without re-injection):
        the decision-log-independent half of the "zero double-relabels"
        guarantee.
    """

    def __init__(self) -> None:
        self.world: dict[str, set[str]] = {}
        self.removed: list[tuple[str, str]] = []
        self.double_relabels: list[tuple[str, str]] = []

    # ── chaos-loop control ──
    def inject_orphan_claim(self, key: str, label: str) -> None:
        self.world.setdefault(key, set()).add(label)

    def live_claim_labels(self) -> list[tuple[str, str]]:
        return [(k, lbl) for k, labels in self.world.items()
                for lbl in labels if lbl.startswith("claim:")]

    # ── Startup-1 (infra never churns in the chaos sim) ──
    def audit_infra(self) -> InfraAuditResult:
        return InfraAuditResult()

    def start_unit(self, unit: str) -> bool:
        return True

    # ── Startup-2 (no interrupted tickets — chaos targets sweep + L6) ──
    def interrupted_tickets(self) -> list[InterruptedTicket]:
        return []

    def has_live_runner(self, key: str) -> bool:
        return False

    def gerrit_change_mergeable(self, key: str) -> bool:
        return False

    def branch_has_commits(self, key: str) -> bool:
        return False

    def mark_resumable(self, key: str) -> None:
        return None

    def transition_under_review(self, key: str) -> None:
        return None

    def reset_to_todo(self, key: str) -> None:
        return None

    # ── Startup-3 (the sweep under test) ──
    def stale_sweep_plan(self) -> StaleSweepPlan:
        claims: dict[str, tuple[str, ...]] = {}
        for key, labels in self.world.items():
            orphans = tuple(sorted(lbl for lbl in labels if lbl.startswith("claim:")))
            if orphans:
                claims[key] = orphans
        return StaleSweepPlan(stale_claims=claims)

    def remove_label(self, key: str, label: str) -> None:
        self.removed.append((key, label))
        present = self.world.get(key, set())
        if label not in present:
            # Removing a label that is not there == a double relabel.
            self.double_relabels.append((key, label))
            return
        present.discard(label)

    def clear_assignee(self, key: str) -> None:
        return None

    # ── shared ──
    def mention_operator(self, key: str, message: str, *, urgency: str = "high") -> None:
        return None


def _chaos_config(tmp_path: Path) -> CoordinatorConfig:
    base = tmp_path / "coordinator"
    return CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
        heartbeat_interval_seconds=60.0,
        tick_interval_seconds=60.0,
        sweep_interval_seconds=3600.0,
    )


def _read_all_records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for f in sorted(directory.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def test_chaos_n50_random_kills_recover_clean_no_orphan_state(tmp_path: Path) -> None:
    """N=50 random kill→restart cycles leave zero orphan claims, zero
    double-relabels, and every decision in a schema-valid log.

    Each cycle: (1) a dead runner may leave a random orphan ``claim:*``;
    (2) a fresh coordinator boots → cold-start runs L6 replay + the Startup-3
    sweep that must clear the orphan; (3) it ticks a random number of times;
    (4) it dies one of two random ways — a mid-tick ``kill -9`` (log ends on a
    tick, no drain marker) or a kill *during drain* (a lone ``shutdown_began``
    that next boot's L6 must recover). The config dir is shared across cycles
    so the decision log is the same append-only trail a real host keeps.
    """
    rng = random.Random(20260520)  # fixed seed → deterministic, no CI flakes
    clock = FakeClock()
    gateway = ChaosGateway()
    config = _chaos_config(tmp_path)
    decision_log_dir = config.decision_log_dir

    total_ticks = 0
    clean_boots = 0
    N = 50
    for cycle in range(N):
        # (1) a dead runner sometimes leaves an orphan claim behind.
        if rng.random() < 0.6:
            ticket = f"OP-{rng.randint(1000, 1010)}"
            gateway.inject_orphan_claim(ticket, f"claim:default:{ticket}")

        # (2) fresh process boots and runs the real 4-phase cold start.
        coord = PipelineCoordinator(config, clock=clock, cold_start_gateway=gateway)
        report = coord.startup()
        assert report.phase1_halted is False, f"cycle {cycle}: boot halted in Startup-1"
        assert report.entered_loop is True, f"cycle {cycle}: boot never reached the loop"
        clean_boots += 1

        # After the sweep, no orphan claim should remain for this boot.
        assert gateway.live_claim_labels() == [], (
            f"cycle {cycle}: Startup-3 left orphan claim labels "
            f"{gateway.live_claim_labels()}"
        )

        # (3) tick a random number of times (each appends one decision_tick).
        ticks = rng.randint(1, 4)
        for _ in range(ticks):
            coord.run_once()
            clock.advance(60.0)
            total_ticks += 1

        # (4) random crash style.
        if rng.random() < 0.5:
            # kill during drain: a started-but-uncompleted shutdown marker.
            coord._decision_log.append(
                {
                    "ts": clock().isoformat(),
                    "event": "shutdown_began",
                    "engine_version": coord._engine.engine_version,
                    "decision_id": f"chaos-drain-{cycle}",
                    "pid": os.getpid(),
                }
            )
        # else: pure mid-tick kill -9 — nothing more is written.

        clock.advance(3600.0)  # an hour passes before the watchdog restarts it

    # ── Invariant 1: every boot recovered cleanly. ──
    assert clean_boots == N

    # ── Invariant 2: zero orphan claim:* labels survived the chaos. ──
    assert gateway.live_claim_labels() == []

    # ── Invariant 3: zero double-relabels (no label removed when absent). ──
    assert gateway.double_relabels == [], (
        f"double-relabels detected: {gateway.double_relabels}"
    )

    # ── Invariant 4: every decision is in the log (count matches our ticks)
    #     and the cold-start lifecycle bracketed every boot. ──
    records = _read_all_records(decision_log_dir)
    decision_ticks = [r for r in records if r["event"] == "decision_tick"]
    assert len(decision_ticks) == total_ticks
    assert sum(1 for r in records if r["event"] == "cold_start_began") == N
    assert sum(1 for r in records if r["event"] == "cold_start_complete") == N
    # Every interrupted drain we injected is recovered by a later boot's L6.
    assert any(r["event"] == "crash_recovery_applied" for r in records)

    # ── Invariant 5: the produced log passes the CI schema gate (strict). ──
    result = _run_validator("--strict", "--report-drains", "--dir", str(decision_log_dir))
    assert result.returncode == 0, (
        f"validator rejected the chaos log:\n{result.stdout}\n{result.stderr}"
    )


def test_validator_accepts_a_clean_coordinator_log(tmp_path: Path) -> None:
    """Exercised AC: the validator passes a normal coordinator run (no false
    positive) — a clean cold-start + a few ticks + a clean drain."""
    clock = FakeClock()
    config = _chaos_config(tmp_path)
    coord = PipelineCoordinator(config, clock=clock, cold_start_gateway=ChaosGateway())
    coord.startup()
    coord.run_forever(install_signals=False, max_ticks=3)  # ticks + clean drain

    result = _run_validator("--strict", "--dir", str(config.decision_log_dir))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "decision-log OK" in result.stdout


def test_validator_rejects_malformed_entries(tmp_path: Path) -> None:
    """Exercised AC: the validator catches real malformed entries — bad JSON,
    unknown event, a tick missing decision_id, a non-§6.1 action kind, and a
    bad timestamp — and exits CI-red (1)."""
    log = tmp_path / "2026-05-20.jsonl"
    good = json.dumps({
        "ts": "2026-05-20T00:00:00+00:00", "event": "decision_tick",
        "engine_version": "v0", "mode": "idle", "decision_id": "abc",
        "actions": [], "reason": "noop", "dry_run": True, "pid": 1,
    })
    bad_lines = [
        good,                                   # one valid line
        "{not json",                            # torn / invalid JSON
        json.dumps({"ts": "2026-05-20T00:00:00+00:00", "event": "who_dis"}),
        json.dumps({                            # decision_tick missing decision_id
            "ts": "2026-05-20T00:00:00+00:00", "event": "decision_tick",
            "engine_version": "v0", "mode": "idle", "actions": [],
            "reason": "x", "dry_run": True,
        }),
        json.dumps({                            # action kind outside §6.1 set
            "ts": "2026-05-20T00:00:00+00:00", "event": "decision_tick",
            "engine_version": "v0", "mode": "idle", "decision_id": "d2",
            "actions": [{"kind": "delete_repo", "target": "OP-1"}],
            "reason": "x", "dry_run": True,
        }),
        json.dumps({"ts": "not-a-timestamp", "event": "cold_start_began",
                    "engine_version": "v0"}),
    ]
    log.write_text("\n".join(bad_lines) + "\n", encoding="utf-8")

    result = _run_validator(str(log))
    assert result.returncode == 1
    # Each distinct defect is surfaced with a file/line annotation.
    assert "invalid JSON" in result.stderr
    assert "not a known decision-log event" in result.stderr
    assert "missing required field 'decision_id'" in result.stderr
    assert "not in §6.1 closed set" in result.stderr
    assert "not an ISO-8601 timestamp" in result.stderr


def test_validator_record_level_checks() -> None:
    """Record-level unit coverage of the validator's schema rules, exercised
    directly against the imported module (decoupled from the CLI)."""
    v = _load_validator()
    base = {
        "ts": "2026-05-20T00:00:00+00:00", "event": "decision_tick",
        "engine_version": "v0", "mode": "idle", "decision_id": "id1",
        "actions": [], "reason": "noop", "dry_run": True, "pid": 7,
    }
    assert v.validate_record(base) == []
    # pid present but not an int.
    assert any("pid" in e for e in v.validate_record({**base, "pid": "7"}))
    # actions must be an array.
    assert any("actions" in e for e in v.validate_record({**base, "actions": {}}))
    # nested action kind must be in the closed §6.1 set.
    errs = v.validate_record({**base, "actions": [{"kind": "nope", "target": "OP-1"}]})
    assert any("closed set" in e for e in errs)
    # a valid relabel action passes.
    assert v.validate_record({
        **base,
        "actions": [{"kind": "relabel", "target": "OP-1",
                     "params": {"label": "x"}, "dry_run": True}],
    }) == []
    # known non-tick events validate on the common envelope.
    assert v.validate_record({
        "ts": "2026-05-20T00:00:00+00:00", "event": "shutdown_complete",
        "engine_version": "v0", "decision_id": "d", "pid": 1,
    }) == []


def test_validator_strict_catches_duplicate_decision_id(tmp_path: Path) -> None:
    """--strict cross-line invariant: a decision_id logged twice (a tick
    written twice — the log half of 'zero double-relabels') is rejected."""
    log = tmp_path / "2026-05-20.jsonl"
    tick = {
        "ts": "2026-05-20T00:00:00+00:00", "event": "decision_tick",
        "engine_version": "v0", "mode": "idle", "decision_id": "dup",
        "actions": [], "reason": "noop", "dry_run": True, "pid": 1,
    }
    log.write_text(json.dumps(tick) + "\n" + json.dumps(tick) + "\n", encoding="utf-8")

    assert _run_validator(str(log)).returncode == 0          # lenient: schema-valid
    strict = _run_validator("--strict", str(log))
    assert strict.returncode == 1
    assert "duplicate decision_id" in strict.stderr


def test_validator_treats_unmatched_drain_as_crash_not_error(tmp_path: Path) -> None:
    """A lone shutdown_began (the crash signature) is NOT a schema error — it
    is reported as a notice under --report-drains and the gate stays green."""
    log = tmp_path / "2026-05-20.jsonl"
    log.write_text(
        json.dumps({"ts": "2026-05-20T00:00:00+00:00", "event": "shutdown_began",
                    "engine_version": "v0", "decision_id": "crash-1", "pid": 1}) + "\n",
        encoding="utf-8",
    )
    result = _run_validator("--strict", "--report-drains", str(log))
    assert result.returncode == 0
    assert "unmatched shutdown_began crash-1" in result.stdout
