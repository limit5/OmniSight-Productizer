"""RPG.W21.2 -- boss-raid 14-day continuous engagement gate.

ADR 0008 defines W21 boss raids as time-gated large refactor tasks. The
W21.1 eligibility gate (see :mod:`backend.agents.boss_raid`) decides
*whether* a party may start a raid; this W21.2 module decides *whether
the raid is still inside its allowed completion window* at a given
moment. Together they bracket a raid's lifecycle: W21.1 at start,
W21.2 throughout.

The gate is intentionally backend-local and persistence-free: callers
pass the raid start time plus the check (or completion) time, and
receive a deterministic :class:`BossRaidTimeGate` record describing the
window state. Any storage, routing, or notification boundary downstream
can enforce the resulting status without re-deriving it.

Window semantics
----------------
The completion window is :data:`BOSS_RAID_COMPLETION_WINDOW_DAYS`
(14 days) long, measured from ``started_at`` to
``started_at + BOSS_RAID_COMPLETION_WINDOW``. The deadline is
*inclusive*: a raid that completes exactly at the 14-day mark is
``complete``; one nanosecond past is ``expired``. Three states are
possible (see :data:`BossRaidGateStatus`):

* ``open``     — ``checked_at`` is within the window and the caller
  has not signalled completion. The raid may continue accepting work.
* ``complete`` — ``checked_at`` is within the window and the caller
  passed ``completed=True``. The raid finished in time.
* ``expired``  — ``checked_at`` is past the deadline, regardless of
  ``completed``. A late completion still expires (this is intentional;
  the W21.2 gate is what the SLA contract enforces).

Public API
----------
* :func:`boss_raid_time_gate` — primary entry point; given raid id +
  start + check/completion timestamps, returns the full gate record.
* :func:`boss_raid_completed_in_window` — convenience predicate that
  collapses the record to a single bool for "did this raid complete in
  time?" callsites.
* :class:`BossRaidTimeGate` — frozen result dataclass with three
  status-shortcut properties (:attr:`~BossRaidTimeGate.is_open`,
  :attr:`~BossRaidTimeGate.is_complete`,
  :attr:`~BossRaidTimeGate.is_expired`).
* :data:`BOSS_RAID_COMPLETION_WINDOW_DAYS` / ``..._WINDOW`` — the
  14-day constant exposed in both ``int`` and :class:`~datetime.timedelta`
  forms so callers can choose the one that fits their math.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants and frozen dataclasses only. It
performs no database access, registry mutation, network I/O, or clock reads.
In particular, ``checked_at`` is *always* an argument — the module never
calls :func:`datetime.now`, so behaviour is fully reproducible from inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

BossRaidGateStatus = Literal["open", "complete", "expired"]
"""Discriminated string label for :class:`BossRaidTimeGate.status`.

See the module docstring's *Window semantics* section for the exact
transition rules. The literal set is closed; downstream code may rely
on exhaustive matching over these three values.
"""

BOSS_RAID_COMPLETION_WINDOW_DAYS = 14
"""W21.2 completion-window length in whole days (ADR 0008)."""

BOSS_RAID_COMPLETION_WINDOW = timedelta(days=BOSS_RAID_COMPLETION_WINDOW_DAYS)
""":class:`~datetime.timedelta` form of :data:`BOSS_RAID_COMPLETION_WINDOW_DAYS`.

