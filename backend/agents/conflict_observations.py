"""OP-746 -- conflict observation writer + reader helpers.

Centralises writes to the ``conflict_observations`` table (alembic 0203)
so the Gerrit/JIRA bridge daemon and the auto-rebase sweeper can record
conflict events through one well-typed surface. The daily report
(``scripts/conflict_report.py``) reads from the same module so the
column shape stays in lock-step with the writers.

Why a thin module instead of inline SQL in the daemon
-----------------------------------------------------
The bridge runs as a long-lived sync process; the rest of the backend
exposes Postgres through ``backend.db_pool`` (asyncpg). Mixing the two
in a single sync caller path is awkward, so this module offers BOTH
a sync writer (used by the bridge, opens a per-call connection via
``psycopg`` if available, falls back to a no-op when the daemon runs
without the optional dep) AND an async reader (used by FastAPI routers
on the asyncpg pool). The two share one schema-shaped record dataclass.

SQLite path (tests + local dev)
-------------------------------
``files_in_conflict`` is JSON-encoded text on SQLite because SQLite has
no native array type; the reader normalises both shapes back to
``list[str]``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)


VALID_CAUSE_CATEGORIES = frozenset({
    "sibling_merged",
    "staging_regression",
    "verified_minus_one",
    "manual_rebase",
})


@dataclass(frozen=True)
class ConflictObservation:
    """One row of ``conflict_observations``. ``ts`` is UTC."""

    ts: datetime
    cause_category: str
    files_in_conflict: tuple[str, ...] = ()
    ps_change_id: str | None = None
    ps_change_number: int | None = None
    ticket: str | None = None
    pre_existing_open_count: int = 0


# ── Writer (sync; used by the bridge daemon) ─────────────────────────


def record_observation_sync(
    obs: ConflictObservation,
    *,
    dsn: str | None = None,
    log: Any = None,
) -> bool:
    """Insert one observation row. Returns True on write, False on skip.

    The bridge calls this from its own process, where asyncpg is awkward
    (would require an event loop). We use ``psycopg`` (v3) when present;
    if neither dep nor DSN is available the call becomes a logged no-op
    so the daemon keeps running. The daily report script can also fall
    back to its SQLite path when ``OMNISIGHT_DATABASE_URL`` points to a
    SQLite file.

    The optional ``log`` argument matches the daemon's
    ``structured_log``-style ``(level, event, **kwargs)`` callable so
    the writer's outcome reaches the same log stream as the rest of the
    bridge.
    """
    emit = log if callable(log) else _structlog_noop

    if obs.cause_category not in VALID_CAUSE_CATEGORIES:
        emit(
            "WARN", "conflict_observation_invalid_cause",
            cause=obs.cause_category,
        )
        return False

    resolved_dsn = dsn or _resolve_pg_dsn()
    sqlite_path = _resolve_sqlite_path() if not resolved_dsn else None

    if resolved_dsn:
        return _insert_pg(obs, resolved_dsn, emit)
    if sqlite_path:
        return _insert_sqlite(obs, sqlite_path, emit)

    emit(
        "WARN", "conflict_observation_skipped_no_db",
        cause=obs.cause_category,
        ps_change_number=obs.ps_change_number,
    )
    return False


# ── Async reader (used by the dashboard router) ──────────────────────


async def fetch_recent_observations(
    conn: Any,
    *,
    since: datetime,
) -> list[ConflictObservation]:
    """Return observations with ``ts >= since`` ordered oldest first.

    ``conn`` is an ``asyncpg.Connection`` borrowed from
    ``backend.db_pool``. The reader re-hydrates ``files_in_conflict``
    from the PG ``TEXT[]`` column directly (asyncpg returns it as
    ``list[str]``).
    """
    rows = await conn.fetch(
        """
        SELECT ts, ps_change_id, ps_change_number, ticket,
               files_in_conflict, cause_category, pre_existing_open_count
          FROM conflict_observations
         WHERE ts >= $1
         ORDER BY ts ASC
        """,
        since,
    )
    out: list[ConflictObservation] = []
    for row in rows:
        out.append(_row_to_observation(dict(row)))
    return out


# ── Sync reader for the daily report (works on SQLite + PG) ──────────


def fetch_recent_observations_sync(
    *,
    since: datetime,
    dsn: str | None = None,
    sqlite_path: str | None = None,
) -> list[ConflictObservation]:
    """Synchronous fetch for the cron report. Mirrors the async reader.

    Tries Postgres via ``psycopg`` first (when DSN resolvable); falls
    back to ``sqlite3`` for the test/dev path. Order is oldest-first to
    match :func:`fetch_recent_observations`.
    """
    resolved_dsn = dsn or _resolve_pg_dsn()
    if resolved_dsn:
        return _select_pg(since, resolved_dsn)
    path = sqlite_path or _resolve_sqlite_path()
    if path:
        return _select_sqlite(since, path)
    return []


# ── Internals ────────────────────────────────────────────────────────


def _structlog_noop(level: str, event: str, **kwargs: Any) -> None:
    logger.log(
        getattr(logging, level.upper(), logging.INFO),
        "%s %s", event, kwargs,
    )


def _resolve_pg_dsn() -> str | None:
    """Mirror :func:`backend.agents.provider_quota_tracker._resolve_dsn`
    so the bridge writer reaches the same Postgres the rest of the
    backend uses. Falls back to ``None`` for SQLite / unconfigured envs.
    """
    try:
        from backend.db_url import parse  # local import: keep tests light
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


def _insert_pg(
    obs: ConflictObservation, dsn: str, emit: Any,
) -> bool:
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError:
        emit(
            "WARN", "conflict_observation_skipped_no_psycopg2",
            cause=obs.cause_category,
        )
        return False
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as exc:  # noqa: BLE001
        emit(
            "WARN", "conflict_observation_pg_connect_failed",
            cause=obs.cause_category,
            err=f"{type(exc).__name__}: {exc}",
        )
        return False
    try:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO conflict_observations (
                            ts, ps_change_id, ps_change_number, ticket,
                            files_in_conflict, cause_category,
                            pre_existing_open_count
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            obs.ts,
                            obs.ps_change_id,
                            obs.ps_change_number,
                            obs.ticket,
                            list(obs.files_in_conflict),
                            obs.cause_category,
                            obs.pre_existing_open_count,
                        ),
                    )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — never crash the daemon
        emit(
            "WARN", "conflict_observation_pg_insert_failed",
            cause=obs.cause_category,
            err=f"{type(exc).__name__}: {exc}",
        )
        return False
    emit(
        "INFO", "conflict_observation_recorded",
        cause=obs.cause_category,
        ps_change_number=obs.ps_change_number,
        ticket=obs.ticket,
        files=list(obs.files_in_conflict),
    )
    return True


def _insert_sqlite(
    obs: ConflictObservation, path: str, emit: Any,
) -> bool:
    import sqlite3
    try:
        with sqlite3.connect(path, timeout=5) as conn:
            conn.execute(
                """
                INSERT INTO conflict_observations (
                    ts, ps_change_id, ps_change_number, ticket,
                    files_in_conflict, cause_category,
                    pre_existing_open_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obs.ts.isoformat(),
                    obs.ps_change_id,
                    obs.ps_change_number,
                    obs.ticket,
                    json.dumps(list(obs.files_in_conflict)),
                    obs.cause_category,
                    obs.pre_existing_open_count,
                ),
            )
            conn.commit()
        emit(
            "INFO", "conflict_observation_recorded",
            cause=obs.cause_category,
            ps_change_number=obs.ps_change_number,
            ticket=obs.ticket,
            files=list(obs.files_in_conflict),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        emit(
            "WARN", "conflict_observation_sqlite_insert_failed",
            cause=obs.cause_category,
            err=f"{type(exc).__name__}: {exc}",
        )
        return False


def _select_pg(since: datetime, dsn: str) -> list[ConflictObservation]:
    import psycopg2  # type: ignore[import-not-found]
    out: list[ConflictObservation] = []
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, ps_change_id, ps_change_number, ticket,
                       files_in_conflict, cause_category,
                       pre_existing_open_count
                  FROM conflict_observations
                 WHERE ts >= %s
                 ORDER BY ts ASC
                """,
                (since,),
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                out.append(_row_to_observation(dict(zip(cols, row))))
    finally:
        conn.close()
    return out


def _select_sqlite(since: datetime, path: str) -> list[ConflictObservation]:
    import sqlite3
    out: list[ConflictObservation] = []
    with sqlite3.connect(path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT ts, ps_change_id, ps_change_number, ticket,
                   files_in_conflict, cause_category,
                   pre_existing_open_count
              FROM conflict_observations
             WHERE ts >= ?
             ORDER BY ts ASC
            """,
            (since.isoformat(),),
        )
        for row in cur.fetchall():
            out.append(_row_to_observation(dict(row)))
    return out


