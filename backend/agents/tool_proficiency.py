"""RPG.W13 -- per-``(agent_id, tool_id)`` MCP/A2A tool proficiency.

ADR-0008 §"MCP/A2A tool proficiency (W13)" defines a 1-5 level ladder
per tool. Levels are derived from invocation count + success ratio
thresholds, not direct XP:

* Lv 1  basic invoke (bootstrap row)
* Lv 2  ≥ 10 success / ≥ 0.70 success ratio   — chain 2 calls
* Lv 3  ≥ 50 success / ≥ 0.80 success ratio   — batch ops
* Lv 4  ≥ 200 success / ≥ 0.85 success ratio  — advanced flags + cross-Guild A2A
* Lv 5  ≥ 500 success / ≥ 0.90 success ratio  — author new MCP wrapper

This module owns the helper surface that the W17.7 telemetry
consumer (OP-117) calls on every ``tool_invocation`` event, plus the
gate ``can_invoke_at_level`` the tool dispatcher checks before
forwarding to a handler. Per-tool required levels live in
``config/tool_proficiency_gates.yaml`` (loaded lazily on first
``can_invoke_at_level`` call so unit tests can override the path).

W13 is intentionally **fail-open on telemetry**: if the consumer
falls behind by more than 24h the gate keeps allowing invocations
but emits ``TelemetryConsumerLag`` as a warning so the operator can
unstick the consumer without users seeing degraded UX.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines constants, dataclasses, exception classes, and
``Protocol``/store classes only. The cached gate config map is the
single piece of mutable state — it is process-local and is reset by
``reset_gate_config_cache_for_tests`` so test isolation does not leak
across runs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol


LOG = logging.getLogger(__name__)


ConnFactory = Callable[[], Any]


# ── Constants from ADR-0008 §"MCP/A2A tool proficiency (W13)" ────────

MAX_TOOL_LEVEL = 5

# ``LEVEL_REQUIREMENTS[L]`` = the minimum (success_count, success_ratio)
# to *reach* level L. Lv 1 is the bootstrap floor for any tool the agent
# has ever invoked (or has yet to invoke; gate semantics described in
# ``can_invoke_at_level``).
LEVEL_REQUIREMENTS: Mapping[int, tuple[int, float]] = MappingProxyType(
    {
        1: (0, 0.0),
        2: (10, 0.70),
        3: (50, 0.80),
        4: (200, 0.85),
        5: (500, 0.90),
    }
)


# Per-level capability semantics — purely descriptive; the helper does
# not enforce these (the tool handler does). Exposed here so the
# Character Card UI and the operator runbook stay in lockstep with
# ADR-0008.
LEVEL_CAPABILITIES: Mapping[int, str] = MappingProxyType(
    {
        1: "basic_invoke",
        2: "chain_two_calls",
        3: "batch_ops",
        4: "advanced_flags_cross_guild_a2a",
        5: "author_new_mcp_wrapper",
    }
)


TELEMETRY_LAG_THRESHOLD = timedelta(hours=24)


# Module-level cache of the YAML config; loaded on first read,
# refreshed on explicit reset. ``None`` means "not yet loaded" so a
# missing file is distinguishable from an empty dict.
_GATE_CONFIG_CACHE: dict[str, int] | None = None
_GATE_CONFIG_PATH: Path | None = None


# ── Errors ──────────────────────────────────────────────────────────


class ToolProficiencyError(RuntimeError):
    """Base class for W13 tool-proficiency errors."""


class ToolProficiencyInsufficient(ToolProficiencyError):
    """Raised when ``can_invoke_at_level`` refuses a low-Lv invocation."""

    def __init__(
        self,
        agent_id: str,
        tool_id: str,
        current_level: int,
        required_level: int,
    ) -> None:
        super().__init__(
            f"agent {agent_id!r} is Lv {current_level} on {tool_id!r}; "
            f"required Lv {required_level}"
        )
        self.agent_id = agent_id
        self.tool_id = tool_id
        self.current_level = current_level
        self.required_level = required_level


class ToolNotInProficiencyTable(ToolProficiencyError):
    """Raised when an op needs an existing row but the agent has never invoked the tool.

    The gate path treats this as Lv 1 + allow (see ``can_invoke_at_level``);
    callers that explicitly need a row should bootstrap one via
    :func:`record_tool_invocation`.
    """


class TelemetryConsumerLag(ToolProficiencyError):
    """Raised (as a *warning*, not a refusal) when proficiency state is >24h stale."""


class ProficiencyGateConfigMissing(ToolProficiencyError):
    """Raised when the gate config file is missing entirely.

    Distinct from "tool not listed in config" — the latter falls back to
    Lv 1 required (permissive). This error fires only when the YAML
    cannot be parsed at all.
    """


# ── Pure helpers ────────────────────────────────────────────────────


def compute_tool_level(invocation_count: int, success_count: int) -> int:
    """Return the W13 tool level (1-5) for ``(invocation_count, success_count)``.

    Lv L is reached when ``success_count >= LEVEL_REQUIREMENTS[L][0]``
    **and** ``success_count / invocation_count >= LEVEL_REQUIREMENTS[L][1]``.
    Lv 1 always applies (the bootstrap floor); higher levels stack on
    top so a single row may satisfy multiple Lv requirements at once —
    the helper returns the *highest* satisfied level.
    """
    if isinstance(invocation_count, bool) or not isinstance(invocation_count, int):
        raise TypeError("invocation_count must be an int")
    if isinstance(success_count, bool) or not isinstance(success_count, int):
        raise TypeError("success_count must be an int")
    if invocation_count < 0:
        raise ValueError("invocation_count must be >= 0")
    if success_count < 0:
        raise ValueError("success_count must be >= 0")
    if success_count > invocation_count:
        raise ValueError("success_count cannot exceed invocation_count")

    if invocation_count == 0:
        return 1
    ratio = success_count / invocation_count
    level = 1
    for candidate in sorted(LEVEL_REQUIREMENTS):
        threshold_count, threshold_ratio = LEVEL_REQUIREMENTS[candidate]
        if success_count >= threshold_count and ratio >= threshold_ratio:
            level = candidate
    return level


def capability_for_level(level: int) -> str:
    """Return the per-level capability label (see ``LEVEL_CAPABILITIES``)."""
    if level < 1 or level > MAX_TOOL_LEVEL:
        raise ValueError(f"level must be 1..{MAX_TOOL_LEVEL}")
    return LEVEL_CAPABILITIES[level]


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolProficiencyState:
    """Durable per-``(agent_id, tool_id)`` row in ``agent_tool_proficiency``."""

    agent_id: str
    tool_id: str
    level: int
    invocation_count: int
    success_count: int
    last_used_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _required("agent_id", self.agent_id))
        object.__setattr__(self, "tool_id", _required("tool_id", self.tool_id))
        if self.level < 1 or self.level > MAX_TOOL_LEVEL:
            raise ValueError(f"level must be 1..{MAX_TOOL_LEVEL}")
        if self.invocation_count < 0:
            raise ValueError("invocation_count must be >= 0")
        if self.success_count < 0:
            raise ValueError("success_count must be >= 0")
        if self.success_count > self.invocation_count:
            raise ValueError("success_count cannot exceed invocation_count")
        object.__setattr__(self, "last_used_at", _utc(self.last_used_at))

    @property
    def success_ratio(self) -> float:
        if self.invocation_count == 0:
            return 0.0
        return self.success_count / self.invocation_count


@dataclass(frozen=True)
class ToolInvocationRecorded:
    """Return value of :func:`record_tool_invocation`."""

    agent_id: str
    tool_id: str
    previous_level: int
    new_level: int
    invocation_count: int
    success_count: int
    bootstrapped: bool


# ── Store protocol + in-memory store ────────────────────────────────


class ToolProficiencyStore(Protocol):
    async def get_state(
        self, agent_id: str, tool_id: str
    ) -> ToolProficiencyState | None: ...
    async def list_states(
        self, agent_id: str
    ) -> tuple[ToolProficiencyState, ...]: ...
    async def upsert_state(
        self, state: ToolProficiencyState
    ) -> ToolProficiencyState: ...
    async def iter_all_states(self) -> AsyncIterator[ToolProficiencyState]: ...


class InMemoryToolProficiencyStore:
    """Dev/test store. Production callers should use the Postgres store."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ToolProficiencyState] = {}

    async def get_state(
        self, agent_id: str, tool_id: str
    ) -> ToolProficiencyState | None:
        return self._rows.get((agent_id, tool_id))

    async def list_states(
        self, agent_id: str
    ) -> tuple[ToolProficiencyState, ...]:
        return tuple(
            row for key, row in self._rows.items() if key[0] == agent_id
        )

    async def upsert_state(
        self, state: ToolProficiencyState
    ) -> ToolProficiencyState:
        self._rows[(state.agent_id, state.tool_id)] = state
        return state

    async def iter_all_states(self) -> AsyncIterator[ToolProficiencyState]:
        for row in list(self._rows.values()):
            yield row


