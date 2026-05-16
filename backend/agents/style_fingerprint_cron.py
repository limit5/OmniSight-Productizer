"""RPG.W3.3 -- daily style-fingerprint recompute cron + drift logging.

ADR-0008 §"Style fingerprint" specifies the fingerprint is
"daily-recomputed" so each agent's per-instance Character Card stays
fresh as their task history evolves. This module owns the cron-side
sweep: walk every Character Card, recompute the W3.2 style
fingerprint over the most-recent N tasks, log per-agent drift events,
and write the new fingerprint back when it changed.

The W3.2 fingerprint is a deterministic SHA-256 over the canonical
(commit_style / test_pattern / refactor_tendency) tuples; this module
adds an axis-level ``drift_ratio`` so operators can tell apart
"hash changed because one extra task landed" from "agent's style
genuinely shifted". Drift ratio is computed inside the new window
itself as the fraction of canonical tuples that differ from the
dominant tuple -- a value in ``[0.0, 1.0)``:

- ``0.0`` = every task in the window shares the same canonical
  signal tuple (perfectly consistent style; the fingerprint hash may
  still differ from yesterday because the rolling window slid by one
  task, but the agent's style itself is stable).
- ``>= DEFAULT_STYLE_DRIFT_THRESHOLD`` (``0.5``) = at least half the
  window diverges from the dominant style -- a strong shift, worth a
  ``WARNING``-level log line + later operator inspection.

Persistence is delegated to the injected
``CharacterCardStore``-shaped store; task-history reads are
delegated to an injected provider so the module stays free of
clock/DB/HTTP IO. The runner wires this against the W1 Postgres
store + a project task-history adapter.

Module-global state audit (SOP Step 1): this module defines
immutable constants, frozen dataclasses, and pure helpers plus one
async ``recompute_style_fingerprints`` orchestrator. No clock reads,
no filesystem access, no module-level mutable state. Callers pass
``now`` and the task-history provider explicitly; tests pin a fixed
clock without monkey-patching ``datetime.now``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from backend.agents.character_card import (
    CharacterCard,
    CharacterCardRosterEntry,
    CharacterCardUpdate,
)
from backend.agents.style_fingerprint import (
    DEFAULT_STYLE_WINDOW,
    TaskStyleSignals,
    compute_style_fingerprint,
)


DEFAULT_STYLE_DRIFT_THRESHOLD = 0.5
"""Default fraction of canonical-tuple divergence above which the
sweep emits a ``WARNING`` instead of an ``INFO`` log. Operators may
override per-call without touching the W3.2 fingerprint contract."""

MIN_DRIFT_THRESHOLD = 0.0
MAX_DRIFT_THRESHOLD = 1.0

LOG = logging.getLogger("rpg.style_fingerprint_cron")


TaskHistoryProvider = Callable[[str], Awaitable[Sequence[TaskStyleSignals]]]
"""Awaitable that maps ``agent_id -> oldest->newest TaskStyleSignals``.

