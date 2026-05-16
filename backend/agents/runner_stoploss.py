"""Per-ticket §11-revert circuit breaker (OP-1140).

When a single ticket §11-reverts repeatedly inside a short trailing window,
refuse further pickups until an operator clears the trip. Without a ceiling,
any new business-logic gap (capability matrix regression, area-label mismatch,
boundary trap, ...) lets the runner pickup → revert → pickup the same ticket
indefinitely — OP-1126/OP-1124 burned ~5–10万 tokens overnight 2026-05-15 in
exactly this loop.

The store is the ticket's own JIRA label set — two label families:

  ``runner-stoploss:revert-<compact-ISO>``         (one per revert)
  ``runner-stoploss:circuit-tripped-<compact-ISO>``  (added on trip)

The revert labels accumulate across runner restarts and across runner
processes/hosts (multi-tenant safe). The circuit-tripped label is the gate;
``pre_pickup_stoploss_ok`` refuses any ticket carrying one. Operators reset
by stripping the circuit-tripped label.

ISO format is ``%Y%m%dT%H%M%SZ`` (no colons) so the timestamp survives any
JIRA label-character normalisation without ambiguity, mirroring the existing
``claim:<instance>:<epoch_us>-<uuid>`` mutex label shape.

Tunables (operator may override via env):

  ``OMNISIGHT_STOPLOSS_WINDOW_MIN``  — default 15
  ``OMNISIGHT_STOPLOSS_THRESHOLD``   — default 3

Invalid env values fall back to the defaults so an operator typo never
silently disables the stoploss.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

log = logging.getLogger(__name__)

REVERT_LABEL_PREFIX = "runner-stoploss:revert-"
TRIPPED_LABEL_PREFIX = "runner-stoploss:circuit-tripped-"

WINDOW_MIN_ENV = "OMNISIGHT_STOPLOSS_WINDOW_MIN"
THRESHOLD_ENV = "OMNISIGHT_STOPLOSS_THRESHOLD"

DEFAULT_WINDOW_MIN = 15
DEFAULT_THRESHOLD = 3

_TS_FORMAT = "%Y%m%dT%H%M%SZ"


def current_config(env: Optional[dict] = None) -> tuple[int, int]:
    """Resolve ``(window_min, threshold)`` from env, falling back to defaults."""
    e = os.environ if env is None else env
    window_min = _coerce_positive_int(e.get(WINDOW_MIN_ENV), DEFAULT_WINDOW_MIN)
    threshold = _coerce_positive_int(e.get(THRESHOLD_ENV), DEFAULT_THRESHOLD)
    return window_min, threshold


def _coerce_positive_int(raw: Optional[str], default: int) -> int:
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def revert_label_for(now: datetime) -> str:
    return REVERT_LABEL_PREFIX + _format_ts(now)


def tripped_label_for(now: datetime) -> str:
    return TRIPPED_LABEL_PREFIX + _format_ts(now)


def _format_ts(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _parse_ts(suffix: str) -> Optional[datetime]:
    try:
        return datetime.strptime(suffix, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def count_recent_reverts(
    labels: Iterable[str],
    now: datetime,
    window_min: int,
) -> int:
    """Count ``revert-<ts>`` labels with timestamp inside the trailing window.

    A label whose timestamp falls exactly on the cutoff is counted in
    (inclusive lower bound). Labels with un-parseable timestamps are
    silently ignored — they cannot have come from this module.
    """
    cutoff_dt = now.astimezone(timezone.utc).timestamp() - window_min * 60
    n = 0
    for label in labels:
        if not isinstance(label, str) or not label.startswith(REVERT_LABEL_PREFIX):
            continue
        ts = _parse_ts(label[len(REVERT_LABEL_PREFIX):])
        if ts is None:
            continue
        if ts.timestamp() >= cutoff_dt:
            n += 1
    return n


def find_tripped_label(labels: Iterable[str]) -> Optional[str]:
    """Return the first ``circuit-tripped-*`` label, or ``None``."""
    for label in labels:
        if isinstance(label, str) and label.startswith(TRIPPED_LABEL_PREFIX):
            return label
    return None


def pre_pickup_stoploss_ok(labels: Iterable[str]) -> tuple[bool, str]:
    """Pre-pickup gate. Refuses any ticket carrying a circuit-tripped label."""
    tripped = find_tripped_label(labels)
    if tripped is not None:
        return False, f"runner_stoploss_circuit_tripped:{tripped}"
    return True, "stoploss-ok"


@dataclass(frozen=True)
class RevertOutcome:
    revert_label: str
    count: int
    tripped_now: bool
    tripped_label: Optional[str]


def register_revert(
    client,
    key: str,
    labels: Iterable[str],
    now: Optional[datetime] = None,
    *,
    window_min: Optional[int] = None,
    threshold: Optional[int] = None,
) -> RevertOutcome:
    """Record a §11-revert against ``key`` and trip the circuit if warranted.

    Side-effects on JIRA:
      * Always adds a ``runner-stoploss:revert-<ts>`` label.
      * When the trailing-window count (including this revert) reaches the
        threshold AND no ``circuit-tripped`` label is already present,
        adds a ``runner-stoploss:circuit-tripped-<ts>`` label and posts
        ONE terminal comment describing the trip.

    ``labels`` is the caller's snapshot of the ticket's current labels —
    the freshly-added revert label is appended in-memory for the counter
    so the caller doesn't have to re-fetch after the write.
    """
    from backend.agents import jira_dispatch  # local import; avoid cycle at module import

    if now is None:
        now = datetime.now(timezone.utc)
    if window_min is None or threshold is None:
        cfg_window, cfg_threshold = current_config()
        if window_min is None:
            window_min = cfg_window
        if threshold is None:
            threshold = cfg_threshold

    revert_label = revert_label_for(now)
    jira_dispatch.add_label(client, key, revert_label)

    labels_after = list(labels) + [revert_label]
    count = count_recent_reverts(labels_after, now, window_min)
    already_tripped = find_tripped_label(labels)

    log.info(
        "runner_stoploss.revert key=%s count=%d window_min=%d threshold=%d "
        "tripped_already=%s",
        key, count, window_min, threshold, already_tripped is not None,
    )

    if count >= threshold and already_tripped is None:
        tripped_label = tripped_label_for(now)
        jira_dispatch.add_label(client, key, tripped_label)
        jira_dispatch.add_comment(
            client,
            key,
            (
                f"[runner-stoploss] Circuit tripped: {count} §11-reverts in "
                f"the last {window_min} minutes (threshold={threshold}). "
                f"Further runner pickups of this ticket are refused.\n\n"
                f"Operator: strip label `{tripped_label}` to re-enable "
                f"pickup. Investigate the root cause before re-arming — a "
                f"repeating §11-revert usually signals a capability/area/"
                f"boundary gap that will keep tripping until fixed."
            ),
        )
        return RevertOutcome(
            revert_label=revert_label,
            count=count,
            tripped_now=True,
            tripped_label=tripped_label,
        )

    return RevertOutcome(
        revert_label=revert_label,
        count=count,
        tripped_now=False,
        tripped_label=already_tripped,
    )