def _row_to_observation(row: dict[str, Any]) -> ConflictObservation:
    files_raw = row.get("files_in_conflict")
    files: tuple[str, ...]
    if isinstance(files_raw, list):
        files = tuple(str(f) for f in files_raw)
    elif isinstance(files_raw, str) and files_raw:
        try:
            decoded = json.loads(files_raw)
            files = tuple(str(f) for f in decoded) if isinstance(decoded, list) else ()
        except json.JSONDecodeError:
            files = ()
    else:
        files = ()
    ts = row.get("ts")
    if isinstance(ts, str):
        # SQLite stores ISO8601 text; PG returns datetime via psycopg
        ts_parsed = _parse_iso(ts)
    elif isinstance(ts, datetime):
        ts_parsed = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        ts_parsed = datetime.now(timezone.utc)
    return ConflictObservation(
        ts=ts_parsed,
        ps_change_id=row.get("ps_change_id") or None,
        ps_change_number=(
            int(row["ps_change_number"])
            if row.get("ps_change_number") is not None
            else None
        ),
        ticket=row.get("ticket") or None,
        files_in_conflict=files,
        cause_category=str(row.get("cause_category") or ""),
        pre_existing_open_count=int(row.get("pre_existing_open_count") or 0),
    )


def _parse_iso(s: str) -> datetime:
    # SQLite default ``CURRENT_TIMESTAMP`` produces "YYYY-MM-DD HH:MM:SS"
    # (no T separator, no tz). Normalise so callers always get aware UTC.
    text = s.strip()
    if " " in text and "T" not in text:
        text = text.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Aggregation helpers (shared by report + dashboard) ───────────────


