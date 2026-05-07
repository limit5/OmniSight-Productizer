"""MP.W1.2 -- provider quota tracker backed by PostgreSQL.

The tracker keeps provider routing state in ``provider_quota_state`` and
uses ``provider_usage_event`` as the SQL source of truth for rolling
windows.  Public functions are synchronous because routing_policy.py will
call them from sync decision code; each call opens a short transaction and
uses a provider-scoped advisory lock for writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

import psycopg2
from psycopg2.extensions import connection as PsycopgConnection
from psycopg2.extras import RealDictCursor

from backend.db_url import parse


logger = logging.getLogger(__name__)

CircuitState = Literal["closed", "open", "half_open"]
QuotaScope = Literal["5h", "weekly"]

DEFAULT_5H_CAP_TOKENS = 200_000
DEFAULT_WEEKLY_CAP_TOKENS = 2_000_000


@dataclass(frozen=True)
class QuotaState:
    provider: str
    rolling_5h_tokens: int
    weekly_tokens: int
    last_reset_at: datetime | None
    last_cap_hit_at: datetime | None
    circuit_state: CircuitState


def record_usage(provider: str, tokens: int, ts: datetime | None = None) -> None:
    """Record one usage event and refresh the cached quota state."""
    provider = _normalise_provider(provider)
    if tokens < 0:
        raise ValueError("tokens must be non-negative")
    event_ts = _normalise_ts(ts)
    update_state: QuotaState | None = None
    update_reason = "usage_recorded"
    update_scopes: list[QuotaScope] = []

    with closing(_connect()) as conn:
        with conn:
            _lock_provider(conn, provider)
            before = _refresh_state(conn, provider)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO provider_usage_event (provider, tokens, ts)
                    VALUES (%s, %s, %s)
                    """,
                    (provider, tokens, event_ts),
                )
            after = _refresh_state(conn, provider)
            cap_scopes = _cap_scopes(after)
            if before.circuit_state == "closed" and cap_scopes:
                now = _db_now(conn)
                _open_circuit(conn, provider, now)
                after = _refresh_state(conn, provider)
                _emit_cap_hit_audit(conn, after, cap_scopes)
                update_reason = "cap_hit"
                update_scopes = cap_scopes
            update_state = after
    if update_state is not None:
        _emit_quota_update(update_state, update_reason, update_scopes)


def get_quota_state(provider: str) -> QuotaState:
    """Return current SQL-computed rolling-window state for *provider*."""
    provider = _normalise_provider(provider)
    with closing(_connect()) as conn:
        with conn:
            _lock_provider(conn, provider)
            return _refresh_state(conn, provider)


def is_at_cap(provider: str, scope: QuotaScope) -> bool:
    """Return whether the provider is at the configured cap for *scope*."""
    state = get_quota_state(provider)
    if scope == "5h":
        return state.rolling_5h_tokens >= _cap_for(provider, "5h")
    if scope == "weekly":
        return state.weekly_tokens >= _cap_for(provider, "weekly")
    raise ValueError(f"unknown quota scope: {scope!r}")


def reset_window(provider: str, scope: QuotaScope) -> None:
    """Clear usage events for one rolling window and close the circuit."""
    provider = _normalise_provider(provider)
    if scope not in ("5h", "weekly"):
        raise ValueError(f"unknown quota scope: {scope!r}")
    interval = "5 hours" if scope == "5h" else "7 days"
    update_state: QuotaState | None = None

    with closing(_connect()) as conn:
        with conn:
            _lock_provider(conn, provider)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    DELETE FROM provider_usage_event
                    WHERE provider = %s
                      AND ts > now() - INTERVAL '{interval}'
                    """,
                    (provider,),
                )
            now = _db_now(conn)
            state = _refresh_state(conn, provider)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE provider_quota_state
                    SET rolling_5h_tokens = %s,
                        weekly_tokens = %s,
                        last_reset_at = %s,
                        last_cap_hit_at = NULL,
                        circuit_state = 'closed',
                        updated_at = %s
                    WHERE provider = %s
                    """,
                    (
                        state.rolling_5h_tokens,
                        state.weekly_tokens,
                        now,
                        now,
                        provider,
                    ),
                )
            update_state = replace(
                state,
                last_reset_at=now,
                last_cap_hit_at=None,
                circuit_state="closed",
            )
    if update_state is not None:
        _emit_quota_update(update_state, "window_reset", [scope])


def _normalise_provider(provider: str) -> str:
    out = provider.strip()
    if not out:
        raise ValueError("provider must be non-empty")
    return out