# ── Postgres store ──────────────────────────────────────────────────


class PostgresToolProficiencyStore:
    """``agent_tool_proficiency``-backed store (alembic 0227).

    Writes are full-row UPSERTs keyed by ``(agent_id, tool_id)`` so the
    caller does not have to know whether a row already exists.
    """

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def get_state(
        self, agent_id: str, tool_id: str
    ) -> ToolProficiencyState | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT agent_id, tool_id, level, invocation_count,
                       success_count, last_used_at
                FROM agent_tool_proficiency
                WHERE agent_id = $1 AND tool_id = $2
                """,
                _required("agent_id", agent_id),
                _required("tool_id", tool_id),
            )
        return _row_to_state(row) if row else None

    async def list_states(
        self, agent_id: str
    ) -> tuple[ToolProficiencyState, ...]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id, tool_id, level, invocation_count,
                       success_count, last_used_at
                FROM agent_tool_proficiency
                WHERE agent_id = $1
                ORDER BY tool_id ASC
                """,
                _required("agent_id", agent_id),
            )
        return tuple(_row_to_state(row) for row in rows)

    async def upsert_state(
        self, state: ToolProficiencyState
    ) -> ToolProficiencyState:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_tool_proficiency (
                    agent_id, tool_id, level, invocation_count,
                    success_count, last_used_at, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
                ON CONFLICT (agent_id, tool_id) DO UPDATE
                    SET level = EXCLUDED.level,
                        invocation_count = EXCLUDED.invocation_count,
                        success_count = EXCLUDED.success_count,
                        last_used_at = EXCLUDED.last_used_at,
                        updated_at = NOW()
                RETURNING agent_id, tool_id, level, invocation_count,
                          success_count, last_used_at
                """,
                state.agent_id,
                state.tool_id,
                state.level,
                state.invocation_count,
                state.success_count,
                state.last_used_at,
            )
        return _row_to_state(row)

    async def iter_all_states(self) -> AsyncIterator[ToolProficiencyState]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id, tool_id, level, invocation_count,
                       success_count, last_used_at
                FROM agent_tool_proficiency
                """
            )
        for row in rows:
            yield _row_to_state(row)