Exposed separately so callers doing ``deadline = started_at + WINDOW``
arithmetic do not have to re-wrap the int each time, and so the day
count and the timedelta cannot drift out of sync.
"""


@dataclass(frozen=True)
class BossRaidTimeGate:
    """Computed W21.2 time-gate state for one boss raid.

    Returned by :func:`boss_raid_time_gate`. All :class:`~datetime.datetime`
    fields are timezone-aware and normalized to UTC by the constructor
    helper, so callers can compare them directly across raids regardless
    of the timezone the caller supplied.

    Fields
    ------
    raid_id:
        The caller-supplied raid identifier, stripped of surrounding
        whitespace. Round-tripping the input string is intentional so
        that log lines / metric labels emitted from the result remain
        keyable against the caller's raid registry.
    started_at:
        Raid start (UTC). Anchors the window; the deadline is derived
        from this plus :data:`BOSS_RAID_COMPLETION_WINDOW`.
    checked_at:
        The moment the gate was evaluated (UTC). For a completion call
        this is the completion timestamp.
    deadline_at:
        ``started_at + BOSS_RAID_COMPLETION_WINDOW`` (UTC). Stored on
        the record so downstream code does not have to recompute it.
    elapsed:
        ``checked_at - started_at``. May exceed the window (the gate
        does not clamp it — the ``status`` field carries the late
        signal). Always non-negative because the constructor rejects
        ``checked_at < started_at``.
    remaining:
        ``max(deadline_at - checked_at, 0)``. Clamped at zero so callers
        can safely render it as a countdown without checking sign.
    status:
        One of :data:`BossRaidGateStatus`. See the module docstring's
        *Window semantics* section for transition rules.
    window_days:
        The window length used for this evaluation. Defaults to
        :data:`BOSS_RAID_COMPLETION_WINDOW_DAYS`; recorded on the
        instance so a future per-raid override (if ADR 0008 is ever
        amended to allow them) round-trips through the gate without
        needing a callsite change.
    """

    raid_id: str
    started_at: datetime
    checked_at: datetime
    deadline_at: datetime
    elapsed: timedelta
    remaining: timedelta
    status: BossRaidGateStatus
    window_days: int = BOSS_RAID_COMPLETION_WINDOW_DAYS

    @property
    def is_open(self) -> bool:
        """Return whether the raid may continue accepting work.

        True iff ``status == "open"``: ``checked_at`` is within the
        window and the caller did not signal completion.
        """

        return self.status == "open"

    @property
    def is_complete(self) -> bool:
        """Return whether the raid completed inside the allowed window.

        True iff ``status == "complete"``. A late completion (one past
        the deadline) reports ``expired`` instead, so this property is
        the canonical "met the SLA" predicate.
        """

        return self.status == "complete"

    @property
    def is_expired(self) -> bool:
        """Return whether the raid exceeded the allowed completion window.

        True iff ``status == "expired"``: ``checked_at`` is strictly
        after ``deadline_at``, regardless of the ``completed`` flag.
        """

        return self.status == "expired"


def boss_raid_time_gate(
    *,
    raid_id: str,
    started_at: datetime,
    checked_at: datetime,
    completed: bool = False,
) -> BossRaidTimeGate:
    """Return W21.2 gate state for a boss raid at ``checked_at``.

    Primary entry point of this module. All arguments are keyword-only
    so callsites read self-documentingly at the boundary where the
    timestamps are most likely to be reordered by mistake.

    Parameters
    ----------
    raid_id:
        Caller-supplied raid identifier. Leading and trailing whitespace
        is stripped before being placed on the returned record; an empty
        or whitespace-only string raises :class:`ValueError`.
    started_at:
        Raid start. Must be a timezone-aware :class:`~datetime.datetime`;
        naive datetimes raise :class:`ValueError`. Normalized to UTC on
        the returned record.
    checked_at:
        Evaluation moment. Same timezone-aware contract as ``started_at``.
        Must be ``>= started_at`` — going backwards in time raises
        :class:`ValueError` because there is no meaningful gate state for
        a check that predates the raid.
    completed:
        Set to ``True`` to mark ``checked_at`` as the raid's completion
        timestamp. A raid may complete *exactly* at the 14-day deadline
        (``checked_at == deadline_at``) and still report ``complete``;
        any later timestamp reports ``expired`` regardless of this flag.

    Raises
    ------
    TypeError
        If ``raid_id`` is not a :class:`str`, or if either datetime is
        not a :class:`~datetime.datetime` instance, or if ``completed``
        is not a :class:`bool`.
    ValueError
        If ``raid_id`` is empty after stripping, if either datetime is
        timezone-naive, or if ``checked_at < started_at``.

    Returns
    -------
    BossRaidTimeGate
        Frozen record with ``elapsed`` / ``remaining`` / ``status``
        derived from the inputs. See the class docstring for field
        contracts.
    """

    clean_raid_id = _required("raid_id", raid_id)
    start = _clean_datetime("started_at", started_at)
    check = _clean_datetime("checked_at", checked_at)
    clean_completed = _clean_bool(completed, field="completed")
    if check < start:
        raise ValueError("checked_at must be >= started_at")

    deadline = start + BOSS_RAID_COMPLETION_WINDOW
    elapsed = check - start
    remaining = max(deadline - check, timedelta(0))
    status = _gate_status(check, deadline, completed=clean_completed)
    return BossRaidTimeGate(
        raid_id=clean_raid_id,
        started_at=start,
        checked_at=check,
        deadline_at=deadline,
        elapsed=elapsed,
        remaining=remaining,
        status=status,
    )


def boss_raid_completed_in_window(
    *,
    started_at: datetime,
    completed_at: datetime,
) -> bool:
    """Return whether a boss raid completed within the 14-day W21.2 gate.

    Convenience predicate for callsites that only need the SLA verdict
    and do not care about the full :class:`BossRaidTimeGate` record. It
    is implemented in terms of :func:`boss_raid_time_gate` with
    ``completed=True`` and a placeholder ``raid_id`` (so the caller does
    not have to invent one); the result is equivalent to
    ``boss_raid_time_gate(...).is_complete``.

    The same validation rules as :func:`boss_raid_time_gate` apply —
    both datetimes must be timezone-aware and ``completed_at`` must not
    precede ``started_at``; violations propagate as the same
    :class:`TypeError` / :class:`ValueError` types.
    """

    return boss_raid_time_gate(
        raid_id="completion-check",
        started_at=started_at,
        checked_at=completed_at,
        completed=True,
    ).is_complete


def _gate_status(
    checked_at: datetime,
    deadline_at: datetime,
    *,
    completed: bool,
) -> BossRaidGateStatus:
    """Resolve the three-state :data:`BossRaidGateStatus` discriminator.

    The expiry check is intentionally evaluated *before* the completion
    flag so that a late-completion call (``completed=True`` but past
    ``deadline_at``) still reports ``expired`` — that is the SLA contract
    documented in the module-level *Window semantics* section.
    """

    if checked_at > deadline_at:
        return "expired"
    if completed:
        return "complete"
    return "open"


def _clean_datetime(field: str, value: datetime) -> datetime:
    """Validate a public-API datetime argument and normalize it to UTC.

    Centralizes the timezone-aware contract: rejects non-datetime
    inputs (:class:`TypeError`) and timezone-naive datetimes
    (:class:`ValueError`), then converts to UTC so the comparison /
    subtraction math downstream is timezone-agnostic. The ``field``
    name is used only in the error message so the caller can tell which
    of ``started_at`` / ``checked_at`` failed validation.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _clean_bool(value: bool, *, field: str) -> bool:
    """Validate a public-API boolean flag without accepting truthy values."""

    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a bool")
    return value


def _required(field: str, value: str) -> str:
    """Validate a public-API non-empty string argument.

    Rejects non-strings (:class:`TypeError`) and strings that are empty
    or whitespace-only after :meth:`str.strip` (:class:`ValueError`),
    returning the stripped value so callers always see a canonical form.
    Used for :func:`boss_raid_time_gate`'s ``raid_id`` so accidental
    whitespace from upstream log parsing does not produce mismatched
    raid identifiers on the returned record.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} is required")
    return stripped


__all__ = [
    "BOSS_RAID_COMPLETION_WINDOW",
    "BOSS_RAID_COMPLETION_WINDOW_DAYS",
    "BossRaidGateStatus",
    "BossRaidTimeGate",
    "boss_raid_completed_in_window",
    "boss_raid_time_gate",
]
