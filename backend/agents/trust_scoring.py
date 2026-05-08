"""OP-735 -- per-(bot, file_class) trust scoring for AI Reviewer auto-+1.

R5 only auto-+1's when historical accuracy of that ``(bot, file_class)``
tuple's batch-merge candidates passes a threshold. Threshold logic
mirrors the spec in OP-735:

  - fewer than 5 observations  →  ``True`` (default trust during ramp-up)
  - failures / total < 10 %     →  ``True``
  - otherwise                   →  ``False`` (auto-+1 disabled, fallback
                                    to Phase-1 AI Reviewer behaviour)

A ``failure`` means: the AI auto-+1'd PS merged, then a follow-up bug
ticket was filed within 7 days that referenced or reverted the patch.
A ``success`` means: it merged and 7 days passed clean. The retro
classifier lives in ``record_outcome()``; the actual scan is wired by
the JIRA bug-tag webhook (out of scope for OP-735 but the schema
supports it).

Persistence
-----------
The ``trust_scores`` table is created by
``backend/alembic/versions/0202_trust_scores.py``. PG is the source of
truth in production; an in-memory shim (``InMemoryTrustStore``) is
provided for unit tests so the gate logic can be exercised without a
DB connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


logger = logging.getLogger(__name__)


# ── File-class classifier ────────────────────────────────────────────


# Match prefixes — order matters; first hit wins. The classifier is
# intentionally coarse: trust scoring tracks "is this bot reliable on
# docs vs. backend vs. frontend", not per-file accuracy. Keeping
# classes coarse means the (successes, failures) counters accumulate
# enough signal to clear the 5-observation ramp-up gate in reasonable
# time.
_FILE_CLASS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("docs/", "docs"),
    ("test/", "tests"),
    ("tests/", "tests"),
    ("backend/tests/", "tests"),
    ("backend/agents/", "backend-agents"),
    ("backend/routers/", "backend-routers"),
    ("backend/", "backend"),
    ("components/", "frontend-components"),
    ("app/", "frontend-app"),
    ("lib/", "frontend-lib"),
    ("hooks/", "frontend-hooks"),
    ("i18n/", "frontend-i18n"),
)


def classify_file_class(path: str) -> str:
    """Map a file path to one of the coarse buckets used for trust
    scoring. Unknown paths fall into ``"other"``.

    Pure function, no I/O — safe to call from the webhook handler or
    tests."""
    for prefix, klass in _FILE_CLASS_PREFIXES:
        if path.startswith(prefix):
            return klass
    return "other"


def dominant_file_class(paths: list[str]) -> str:
    """Pick the file class that appears most often in ``paths``.

    Used at gate-time: a single change can touch multiple file classes
    but the trust score is per-(bot, class), so we score against the
    *dominant* class. Ties resolve to the first-seen class (stable for
    deterministic tests).
    """
    if not paths:
        return "other"
    counts: dict[str, int] = {}
    order: list[str] = []
    for p in paths:
        if p == "/COMMIT_MSG":
            continue
        klass = classify_file_class(p)
        if klass not in counts:
            order.append(klass)
        counts[klass] = counts.get(klass, 0) + 1
    if not counts:
        return "other"
    # Sort by (-count, original-order) for stable winners.
    return min(order, key=lambda k: (-counts[k], order.index(k)))


# ── TrustScore data model ────────────────────────────────────────────


@dataclass
class TrustScore:
    bot: str
    file_class: str
    successes: int = 0
    failures: int = 0
    last_updated: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def failure_rate(self) -> float:
        return (self.failures / self.total) if self.total else 0.0


# OP-735 spec literals — kept as module constants so callers and tests
# share the same source of truth.
TRUST_SCORE_RAMP_UP_OBSERVATIONS = 5
TRUST_SCORE_FAILURE_THRESHOLD = 0.10


def trust_score_ok(score: TrustScore) -> bool:
    """Pure decision: does this trust score allow auto-+1?

    See module docstring for the rules. Pure function so tests can
    construct a TrustScore directly without a store."""
    if score.total < TRUST_SCORE_RAMP_UP_OBSERVATIONS:
        return True
    return score.failure_rate < TRUST_SCORE_FAILURE_THRESHOLD


# ── Storage protocol + in-memory shim ────────────────────────────────


class TrustScoreStore(Protocol):
    async def get(self, bot: str, file_class: str) -> TrustScore: ...
    async def record_outcome(
        self, bot: str, file_class: str, *, success: bool
    ) -> TrustScore: ...


class InMemoryTrustStore:
    """Test/unit shim. Not used in production; ``PgTrustScoreStore`` is
    the production implementation backed by the ``trust_scores`` table
    from alembic 0202."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], TrustScore] = {}

    async def get(self, bot: str, file_class: str) -> TrustScore:
        key = (bot, file_class)
        if key not in self._rows:
            self._rows[key] = TrustScore(bot=bot, file_class=file_class)
        return self._rows[key]

    async def record_outcome(
        self, bot: str, file_class: str, *, success: bool
    ) -> TrustScore:
        score = await self.get(bot, file_class)
        if success:
            score.successes += 1
        else:
            score.failures += 1
        score.last_updated = datetime.now(timezone.utc)
        return score


