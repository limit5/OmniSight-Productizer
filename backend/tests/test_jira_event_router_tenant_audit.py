"""Y-prep.3 (#289) — tenant-context / audit-event contract for the JIRA router.

Locks the contract that the three JIRA webhook handlers
(``handle_comment_created`` / ``handle_issue_updated`` /
``handle_issue_created`` in ``backend/jira_event_router.py``) each emit
one audit row with a fixed ``action`` string, carry the ``tenant_id``
inherited from the live ``db_context.current_tenant_id()`` contextvar,
and record that tenant in both the ``actor`` string and the ``after``
payload.

Why this matters:
  * The inbound JIRA webhook has no user session — it is gated only by a
    shared secret. The dispatcher in ``webhooks.py::_on_jira_event``
    explicitly scopes the request to ``t-default`` for Y-prep.3, and Y4
    will swap that seam for a real per-tenant lookup. This test fixes
    the two ends (dispatcher sets tenant → audit row carries tenant) so
    the Y4 transition becomes a one-line diff.
  * The three action strings must match the Y-prep.3 bullet spec
    exactly: any rename silently breaks downstream audit-query filters.

These tests intentionally do NOT exercise the full positive/negative
handler matrix — that is the scope of a separate Y-prep.3 bullet
(``測試 3 條：每個 handler 一個 positive + 一個 negative``). Here we only
pin the audit + tenant contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture()
async def _clean_audit(pg_test_pool):
    """Empty ``audit_log`` before and after each test.

    We can't use pg_test_conn's rollback because ``audit.log`` opens its
    own pool-scoped transaction and commits. Truncate explicitly.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE audit_log RESTART IDENTITY CASCADE")
    try:
        yield
    finally:
        from backend.db_context import set_tenant_id
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE audit_log RESTART IDENTITY CASCADE")


async def _fetch_rows(pg_test_pool, action: str):
    async with pg_test_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, actor, action, entity_kind, entity_id, "
            "       before_json, after_json, tenant_id "
            "FROM audit_log WHERE action = $1 ORDER BY id ASC",
            action,
        )


# ──────────────────────────────────────────────────────────────────────
#  Contract 1: all three actions ship the expected audit row.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_comment_handler_emits_jira_command_received_audit(
    pg_test_pool, _clean_audit,
):
    """``/deploy foo`` comment → one ``jira.command_received`` audit row."""
    from backend import jira_event_router

    event = {
        "issue": {"key": "OPS-42"},
        "comment": {
            "id": "c-1",
            "body": "/deploy prod --dry-run",
            "author": {"displayName": "alice"},
        },
    }
    # Stub the bus so no real Redis round-trip fires.
    with patch.object(jira_event_router, "_publish_bus"):
        result = await jira_event_router.handle_comment_created(event)
    assert result["status"] == "dispatched"
    assert result["command"] == "deploy"

    rows = await _fetch_rows(pg_test_pool, "jira.command_received")
    assert len(rows) == 1
    r = rows[0]
    assert r["entity_kind"] == "jira_event"
    assert r["entity_id"] == "OPS-42"


@pytest.mark.asyncio
async def test_issue_updated_handler_emits_status_transitioned_audit(
    pg_test_pool, _clean_audit,
):
    """Status ``In Progress → Done`` → one ``jira.status_transitioned`` row."""
    from backend import jira_event_router

    event = {
        "issue": {"key": "OPS-43", "fields": {"summary": "ship it"}},
        "changelog": {
            "items": [
                {"field": "status",
                 "fromString": "In Progress",
                 "toString": "Done"},
            ],
        },
    }
    # Suppress the background artifact-packaging spawn so the test stays
    # hermetic (no real tarball work).
    with patch(
        "backend.routers.webhooks._package_merged_artifacts",
        new=AsyncMock(return_value=None),
    ):
        result = await jira_event_router.handle_issue_updated(event)
    assert result["status"] == "dispatched"
    assert result["to"] == "Done"

    rows = await _fetch_rows(pg_test_pool, "jira.status_transitioned")
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "OPS-43"


