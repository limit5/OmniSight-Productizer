"""Q.7 #301 — shared optimistic-lock helpers.

The J2 pattern lived inline in ``backend/workflow.py`` until Q.7
(Workflow_runs is the original carrier of the ``If-Match`` + 409
contract). The Q.7 expansion ports the same pattern to four more
tables — ``tasks`` / ``npi_state`` / ``tenant_secrets`` /
``project_runs`` — and it was getting repetitive to copy the
"parse If-Match / UPDATE ... RETURNING version / raise 409" triple
into every handler.

What this module provides
-------------------------

1. ``VersionConflict`` — exception carrying ``current_version``
   (what the row actually has in PG after the racy write won) and
   ``your_version`` (what the client sent). Handlers catch this and
   translate to an HTTP 409.

2. ``parse_if_match(header)`` — handler-side header parsing.
   Missing → 428 Precondition Required (matches workflow.py's
   ``_parse_if_match``). Malformed → 400 Bad Request. Weak-ETag
   quoting (``"42"``) is stripped. Returns an ``int``.

3. ``raise_conflict(current_version, your_version, hint, *, resource)``
   — build the HTTP 409 response body that the frontend
   ``use409Conflict`` hook expects. Body shape:
   ``{"detail": {"current_version": N, "your_version": M,
   "hint": "<short operator-facing copy>", "resource": "<name>"}}``

4. ``bump_version_pg(conn, table, *, pk_col, pk_value, expected_version,
   updates)`` — the generic ``UPDATE ... SET ..., version = version
   + 1 WHERE pk = :pk AND version = :expected RETURNING version``.
   Returns the new ``int`` version or raises ``VersionConflict``.
   Uses asyncpg ``$N`` placeholders.

Module-global state: none. Pure helper functions + one Exception
class. All state-of-the-world lives in PG.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import HTTPException


class VersionConflict(Exception):
    """Raised when an optimistic-lock version check misses the row.

    Attributes carry enough information for the handler to build the
    shaped 409 response body (``current_version`` comes from the
    post-write SELECT; ``your_version`` from the If-Match header).
    """

    def __init__(
        self,
        *,
        current_version: int | None,
        your_version: int,
        resource: str = "",
    ):
        super().__init__(
            f"version conflict on {resource or 'resource'}: "
            f"client sent {your_version}, server at {current_version}"
        )
        self.current_version = current_version
        self.your_version = your_version
        self.resource = resource


def parse_if_match(if_match: str | None) -> int:
    """Parse a weak-ETag-ish ``If-Match`` header as an integer version.

    Raises:
      HTTPException(428) — header missing; the J2 contract requires
          every mutation carry a version so the racy-overwrite path
          never silently "succeeds" for clients that forgot to send it.
      HTTPException(400) — header present but not parseable as int.

    Strips surrounding quotes (``"42"`` / ``42`` both accepted) so the
    frontend can use a proper weak ETag or a bare integer without
    server-side branching.
    """
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail="If-Match header required",
        )
    try:
        return int(if_match.strip().strip('"').strip())
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="If-Match must be an integer version",
        )


def raise_conflict(
    current_version: int | None,
    your_version: int,
    hint: str = "另一裝置已修改，請重載",
    *,
    resource: str = "",
) -> None:
    """Raise an HTTP 409 with the shaped body that the frontend
    ``use409Conflict`` hook consumes.

    The ``hint`` default is a Traditional-Chinese operator-facing
    copy (Q.7 spec) — handlers may override for endpoint-specific
    copy but the default keeps the baseline consistent across
    resources so the toast reads the same regardless of which
    resource conflicted.
    """
    raise HTTPException(
        status_code=409,
        detail={
            "current_version": current_version,
            "your_version": your_version,
            "hint": hint,
            "resource": resource,
        },
    )


async def bump_version_pg(
    conn,
    table: str,
    *,
    pk_col: str,
    pk_value: Any,
    expected_version: int,
    updates: Mapping[str, Any],
) -> int:
    """Apply ``updates`` to one row with an optimistic-lock version guard.

    Builds ``UPDATE <table> SET col1=$1, col2=$2, ..., version =
    version + 1 WHERE <pk_col> = $K AND version = $(K+1) RETURNING
    version`` using asyncpg ``$N`` placeholders. The single
    ``RETURNING`` read tells us (a) whether the guard matched (row is
    non-None) and (b) the post-bump version to echo back to the
    client — one round-trip, no rowcount gymnastics.

    Returns:
      The new ``int`` version.

    Raises:
      ``VersionConflict`` — row exists with a different version, or
      row does not exist at all. Handlers typically re-SELECT in the
      rescue clause to distinguish 404 (gone) from 409 (drifted) and
      to populate ``current_version`` on the 409 body.

    Notes:
      - ``updates`` may be empty — this lets callers bump the version
        as a pure "heartbeat" without changing any business columns
        (rare, but useful for cross-device echo).
      - Column names are NOT parameterised (PG placeholders only bind
        values, not identifiers). Callers MUST pass only trusted
        literal column names — never operator-controlled input.
    """
    set_parts: list[str] = []
    params: list[Any] = []
    for col, val in updates.items():
        params.append(val)
        set_parts.append(f"{col} = ${len(params)}")
    set_parts.append("version = version + 1")

    params.append(pk_value)
    pk_placeholder = f"${len(params)}"
    params.append(expected_version)
    ver_placeholder = f"${len(params)}"

    sql = (
        f"UPDATE {table} SET {', '.join(set_parts)} "
        f"WHERE {pk_col} = {pk_placeholder} "
        f"AND version = {ver_placeholder} "
        f"RETURNING version"
    )
    row = await conn.fetchrow(sql, *params)
    if row is None:
        # Re-read the row's current version (if any) so the caller can
        # populate ``current_version`` on the 409 body. A missing row
        # comes back as None → the handler should translate to 404.
        cur_row = await conn.fetchrow(
            f"SELECT version FROM {table} WHERE {pk_col} = $1",
            pk_value,
        )
        current = int(cur_row["version"]) if cur_row else None
        raise VersionConflict(
            current_version=current,
            your_version=expected_version,
            resource=table,
        )
    return int(row["version"])