def _normalise_ts(ts: datetime | None) -> datetime:
    if ts is None:
        return datetime.now(timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _connect() -> PsycopgConnection:
    dsn = _resolve_dsn()
    if not dsn:
        raise RuntimeError(
            "provider_quota_tracker requires a PostgreSQL DSN via "
            "OMNISIGHT_DATABASE_URL, DATABASE_URL, or OMNI_TEST_PG_URL"
        )
    return psycopg2.connect(dsn)


def _resolve_dsn() -> str:
    for key in ("OMNISIGHT_DATABASE_URL", "DATABASE_URL", "OMNI_TEST_PG_URL"):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        parsed = parse(raw)
        if not parsed.is_postgres:
            continue
        return parsed.sqlalchemy_url(sync=True).replace(
            "postgresql+psycopg2://", "postgresql://", 1
        )
    return ""


def _lock_provider(conn: PsycopgConnection, provider: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (provider,))


def _refresh_state(conn: PsycopgConnection, provider: str) -> QuotaState:
    now = _db_now(conn)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(tokens) FILTER (
                    WHERE ts > now() - INTERVAL '5 hours'
                ), 0)::bigint AS rolling_5h_tokens,
                COALESCE(SUM(tokens) FILTER (
                    WHERE ts > now() - INTERVAL '7 days'
                ), 0)::bigint AS weekly_tokens
            FROM provider_usage_event
            WHERE provider = %s
            """,
            (provider,),
        )
        sums = cur.fetchone() or {}
        rolling_5h = int(sums.get("rolling_5h_tokens") or 0)
        weekly = int(sums.get("weekly_tokens") or 0)
        cur.execute(
            """
            INSERT INTO provider_quota_state (
                provider, rolling_5h_tokens, weekly_tokens, updated_at
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (provider) DO UPDATE SET
                rolling_5h_tokens = EXCLUDED.rolling_5h_tokens,
                weekly_tokens = EXCLUDED.weekly_tokens,
                updated_at = EXCLUDED.updated_at
            RETURNING provider, rolling_5h_tokens, weekly_tokens,
                      last_reset_at, last_cap_hit_at, circuit_state
            """,
            (provider, rolling_5h, weekly, now),
        )
        row = cur.fetchone()
    return _row_to_state(row)


def _db_now(conn: PsycopgConnection) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT CURRENT_TIMESTAMP")
        value = cur.fetchone()[0]
    return _normalise_ts(value)


def _open_circuit(
    conn: PsycopgConnection,
    provider: str,
    cap_hit_at: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE provider_quota_state
            SET circuit_state = 'open',
                last_cap_hit_at = %s,
                updated_at = %s
            WHERE provider = %s
            """,
            (cap_hit_at, cap_hit_at, provider),
        )


def _cap_scopes(state: QuotaState) -> list[QuotaScope]:
    scopes: list[QuotaScope] = []
    if state.rolling_5h_tokens >= _cap_for(state.provider, "5h"):
        scopes.append("5h")
    if state.weekly_tokens >= _cap_for(state.provider, "weekly"):
        scopes.append("weekly")
    return scopes


def _cap_for(provider: str, scope: QuotaScope) -> int:
    suffix = "5H" if scope == "5h" else "WEEKLY"
    env_name = f"OMNISIGHT_PROVIDER_CAP_{_env_provider(provider)}_{suffix}"
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{env_name} must be an integer") from exc
    if scope == "5h":
        return DEFAULT_5H_CAP_TOKENS
    return DEFAULT_WEEKLY_CAP_TOKENS


def _env_provider(provider: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in provider.upper())


def _emit_cap_hit_audit(
    conn: PsycopgConnection,
    state: QuotaState,
    scopes: list[QuotaScope],
) -> None:
    after = {
        "provider": state.provider,
        "rolling_5h_tokens": state.rolling_5h_tokens,
        "weekly_tokens": state.weekly_tokens,
        "scopes": scopes,
        "circuit_state": state.circuit_state,
    }
    payload = {
        "action": "provider_quota_cap_hit",
        "entity_kind": "provider_quota",
        "entity_id": state.provider,
        "before": {},
        "after": after,
        "actor": "system",
    }
    payload_canon = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("audit-chain-t-default",),
        )
        cur.execute(
            """
            SELECT curr_hash
            FROM audit_log
            WHERE tenant_id = 't-default'
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        prev = row[0] if row else ""
        ts = time.time()
        curr = hashlib.sha256(
            (prev + payload_canon + str(round(ts, 6))).encode("utf-8")
        ).hexdigest()
        cur.execute(
            """
            INSERT INTO audit_log (
                ts, actor, action, entity_kind, entity_id, before_json,
                after_json, prev_hash, curr_hash, session_id, tenant_id
            )
            VALUES (%s, 'system', 'provider_quota_cap_hit',
                    'provider_quota', %s, '{}', %s, %s, %s, NULL, 't-default')
            """,
            (
                ts,
                state.provider,
                json.dumps(after, ensure_ascii=False),
                prev,
                curr,
            ),
        )


def _emit_quota_update(
    state: QuotaState,
    reason: str,
    scopes: list[QuotaScope],
) -> None:
    """Best-effort SSE emit for live dashboard quota consumers."""
    try:
        from backend.events import emit_provider_quota_updated
        emit_provider_quota_updated(
            state.provider,
            state.rolling_5h_tokens,
            state.weekly_tokens,
            _cap_for(state.provider, "5h"),
            _cap_for(state.provider, "weekly"),
            state.circuit_state,
            last_reset_at=state.last_reset_at,
            last_cap_hit_at=state.last_cap_hit_at,
            reason=reason,
            scopes=list(scopes),
            broadcast_scope="user",
        )
    except Exception as exc:
        logger.debug("provider quota SSE publish failed: %s", exc)


def _row_to_state(row: object) -> QuotaState:
    if row is None:
        raise RuntimeError("provider_quota_state upsert returned no row")
    return QuotaState(
        provider=row["provider"],
        rolling_5h_tokens=int(row["rolling_5h_tokens"]),
        weekly_tokens=int(row["weekly_tokens"]),
        last_reset_at=row["last_reset_at"],
        last_cap_hit_at=row["last_cap_hit_at"],
        circuit_state=row["circuit_state"],
    )


__all__ = [
    "DEFAULT_5H_CAP_TOKENS",
    "DEFAULT_WEEKLY_CAP_TOKENS",
    "QuotaState",
    "get_quota_state",
    "is_at_cap",
    "record_usage",
    "reset_window",
]