# ── Public operations ───────────────────────────────────────────────


async def record_tool_invocation(
    store: ToolProficiencyStore,
    agent_id: str,
    tool_id: str,
    outcome: str,
    *,
    now: datetime | None = None,
    emit_level_up: Callable[[str, str, int, int], None] | None = None,
) -> ToolInvocationRecorded:
    """Increment counters for ``(agent_id, tool_id)`` and recompute level.

    ``outcome`` is the W17.7 telemetry status:

    * ``success`` — increments both ``invocation_count`` and ``success_count``.
    * Anything else (``fail``, ``failed``, ``error`` …) — increments only
      ``invocation_count``.

    Emits ``tool:level_up`` via ``emit_level_up`` (or the SSE bus) when
    the level crosses up. Returns a :class:`ToolInvocationRecorded`
    describing the transition; callers (the W17.7 consumer) usually
    ignore the return value.
    """
    agent_id = _required("agent_id", agent_id)
    tool_id = _required("tool_id", tool_id)
    when = _utc(now or datetime.now(timezone.utc))

    existing = await store.get_state(agent_id, tool_id)
    previous_level = existing.level if existing else 1
    invocation_count = (existing.invocation_count if existing else 0) + 1
    success_delta = 1 if _clean_outcome(outcome) == "success" else 0
    success_count = (existing.success_count if existing else 0) + success_delta
    new_level = compute_tool_level(invocation_count, success_count)

    state = ToolProficiencyState(
        agent_id=agent_id,
        tool_id=tool_id,
        level=new_level,
        invocation_count=invocation_count,
        success_count=success_count,
        last_used_at=when,
    )
    await store.upsert_state(state)

    bootstrapped = existing is None
    if new_level > previous_level:
        _emit_level_up(
            emit_level_up, agent_id, tool_id, previous_level, new_level
        )

    return ToolInvocationRecorded(
        agent_id=agent_id,
        tool_id=tool_id,
        previous_level=previous_level,
        new_level=new_level,
        invocation_count=invocation_count,
        success_count=success_count,
        bootstrapped=bootstrapped,
    )