class PgTrustScoreStore:
    """Production store backed by the ``trust_scores`` PG table.

    Schema (alembic 0202):
        bot           TEXT NOT NULL
        file_class    TEXT NOT NULL
        successes     INTEGER NOT NULL DEFAULT 0
        failures      INTEGER NOT NULL DEFAULT 0
        last_updated  TIMESTAMPTZ NOT NULL DEFAULT now()
        PRIMARY KEY (bot, file_class)
    """

    _GET_SQL = (
        "SELECT bot, file_class, successes, failures, last_updated "
        "FROM trust_scores WHERE bot = $1 AND file_class = $2"
    )
    _UPSERT_SQL = (
        "INSERT INTO trust_scores (bot, file_class, successes, failures, last_updated) "
        "VALUES ($1, $2, $3, $4, now()) "
        "ON CONFLICT (bot, file_class) DO UPDATE SET "
        "  successes = trust_scores.successes + EXCLUDED.successes, "
        "  failures = trust_scores.failures + EXCLUDED.failures, "
        "  last_updated = now() "
        "RETURNING bot, file_class, successes, failures, last_updated"
    )

    async def get(self, bot: str, file_class: str) -> TrustScore:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(self._GET_SQL, bot, file_class)
        if row is None:
            return TrustScore(bot=bot, file_class=file_class)
        return TrustScore(
            bot=row["bot"],
            file_class=row["file_class"],
            successes=row["successes"],
            failures=row["failures"],
            last_updated=row["last_updated"],
        )

    async def record_outcome(
        self, bot: str, file_class: str, *, success: bool
    ) -> TrustScore:
        from backend.db_pool import get_pool
        delta_s = 1 if success else 0
        delta_f = 0 if success else 1
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                self._UPSERT_SQL, bot, file_class, delta_s, delta_f,
            )
        return TrustScore(
            bot=row["bot"],
            file_class=row["file_class"],
            successes=row["successes"],
            failures=row["failures"],
            last_updated=row["last_updated"],
        )


# Module-level singleton for callers that want the production store
# without threading config through. Tests should construct
# ``InMemoryTrustStore`` directly and pass it to the helper rather
# than relying on this singleton, so they don't touch PG.
_default_store: TrustScoreStore | None = None


def get_default_store() -> TrustScoreStore:
    global _default_store
    if _default_store is None:
        _default_store = PgTrustScoreStore()
    return _default_store


def set_default_store(store: TrustScoreStore | None) -> None:
    """Override the module-default store (used by tests / DI)."""
    global _default_store
    _default_store = store
