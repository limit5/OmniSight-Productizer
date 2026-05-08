"""OP-807 (G5) — per-agent Tier-classification cooldown enforcement.

ADR-0005 §4 layer 4 protection: when an AI agent under-classifies a
patchset more than three times in a 30-day rolling window, it enters a
30-day cooldown during which the classifier force-promotes any of its
Tier S calls to Tier M. Repeat offences escalate (90-day cooldown,
then operator-revocation).

The module owns three things:

1. **Observation writer** (:func:`record_classification`) — every
   patchset upload that has both a *computed* tier (what the agent
   said) and an *override* tier (what the reviewer set) lands one row.
   Same rows that the cron's window scan reads.

2. **Daily sweep** (:func:`run_daily_sweep`) — counts overrides in the
   30 / 90 / 365-day windows, transitions agents between cooldown
   levels, and posts ``tier-cooldown:<level>`` labels via the same
   JIRA-add-label helper the rest of the bridge uses. Idempotent: a
   second run on the same day is a no-op (state row's
   ``last_evaluation_at`` skip-flag).

3. **Classifier integration hook** (:func:`apply_cooldown_to_tier`) —
   the future G2 classifier calls this with the agent_id + the
   path-derived tier. If the agent is in 30d/90d cooldown, any ``s``
   gets promoted to ``m``; ``revoked`` agents always get ``m`` (and
   the operator should be removing them from the bot pool by then).

Schema lives in alembic ``0205_tier_cooldown.py``. Two tables:

* ``tier_cooldown_observation`` — append-only event log
  ``(agent_id, classification_date, computed_tier, override_tier,
   ticket, change_id, patchset)``. The ``classification_date`` column
  is a ``DATE`` (not timestamp) so the window query is index-friendly
  on a daily granularity, which matches the cron cadence.

* ``tier_cooldown_state`` — one row per agent, mutated by the sweep.
  Carries the current ``cooldown_level`` (``none|30d|90d|revoked``),
  ``cooldown_started_at`` / ``cooldown_expires_at``, and a JSON
  ``cooldown_history`` array for the 3-in-365-day promotion-to-revoked
  rule.

Read-after-write timing audit
-----------------------------
The sweep does its writes inside a single transaction per agent — so
the state row and the JIRA label call only fire if the DB write
succeeded. The sweep emits one structured log line per state
transition for the operator dashboard tile feed.

Module-global / cross-worker state audit
----------------------------------------
No module-level cache. Every call resolves the DSN and opens its own
connection. Safe to call from the daily cron (one-shot) and from the
FastAPI router (per-request) without coordination.

SQLite path
-----------
For tests + local dev the same writers work via ``sqlite3``. The
``classification_date`` column is stored as ``TEXT`` ISO-8601 there
(SQLite's date model is text); the readers normalise both shapes.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ── Constants documented as module-level so the AC tests can pin them ─

# A misclassification = the reviewer had to *promote* the agent's
# computed tier (S→M, M→L, L→X). A reviewer who held the same tier or
# (impossibly) demoted it is not counted: the cooldown is for under-
# classification specifically.
TIER_RANK = {"s": 1, "m": 2, "l": 3, "x": 4}

# ADR-0005 §4 layer 4 thresholds. Centralised so the test suite (and a
# future operator override surface) can read the same numbers the cron
# uses.
WINDOW_30D_DAYS = 30
WINDOW_90D_DAYS = 90
WINDOW_365D_DAYS = 365
THRESHOLD_30D_OVERRIDES = 3      # > 3 overrides in 30d → 30d cooldown
THRESHOLD_90D_COOLDOWNS = 2      # ≥ 2 cooldowns in 90d → escalate to 90d
THRESHOLD_365D_COOLDOWNS = 3     # ≥ 3 cooldowns in 365d → revoked

VALID_TIERS = ("s", "m", "l", "x")
VALID_COOLDOWN_LEVELS = ("none", "30d", "90d", "revoked")


@dataclass(frozen=True)
class ClassificationRecord:
    """One row of ``tier_cooldown_observation``.

    ``classification_date`` is a *date*, not a datetime — the sweep
    window is daily-granular on purpose (a single contributor pushing
    five PSes in a minute should count as five misclassifications, not
    one, but reducing to date is fine for the 30-day window).
    """
    agent_id: str
    classification_date: date
    computed_tier: str
    override_tier: str
    ticket: str | None = None
    change_id: str | None = None
    patchset: int | None = None

    def is_misclassification(self) -> bool:
        """True if the reviewer had to *promote* the agent's call."""
        c = TIER_RANK.get(self.computed_tier.lower(), 0)
        o = TIER_RANK.get(self.override_tier.lower(), 0)
        return o > c