async def can_invoke_at_level(
    store: ToolProficiencyStore,
    agent_id: str,
    tool_id: str,
    required_level: int,
    *,
    now: datetime | None = None,
    emit_gate_blocked: Callable[[str, str, int, int], None] | None = None,
) -> bool:
    """Return True iff the agent's current Lv ≥ ``required_level``.

    Side effects: when the gate refuses, emits ``tool:gate:blocked`` on
    the SSE bus (or via ``emit_gate_blocked`` for tests) so the operator
    sees the refusal in real time. When the consumer is lagging
    (``last_used_at`` older than :data:`TELEMETRY_LAG_THRESHOLD`) the
    helper logs a warning but allows the invocation (W13 fail-open
    semantics).
    """
    agent_id = _required("agent_id", agent_id)
    tool_id = _required("tool_id", tool_id)
    if required_level < 1 or required_level > MAX_TOOL_LEVEL:
        raise ValueError(f"required_level must be 1..{MAX_TOOL_LEVEL}")
    when = _utc(now or datetime.now(timezone.utc))

    state = await store.get_state(agent_id, tool_id)
    if state is None:
        # First-time invocation; Lv 1 bootstrap on the *next* successful
        # record. The gate refuses only if required_level > 1.
        if required_level <= 1:
            return True
        _emit_gate_blocked(
            emit_gate_blocked, agent_id, tool_id, 1, required_level
        )
        return False

    if when - state.last_used_at > TELEMETRY_LAG_THRESHOLD:
        # Fail-open: warn but do not refuse.
        LOG.warning(
            "TelemetryConsumerLag: agent_id=%s tool_id=%s last_used_at=%s "
            "(>24h stale); allowing invocation per W13 fail-open semantics",
            agent_id,
            tool_id,
            state.last_used_at.isoformat(),
        )

    if state.level < required_level:
        _emit_gate_blocked(
            emit_gate_blocked, agent_id, tool_id, state.level, required_level
        )
        return False
    return True


async def list_proficiencies(
    store: ToolProficiencyStore, agent_id: str
) -> tuple[ToolProficiencyState, ...]:
    """Return all proficiency rows for one agent (Character Card "Tools" tab feed)."""
    return await store.list_states(_required("agent_id", agent_id))


# ── Gate config loading ─────────────────────────────────────────────


def get_required_level(
    tool_id: str, *, config_path: Path | str | None = None
) -> int:
    """Return the required Lv for ``tool_id`` from the gates YAML.

    Tools not listed in the YAML default to Lv 1 (permissive) per the
    ``ProficiencyGateConfigMissing`` error catalog entry — only a
    missing/invalid *file* raises, not a missing *entry*.
    """
    config = _load_gate_config(config_path)
    return config.get(tool_id, 1)


def reset_gate_config_cache_for_tests() -> None:
    """Test hook: clear the cached config so a fresh YAML reload happens."""
    global _GATE_CONFIG_CACHE, _GATE_CONFIG_PATH
    _GATE_CONFIG_CACHE = None
    _GATE_CONFIG_PATH = None


def _load_gate_config(path: Path | str | None) -> dict[str, int]:
    global _GATE_CONFIG_CACHE, _GATE_CONFIG_PATH
    resolved = _resolve_config_path(path)
    if (
        _GATE_CONFIG_CACHE is not None
        and _GATE_CONFIG_PATH == resolved
    ):
        return _GATE_CONFIG_CACHE

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - PyYAML is a runtime dep
        raise ProficiencyGateConfigMissing(
            "PyYAML is required to load tool_proficiency_gates.yaml"
        ) from exc

    if not resolved.is_file():
        raise ProficiencyGateConfigMissing(
            f"tool_proficiency_gates.yaml not found at {resolved}"
        )
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProficiencyGateConfigMissing(
            f"failed to parse tool_proficiency_gates.yaml at {resolved}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ProficiencyGateConfigMissing(
            f"tool_proficiency_gates.yaml at {resolved} must be a mapping"
        )

    parsed: dict[str, int] = {}
    gates = raw.get("gates") if isinstance(raw.get("gates"), dict) else raw
    if not isinstance(gates, dict):
        raise ProficiencyGateConfigMissing(
            f"tool_proficiency_gates.yaml at {resolved} has no 'gates' mapping"
        )
    for tool_id, required in gates.items():
        if not isinstance(tool_id, str):
            continue
        if isinstance(required, bool) or not isinstance(required, int):
            continue
        if required < 1 or required > MAX_TOOL_LEVEL:
            continue
        parsed[tool_id] = required

    _GATE_CONFIG_CACHE = parsed
    _GATE_CONFIG_PATH = resolved
    return parsed


