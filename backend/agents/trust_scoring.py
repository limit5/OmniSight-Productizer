"""OP-735 / OP-756 -- per-(bot, file_class) trust scoring for AI Reviewer.

OP-735 introduced a binary trust gate (auto-+1 on / off). OP-756 replaces
that with a 4-tier graceful-degradation ladder so a single failure
doesn't permanently disable the auto-flow:

  - score >= 0.95              →  AUTO     (auto-+1 + batch-merge hashtag)
  - 0.80 <= score < 0.95       →  GLANCE   (+1 + glance-required hashtag)
  - 0.50 <= score < 0.80       →  COMMENT  (comment-only, no +1)
  - score < 0.50               →  DISABLED (AI Reviewer skipped)

``score`` is success_rate (= 1 - failure_rate). During ramp-up
(< 5 observations) the score defaults to 1.0 so a new (bot, file_class)
tuple starts in AUTO tier and earns its real score with use.

A ``failure`` means: the AI's vote/tag turned out wrong (e.g., the
auto-+1'd PS merged, then a follow-up bug ticket was filed within 7
days that referenced or reverted the patch). A ``success`` means: it
merged and 7 days passed clean. The retro classifier lives in
``record_outcome()``; the actual scan is wired by the JIRA bug-tag
webhook.

Recovery / decay
----------------
The cumulative-counter model gives natural recovery: each new success
shrinks ``failures / total``, climbing the score back up. We do *not*
do per-observation time-decay — that would need timestamp tracking per
outcome, which is out of scope (would require a DB schema change).

Operator pin
------------
For debugging, an operator can pin the tier via a Gerrit hashtag
``runner-trust-tier=<auto|glance|comment|disabled>`` on the change.
``parse_pinned_tier`` reads the change's hashtags and the resolver
honours the override.

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
from enum import Enum
from typing import Iterable, Protocol


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


# OP-735 / OP-756 spec literals — kept as module constants so callers
# and tests share the same source of truth.
TRUST_SCORE_RAMP_UP_OBSERVATIONS = 5

# OP-756 tier lower bounds (success_rate). Each tier is [bound, next_bound).
TIER_THRESHOLD_AUTO = 0.95
TIER_THRESHOLD_GLANCE = 0.80
TIER_THRESHOLD_COMMENT = 0.50

# Operator override hashtag prefix (Gerrit set-hashtags target). Value is
# one of ``auto``, ``glance``, ``comment``, ``disabled``.
TIER_PIN_HASHTAG_PREFIX = "runner-trust-tier="


class TrustTier(str, Enum):
    """OP-756 graceful-degradation tier for AI Reviewer auto-+1.

    Subclassing ``str`` so the enum can be JSON-encoded into the
    BatchMergeCandidate snapshot and the dashboard payload without an
    explicit converter.
    """

    AUTO = "auto"
    GLANCE = "glance"
    COMMENT = "comment"
    DISABLED = "disabled"


def trust_score_value(score: TrustScore) -> float:
    """Trust score in [0.0, 1.0]; higher = more reliable.

    During ramp-up returns 1.0 so a brand-new (bot, file_class) pair
    starts in AUTO tier and earns a real score from observations.
    """
    if score.total < TRUST_SCORE_RAMP_UP_OBSERVATIONS:
        return 1.0
    return 1.0 - score.failure_rate


def trust_tier(
    score: TrustScore, *, pinned: TrustTier | None = None,
) -> TrustTier:
    """Map a TrustScore to its OP-756 degradation tier.

    ``pinned`` (operator's ``runner-trust-tier=<value>`` hashtag, parsed
    via :func:`parse_pinned_tier`) bypasses the score-based assignment.
    Pure function: no I/O, deterministic, suitable for the webhook fast
    path.
    """
    if pinned is not None:
        return pinned
    s = trust_score_value(score)
    if s >= TIER_THRESHOLD_AUTO:
        return TrustTier.AUTO
    if s >= TIER_THRESHOLD_GLANCE:
        return TrustTier.GLANCE
    if s >= TIER_THRESHOLD_COMMENT:
        return TrustTier.COMMENT
    return TrustTier.DISABLED


def trust_score_ok(score: TrustScore) -> bool:
    """Backward-compat: True iff the (bot, file_class) clears the AUTO
    tier — i.e. eligible for the OP-735 auto-+1 batch-merge hashtag.
    """
    return trust_tier(score) == TrustTier.AUTO


def parse_pinned_tier(hashtags: Iterable[str] | None) -> TrustTier | None:
    """Extract the operator's pinned tier from a change's hashtags.

    Looks for ``runner-trust-tier=<tier>`` (case-insensitive on the
    value half). Returns ``None`` if no pin is set or the value is
    unrecognized — the resolver then falls back to the score-derived
    tier.
    """
    if not hashtags:
        return None
    for tag in hashtags:
        if not tag or not tag.startswith(TIER_PIN_HASHTAG_PREFIX):
            continue
        value = tag[len(TIER_PIN_HASHTAG_PREFIX):].strip().lower()
        for tier in TrustTier:
            if tier.value == value:
                return tier
    return None


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