@dataclass
class CooldownState:
    """Current cooldown state for one agent."""
    agent_id: str
    cooldown_level: str = "none"
    cooldown_started_at: datetime | None = None
    cooldown_expires_at: datetime | None = None
    last_evaluation_at: datetime | None = None
    cooldown_history: list[dict[str, Any]] = field(default_factory=list)


# ── DSN/path resolution (mirrors backend.agents.conflict_observations) ─


def _resolve_pg_dsn() -> str | None:
    try:
        from backend.db_url import parse  # local import — keep tests light
    except ImportError:
        return None
    for key in ("OMNISIGHT_DATABASE_URL", "DATABASE_URL", "OMNI_TEST_PG_URL"):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = parse(raw)
        except Exception:
            continue
        if not parsed.is_postgres:
            continue
        return parsed.sqlalchemy_url(sync=True).replace(
            "postgresql+psycopg2://", "postgresql://", 1,
        )
    return None


def _resolve_sqlite_path() -> str | None:
    val = os.environ.get("OMNISIGHT_DATABASE_PATH", "").strip()
    if val:
        return val
    val = os.environ.get("OMNISIGHT_DATABASE_URL", "").strip()
    if val.startswith("sqlite:///"):
        return val[len("sqlite:///") :]
    return None


# ── Public writer ────────────────────────────────────────────────────


def record_classification(
    record: ClassificationRecord,
    *,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> bool:
    """Insert one observation row. Returns True on write, False on skip.

    Called by the Gerrit hook's audit step (after it knows both the
    agent's computed tier and the reviewer-set override). The function
    is permissive about same-tier rows on purpose: the cron filters by
    ``computed_tier != override_tier`` server-side, but keeping all
    rows lets the operator dashboard show the *full* override-attempt
    history (AC: "shows recent override attempts").
    """
    if record.computed_tier.lower() not in VALID_TIERS:
        logger.warning(
            "tier_cooldown_invalid_computed_tier value=%s agent=%s",
            record.computed_tier, record.agent_id,
        )
        return False
    if record.override_tier.lower() not in VALID_TIERS:
        logger.warning(
            "tier_cooldown_invalid_override_tier value=%s agent=%s",
            record.override_tier, record.agent_id,
        )
        return False

    resolved_pg = dsn or _resolve_pg_dsn()
    if resolved_pg:
        return _insert_pg(record, resolved_pg)
    resolved_sqlite = sqlite_path or _resolve_sqlite_path()
    if resolved_sqlite:
        return _insert_sqlite(record, resolved_sqlite)
    logger.info(
        "tier_cooldown_skipped_no_db agent=%s ticket=%s",
        record.agent_id, record.ticket,
    )
    return False


def _insert_pg(record: ClassificationRecord, dsn: str) -> bool:
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("tier_cooldown_skipped_no_psycopg2 agent=%s", record.agent_id)
        return False
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tier_cooldown_pg_connect_failed agent=%s err=%r",
            record.agent_id, exc,
        )
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tier_cooldown_observation (
                        agent_id, classification_date, computed_tier,
                        override_tier, ticket, change_id, patchset
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.agent_id,
                        record.classification_date,
                        record.computed_tier.lower(),
                        record.override_tier.lower(),
                        record.ticket,
                        record.change_id,
                        record.patchset,
                    ),
                )
    finally:
        conn.close()
    return True


