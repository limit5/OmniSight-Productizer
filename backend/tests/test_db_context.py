"""Unit tests for backend.db_context request-scope helpers."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _reset_db_context():
    from backend import db_context

    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)
    try:
        yield
    finally:
        db_context.set_tenant_id(None)
        db_context.set_project_id(None)
        db_context.set_user_role(None)


def test_project_context_set_require_and_clear():
    from backend import db_context

    assert db_context.current_project_id() is None
    with pytest.raises(RuntimeError, match="No project_id"):
        db_context.require_current_project()

    db_context.set_project_id("p-unit")
    assert db_context.current_project_id() == "p-unit"
    assert db_context.require_current_project() == "p-unit"

    db_context.set_project_id(None)
    assert db_context.current_project_id() is None


def test_user_role_context_set_require_and_clear():
    from backend import db_context

    assert db_context.current_user_role() is None
    with pytest.raises(RuntimeError, match="No user_role"):
        db_context.require_current_user_role()

    db_context.set_user_role("contributor")
    assert db_context.current_user_role() == "contributor"
    assert db_context.require_current_user_role() == "contributor"

    db_context.set_user_role(None)
    assert db_context.current_user_role() is None


def test_tenant_where_pg_uses_next_positional_placeholder():
    from backend import db_context

    db_context.set_tenant_id("t-pg")
    conditions = ["status = $1"]
    params = ["open"]

    db_context.tenant_where_pg(conditions, params, table_alias="a")

    assert conditions == ["status = $1", "a.tenant_id = $2"]
    assert params == ["open", "t-pg"]


def test_tenant_where_pg_no_tenant_no_filter():
    from backend import db_context

    conditions = ["status = $1"]
    params = ["open"]

    db_context.tenant_where_pg(conditions, params)

    assert conditions == ["status = $1"]
    assert params == ["open"]


@pytest.mark.asyncio
async def test_contextvars_are_task_local():
    from backend import db_context

    async def _read_in_task(tid: str) -> str | None:
        db_context.set_tenant_id(tid)
        await asyncio.sleep(0)
        return db_context.current_tenant_id()

    left, right = await asyncio.gather(
        _read_in_task("t-left"),
        _read_in_task("t-right"),
    )

    assert (left, right) == ("t-left", "t-right")
    assert db_context.current_tenant_id() is None