@dataclass(frozen=True)
class HotspotEntry:
    file: str
    events: int


@dataclass(frozen=True)
class WindowSummary:
    """Summary numbers for a single observation window."""

    window_start: datetime
    window_end: datetime
    total_events: int
    by_cause: dict[str, int]
    hotspots: tuple[HotspotEntry, ...]
    distinct_changes: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "total_events": self.total_events,
            "by_cause": dict(self.by_cause),
            "hotspots": [
                {"file": h.file, "events": h.events} for h in self.hotspots
            ],
            "distinct_changes": self.distinct_changes,
        }


def summarise(
    rows: Iterable[ConflictObservation],
    *,
    window_start: datetime,
    window_end: datetime,
    top_n_hotspots: int = 5,
) -> WindowSummary:
    """Aggregate raw rows into the report shape used by the JSON report
    and the operator dashboard tile.

    Hotspots count one event per (row, file) — i.e. a single row touching
    three files counts each file once. The operator-facing summary line
    in the report (``19% of PSes hit at least 1 conflict``) is computed
    from the broader ``ps_merged_metrics`` log line by the report
    script, not from this aggregator (which sees only conflict rows).
    """
    rows_list = list(rows)
    by_cause: dict[str, int] = {}
    file_counts: dict[str, int] = {}
    distinct: set[str] = set()
    for r in rows_list:
        by_cause[r.cause_category] = by_cause.get(r.cause_category, 0) + 1
        for f in r.files_in_conflict:
            file_counts[f] = file_counts.get(f, 0) + 1
        if r.ps_change_id:
            distinct.add(r.ps_change_id)
        elif r.ps_change_number is not None:
            distinct.add(f"#{r.ps_change_number}")
    hotspots = sorted(
        ((f, n) for f, n in file_counts.items()),
        key=lambda fn: (-fn[1], fn[0]),
    )[:top_n_hotspots]
    return WindowSummary(
        window_start=window_start,
        window_end=window_end,
        total_events=len(rows_list),
        by_cause=dict(sorted(by_cause.items())),
        hotspots=tuple(HotspotEntry(file=f, events=n) for f, n in hotspots),
        distinct_changes=len(distinct),
    )


def hourly_buckets(
    rows: Iterable[ConflictObservation],
    *,
    window_end: datetime,
    bucket_count: int = 4,
) -> list[int]:
    """Count observations in the last ``bucket_count`` 1-hour buckets
    ending at ``window_end``. Returned newest bucket last.

    Used by the alert path to compute "hourly conflict rate > 30 %"
    over the last 4 hourly windows.
    """
    buckets = [0] * bucket_count
    rows_list = list(rows)
    for r in rows_list:
        delta_seconds = (window_end - r.ts).total_seconds()
        if delta_seconds < 0:
            continue
        idx_from_end = int(delta_seconds // 3600)
        if idx_from_end >= bucket_count:
            continue
        buckets[bucket_count - 1 - idx_from_end] += 1
    return buckets


def file_event_counts(
    rows: Iterable[ConflictObservation],
) -> dict[str, int]:
    """Per-file event count over the supplied rows. One event per
    (row, file) — same semantics as :func:`summarise`'s ``hotspots``."""
    counts: dict[str, int] = {}
    for r in rows:
        for f in r.files_in_conflict:
            counts[f] = counts.get(f, 0) + 1
    return counts
