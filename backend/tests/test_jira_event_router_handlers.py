"""Y-prep.3 (#289) — per-handler positive + negative dispatch matrix.

Scope: pin the trigger contract for each of the three JIRA event
handlers in ``backend/jira_event_router.py``. Each handler gets one
positive (the documented action fires) plus one negative (a documented
non-trigger condition stays silent). The three negatives correspond
1:1 to the user-spec bullet:

  * ``wrong event type``         → comment-handler is NOT reached when
                                   the dispatcher sees a non-comment
                                   ``webhookEvent`` (asserted via the
                                   ``_on_jira_event`` dispatcher in
                                   ``backend/routers/webhooks.py``).
  * ``non-whitelisted status``   → ``handle_issue_updated`` ignores a
                                   transition into a status that is not
                                   in the configured "done" allowlist.
  * ``missing label``            → ``handle_issue_created`` ignores an
                                   issue whose ``fields.labels`` does
                                   not contain the configured intake
                                   label.

These tests deliberately stay hermetic — they stub the four side-effect
seams (``_audit`` / ``_publish_bus`` / ``_package_merged_artifacts`` /
``intent_bridge.on_intake_queued``) and assert call/no-call directly.
The audit-row + tenant contract is already locked separately in
``test_jira_event_router_tenant_audit.py``; that file owns the DB
round-trip and we don't duplicate it here. Together the two files
form the full Step-4 drift-guard surface for the router.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────
#  Handler 1 — comment_created
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_comment_handler_positive_dispatches_jira_command():
    """``/deploy prod`` comment → bus publish + dispatched status.

    Locks the happy path: a comment whose body starts with the
    configured prefix (default ``/``) followed by a non-empty token
    publishes a ``jira_command`` event with the parsed command, args,
    issue key, author, and comment id.
    """
    from backend import jira_event_router

    event = {
        "issue": {"key": "OPS-101"},
        "comment": {
            "id": "c-101",
            "body": "/deploy prod --canary",
            "author": {"displayName": "alice"},
        },
    }

    with patch.object(jira_event_router, "_publish_bus") as bus_mock, \
            patch.object(
                jira_event_router, "_audit", new=AsyncMock(return_value=None)
            ) as audit_mock:
        result = await jira_event_router.handle_comment_created(event)

    assert result == {
        "status": "dispatched",
        "command": "deploy",
        "issue_key": "OPS-101",
    }
    bus_mock.assert_called_once()
    topic, payload = bus_mock.call_args.args
    assert topic == "jira_command"
    assert payload == {
        "issue_key": "OPS-101",
        "command": "deploy",
        "args": "prod --canary",
        "author": "alice",
        "comment_id": "c-101",
    }
    audit_mock.assert_awaited_once()
    assert audit_mock.await_args.args[0] == "jira.command_received"


@pytest.mark.asyncio
async def test_comment_handler_negative_wrong_event_type_skips_handler():
    """Dispatcher fed a ``jira:issue_updated`` event MUST NOT call the
    comment handler — the bus stays silent, the comment-handler audit
    string is never written. This is the "wrong event type" negative
    spelled out in the user bullet.

    Driven through the ``_on_jira_event`` dispatcher (not the handler
    directly) because "wrong event type" is by definition a routing-
    layer concern — bypassing the dispatcher would be testing the
    wrong contract.
    """
    from backend import jira_event_router
    from backend.db_context import set_tenant_id
    from backend.routers.webhooks import _on_jira_event

    set_tenant_id(None)

    # Comment-shaped payload, but webhookEvent says "issue_updated".
    # The dispatcher must route to the issue-updated handler (which
    # sees no status change → ignored), and the comment handler must
    # not run.
    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "OPS-102"},
        "comment": {
            "id": "c-102",
            "body": "/deploy prod",
            "author": {"displayName": "bob"},
        },
        # No changelog → issue_updated handler returns "no_status_change".
    }

    with patch.object(jira_event_router, "_publish_bus") as bus_mock, \
            patch.object(
                jira_event_router, "handle_comment_created",
                new=AsyncMock(),
            ) as comment_mock, \
            patch.object(
                jira_event_router, "_audit", new=AsyncMock(return_value=None)
            ):
        await _on_jira_event(event)

    comment_mock.assert_not_awaited()
    bus_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
#  Handler 2 — jira:issue_updated → status transition → packaging
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_updated_handler_positive_done_status_packages():
    """Status ``In Progress`` → ``Done`` triggers artifact packaging.

    The packaging coroutine is spawned via ``asyncio.create_task`` from
    inside the handler, so we patch the import target on the
    ``backend.routers.webhooks`` module (the lazy import site) and
    assert the AsyncMock was invoked with the ``jira:<issue_key>``
    artifact-id shape that the bullet spec mandates.
    """
    from backend import jira_event_router

    event = {
        "issue": {"key": "OPS-201", "fields": {"summary": "ship the bits"}},
        "changelog": {
            "items": [
                {"field": "status",
                 "fromString": "In Progress",
                 "toString": "Done"},
            ],
        },
    }

    pkg_mock = AsyncMock(return_value=None)
    with patch(
        "backend.routers.webhooks._package_merged_artifacts", new=pkg_mock,
    ), patch.object(
        jira_event_router, "_audit", new=AsyncMock(return_value=None),
    ) as audit_mock:
        result = await jira_event_router.handle_issue_updated(event)
        # Yield once so the asyncio.create_task() spawn actually starts.
        import asyncio
        await asyncio.sleep(0)

    assert result == {
        "status": "dispatched",
        "issue_key": "OPS-201",
        "from": "In Progress",
        "to": "Done",
    }
    pkg_mock.assert_awaited_once_with("jira:OPS-201", "ship the bits")
    audit_mock.assert_awaited_once()
    assert audit_mock.await_args.args[0] == "jira.status_transitioned"


@pytest.mark.asyncio
async def test_issue_updated_handler_negative_non_whitelisted_status_skips():
    """Status ``In Progress`` → ``In Review`` MUST NOT package artifacts.

    ``In Review`` is not in the default ``Done,Closed`` whitelist, so
    the handler must return an ``ignored / status_not_whitelisted``
    result, the packaging pipeline must not be invoked, and no
    ``jira.status_transitioned`` audit row is written.
    """
    from backend import jira_event_router

    event = {
        "issue": {"key": "OPS-202", "fields": {"summary": "WIP"}},
        "changelog": {
            "items": [
                {"field": "status",
                 "fromString": "In Progress",
                 "toString": "In Review"},
            ],
        },
    }

    pkg_mock = AsyncMock(return_value=None)
    with patch(
        "backend.routers.webhooks._package_merged_artifacts", new=pkg_mock,
    ), patch.object(
        jira_event_router, "_audit", new=AsyncMock(return_value=None),
    ) as audit_mock:
        result = await jira_event_router.handle_issue_updated(event)

    assert result == {
        "status": "ignored",
        "reason": "status_not_whitelisted",
        "from": "In Progress",
        "to": "In Review",
    }
    pkg_mock.assert_not_called()
    audit_mock.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────
#  Handler 3 — jira:issue_created → intake label → intent_bridge
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_created_handler_positive_intake_label_invokes_bridge():
    """Issue carrying the configured intake label calls intent_bridge.

    Asserts the bridge invocation shape spelled out by the bullet
    (parent / vendor / cards / dag_id) plus the ``intake_label`` echoed
    in the result. Audit row spelling pinned by the audit mock check.
    """
    from backend import jira_event_router

    event = {
        "issue": {
            "key": "OPS-301",
            "fields": {"labels": ["omnisight-intake", "frontend"]},
        },
    }

    bridge_mock = AsyncMock(return_value=None)
    with patch(
        "backend.intent_bridge.on_intake_queued", new=bridge_mock,
    ), patch.object(
        jira_event_router, "_audit", new=AsyncMock(return_value=None),
    ) as audit_mock:
        result = await jira_event_router.handle_issue_created(event)

    assert result == {
        "status": "dispatched",
        "issue_key": "OPS-301",
        "intake_label": "omnisight-intake",
    }
    bridge_mock.assert_awaited_once_with(
        parent="OPS-301",
        vendor="jira",
        cards_with_task_ids=[],
        dag_id="jira-intake:OPS-301",
    )
    audit_mock.assert_awaited_once()
    assert audit_mock.await_args.args[0] == "jira.intake_triggered"


@pytest.mark.asyncio
async def test_issue_created_handler_negative_missing_label_skips():
    """Issue without the configured intake label MUST NOT call the bridge.

    The bullet spec's third negative ("missing label" must not trigger).
    Returns ``ignored / missing_intake_label`` and writes no audit row.
    """
    from backend import jira_event_router

    event = {
        "issue": {
            "key": "OPS-302",
            "fields": {"labels": ["bug", "p1"]},
        },
    }

    bridge_mock = AsyncMock(return_value=None)
    with patch(
        "backend.intent_bridge.on_intake_queued", new=bridge_mock,
    ), patch.object(
        jira_event_router, "_audit", new=AsyncMock(return_value=None),
    ) as audit_mock:
        result = await jira_event_router.handle_issue_created(event)

    assert result == {
        "status": "ignored",
        "reason": "missing_intake_label",
        "labels": ["bug", "p1"],
    }
    bridge_mock.assert_not_called()
    audit_mock.assert_not_awaited()