def _resolve_config_path(path: Path | str | None) -> Path:
    if path is not None:
        return Path(path).resolve()
    env = os.environ.get("OMNISIGHT_TOOL_PROFICIENCY_GATES")
    if env:
        return Path(env).resolve()
    # Default: config/tool_proficiency_gates.yaml relative to repo root.
    return (Path(__file__).resolve().parents[2] / "config"
            / "tool_proficiency_gates.yaml")


# ── Internal helpers ────────────────────────────────────────────────


@asynccontextmanager
async def _acquire(factory: ConnFactory) -> AsyncIterator[Any]:
    cm = factory()
    async with cm as conn:
        yield conn


def _row_to_state(row: Any) -> ToolProficiencyState:
    return ToolProficiencyState(
        agent_id=row["agent_id"],
        tool_id=row["tool_id"],
        level=int(row["level"]),
        invocation_count=int(row["invocation_count"]),
        success_count=int(row["success_count"]),
        last_used_at=row["last_used_at"],
    )


def _emit_level_up(
    emit: Callable[[str, str, int, int], None] | None,
    agent_id: str,
    tool_id: str,
    previous_level: int,
    new_level: int,
) -> None:
    if emit is not None:
        emit(agent_id, tool_id, previous_level, new_level)
        return
    try:
        from backend.events import bus

        bus.publish(
            "tool:level_up",
            {
                "agent_id": agent_id,
                "tool_id": tool_id,
                "previous_level": previous_level,
                "new_level": new_level,
            },
            broadcast_scope="global",
        )
    except Exception as exc:  # pragma: no cover - SSE is best-effort
        LOG.debug("tool:level_up SSE emit failed: %s", exc)


def _emit_gate_blocked(
    emit: Callable[[str, str, int, int], None] | None,
    agent_id: str,
    tool_id: str,
    current_level: int,
    required_level: int,
) -> None:
    if emit is not None:
        emit(agent_id, tool_id, current_level, required_level)
        return
    try:
        from backend.events import bus

        bus.publish(
            "tool:gate:blocked",
            {
                "agent_id": agent_id,
                "tool_id": tool_id,
                "current_level": current_level,
                "required_level": required_level,
            },
            broadcast_scope="global",
        )
    except Exception as exc:  # pragma: no cover - SSE is best-effort
        LOG.debug("tool:gate:blocked SSE emit failed: %s", exc)


def _clean_outcome(outcome: Any) -> str:
    if isinstance(outcome, bool):
        return "success" if outcome else "fail"
    if not isinstance(outcome, str):
        raise TypeError("outcome must be a string")
    clean = outcome.strip().lower()
    if clean in ("success", "ok", "pass", "passed", "done"):
        return "success"
    return "fail"


def _required(field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} is required")
    return clean


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "InMemoryToolProficiencyStore",
    "LEVEL_CAPABILITIES",
    "LEVEL_REQUIREMENTS",
    "MAX_TOOL_LEVEL",
    "PostgresToolProficiencyStore",
    "ProficiencyGateConfigMissing",
    "TELEMETRY_LAG_THRESHOLD",
    "TelemetryConsumerLag",
    "ToolInvocationRecorded",
    "ToolNotInProficiencyTable",
    "ToolProficiencyError",
    "ToolProficiencyInsufficient",
    "ToolProficiencyState",
    "ToolProficiencyStore",
    "can_invoke_at_level",
    "capability_for_level",
    "compute_tool_level",
    "get_required_level",
    "list_proficiencies",
    "record_tool_invocation",
    "reset_gate_config_cache_for_tests",
]
