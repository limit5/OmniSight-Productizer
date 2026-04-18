"""J1 tests — SSE per-session filter: event envelope, session_id derivation."""

from __future__ import annotations

import json



def test_session_id_from_token_deterministic():
    from backend.auth import session_id_from_token
    token = "abc123-test-token"
    sid1 = session_id_from_token(token)
    sid2 = session_id_from_token(token)
    assert sid1 == sid2
    assert len(sid1) == 16


def test_session_id_from_token_different_tokens():
    from backend.auth import session_id_from_token
    sid_a = session_id_from_token("token-a")
    sid_b = session_id_from_token("token-b")
    assert sid_a != sid_b


def test_event_envelope_includes_session_metadata():
    from backend.events import EventBus
    bus = EventBus()
    q = bus.subscribe()

    bus.publish("agent_update", {
        "agent_id": "a1", "status": "running", "thought_chain": "",
    }, session_id="sess-123", broadcast_scope="session")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_session_id"] == "sess-123"
    assert data["_broadcast_scope"] == "session"
    bus.unsubscribe(q)


def test_event_envelope_defaults():
    from backend.events import EventBus
    bus = EventBus()
    q = bus.subscribe()

    bus.publish("heartbeat", {"subscribers": 1})

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_session_id"] == ""
    assert data["_broadcast_scope"] == "global"
    bus.unsubscribe(q)


def test_emit_agent_update_passes_session_id():
    from backend.events import bus, emit_agent_update
    q = bus.subscribe()

    emit_agent_update("a1", "running", session_id="sess-x", broadcast_scope="session")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_session_id"] == "sess-x"
    assert data["_broadcast_scope"] == "session"
    bus.unsubscribe(q)


def test_emit_task_update_passes_session_id():
    from backend.events import bus, emit_task_update
    q = bus.subscribe()

    emit_task_update("t1", "done", session_id="sess-y", broadcast_scope="user")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_session_id"] == "sess-y"
    assert data["_broadcast_scope"] == "user"
    bus.unsubscribe(q)


def test_backward_compat_no_session_id():
    """Events without explicit session_id still work (empty string defaults)."""
    from backend.events import bus, emit_pipeline_phase
    q = bus.subscribe()

    emit_pipeline_phase("build", "started")

    msg = q.get_nowait()
    data = json.loads(msg["data"])
    assert data["_session_id"] == ""
    assert data["_broadcast_scope"] == "global"
    bus.unsubscribe(q)
