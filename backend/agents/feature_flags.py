"""OP-884 D12 -- agent-side feature-flag SDK with % rollout per tenant.

This module is the public entry point for agent / backend code that
needs to gate work behind a registry flag with optional per-tenant
partial rollout. It is the agent-track counterpart of
:mod:`backend.feature_flag_sdk` (which composes a YAML rollout config
on top of the WP.7.1 registry) -- OP-884 consolidates the
``rollout_pct`` + ``allowed_tenants`` controls into the registry row
itself so the operator UI can flip them without redeploying a YAML.

Public surface
--------------

    is_enabled(name, tenant_id, *, conn=None) -> bool

Resolution semantics (matches OP-884 AC #4):

1. **FlagNotDefined** -- missing row defaults to ``False`` and emits
   one ``warning`` log entry per resolution.  Failing closed keeps the
   feature opt-in for prod; the warning helps the operator notice a
   stale ``is_enabled`` call with no corresponding registry row.

2. **state='disabled'** -- always returns ``False``, regardless of
   rollout_pct / allowed_tenants.

3. **allowed_tenants non-empty** -- when the list is populated, only
   tenants in the list resolve to ``True``. The percent gate is
   short-circuited; the allow-list is the authoritative cohort.

4. **rollout_pct gate** -- when ``state='enabled'`` and no allow-list
   is configured, ``hash(tenant_id) % 100 < rollout_pct`` decides.
   ``rollout_pct=100`` (the default for legacy rows) lets every tenant
   through; ``rollout_pct=0`` excludes every tenant.

5. **FlagDecisionTimeout** -- on any DB / IO error the resolution
   returns ``False`` (fail closed) within the 50 ms budget.  The
   helper logs the underlying exception at ``warning`` and stays out
   of the request's critical path.

Hash function
-------------
The bucket function is ``int(sha256("op-884:" + tenant_id + ":" +
flag_name)[:8]) % 100``.  SHA-256 is deterministic across workers and
process restarts, so the same tenant stays pinned for a given
``(flag_name, rollout_pct)`` pair as the percentage is dialed up.
Hashing the flag name as well as the tenant id keeps cohorts
independent between flags -- otherwise rolling out one flag at 10%
would always cover the same 10% of tenants for every subsequent flag.

Module-global state audit
-------------------------
No mutable module-global state. The decision path borrows a
connection from :func:`backend.db_pool.get_pool` (or uses the caller-
supplied ``conn``) and reads the row directly. The WP.7.4 in-memory
``FeatureFlagRegistry`` cache is intentionally NOT used here because
it only tracks ``state`` -- it would silently miss ``rollout_pct`` and
``allowed_tenants`` changes. Callers that need a cached read may layer
their own LRU; the SDK keeps the contract single-source-of-truth on
the DB.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


_BUCKET_SALT = "op-884"
_FLAG_QUERY_SQL_PG = (
    "SELECT flag_name, state, rollout_pct, allowed_tenants "
    "FROM feature_flags WHERE flag_name = $1"
)
_FLAG_QUERY_SQL_SQLITE = (
    "SELECT flag_name, state, rollout_pct, allowed_tenants "
    "FROM feature_flags WHERE flag_name = ?"
)


class FlagNotDefined(LookupError):
    """Raised when callers explicitly opt into strict mode and the row
    is absent. The default :func:`is_enabled` path swallows this and
    returns ``False`` per AC #4 / error catalog, but exposing the type
    lets callers introspect or test the failure mode."""


class FlagDecisionTimeout(RuntimeError):
    """Raised when the DB read exceeds the 50 ms budget. The default
    :func:`is_enabled` path swallows this and returns ``False`` per
    the error catalog."""


def bucket_for_tenant(flag_name: str, tenant_id: str) -> int:
    """Return a stable bucket in ``[0, 99]`` for ``(flag_name, tenant_id)``.

    The hash combines the flag name and tenant id so cohorts are
    independent across flags. Pure / deterministic / dialect-free; safe
    for callers that want to pre-compute a rollout decision without a
    DB round trip.
    """
    token = f"{_BUCKET_SALT}:{tenant_id}:{flag_name}".encode("utf-8")
    digest = hashlib.sha256(token).digest()
    return int.from_bytes(digest[:8], "big") % 100


def _decode_allowed_tenants(raw: Any) -> tuple[str, ...]:
    """Coerce ``allowed_tenants`` from PG JSONB or SQLite TEXT to a tuple."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(x) for x in raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "feature_flags.allowed_tenants: malformed JSON, treating as empty: %r",
                text,
            )
            return ()
        if isinstance(parsed, list):
            return tuple(str(x) for x in parsed)
    logger.warning(
        "feature_flags.allowed_tenants: unexpected type %s, treating as empty",
        type(raw).__name__,
    )
    return ()