@pytest.mark.asyncio
async def test_issue_created_handler_emits_intake_triggered_audit(
    pg_test_pool, _clean_audit,
):
    """``omnisight-intake`` label → one ``jira.intake_triggered`` row."""
    from backend import jira_event_router

    event = {
        "issue": {
            "key": "OPS-44",
            "fields": {"labels": ["omnisight-intake", "unrelated"]},
        },
    }
    # Stub intent_bridge so the test doesn't need the full orchestrator.
    with patch(
        "backend.intent_bridge.on_intake_queued",
        new=AsyncMock(return_value=None),
    ):
        result = await jira_event_router.handle_issue_created(event)
    assert result["status"] == "dispatched"
    assert result["intake_label"] == "omnisight-intake"

    rows = await _fetch_rows(pg_test_pool, "jira.intake_triggered")
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "OPS-44"


# ──────────────────────────────────────────────────────────────────────
#  Contract 2: audit rows inherit current tenant context.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_row_lands_on_default_tenant_when_unscoped(
    pg_test_pool, _clean_audit,
):
    """No ``set_tenant_id`` → audit row tenant_id == ``t-default``.

    This mirrors the pre-dispatcher state and proves the library fallback
    in ``tenant_insert_value()`` is still honoured for direct-call paths
    (unit tests, CLI tools) that don't go through the webhook dispatcher.
    """
    from backend import jira_event_router
    from backend.db_context import set_tenant_id

    set_tenant_id(None)  # explicit: no tenant in context

    with patch.object(jira_event_router, "_publish_bus"):
        await jira_event_router.handle_comment_created({
            "issue": {"key": "OPS-100"},
            "comment": {"id": "c", "body": "/ping", "author": {}},
        })

    rows = await _fetch_rows(pg_test_pool, "jira.command_received")
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "t-default"
    # Actor string stamps the live tenant so operators can grep by it.
    assert rows[0]["actor"] == "jira_event_router/t-default"
    import json as _json
    after = _json.loads(rows[0]["after_json"])
    assert after.get("tenant_id") == "t-default"


@pytest.mark.asyncio
async def test_audit_row_inherits_explicitly_set_tenant(
    pg_test_pool, _clean_audit,
):
    """Explicit ``set_tenant_id("t-alpha")`` → audit row under t-alpha.

    This is the Y4 readiness check: once the dispatcher derives a real
    tenant instead of ``t-default``, nothing inside the handlers or
    ``audit.log`` needs to change. The only required seam is the one
    ``set_tenant_id`` call in ``webhooks.py::_on_jira_event``.
    """
    from backend import jira_event_router
    from backend.db_context import set_tenant_id
    from backend.db_pool import get_pool

    # FK: tenants row must exist before audit_log.tenant_id = 't-alpha'.
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ('t-alpha', 'Alpha', 'free') "
            "ON CONFLICT (id) DO NOTHING"
        )
    try:
        set_tenant_id("t-alpha")
        with patch.object(jira_event_router, "_publish_bus"):
            await jira_event_router.handle_comment_created({
                "issue": {"key": "OPS-200"},
                "comment": {"id": "c", "body": "/deploy", "author": {}},
            })
    finally:
        set_tenant_id(None)

    rows = await _fetch_rows(pg_test_pool, "jira.command_received")
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "t-alpha"
    assert rows[0]["actor"] == "jira_event_router/t-alpha"
    import json as _json
    after = _json.loads(rows[0]["after_json"])
    assert after.get("tenant_id") == "t-alpha"


# ──────────────────────────────────────────────────────────────────────
#  Contract 3: the dispatcher seam scopes the request to t-default.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_scopes_to_t_default_when_unauthenticated(
    pg_test_pool, _clean_audit,
):
    """``_on_jira_event`` sets ``t-default`` before routing, restores after.

    Invariant: the dispatcher must leave the contextvar in exactly the
    state it found it (``None`` when entering from an unauthenticated
    webhook). The audit row created in-flight lands on ``t-default``.
    """
    from backend.db_context import current_tenant_id, set_tenant_id
    from backend.routers.webhooks import _on_jira_event
    from backend import jira_event_router

    set_tenant_id(None)
    assert current_tenant_id() is None

    event = {
        "webhookEvent": "comment_created",
        "issue": {"key": "OPS-999"},
        "comment": {"id": "c", "body": "/status", "author": {}},
    }
    with patch.object(jira_event_router, "_publish_bus"):
        await _on_jira_event(event)

    # Contextvar restored to its prior value (None).
    assert current_tenant_id() is None

    rows = await _fetch_rows(pg_test_pool, "jira.command_received")
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "t-default"