def _insert_sqlite(record: ClassificationRecord, path: str) -> bool:
    with sqlite3.connect(path, timeout=5) as conn:
        conn.execute(
            """
            INSERT INTO tier_cooldown_observation (
                agent_id, classification_date, computed_tier,
                override_tier, ticket, change_id, patchset
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.agent_id,
                record.classification_date.isoformat(),
                record.computed_tier.lower(),
                record.override_tier.lower(),
                record.ticket,
                record.change_id,
                record.patchset,
            ),
        )
        conn.commit()
    return True


# ── Public reader (used by the cron + the dashboard router) ──────────


def fetch_overrides_in_window(
    *,
    agent_id: str,
    since: date,
    until: date,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> list[ClassificationRecord]:
    """All observations for ``agent_id`` with date in ``[since, until)``.

    Date-half-open so ``until`` of "today" excludes today's still-fresh
    rows that the cron hasn't decided about yet (the cron runs at
    early-UTC and reads up to the previous midnight).
    """
    resolved_pg = dsn or _resolve_pg_dsn()
    if resolved_pg:
        return _select_pg(agent_id, since, until, resolved_pg)
    resolved_sqlite = sqlite_path or _resolve_sqlite_path()
    if resolved_sqlite:
        return _select_sqlite(agent_id, since, until, resolved_sqlite)
    return []


def _select_pg(
    agent_id: str, since: date, until: date, dsn: str,
) -> list[ClassificationRecord]:
    import psycopg2  # type: ignore[import-not-found]
    out: list[ClassificationRecord] = []
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT agent_id, classification_date, computed_tier,
                       override_tier, ticket, change_id, patchset
                  FROM tier_cooldown_observation
                 WHERE agent_id = %s
                   AND classification_date >= %s
                   AND classification_date < %s
                 ORDER BY classification_date ASC, id ASC
                """,
                (agent_id, since, until),
            )
            for row in cur.fetchall():
                out.append(_row_to_record(row))
    finally:
        conn.close()
    return out


def _select_sqlite(
    agent_id: str, since: date, until: date, path: str,
) -> list[ClassificationRecord]:
    out: list[ClassificationRecord] = []
    with sqlite3.connect(path, timeout=5) as conn:
        cur = conn.execute(
            """
            SELECT agent_id, classification_date, computed_tier,
                   override_tier, ticket, change_id, patchset
              FROM tier_cooldown_observation
             WHERE agent_id = ?
               AND classification_date >= ?
               AND classification_date < ?
             ORDER BY classification_date ASC, id ASC
            """,
            (agent_id, since.isoformat(), until.isoformat()),
        )
        for row in cur.fetchall():
            out.append(_row_to_record(row))
    return out


def _row_to_record(row: Iterable[Any]) -> ClassificationRecord:
    seq = list(row)
    cd = seq[1]
    if isinstance(cd, str):
        cd_parsed = date.fromisoformat(cd[:10])
    elif isinstance(cd, date):
        cd_parsed = cd
    else:
        cd_parsed = date.today()
    return ClassificationRecord(
        agent_id=str(seq[0]),
        classification_date=cd_parsed,
        computed_tier=str(seq[2]),
        override_tier=str(seq[3]),
        ticket=seq[4] if seq[4] else None,
        change_id=seq[5] if seq[5] else None,
        patchset=int(seq[6]) if seq[6] is not None else None,
    )


# ── Per-agent state read/write ───────────────────────────────────────