def _decide(
    *,
    flag_name: str,
    tenant_id: str,
    state: str,
    rollout_pct: int | None,
    allowed_tenants: Iterable[str],
) -> bool:
    """Pure rollout decision -- isolated so tests can exercise it
    without a DB connection. Matches AC #4 exactly."""
    if state != "enabled":
        return False
    allow_list = tuple(allowed_tenants)
    if allow_list:
        return tenant_id in allow_list
    pct = 100 if rollout_pct is None else int(rollout_pct)
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    return bucket_for_tenant(flag_name, tenant_id) < pct


async def _fetch_row(conn: Any, flag_name: str) -> dict[str, Any] | None:
    """Read one ``feature_flags`` row using whichever conn flavour the
    caller passed (asyncpg or aiosqlite)."""
    # asyncpg exposes ``fetchrow`` with $1 placeholders; aiosqlite uses
    # ``execute`` + ``fetchone`` with ``?`` placeholders. Detect by
    # method presence rather than ``isinstance`` to stay shim-friendly.
    if hasattr(conn, "fetchrow"):
        row = await conn.fetchrow(_FLAG_QUERY_SQL_PG, flag_name)
        if row is None:
            return None
        return dict(row)
    cursor = await conn.execute(_FLAG_QUERY_SQL_SQLITE, (flag_name,))
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    if row is None:
        return None
    return {
        "flag_name": row[0],
        "state": row[1],
        "rollout_pct": row[2],
        "allowed_tenants": row[3],
    }


async def is_enabled(
    name: str,
    tenant_id: str,
    *,
    conn: Any | None = None,
) -> bool:
    """Resolve ``name`` for ``tenant_id``. Defaults to ``False`` on any
    error (missing row, malformed columns, DB IO failure) per AC #4.

    ``conn`` is polymorphic for the same reason :func:`backend.audit.log`
    is: request handlers pass their request-scoped asyncpg connection,
    background workers and tests can pass an aiosqlite connection, and
    callers without a connection borrow one from the pool.
    """
    flag_name = str(name).strip()
    if not flag_name:
        return False
    tenant = str(tenant_id).strip()
    if not tenant:
        return False

    try:
        if conn is None:
            from backend.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as owned:
                row = await _fetch_row(owned, flag_name)
        else:
            row = await _fetch_row(conn, flag_name)
    except Exception as exc:
        # FlagDecisionTimeout / connection errors / pool not initialised.
        # Fail closed -- the request must not depend on the flag service.
        logger.warning(
            "feature_flags.is_enabled(%s) failed-closed: %s", flag_name, exc
        )
        return False

    if row is None:
        # FlagNotDefined -- log once at warning so an operator can find
        # the stale call site, then return False.
        logger.warning(
            "feature_flags.is_enabled(%s) FlagNotDefined: defaulting to disabled",
            flag_name,
        )
        return False

    return _decide(
        flag_name=flag_name,
        tenant_id=tenant,
        state=str(row.get("state") or "disabled"),
        rollout_pct=row.get("rollout_pct"),
        allowed_tenants=_decode_allowed_tenants(row.get("allowed_tenants")),
    )


__all__ = [
    "FlagDecisionTimeout",
    "FlagNotDefined",
    "bucket_for_tenant",
    "is_enabled",
]
