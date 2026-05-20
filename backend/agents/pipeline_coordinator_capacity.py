"""Pipeline-coordinator runner-capacity seam (29f-coord skeleton).

ADR-0021 §2.2 mandates *sprint-level capacity-aware re-planning* because
subscription-tier vs API-tier runner capability + budget vary enormously.
The real capacity tracker (runner class strengths, TPM headroom, budget
burn) lands in a later phase (29f-3 rules / capacity refresh). This module
defines the importable seam that the daemon and the future rule engine
share *now* so that wiring the real implementation later does not change
the daemon contract.

Module-global state audit (per project SOP)
-------------------------------------------
Frozen dataclasses + a pure ``empty`` constructor only. No I/O, no module
import-time side effects, no mutable module globals. The daemon injects a
``CapacitySnapshot`` per tick; the skeleton hands the engine an empty one.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from backend.agents import provider_quota_tracker


RUNNER_QUOTA_STATE_DIR_ENV = "OMNISIGHT_RUNNER_QUOTA_STATE_DIR"
RUNNER_CAPACITY_PATH_ENV = "OMNISIGHT_RUNNER_CAPACITY_PATH"
DEFAULT_RUNNER_QUOTA_STATE_DIR = Path("~/.cache/omnisight/runner-quota-state")
DEFAULT_WEEKLY_RESET_SECONDS = 7 * 24 * 60 * 60

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_RUNNER_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class RunnerCapacity:
    """Per-runner-class capacity figures.

    ``free_slots`` is the headroom the coordinator may schedule into;
    ``budget_remaining`` is a coarse cost signal (units intentionally
    left to the later capacity phase — the skeleton never reads it).
    """

    runner_class: str
    free_slots: int = 0
    budget_remaining: float = 0.0
    tokens_in_current_week: int = 0
    weekly_cap: int = 0
    reset_at: datetime | None = None
    tickets_completed: int = 0
    runner_id: str = ""
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.runner_class, str) or not self.runner_class.strip():
            raise ValueError("runner_class must be a non-empty string")
        if self.free_slots < 0:
            raise ValueError("free_slots must be non-negative")
        if self.tokens_in_current_week < 0:
            raise ValueError("tokens_in_current_week must be non-negative")
        if self.weekly_cap < 0:
            raise ValueError("weekly_cap must be non-negative")
        if self.tickets_completed < 0:
            raise ValueError("tickets_completed must be non-negative")


@dataclass(frozen=True)
class CapacitySnapshot:
    """Immutable snapshot of runner capacity at one coordinator tick.

    Keyed by runner class (``claude``, ``codex``, ``api``, ``local-llm``
    per ADR-0021 §1.3). The skeleton produces an empty snapshot every
    tick; 29f-3 fills it from live runner / quota state.
    """

    captured_at: datetime
    runners: Mapping[str, RunnerCapacity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        object.__setattr__(self, "runners", dict(self.runners))

    @classmethod
    def empty(cls, *, captured_at: datetime | None = None) -> "CapacitySnapshot":
        """An empty snapshot — the skeleton's only producer.

        ``captured_at`` defaults to ``now(UTC)``; tests pass an injected
        clock value for determinism.
        """
        return cls(captured_at=captured_at or datetime.now(timezone.utc), runners={})

    @property
    def total_free_slots(self) -> int:
        return sum(rc.free_slots for rc in self.runners.values())

    def to_record(self) -> dict[str, Any]:
        """Serialisable capacity view written for later coordinator phases."""
        runners: dict[str, dict[str, Any]] = {}
        for key, cap in sorted(self.runners.items()):
            runners[key] = {
                "runner_class": cap.runner_class,
                "runner_id": cap.runner_id,
                "free_slots": cap.free_slots,
                "budget_remaining": cap.budget_remaining,
                "tokens_in_current_week": cap.tokens_in_current_week,
                "weekly_cap": cap.weekly_cap,
                "reset_at": cap.reset_at.isoformat() if cap.reset_at else None,
                "tickets_completed": cap.tickets_completed,
                "updated_at": cap.updated_at.isoformat() if cap.updated_at else None,
            }
        return {
            "captured_at": self.captured_at.isoformat(),
            "total_free_slots": self.total_free_slots,
            "runners": runners,
        }


@dataclass(frozen=True)
class RunnerQuotaEmit:
    """One runner's per-tick quota payload before it is appended to JSONL."""

    agent_class: str
    instance_id: str
    tokens_in_current_week: int
    weekly_cap: int
    reset_at: datetime
    tickets_completed: int
    provider: str = ""
    emitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def runner_id(self) -> str:
        return runner_id(self.agent_class, self.instance_id)

    def to_record(self) -> dict[str, Any]:
        return {
            "ts": self.emitted_at.isoformat(),
            "runner_id": self.runner_id,
            "agent_class": self.agent_class,
            "instance_id": self.instance_id,
            "provider": self.provider,
            "tokens_in_current_week": self.tokens_in_current_week,
            "weekly_cap": self.weekly_cap,
            "reset_at": self.reset_at.isoformat(),
            "tickets_completed": self.tickets_completed,
        }


