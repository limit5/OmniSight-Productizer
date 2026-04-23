"""Q.3-SUB-2 (#297) — Task CRUD cross-device SSE sync.

Before Q.3-SUB-2 only ``PATCH /tasks/{id}`` emitted ``task_update`` —
device A creating or deleting a task stayed invisible to device B
until the next full-list poll. This suite locks the fix:

  * ``test_task_emit_on_create`` — ``POST /tasks`` emits exactly one
    ``task_update`` SSE event with ``action='created'`` and the
    newly-assigned task_id.
  * ``test_task_emit_on_delete`` — ``DELETE /tasks/{id}`` emits
    exactly one ``task_update`` SSE event with ``action='deleted'``.
  * ``test_task_emit_dispatcher_action_contract`` — payload shape
    contract so the frontend dispatcher can switch on ``action``
    without inspecting ``status``.

Audit evidence: ``docs/design/multi-device-state-sync.md`` Path 4.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.events import bus as _bus


async def _drain_for_task_event(queue, task_id: str, timeout: float = 2.0):
    """Drain the SSE queue until a ``task_update`` for ``task_id``
    arrives, or return None on timeout.

    Filters out heartbeats and other events so ambient chatter from
    sibling fixtures doesn't masquerade as the Q.3-SUB-2 payload.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        if msg.get("event") != "task_update":
            continue
        data = json.loads(msg["data"])
        if data.get("task_id") != task_id:
            continue
        return data
    return None


@pytest.mark.asyncio
async def test_task_emit_on_create(client):
    """POST /tasks must emit a ``task_update`` SSE event tagged
    ``action='created'`` so other devices can append the new row
    without waiting for the next poll.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Cross-device create",
                "description": "Q.3-SUB-2 emit test",
                "priority": "medium",
            },
        )
        assert res.status_code == 201, res.text
        body = res.json()
        task_id = body["id"]

        data = await _drain_for_task_event(q, task_id)
        assert data is not None, (
            "POST /tasks must publish a task_update event — "
            "cross-device append would otherwise wait for the next "
            "listTasks poll."
        )
        assert data["action"] == "created"
        assert data["task_id"] == task_id
        # Fresh tasks start in the backlog with no agent assigned.
        assert data["status"] == "backlog"
        assert data["assigned_agent_id"] is None
    finally:
        _bus.unsubscribe(q)
        # Clean up so the in-memory mirror doesn't leak between tests.
        from backend.routers import tasks as _tasks_router
        _tasks_router._tasks.pop(task_id, None)


@pytest.mark.asyncio
async def test_task_emit_on_delete(client):
    """DELETE /tasks/{id} must emit a ``task_update`` SSE event
    tagged ``action='deleted'`` so other devices can remove the row
    immediately. Pre-Q.3-SUB-2 this went un-broadcast.
    """
    # Seed a task via the HTTP surface so the in-memory mirror AND
    # the DB row are both populated — matches how production traffic
    # reaches the DELETE handler.
    create = await client.post(
        "/api/v1/tasks",
        json={"title": "To be deleted", "priority": "low"},
    )
    assert create.status_code == 201, create.text
    task_id = create.json()["id"]

    # Subscribe AFTER the create emit so we don't conflate the two
    # events; we only want the delete-specific payload on the queue.
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.delete(f"/api/v1/tasks/{task_id}")
        assert res.status_code == 204, res.text

        data = await _drain_for_task_event(q, task_id)
        assert data is not None, (
            "DELETE /tasks must publish a task_update event — "
            "cross-device drop would otherwise wait for the next "
            "listTasks poll."
        )
        assert data["action"] == "deleted"
        assert data["task_id"] == task_id
    finally:
        _bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_task_emit_dispatcher_action_contract(client):
    """Payload shape contract: ``action`` is the canonical field the
    frontend dispatcher switches on. Guards against a refactor that
    moves the discriminator onto another key (e.g. status='deleted'
    alone) which would silently break the dispatcher's ``deleted``
    branch — the frontend reads ``data.action``, not ``data.status``.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        created = await client.post(
            "/api/v1/tasks",
            json={"title": "Action contract", "priority": "low"},
        )
        assert created.status_code == 201, created.text
        tid = created.json()["id"]

        create_evt = await _drain_for_task_event(q, tid)
        assert create_evt is not None
        assert "action" in create_evt
        assert create_evt["action"] == "created"

        deleted = await client.delete(f"/api/v1/tasks/{tid}")
        assert deleted.status_code == 204, deleted.text

        delete_evt = await _drain_for_task_event(q, tid)
        assert delete_evt is not None
        assert "action" in delete_evt
        assert delete_evt["action"] == "deleted"
    finally:
        _bus.unsubscribe(q)