Implementations are runner-side; the module never reaches into a DB
or HTTP itself so unit tests can pass a plain coroutine."""


class CardListAndUpdateStore(Protocol):
    """Subset of :class:`backend.agents.character_card.CharacterCardStore`
    the daily sweep needs.

    Using a narrow Protocol keeps the cron module decoupled from the
    full CRUD surface so any future store (sharded, read-replica,
    cache-wrapped) only has to satisfy the two methods used here.
    """

    async def list_cards(  # pragma: no cover - protocol
        self,
    ) -> Sequence[CharacterCard | CharacterCardRosterEntry]:
        ...

    async def update_card(
        self,
        agent_id: str,
        patch: CharacterCardUpdate,
    ) -> CharacterCard:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class StyleDriftReport:
    """One per character card the sweep visited.

    The report is emitted whether or not the fingerprint actually
    changed; ``fingerprint_changed`` lets a caller distinguish a no-op
    visit from a real drift event without re-hashing on its side.
    """

    agent_id: str
    previous_fingerprint: str
    current_fingerprint: str
    drift_ratio: float
    samples_considered: int
    window_size: int
    fingerprint_changed: bool
    above_threshold: bool


def style_drift_ratio(samples: Sequence[TaskStyleSignals]) -> float:
    """Return the fraction of canonical tuples in ``samples`` that
    differ from the most-common canonical tuple in the same window.

    Range: ``[0.0, 1.0)``. ``0.0`` when every task carries the same
    canonical (commit_style / test_pattern / refactor_tendency)
    tuple; approaches ``1.0`` as the window splits across many
    distinct styles. An empty window returns ``0.0`` (no drift can
    be measured from zero tasks).
    """
    if not samples:
        return 0.0
    canonical_tuples = [_canonical_signature(sample) for sample in samples]
    mode_count = Counter(canonical_tuples).most_common(1)[0][1]
    return 1.0 - (mode_count / len(canonical_tuples))


async def recompute_style_fingerprints(
    card_store: CardListAndUpdateStore,
    task_history_provider: TaskHistoryProvider,
    *,
    drift_threshold: float = DEFAULT_STYLE_DRIFT_THRESHOLD,
    last_n: int = DEFAULT_STYLE_WINDOW,
    logger: logging.Logger | None = None,
) -> tuple[StyleDriftReport, ...]:
    """Daily-cron sweep: recompute every Character Card's style
    fingerprint and persist + log any drift.

    For each card from ``card_store.list_cards()``:

    1. Resolve the agent's most-recent ``last_n`` task style signals
       via ``task_history_provider``.
    2. Re-hash via :func:`compute_style_fingerprint`.
    3. Compare to the card's stored fingerprint. If different, patch
       the card via ``card_store.update_card`` so the next read sees
       the fresh value.
    4. Emit one log line per visited card:

       - ``INFO`` "style_fingerprint_unchanged" when the hash matches.
       - ``INFO`` "style_fingerprint_drift" when the hash changed but
         the drift ratio sits below ``drift_threshold``.
       - ``WARNING`` "style_fingerprint_drift_above_threshold" when
         the hash changed and at least ``drift_threshold`` of the
         window diverges from the dominant canonical tuple.

    Returns a tuple of :class:`StyleDriftReport`, one per card,
    so tests + operator-facing dashboards have a stable data shape.
    """
    _validate_threshold(drift_threshold)
    if last_n < 1:
        raise ValueError("last_n must be >= 1")

    log = logger or LOG
    reports: list[StyleDriftReport] = []
    for card_listing in await card_store.list_cards():
        card = _card_from_listing(card_listing)
        history = tuple(await task_history_provider(card.agent_id))
        window = history[-last_n:]
        new_fingerprint = compute_style_fingerprint(window, last_n=last_n)
        drift_ratio = style_drift_ratio(window)
        fingerprint_changed = new_fingerprint != card.style_fingerprint
        above_threshold = fingerprint_changed and drift_ratio >= drift_threshold

        if fingerprint_changed:
            await card_store.update_card(
                card.agent_id,
                CharacterCardUpdate(style_fingerprint=new_fingerprint),
            )

        _emit_log(
            log,
            agent_id=card.agent_id,
            previous_fingerprint=card.style_fingerprint,
            current_fingerprint=new_fingerprint,
            drift_ratio=drift_ratio,
            samples_considered=len(window),
            window_size=last_n,
            fingerprint_changed=fingerprint_changed,
            above_threshold=above_threshold,
            drift_threshold=drift_threshold,
        )

        reports.append(
            StyleDriftReport(
                agent_id=card.agent_id,
                previous_fingerprint=card.style_fingerprint,
                current_fingerprint=new_fingerprint,
                drift_ratio=drift_ratio,
                samples_considered=len(window),
                window_size=last_n,
                fingerprint_changed=fingerprint_changed,
                above_threshold=above_threshold,
            )
        )
    return tuple(reports)


def summarize_drift(
    reports: Sequence[StyleDriftReport],
) -> Mapping[str, int]:
    """Return aggregate counters for a sweep, useful for operator
    dashboards and the ``--summary`` flag on the wrapper script.

    Keys:

    - ``visited`` -- total cards looked at this run.
    - ``unchanged`` -- fingerprint matched what was on the card.
    - ``drifted`` -- fingerprint changed (any drift_ratio).
    - ``above_threshold`` -- fingerprint changed AND drift_ratio
      reached or exceeded the threshold passed to the sweep.
    """
    visited = len(reports)
    drifted = sum(1 for r in reports if r.fingerprint_changed)
    above_threshold = sum(1 for r in reports if r.above_threshold)
    return {
        "visited": visited,
        "unchanged": visited - drifted,
        "drifted": drifted,
        "above_threshold": above_threshold,
    }


def _canonical_signature(sample: TaskStyleSignals) -> tuple[str, str, str]:
    canonical = sample.canonical()
    return (
        canonical["commit_style"],
        canonical["test_pattern"],
        canonical["refactor_tendency"],
    )


def _card_from_listing(
    listing: CharacterCard | CharacterCardRosterEntry,
) -> CharacterCard:
    if isinstance(listing, CharacterCardRosterEntry):
        return listing.card
    return listing


def _validate_threshold(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("drift_threshold must be a number")
    if value < MIN_DRIFT_THRESHOLD or value > MAX_DRIFT_THRESHOLD:
        raise ValueError(
            "drift_threshold must be in "
            f"[{MIN_DRIFT_THRESHOLD}, {MAX_DRIFT_THRESHOLD}]"
        )


def _emit_log(
    log: logging.Logger,
    *,
    agent_id: str,
    previous_fingerprint: str,
    current_fingerprint: str,
    drift_ratio: float,
    samples_considered: int,
    window_size: int,
    fingerprint_changed: bool,
    above_threshold: bool,
    drift_threshold: float,
) -> None:
    extra = {
        "agent_id": agent_id,
        "previous_fingerprint": previous_fingerprint,
        "current_fingerprint": current_fingerprint,
        "drift_ratio": round(drift_ratio, 6),
        "drift_threshold": drift_threshold,
        "samples_considered": samples_considered,
        "window_size": window_size,
    }
    if not fingerprint_changed:
        log.info("style_fingerprint_unchanged", extra=extra)
        return
    if above_threshold:
        log.warning("style_fingerprint_drift_above_threshold", extra=extra)
        return
    log.info("style_fingerprint_drift", extra=extra)


__all__ = [
    "DEFAULT_STYLE_DRIFT_THRESHOLD",
    "MAX_DRIFT_THRESHOLD",
    "MIN_DRIFT_THRESHOLD",
    "CardListAndUpdateStore",
    "StyleDriftReport",
    "TaskHistoryProvider",
    "recompute_style_fingerprints",
    "style_drift_ratio",
    "summarize_drift",
]