def runner_id(agent_class: str, instance_id: str) -> str:
    return f"{agent_class}:{instance_id or 'default'}"


def quota_state_dir_from_env(env: Mapping[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    return Path(
        env.get(RUNNER_QUOTA_STATE_DIR_ENV, str(DEFAULT_RUNNER_QUOTA_STATE_DIR))
    ).expanduser()


def capacity_path_from_env(
    config_dir: Path,
    env: Mapping[str, str] | None = None,
) -> Path:
    env = env if env is not None else os.environ
    return Path(
        env.get(RUNNER_CAPACITY_PATH_ENV, str(Path(config_dir) / "runner_capacity.json"))
    ).expanduser()


def emit_path_for(agent_class: str, instance_id: str, directory: Path) -> Path:
    name = _RUNNER_FILE_RE.sub("_", runner_id(agent_class, instance_id)).strip("_")
    return Path(directory) / f"{name or 'runner'}.jsonl"


def quota_emit_from_provider(
    *,
    agent_class: str,
    instance_id: str,
    provider: str,
    tickets_completed: int,
    now: datetime | None = None,
) -> RunnerQuotaEmit:
    """Build a quota emit payload from provider_quota_tracker, fail-open.

    Runner ticks must keep progressing when the quota DB is unavailable, so
    the fallback still writes a complete schema with zero observed usage and
    the configured weekly cap.
    """
    now = _normalise_dt(now or datetime.now(timezone.utc))
    weekly_cap = _weekly_cap_for(provider)
    reset_at = now + timedelta(seconds=DEFAULT_WEEKLY_RESET_SECONDS)
    weekly_tokens = 0
    try:
        state = provider_quota_tracker.get_quota_state(provider)
        weekly_tokens = max(0, int(state.weekly_tokens))
        if state.last_reset_at is not None:
            reset_at = _normalise_dt(state.last_reset_at) + timedelta(
                seconds=DEFAULT_WEEKLY_RESET_SECONDS
            )
    except Exception:
        pass
    return RunnerQuotaEmit(
        agent_class=agent_class,
        instance_id=instance_id,
        provider=provider,
        tokens_in_current_week=weekly_tokens,
        weekly_cap=weekly_cap,
        reset_at=reset_at,
        tickets_completed=max(0, tickets_completed),
        emitted_at=now,
    )


def append_runner_quota_emit(
    emit: RunnerQuotaEmit,
    *,
    directory: Path | None = None,
) -> Path:
    """Append one JSONL quota state line for the runner and return its path."""
    directory = directory or quota_state_dir_from_env()
    directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    path = emit_path_for(emit.agent_class, emit.instance_id, directory)
    existed = path.exists()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(emit.to_record(), sort_keys=True) + "\n")
    if not existed:
        os.chmod(path, _FILE_MODE)
    return path


def load_capacity_snapshot_from_jsonl(
    *,
    directory: Path | None = None,
    captured_at: datetime | None = None,
) -> CapacitySnapshot:
    """Tail each runner JSONL file and return the latest capacity snapshot."""
    directory = directory or quota_state_dir_from_env()
    captured_at = _normalise_dt(captured_at or datetime.now(timezone.utc))
    runners: dict[str, RunnerCapacity] = {}
    if not directory.exists():
        return CapacitySnapshot(captured_at=captured_at, runners=runners)
    for path in sorted(directory.glob("*.jsonl")):
        record = _last_json_record(path)
        if record is None:
            continue
        try:
            cap = _runner_capacity_from_record(record)
        except (TypeError, ValueError):
            continue
        runners[record.get("runner_id") or cap.runner_id or path.stem] = cap
    return CapacitySnapshot(captured_at=captured_at, runners=runners)


def write_capacity_snapshot(snapshot: CapacitySnapshot, path: Path) -> Path:
    """Atomically write ``runner_capacity.json`` for coordinator consumers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(snapshot.to_record(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, _FILE_MODE)
    os.replace(tmp, path)
    return path


def load_capacity_snapshot_from_json(
    path: Path,
    *,
    captured_at: datetime | None = None,
) -> CapacitySnapshot:
    """Read the coordinator's ``runner_capacity.json`` snapshot.

    29f-4 writes this file via :func:`write_capacity_snapshot`; sprint-level
    re-planning reads it back rather than re-tailing runner JSONL so the
    coordinator has one capacity artifact.
    """
    captured_at = _normalise_dt(captured_at or datetime.now(timezone.utc))
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CapacitySnapshot(captured_at=captured_at, runners={})
    if not isinstance(record, Mapping):
        return CapacitySnapshot(captured_at=captured_at, runners={})
    snap_at = captured_at
    raw_captured = record.get("captured_at")
    if isinstance(raw_captured, str):
        try:
            snap_at = _normalise_dt(datetime.fromisoformat(raw_captured))
        except ValueError:
            snap_at = captured_at
    runners: dict[str, RunnerCapacity] = {}
    raw_runners = record.get("runners")
    if isinstance(raw_runners, Mapping):
        for key, raw in raw_runners.items():
            if not isinstance(raw, Mapping):
                continue
            try:
                runners[str(key)] = _runner_capacity_from_snapshot_record(raw)
            except (TypeError, ValueError):
                continue
    return CapacitySnapshot(captured_at=snap_at, runners=runners)


def _weekly_cap_for(provider: str) -> int:
    env_name = "OMNISIGHT_PROVIDER_CAP_{}_WEEKLY".format(
        "".join(ch if ch.isalnum() else "_" for ch in provider.upper())
    )
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return provider_quota_tracker.DEFAULT_WEEKLY_CAP_TOKENS


def _last_json_record(path: Path) -> dict[str, Any] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None
    return None


def _runner_capacity_from_record(record: Mapping[str, Any]) -> RunnerCapacity:
    return RunnerCapacity(
        runner_class=str(record["agent_class"]),
        free_slots=0,
        budget_remaining=0.0,
        tokens_in_current_week=max(0, int(record["tokens_in_current_week"])),
        weekly_cap=max(0, int(record["weekly_cap"])),
        reset_at=_normalise_dt(datetime.fromisoformat(str(record["reset_at"]))),
        tickets_completed=max(0, int(record["tickets_completed"])),
        runner_id=str(record.get("runner_id") or ""),
        updated_at=_normalise_dt(datetime.fromisoformat(str(record["ts"]))),
    )


def _runner_capacity_from_snapshot_record(record: Mapping[str, Any]) -> RunnerCapacity:
    reset_at = record.get("reset_at")
    updated_at = record.get("updated_at")
    return RunnerCapacity(
        runner_class=str(record["runner_class"]),
        free_slots=max(0, int(record.get("free_slots", 0) or 0)),
        budget_remaining=float(record.get("budget_remaining", 0.0) or 0.0),
        tokens_in_current_week=max(0, int(record.get("tokens_in_current_week", 0) or 0)),
        weekly_cap=max(0, int(record.get("weekly_cap", 0) or 0)),
        reset_at=(
            _normalise_dt(datetime.fromisoformat(str(reset_at)))
            if reset_at
            else None
        ),
        tickets_completed=max(0, int(record.get("tickets_completed", 0) or 0)),
        runner_id=str(record.get("runner_id") or ""),
        updated_at=(
            _normalise_dt(datetime.fromisoformat(str(updated_at)))
            if updated_at
            else None
        ),
    )


def _normalise_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
