"""AB.9 — Per-task-kind batch eligibility + lane routing + auto-batch.

Decides whether a given task goes to the real-time lane (immediate
Anthropic API call, lower latency, full price) or the batch lane
(50% cheaper, 24h SLA window). Three layers:

  1. ``EligibilityRule`` — static metadata per ``task_kind``:
     ``batch_eligible`` / ``batch_priority`` / ``realtime_required``
     / ``reason``. Defaults shipped for HD / L4 / generic / chat
     types in ``DEFAULT_ROUTING``.

  2. ``EligibilityRegistry`` — runtime layer that wraps defaults
     with operator overrides + provides the ``route()`` function
     that AB.4 dispatcher's caller invokes.

  3. ``AutoBatchAccumulator`` — buffer that accumulates same-kind
     tasks and flushes them as a wave when threshold / timeout
     reached. Lets a stream of incoming tasks build up into a
     proper batch instead of submitting each individually (which
     would defeat the 50% discount benefit and pound the dispatcher).

Routing rule: by default ``batch_eligible=True`` tasks go to batch
lane unless caller passes ``force_lane="realtime"`` AND the rule
doesn't have ``realtime_required=True`` (operator override is
allowed for "prefer-batch but operator wants instant"). Tasks marked
``realtime_required=True`` (chat UI / live console / sandbox preview)
CANNOT be routed to batch even with operator override — these would
be useless 24h after submission.

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §7
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from backend.agents.batch_dispatcher import BatchableTask, LaneType, PriorityLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EligibilityRule:
    """Static routing metadata for one ``task_kind``.

    ``batch_eligible``     — default lane is batch
    ``batch_priority``     — P0..P3 if routed to batch (HD bring-up
                             might be P1, datasheet backfill P3)
    ``reason``             — human-readable why this default chosen
    ``realtime_required``  — hard veto on batch lane regardless of
                             operator override (chat / live console)
    ``auto_batch_threshold`` — if set, AutoBatchAccumulator flushes
                             a wave when this many same-kind tasks
                             accumulate (None = no auto-batching)
    """

    task_kind: str
    batch_eligible: bool
    batch_priority: PriorityLevel = "P2"
    reason: str = ""
    realtime_required: bool = False
    auto_batch_threshold: int | None = None

    def __post_init__(self) -> None:
        if self.realtime_required and self.batch_eligible:
            raise ValueError(
                f"task_kind {self.task_kind!r}: cannot be both "
                "realtime_required=True and batch_eligible=True"
            )


# ─── Default routing table ───────────────────────────────────────


DEFAULT_ROUTING: dict[str, EligibilityRule] = {
    # ── HD parser tasks — long-running, batch-friendly ──
    "hd_parse_kicad": EligibilityRule(
        task_kind="hd_parse_kicad",
        batch_eligible=True,
        batch_priority="P2",
        reason="EDA parsing — long-running, no UI dependency",
        auto_batch_threshold=10,
    ),
    "hd_parse_altium": EligibilityRule(
        task_kind="hd_parse_altium",
        batch_eligible=True,
        batch_priority="P2",
        reason="Altium binary parse via subprocess — bulk-friendly",
        auto_batch_threshold=10,
    ),
    "hd_parse_odb": EligibilityRule(
        task_kind="hd_parse_odb",
        batch_eligible=True,
        batch_priority="P2",
        reason="ODB++ parse via Docker sidecar — bulk-friendly",
        auto_batch_threshold=10,
    ),
    "hd_parse_eagle": EligibilityRule(
        task_kind="hd_parse_eagle",
        batch_eligible=True,
        batch_priority="P2",
        reason="Eagle XML via KiCad importer — bulk-friendly",
        auto_batch_threshold=10,
    ),
    "hd_diff_reference": EligibilityRule(
        task_kind="hd_diff_reference",
        batch_eligible=True,
        batch_priority="P2",
        reason="Multi-component diff — long, no UI",
        auto_batch_threshold=5,
    ),
    "hd_sensor_kb_extract": EligibilityRule(
        task_kind="hd_sensor_kb_extract",
        batch_eligible=True,
        batch_priority="P3",
        reason="Datasheet vision LLM extraction — backlog priority",
        auto_batch_threshold=20,
    ),
    "hd_avl_substitution": EligibilityRule(
        task_kind="hd_avl_substitution",
        batch_eligible=True,
        batch_priority="P2",
        reason="AVL workflow — moderately bulky",
        auto_batch_threshold=5,
    ),
    "hd_fw_dts_parse": EligibilityRule(
        task_kind="hd_fw_dts_parse",
        batch_eligible=True,
        batch_priority="P2",
        reason="DTS parsing — bulk-friendly",
        auto_batch_threshold=10,
    ),
    "hd_fw_linker_parse": EligibilityRule(
        task_kind="hd_fw_linker_parse",
        batch_eligible=True,
        batch_priority="P2",
        reason="Linker script parse — bulk-friendly",
        auto_batch_threshold=10,
    ),
    "hd_cve_impact": EligibilityRule(
        task_kind="hd_cve_impact",
        batch_eligible=True,
        batch_priority="P3",
        reason="CVE impact backfill — non-urgent",
        auto_batch_threshold=20,
    ),

    # ── L4 cross-cutting CI batches ──
    "l4_determinism_regression": EligibilityRule(
        task_kind="l4_determinism_regression",
        batch_eligible=True,
        batch_priority="P3",
        reason="Nightly determinism regression — high-volume, low-urgency",
        auto_batch_threshold=50,
    ),
    "l4_adversarial_ci": EligibilityRule(
        task_kind="l4_adversarial_ci",
        batch_eligible=True,
        batch_priority="P3",
        reason="PR-time adversarial robustness CI — high volume",
        auto_batch_threshold=30,
    ),

    # ── TODO routine bulk tasks ──
    "todo_routine": EligibilityRule(
        task_kind="todo_routine",
        batch_eligible=True,
        batch_priority="P3",
        reason="Bulk routine processing of TODO checkboxes",
        auto_batch_threshold=10,
    ),

    # ── Real-time required (cannot batch — would be 24h late) ──
    "chat_ui": EligibilityRule(
        task_kind="chat_ui",
        batch_eligible=False,
        batch_priority="P0",
        reason="User-facing chat — real-time required",
        realtime_required=True,
    ),
    "w14_sandbox": EligibilityRule(
        task_kind="w14_sandbox",
        batch_eligible=False,
        batch_priority="P0",
        reason="Live sandbox preview — real-time required",
        realtime_required=True,
    ),
    "hd_bringup_live": EligibilityRule(
        task_kind="hd_bringup_live",
        batch_eligible=False,
        batch_priority="P0",
        reason="Live boot console parse — real-time required",
        realtime_required=True,
    ),

    # ── Real-time preferred but soft (operator can override) ──
    "planning": EligibilityRule(
        task_kind="planning",
        batch_eligible=False,
        batch_priority="P1",
        reason="Operator interactive planning — usually wants response now",
    ),
    "hd_bringup": EligibilityRule(
        task_kind="hd_bringup",
        batch_eligible=False,
        batch_priority="P1",
        reason="HD bring-up workbench — usually interactive",
    ),
    "generic_dev": EligibilityRule(
        task_kind="generic_dev",
        batch_eligible=False,
        batch_priority="P1",
        reason="Default dev task — operator usually wants response now",
    ),
}


# ─── Routing decisions ───────────────────────────────────────────


@dataclass(frozen=True)
class RoutingDecision:
    """Outcome of `route()` — explicit so caller can audit / log it."""

    lane: LaneType
    priority: PriorityLevel
    rule: EligibilityRule
    reason: str
    """Human-readable explanation including override info if any."""


class EligibilityRegistry:
    """Operator-overridable wrapper around the default routing table."""

    def __init__(
        self, defaults: dict[str, EligibilityRule] | None = None
    ) -> None:
        self._defaults: dict[str, EligibilityRule] = dict(
            defaults if defaults is not None else DEFAULT_ROUTING
        )
        self._overrides: dict[str, EligibilityRule] = {}

    def get(self, task_kind: str) -> EligibilityRule:
        """Return the effective rule (override > default > generic_dev fallback)."""
        if task_kind in self._overrides:
            return self._overrides[task_kind]
        if task_kind in self._defaults:
            return self._defaults[task_kind]
        # Unknown task_kind → fall back to generic_dev (real-time, P1).
        # Warning logged so operator can register a proper rule later.
        logger.warning(
            "No eligibility rule for task_kind %r; falling back to generic_dev",
            task_kind,
        )
        return self._defaults.get(
            "generic_dev",
            EligibilityRule(
                task_kind=task_kind,
                batch_eligible=False,
                batch_priority="P1",
                reason="No rule; default conservative",
            ),
        )

    def set_override(self, rule: EligibilityRule) -> None:
        """Operator override — takes precedence over default. Same
        validation rules as default (realtime_required + batch_eligible
        is rejected by EligibilityRule.__post_init__)."""
        self._overrides[rule.task_kind] = rule

    def clear_override(self, task_kind: str) -> bool:
        """Returns True if an override existed and was removed."""
        return self._overrides.pop(task_kind, None) is not None

    def list_overrides(self) -> dict[str, EligibilityRule]:
        return dict(self._overrides)

    def route(
        self,
        task_kind: str,
        *,
        force_lane: LaneType | None = None,
    ) -> RoutingDecision:
        """Produce a final routing decision.

        Caller may pass ``force_lane`` to override the default.
        ``realtime_required=True`` rules veto force_lane="batch" —
        these tasks would be useless after batch's 24h delay.
        """
        rule = self.get(task_kind)

        if force_lane == "batch" and rule.realtime_required:
            return RoutingDecision(
                lane="realtime",
                priority="P0",
                rule=rule,
                reason=(
                    f"force_lane='batch' VETOED — task_kind {task_kind!r} "
                    f"is realtime_required ({rule.reason})"
                ),
            )

        if force_lane is not None:
            return RoutingDecision(
                lane=force_lane,
                priority=rule.batch_priority if force_lane == "batch" else "P0",
                rule=rule,
                reason=(
                    f"operator override force_lane={force_lane!r} "
                    f"(default would be {'batch' if rule.batch_eligible else 'realtime'})"
                ),
            )

        # Default path
        if rule.batch_eligible:
            return RoutingDecision(
                lane="batch",
                priority=rule.batch_priority,
                rule=rule,
                reason=f"default batch-eligible: {rule.reason}",
            )
        return RoutingDecision(
            lane="realtime",
            priority="P0",
            rule=rule,
            reason=f"default realtime: {rule.reason}",
        )


# ─── Auto-batch accumulator ──────────────────────────────────────


@dataclass
class _AccumBucket:
    """One bucket per (task_kind, model, tools_signature) tuple."""

    tasks: list[BatchableTask] = field(default_factory=list)
    first_added_at: float | None = None


DispatcherEnqueue = Callable[[BatchableTask], Awaitable[None]]


class AutoBatchAccumulator:
    """Accumulates batch-eligible tasks and emits flushes when ready.

    Two flush triggers:

      1. **Count threshold** — bucket reaches its task_kind's
         ``auto_batch_threshold``. Default per-task-kind in
         ``DEFAULT_ROUTING``; operator override via registry.

      2. **Age timeout** — first task in bucket older than
         ``max_age_seconds`` (default 60s). Prevents low-volume
         task kinds from getting stuck waiting for the threshold
         that never arrives.

    Flushed tasks are dispatched via injected ``dispatcher_enqueue``
    callback. Caller wires this to ``BatchDispatcher.enqueue()`` (AB.4).

    NOT a long-running worker — caller polls ``flush_due()`` from
    their own loop (or the dispatcher's) so the time semantics stay
    explicit + testable. ``add()`` opportunistically flushes a single
    bucket when its threshold is hit, which covers the
    high-throughput steady-state path without any polling.
    """

    def __init__(
        self,
        registry: EligibilityRegistry,
        *,
        dispatcher_enqueue: DispatcherEnqueue,
        max_age_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self._dispatcher_enqueue = dispatcher_enqueue
        self._max_age = max_age_seconds
        self._clock = clock
        # key = (task_kind, model, tools_signature)
        self._buckets: dict[tuple[str, str, str], _AccumBucket] = {}

    async def add(self, task: BatchableTask) -> int:
        """Add a task to the appropriate bucket.

        Returns count of tasks flushed as a result (0 if still
        accumulating, threshold-many if threshold hit).
        """
        kind = task.metadata.get("task_kind", "generic_dev")
        rule = self.registry.get(kind)

        if not rule.batch_eligible:
            # Caller shouldn't hand realtime tasks to the accumulator,
            # but we defensively forward instead of silently dropping.
            await self._dispatcher_enqueue(task)
            return 1

        key = (kind, task.model, task.tools_signature)
        bucket = self._buckets.setdefault(key, _AccumBucket())
        if bucket.first_added_at is None:
            bucket.first_added_at = self._clock()
        bucket.tasks.append(task)

        threshold = rule.auto_batch_threshold
        if threshold is not None and len(bucket.tasks) >= threshold:
            return await self._flush_bucket(key)
        return 0

    async def flush_due(self) -> int:
        """Flush any bucket whose first task is older than max_age.

        Caller invokes periodically (e.g., dispatcher's poll loop).
        Returns total tasks flushed across all due buckets.
        """
        now = self._clock()
        due_keys = [
            k for k, b in self._buckets.items()
            if b.first_added_at is not None
            and (now - b.first_added_at) >= self._max_age
        ]
        flushed_total = 0
        for k in due_keys:
            flushed_total += await self._flush_bucket(k)
        return flushed_total

    async def flush_all(self) -> int:
        """Force-flush every bucket. Used at shutdown."""
        flushed = 0
        for key in list(self._buckets):
            flushed += await self._flush_bucket(key)
        return flushed

    async def _flush_bucket(self, key: tuple[str, str, str]) -> int:
        bucket = self._buckets.pop(key, None)
        if bucket is None or not bucket.tasks:
            return 0
        for task in bucket.tasks:
            await self._dispatcher_enqueue(task)
        logger.info(
            "AutoBatchAccumulator flushed %d tasks for kind=%s model=%s tools=%s",
            len(bucket.tasks), key[0], key[1], key[2],
        )
        return len(bucket.tasks)

    @property
    def pending_count(self) -> int:
        return sum(len(b.tasks) for b in self._buckets.values())

    def bucket_keys(self) -> list[tuple[str, str, str]]:
        return list(self._buckets.keys())
