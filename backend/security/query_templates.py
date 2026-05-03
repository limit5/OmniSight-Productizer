"""SC.7.3 — OWASP parameterized-query templates for generated apps.

Small PostgreSQL / asyncpg-style query builders intended for generated
FastAPI / service templates.  The helpers return SQL text plus a params
tuple; callers still choose ``fetch``, ``fetchrow``, or ``execute``.

Security boundary:

  * This module covers parameterized CRUD query templates only.
  * Identifier allowlisting is intentionally strict and reuses the
    SC.7.1 symbolic-identifier shape.  Arbitrary SQL expressions,
    joins, raw WHERE fragments, and vendor-specific operators are out
    of scope for this row.
  * Input validation, output encoding, CSRF templates, and path / SSRF
    protection are separate SC.7 rows.

All module-level state is immutable constants.  Cross-worker safety
follows SOP Step 1 answer #1: each uvicorn worker derives identical
templates from the same source code; there is no shared cache,
singleton, or runtime mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from backend.security.input_validation import (
    InputValidationError,
    validate_identifier,
    validate_int_range,
)


ASC = "ASC"
DESC = "DESC"
ORDER_DIRECTIONS = (ASC, DESC)


@dataclass(frozen=True)
class ParameterizedQueryIssue:
    """Machine-readable query-template configuration failure detail."""

    field: str
    code: str
    message: str


class ParameterizedQueryError(ValueError):
    """Raised when a query template cannot be generated safely."""

    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.issue = ParameterizedQueryIssue(field=field, code=code, message=message)


@dataclass(frozen=True)
class QueryTemplate:
    """Parameterized SQL text and ordered bind parameters."""

    sql: str
    params: tuple[object, ...] = ()


def _fail(field: str, code: str, message: str) -> None:
    raise ParameterizedQueryError(field, code, message)


def _field_name(field: str) -> str:
    return (field or "value").strip() or "value"


def placeholder(index: int) -> str:
    """Return a PostgreSQL positional placeholder such as ``$1``."""

    n = _positive_int(index, field="index")
    return f"${n}"


def quote_identifier(value: object, *, field: str = "identifier") -> str:
    """Validate and double-quote one SQL identifier.

    Quoting every identifier lets generated apps safely use reserved
    words such as ``user`` while the allowlist blocks identifier
    injection attempts like ``name; DROP TABLE users``.
    """

    name = _field_name(field)
    try:
        identifier = validate_identifier(value, field=name)
    except InputValidationError as exc:
        _fail(name, "identifier", exc.issue.message)
    return f'"{identifier}"'


def placeholders(count: int, *, start: int = 1) -> tuple[str, ...]:
    """Return ``count`` consecutive PostgreSQL placeholders."""

    total = _bounded_int(count, field="count", minimum=0)
    first = _positive_int(start, field="start")
    return tuple(placeholder(i) for i in range(first, first + total))


def select_rows(
    table: object,
    columns: Iterable[object],
    *,
    where: Mapping[object, object] | None = None,
    order_by: Sequence[tuple[object, str]] = (),
    limit: int | None = None,
) -> QueryTemplate:
    """Build a parameterized ``SELECT`` with equality filters."""

    selected = _quote_columns(columns, field="columns")
    sql = f"SELECT {', '.join(selected)} FROM {quote_identifier(table, field='table')}"
    params: list[object] = []
    conditions = _where_conditions(where or {}, params, start=1)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    ordering = _order_by(order_by)
    if ordering:
        sql += " ORDER BY " + ", ".join(ordering)
    if limit is not None:
        params.append(_positive_int(limit, field="limit"))
        sql += f" LIMIT {placeholder(len(params))}"
    return QueryTemplate(sql=sql, params=tuple(params))


def insert_row(
    table: object,
    values: Mapping[object, object],
    *,
    returning: Iterable[object] = (),
) -> QueryTemplate:
    """Build a parameterized single-row ``INSERT``."""

    items = _nonempty_mapping(values, field="values")
    columns = [quote_identifier(column, field="values") for column, _ in items]
    binds = placeholders(len(items))
    sql = (
        f"INSERT INTO {quote_identifier(table, field='table')} "
        f"({', '.join(columns)}) VALUES ({', '.join(binds)})"
    )
    sql += _returning_clause(returning)
    return QueryTemplate(sql=sql, params=tuple(value for _, value in items))


def update_rows(
    table: object,
    values: Mapping[object, object],
    where: Mapping[object, object],
    *,
    returning: Iterable[object] = (),
) -> QueryTemplate:
    """Build a parameterized ``UPDATE`` with required equality filters."""

    set_items = _nonempty_mapping(values, field="values")
    where_items = _nonempty_mapping(where, field="where")
    params = [value for _, value in set_items]
    assignments = [
        f"{quote_identifier(column, field='values')} = {placeholder(i)}"
        for i, (column, _) in enumerate(set_items, start=1)
    ]
    conditions = _where_conditions(dict(where_items), params, start=len(params) + 1)
    sql = (
        f"UPDATE {quote_identifier(table, field='table')} "
        f"SET {', '.join(assignments)} WHERE {' AND '.join(conditions)}"
    )
    sql += _returning_clause(returning)
    return QueryTemplate(sql=sql, params=tuple(params))


def delete_rows(
    table: object,
    where: Mapping[object, object],
    *,
    returning: Iterable[object] = (),
) -> QueryTemplate:
    """Build a parameterized ``DELETE`` with required equality filters."""

    where_items = _nonempty_mapping(where, field="where")
    params: list[object] = []
    conditions = _where_conditions(dict(where_items), params, start=1)
    sql = (
        f"DELETE FROM {quote_identifier(table, field='table')} "
        f"WHERE {' AND '.join(conditions)}"
    )
    sql += _returning_clause(returning)
    return QueryTemplate(sql=sql, params=tuple(params))


def _nonempty_mapping(
    values: Mapping[object, object],
    *,
    field: str,
) -> tuple[tuple[object, object], ...]:
    items = tuple(values.items())
    if not items:
        _fail(field, "empty", "must include at least one column")
    return items


def _bounded_int(value: object, *, field: str, minimum: int) -> int:
    try:
        return validate_int_range(value, field=field, minimum=minimum)
    except InputValidationError as exc:
        _fail(field, exc.issue.code, exc.issue.message)


def _positive_int(value: object, *, field: str) -> int:
    return _bounded_int(value, field=field, minimum=1)


def _quote_columns(columns: Iterable[object], *, field: str) -> tuple[str, ...]:
    quoted = tuple(quote_identifier(column, field=field) for column in columns)
    if not quoted:
        _fail(field, "empty", "must include at least one column")
    return quoted


def _where_conditions(
    where: Mapping[object, object],
    params: list[object],
    *,
    start: int,
) -> list[str]:
    conditions: list[str] = []
    for offset, (column, value) in enumerate(where.items()):
        params.append(value)
        conditions.append(
            f"{quote_identifier(column, field='where')} = {placeholder(start + offset)}"
        )
    return conditions


def _order_by(order_by: Sequence[tuple[object, str]]) -> tuple[str, ...]:
    clauses: list[str] = []
    for column, direction in order_by:
        if not isinstance(direction, str):
            _fail("order_by", "direction", "direction must be ASC or DESC")
        upper = direction.upper()
        if upper not in ORDER_DIRECTIONS:
            _fail("order_by", "direction", "direction must be ASC or DESC")
        clauses.append(f"{quote_identifier(column, field='order_by')} {upper}")
    return tuple(clauses)


def _returning_clause(returning: Iterable[object]) -> str:
    quoted = tuple(quote_identifier(column, field="returning") for column in returning)
    if not quoted:
        return ""
    return " RETURNING " + ", ".join(quoted)


__all__ = [
    "ASC",
    "DESC",
    "ORDER_DIRECTIONS",
    "ParameterizedQueryError",
    "ParameterizedQueryIssue",
    "QueryTemplate",
    "delete_rows",
    "insert_row",
    "placeholder",
    "placeholders",
    "quote_identifier",
    "select_rows",
    "update_rows",
]