def get_state(
    agent_id: str,
    *,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> CooldownState:
    """Return the agent's current cooldown state (default ``none``)."""
    resolved_pg = dsn or _resolve_pg_dsn()
    if resolved_pg:
        return _get_state_pg(agent_id, resolved_pg)
    resolved_sqlite = sqlite_path or _resolve_sqlite_path()
    if resolved_sqlite:
        return _get_state_sqlite(agent_id, resolved_sqlite)
    return CooldownState(agent_id=agent_id)


def _get_state_pg(agent_id: str, dsn: str) -> CooldownState:
    import psycopg2  # type: ignore[import-not-found]
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT agent_id, cooldown_level, cooldown_started_at,
                       cooldown_expires_at, last_evaluation_at,
                       cooldown_history
                  FROM tier_cooldown_state
                 WHERE agent_id = %s
                """,
                (agent_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return CooldownState(agent_id=agent_id)
    return _row_to_state(row)


def _get_state_sqlite(agent_id: str, path: str) -> CooldownState:
    with sqlite3.connect(path, timeout=5) as conn:
        cur = conn.execute(
            """
            SELECT agent_id, cooldown_level, cooldown_started_at,
                   cooldown_expires_at, last_evaluation_at,
                   cooldown_history
              FROM tier_cooldown_state
             WHERE agent_id = ?
            """,
            (agent_id,),
        )
        row = cur.fetchone()
    if not row:
        return CooldownState(agent_id=agent_id)
    return _row_to_state(row)


def _row_to_state(row: Iterable[Any]) -> CooldownState:
    seq = list(row)
    history_raw = seq[5]
    if isinstance(history_raw, str) and history_raw:
        try:
            history = json.loads(history_raw)
        except json.JSONDecodeError:
            history = []
    elif isinstance(history_raw, list):
        history = history_raw
    else:
        history = []
    return CooldownState(
        agent_id=str(seq[0]),
        cooldown_level=str(seq[1] or "none"),
        cooldown_started_at=_parse_dt(seq[2]),
        cooldown_expires_at=_parse_dt(seq[3]),
        last_evaluation_at=_parse_dt(seq[4]),
        cooldown_history=list(history),
    )


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def upsert_state(
    state: CooldownState,
    *,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> None:
    """Persist the (possibly transitioned) state row."""
    resolved_pg = dsn or _resolve_pg_dsn()
    if resolved_pg:
        _upsert_state_pg(state, resolved_pg)
        return
    resolved_sqlite = sqlite_path or _resolve_sqlite_path()
    if resolved_sqlite:
        _upsert_state_sqlite(state, resolved_sqlite)
        return
    logger.info(
        "tier_cooldown_state_skipped_no_db agent=%s level=%s",
        state.agent_id, state.cooldown_level,
    )


def _upsert_state_pg(state: CooldownState, dsn: str) -> None:
    import psycopg2  # type: ignore[import-not-found]
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tier_cooldown_state (
                        agent_id, cooldown_level, cooldown_started_at,
                        cooldown_expires_at, last_evaluation_at,
                        cooldown_history
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (agent_id) DO UPDATE SET
                        cooldown_level      = EXCLUDED.cooldown_level,
                        cooldown_started_at = EXCLUDED.cooldown_started_at,
                        cooldown_expires_at = EXCLUDED.cooldown_expires_at,
                        last_evaluation_at  = EXCLUDED.last_evaluation_at,
                        cooldown_history    = EXCLUDED.cooldown_history
                    """,
                    (
                        state.agent_id,
                        state.cooldown_level,
                        state.cooldown_started_at,
                        state.cooldown_expires_at,
                        state.last_evaluation_at,
                        json.dumps(state.cooldown_history),
                    ),
                )
    finally:
        conn.close()


def _upsert_state_sqlite(state: CooldownState, path: str) -> None:
    with sqlite3.connect(path, timeout=5) as conn:
        conn.execute(
            """
            INSERT INTO tier_cooldown_state (
                agent_id, cooldown_level, cooldown_started_at,
                cooldown_expires_at, last_evaluation_at,
                cooldown_history
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                cooldown_level      = excluded.cooldown_level,
                cooldown_started_at = excluded.cooldown_started_at,
                cooldown_expires_at = excluded.cooldown_expires_at,
                last_evaluation_at  = excluded.last_evaluation_at,
                cooldown_history    = excluded.cooldown_history
            """,
            (
                state.agent_id,
                state.cooldown_level,
                _to_iso(state.cooldown_started_at),
                _to_iso(state.cooldown_expires_at),
                _to_iso(state.last_evaluation_at),
                json.dumps(state.cooldown_history),
            ),
        )
        conn.commit()


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ── Sweep — daily cooldown evaluation (the heart of layer 4) ─────────


@dataclass(frozen=True)
class SweepOutcome:
    """One cron-run summary row, returned to the caller for logging."""
    agent_id: str
    misclass_30d: int
    cooldowns_in_90d: int
    cooldowns_in_365d: int
    previous_level: str
    new_level: str
    transitioned: bool


