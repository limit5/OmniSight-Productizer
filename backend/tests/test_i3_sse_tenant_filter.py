"""I3 tests — SSE per-tenant + per-user filter.

Verifies:
  - Event envelope includes _tenant_id
  - broadcast_scope="tenant" delivers only to matching tenant subscribers
  - Global/session/user scopes still work across tenants (no regression)
  - Subscriber auto-binds tenant_id
  - _auto_tenant reads from db_context when not explicitly provided
"""

from __future__ import annotations

import json

import pytest


# ─── Event envelope ───

def test_event_envelope_includes_tenant_id():
    from backend.events import EventBus
    bus = EventBus()
    q = bus.subscribe(tenant_id="t-acme")

    bus.publish("agent_update", {
        "agent_id": "a1", "status": "running", "thought_chain": "",
    }, session_id="sess-1", broadcast_scope="tenant", tenant_id="t-acme")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_tenant_id"] == "t-acme"
    assert data["_broadcast_scope"] == "tenant"
    bus.unsubscribe(q)


def test_event_envelope_tenant_id_defaults_empty():
    from backend.events import EventBus
    bus = EventBus()
    q = bus.subscribe()

    bus.publish("heartbeat", {"subscribers": 1})

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_tenant_id"] == ""
    assert data["_broadcast_scope"] == "global"
    bus.unsubscribe(q)


# ─── Tenant-scope filtering (server-side) ───

def test_tenant_scope_delivers_to_matching_subscriber():
    from backend.events import EventBus
    bus = EventBus()
    q_acme = bus.subscribe(tenant_id="t-acme")
    q_globex = bus.subscribe(tenant_id="t-globex")

    bus.publish("task_update", {
        "task_id": "t1", "status": "done", "assigned_agent_id": None,
    }, broadcast_scope="tenant", tenant_id="t-acme")

    assert not q_globex.empty() is False or q_globex.qsize() == 0
    # acme should receive
    msg = q_acme.get_nowait()
    data = json.loads(msg["data"])
    assert data["task_id"] == "t1"
    assert data["_tenant_id"] == "t-acme"

    # globex should NOT receive
    assert q_globex.empty()

    bus.unsubscribe(q_acme)
    bus.unsubscribe(q_globex)


def test_tenant_scope_isolation_a_only_sees_a():
    """A tenant listener only receives A's events — the core I3 requirement."""
    from backend.events import EventBus
    bus = EventBus()
    q_a = bus.subscribe(tenant_id="t-alpha")
    q_b = bus.subscribe(tenant_id="t-beta")

    # Emit 3 events: one for alpha, one for beta, one global
    bus.publish("agent_update", {"agent_id": "a1", "status": "running", "thought_chain": ""},
                broadcast_scope="tenant", tenant_id="t-alpha")
    bus.publish("agent_update", {"agent_id": "a2", "status": "running", "thought_chain": ""},
                broadcast_scope="tenant", tenant_id="t-beta")
    bus.publish("heartbeat", {"subscribers": 2},
                broadcast_scope="global")

    # Alpha: should see own event + global = 2 events
    a_events = []
    while not q_a.empty():
        a_events.append(json.loads(q_a.get_nowait()["data"]))
    assert len(a_events) == 2
    assert a_events[0]["agent_id"] == "a1"
    assert a_events[0]["_tenant_id"] == "t-alpha"
    assert a_events[1]["_broadcast_scope"] == "global"

    # Beta: should see own event + global = 2 events
    b_events = []
    while not q_b.empty():
        b_events.append(json.loads(q_b.get_nowait()["data"]))
    assert len(b_events) == 2
    assert b_events[0]["agent_id"] == "a2"
    assert b_events[0]["_tenant_id"] == "t-beta"
    assert b_events[1]["_broadcast_scope"] == "global"

    bus.unsubscribe(q_a)
    bus.unsubscribe(q_b)


def test_tenant_scope_subscriber_without_tenant_receives_all():
    """Subscribers without tenant_id (e.g. admin) get all tenant-scoped events."""
    from backend.events import EventBus
    bus = EventBus()
    q_admin = bus.subscribe()  # no tenant_id
    q_acme = bus.subscribe(tenant_id="t-acme")

    bus.publish("task_update", {"task_id": "t1", "status": "done", "assigned_agent_id": None},
                broadcast_scope="tenant", tenant_id="t-acme")

    # Admin sees it (sub_tenant is None, filter skips)
    assert not q_admin.empty()
    # Acme sees it (matching tenant)
    assert not q_acme.empty()

    bus.unsubscribe(q_admin)
    bus.unsubscribe(q_acme)


def test_tenant_scope_event_without_tenant_id_delivers_to_all():
    """tenant-scoped event with no tenant_id is delivered to everyone (backward compat)."""
    from backend.events import EventBus
    bus = EventBus()
    q_a = bus.subscribe(tenant_id="t-alpha")
    q_b = bus.subscribe(tenant_id="t-beta")

    bus.publish("task_update", {"task_id": "t1", "status": "done", "assigned_agent_id": None},
                broadcast_scope="tenant", tenant_id=None)

    assert not q_a.empty()
    assert not q_b.empty()

    bus.unsubscribe(q_a)
    bus.unsubscribe(q_b)


# ─── No regression: existing scopes still work across tenants ───