def list_distinct_agents(
    *,
    since: date,
    until: date,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> list[str]:
    """All agent_ids with at least one observation in the window."""
    resolved_pg = dsn or _resolve_pg_dsn()
    if resolved_pg:
        return _list_agents_pg(since, until, resolved_pg)
    resolved_sqlite = sqlite_path or _resolve_sqlite_path()
    if resolved_sqlite:
        return _list_agents_sqlite(since, until, resolved_sqlite)
    return []


def _list_agents_pg(since: date, until: date, dsn: str) -> list[str]:
    import psycopg2  # type: ignore[import-not-found]
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT agent_id
                  FROM tier_cooldown_observation
                 WHERE classification_date >= %s
                   AND classification_date < %s
                 ORDER BY agent_id
                """,
                (since, until),
            )
            return [str(row[0]) for row in cur.fetchall()]
    finally:
        conn.close()


def _list_agents_sqlite(since: date, until: date, path: str) -> list[str]:
    with sqlite3.connect(path, timeout=5) as conn:
        cur = conn.execute(
            """
            SELECT DISTINCT agent_id
              FROM tier_cooldown_observation
             WHERE classification_date >= ?
               AND classification_date < ?
             ORDER BY agent_id
            """,
            (since.isoformat(), until.isoformat()),
        )
        return [str(row[0]) for row in cur.fetchall()]


def evaluate_agent(
    agent_id: str,
    *,
    now: datetime,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> SweepOutcome:
    """Evaluate one agent's window counts and transition its state.

    Pure of *labelling side-effects* — the JIRA-label call lives in
    :func:`run_daily_sweep` so this helper is unit-testable without
    network. Persists the new state row before returning.
    """
    today = now.date()
    window_30d_start = today - timedelta(days=WINDOW_30D_DAYS)
    window_90d_start = today - timedelta(days=WINDOW_90D_DAYS)
    window_365d_start = today - timedelta(days=WINDOW_365D_DAYS)

    # 30-day misclassification count.
    obs_30d = fetch_overrides_in_window(
        agent_id=agent_id, since=window_30d_start, until=today,
        dsn=dsn, sqlite_path=sqlite_path,
    )
    misclass_30d = sum(1 for o in obs_30d if o.is_misclassification())

    state = get_state(agent_id, dsn=dsn, sqlite_path=sqlite_path)
    previous_level = state.cooldown_level

    # Past-cooldown counts from history (excluding currently-active one).
    cooldowns_in_90d = _count_history(
        state.cooldown_history, since=window_90d_start, today=today,
    )
    cooldowns_in_365d = _count_history(
        state.cooldown_history, since=window_365d_start, today=today,
    )

    # Decide the new level. Order matters — revoked is sticky;
    # 90d wins over 30d once it triggers; 30d is the entry point.
    new_level = previous_level

    # Has the active cooldown expired? Step it down.
    if state.cooldown_expires_at and state.cooldown_expires_at <= now \
            and previous_level in ("30d", "90d"):
        new_level = "none"
        # Don't clear started_at yet — we still need it for window
        # counts above. Just unset the active window markers.
        state.cooldown_started_at = None
        state.cooldown_expires_at = None

    # Fresh evaluation: only re-trigger if the agent isn't already in a
    # higher-or-equal active cooldown.
    just_triggered = False
    if misclass_30d > THRESHOLD_30D_OVERRIDES and new_level == "none":
        # Fresh 30d cooldown — but escalate if recent history qualifies.
        if cooldowns_in_365d + 1 >= THRESHOLD_365D_COOLDOWNS:
            new_level = "revoked"
            expires = None  # operator-only lift
        elif cooldowns_in_90d + 1 >= THRESHOLD_90D_COOLDOWNS:
            new_level = "90d"
            expires = now + timedelta(days=WINDOW_90D_DAYS)
        else:
            new_level = "30d"
            expires = now + timedelta(days=WINDOW_30D_DAYS)
        state.cooldown_started_at = now
        state.cooldown_expires_at = expires
        state.cooldown_history = list(state.cooldown_history) + [
            {
                "started_at": now.isoformat(),
                "level": new_level,
                "misclass_count": misclass_30d,
            }
        ]
        just_triggered = True

    state.cooldown_level = new_level
    state.last_evaluation_at = now
    upsert_state(state, dsn=dsn, sqlite_path=sqlite_path)

    return SweepOutcome(
        agent_id=agent_id,
        misclass_30d=misclass_30d,
        cooldowns_in_90d=cooldowns_in_90d,
        cooldowns_in_365d=cooldowns_in_365d,
        previous_level=previous_level,
        new_level=new_level,
        transitioned=just_triggered or (previous_level != new_level),
    )


def _count_history(
    history: list[dict[str, Any]],
    *,
    since: date,
    today: date,
) -> int:
    """Count past-cooldown entries with ``started_at`` date in window."""
    n = 0
    for entry in history:
        raw = entry.get("started_at") if isinstance(entry, dict) else None
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        d = dt.date()
        if since <= d < today:
            n += 1
    return n


def run_daily_sweep(
    *,
    now: datetime | None = None,
    label_callback: Optional[Any] = None,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> list[SweepOutcome]:
    """Top-level cron entrypoint. Returns one outcome per evaluated agent.

    ``label_callback`` is an optional ``(agent_id, level, ticket) -> None``
    hook the cron uses to write the ``tier-cooldown:<level>`` JIRA
    label. Tests pass a no-op or a recorder to verify the label call
    without hitting JIRA.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    # Evaluate every agent that has been active in the trailing 90d
    # (90d is enough to cover state-stepdown for the longest active
    # cooldown level we issue without operator action).
    window_start = today - timedelta(days=WINDOW_90D_DAYS)
    agents = list_distinct_agents(
        since=window_start, until=today + timedelta(days=1),
        dsn=dsn, sqlite_path=sqlite_path,
    )
    # Plus any agent already in a non-``none`` state (so a quiet agent
    # whose cooldown expires today still gets a step-down evaluation).
    extra = _agents_with_active_state(dsn=dsn, sqlite_path=sqlite_path)
    seen = set(agents)
    for a in extra:
        if a not in seen:
            agents.append(a)
            seen.add(a)

    outcomes: list[SweepOutcome] = []
    for agent_id in agents:
        outcome = evaluate_agent(
            agent_id, now=now, dsn=dsn, sqlite_path=sqlite_path,
        )
        outcomes.append(outcome)
        if outcome.transitioned and outcome.new_level in ("30d", "90d", "revoked"):
            logger.info(
                "tier_cooldown_transition agent=%s level=%s misclass_30d=%d",
                outcome.agent_id, outcome.new_level, outcome.misclass_30d,
            )
            if callable(label_callback):
                try:
                    label_callback(outcome.agent_id, outcome.new_level, None)
                except Exception as exc:  # noqa: BLE001
                    # Sweep must finish even if labelling fails; the
                    # next run will re-detect via state diff.
                    logger.warning(
                        "tier_cooldown_label_callback_failed agent=%s err=%r",
                        outcome.agent_id, exc,
                    )
    return outcomes


def _agents_with_active_state(
    *,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> list[str]:
    resolved_pg = dsn or _resolve_pg_dsn()
    if resolved_pg:
        import psycopg2  # type: ignore[import-not-found]
        conn = psycopg2.connect(resolved_pg, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT agent_id FROM tier_cooldown_state
                     WHERE cooldown_level <> 'none'
                     ORDER BY agent_id
                    """,
                )
                return [str(row[0]) for row in cur.fetchall()]
        finally:
            conn.close()
    resolved_sqlite = sqlite_path or _resolve_sqlite_path()
    if resolved_sqlite:
        with sqlite3.connect(resolved_sqlite, timeout=5) as conn:
            cur = conn.execute(
                """
                SELECT agent_id FROM tier_cooldown_state
                 WHERE cooldown_level <> 'none'
                 ORDER BY agent_id
                """,
            )
            return [str(row[0]) for row in cur.fetchall()]
    return []


# ── Classifier integration hook ──────────────────────────────────────


def apply_cooldown_to_tier(
    agent_id: str,
    computed_tier: str,
    *,
    now: datetime | None = None,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> str:
    """Force-promote ``computed_tier`` if the agent is in cooldown.

    G2 (the path classifier) calls this on every classification call.
    Rules (ADR-0005 §4 layer 4):

    * ``none`` → return ``computed_tier`` unchanged.
    * ``30d`` / ``90d`` → if expired, return unchanged; otherwise
      bump ``s`` to ``m``. Tiers ≥ M are unaffected (the cooldown is
      protection against under-classification, not a tax on legitimate
      Tier S work).
    * ``revoked`` → always return ``m`` (operator must remove agent
      from the bot pool to escape; this is a backstop, not the gate).

    The function is read-only; it does not advance the state machine.
    """
    state = get_state(agent_id, dsn=dsn, sqlite_path=sqlite_path)
    now_dt = now or datetime.now(timezone.utc)

    if state.cooldown_level == "revoked":
        return _max_tier(computed_tier, "m")
    if state.cooldown_level in ("30d", "90d"):
        if state.cooldown_expires_at and state.cooldown_expires_at <= now_dt:
            return computed_tier
        return _max_tier(computed_tier, "m")
    return computed_tier


def _max_tier(a: str, b: str) -> str:
    ra = TIER_RANK.get(a.lower(), 0)
    rb = TIER_RANK.get(b.lower(), 0)
    return a.lower() if ra >= rb else b.lower()