def test_global_scope_reaches_all_tenants():
    from backend.events import EventBus
    bus = EventBus()
    q_a = bus.subscribe(tenant_id="t-alpha")
    q_b = bus.subscribe(tenant_id="t-beta")

    bus.publish("heartbeat", {"subscribers": 2}, broadcast_scope="global")

    assert not q_a.empty()
    assert not q_b.empty()

    bus.unsubscribe(q_a)
    bus.unsubscribe(q_b)


def test_session_scope_reaches_all_tenants():
    from backend.events import EventBus
    bus = EventBus()
    q_a = bus.subscribe(tenant_id="t-alpha")
    q_b = bus.subscribe(tenant_id="t-beta")

    bus.publish("agent_update", {"agent_id": "a1", "status": "running", "thought_chain": ""},
                session_id="sess-1", broadcast_scope="session")

    # Session-scoped events are not filtered server-side by tenant
    assert not q_a.empty()
    assert not q_b.empty()

    bus.unsubscribe(q_a)
    bus.unsubscribe(q_b)


def test_user_scope_reaches_all_tenants():
    from backend.events import EventBus
    bus = EventBus()
    q_a = bus.subscribe(tenant_id="t-alpha")
    q_b = bus.subscribe(tenant_id="t-beta")

    bus.publish("token_warning", {"level": "warn", "message": "test", "usage": 0, "budget": 0},
                broadcast_scope="user")

    assert not q_a.empty()
    assert not q_b.empty()

    bus.unsubscribe(q_a)
    bus.unsubscribe(q_b)


# ─── emit_* convenience functions pass tenant_id ───

def test_emit_agent_update_passes_tenant_id():
    from backend.events import bus, emit_agent_update
    q = bus.subscribe()

    emit_agent_update("a1", "running", tenant_id="t-acme", broadcast_scope="tenant")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_tenant_id"] == "t-acme"
    assert data["_broadcast_scope"] == "tenant"
    bus.unsubscribe(q)


def test_emit_task_update_passes_tenant_id():
    from backend.events import bus, emit_task_update
    q = bus.subscribe()

    emit_task_update("t1", "done", tenant_id="t-globex", broadcast_scope="tenant")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_tenant_id"] == "t-globex"
    bus.unsubscribe(q)


def test_emit_debug_finding_passes_tenant_id():
    from backend.events import bus, emit_debug_finding
    q = bus.subscribe()

    emit_debug_finding(
        task_id="task-1", agent_id="agent-1",
        finding_type="test", severity="info", message="hello",
        tenant_id="t-acme", broadcast_scope="tenant",
    )

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_tenant_id"] == "t-acme"
    bus.unsubscribe(q)


# ─── _auto_tenant reads from db_context ───

def test_auto_tenant_reads_context():
    from backend.db_context import set_tenant_id
    from backend.events import bus, emit_agent_update

    q = bus.subscribe()
    set_tenant_id("t-context")

    emit_agent_update("a1", "running", broadcast_scope="tenant")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_tenant_id"] == "t-context"

    set_tenant_id(None)
    bus.unsubscribe(q)


def test_auto_tenant_explicit_overrides_context():
    from backend.db_context import set_tenant_id
    from backend.events import bus, emit_agent_update

    q = bus.subscribe()
    set_tenant_id("t-context")

    emit_agent_update("a1", "running", tenant_id="t-explicit", broadcast_scope="tenant")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_tenant_id"] == "t-explicit"

    set_tenant_id(None)
    bus.unsubscribe(q)


# ─── Multi-tenant end-to-end scenario ───

def test_multi_tenant_e2e_scenario():
    """Full scenario: 3 tenants, mixed scopes, verify isolation."""
    from backend.events import EventBus
    bus = EventBus()
    q_a = bus.subscribe(tenant_id="t-a")
    q_b = bus.subscribe(tenant_id="t-b")
    q_c = bus.subscribe(tenant_id="t-c")

    # Tenant-scoped for A
    bus.publish("agent_update", {"agent_id": "a1", "status": "running", "thought_chain": ""},
                broadcast_scope="tenant", tenant_id="t-a")
    # Tenant-scoped for B
    bus.publish("agent_update", {"agent_id": "a2", "status": "error", "thought_chain": ""},
                broadcast_scope="tenant", tenant_id="t-b")
    # Global
    bus.publish("heartbeat", {"subscribers": 3}, broadcast_scope="global")
    # Tenant-scoped for C
    bus.publish("task_update", {"task_id": "t1", "status": "done", "assigned_agent_id": None},
                broadcast_scope="tenant", tenant_id="t-c")

    def drain(q):
        events = []
        while not q.empty():
            events.append(json.loads(q.get_nowait()["data"]))
        return events

    a_events = drain(q_a)
    b_events = drain(q_b)
    c_events = drain(q_c)

    # A: own event + global = 2
    assert len(a_events) == 2
    assert a_events[0]["agent_id"] == "a1"

    # B: own event + global = 2
    assert len(b_events) == 2
    assert b_events[0]["agent_id"] == "a2"

    # C: global + own task = 2
    assert len(c_events) == 2
    assert c_events[0]["_broadcast_scope"] == "global"
    assert c_events[1]["task_id"] == "t1"

    bus.unsubscribe(q_a)
    bus.unsubscribe(q_b)
    bus.unsubscribe(q_c)
