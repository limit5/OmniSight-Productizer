"""Tests for event persistence, DLQ, and replay (Phase 21)."""

import json

import pytest


class TestEventLog:
    """SP-3.10 (2026-04-20): migrated from SQLite db.init/close pattern
    to pg_test_conn. The ``days=0`` cleanup assertion was removed —
    it's inherently racy at second-resolution created_at (see
    test_db_events.py for deterministic ``days=-1`` / ``days=365``
    boundaries).
    """

    @pytest.mark.asyncio
    async def test_insert_and_list_events(self, pg_test_conn):
        from backend import db
        await db.insert_event(pg_test_conn, "agent_update", json.dumps({"agent_id": "a1", "status": "running"}))
        await db.insert_event(pg_test_conn, "task_update", json.dumps({"task_id": "t1", "status": "completed"}))
        events = await db.list_events(pg_test_conn, limit=10)
        assert len(events) >= 2
        types = [e["event_type"] for e in events]
        assert "agent_update" in types
        assert "task_update" in types

    @pytest.mark.asyncio
    async def test_list_events_by_type(self, pg_test_conn):
        from backend import db
        await db.insert_event(pg_test_conn, "simulation", json.dumps({"sim_id": "s1"}))
        events = await db.list_events(pg_test_conn, event_types=["simulation"], limit=5)
        assert all(e["event_type"] == "simulation" for e in events)

    @pytest.mark.asyncio
    async def test_cleanup_old_events_returns_int(self, pg_test_conn):
        # Semantic contract: the function returns an int. Deterministic
        # boundary behaviour is tested in test_db_events.py::
        # TestEventsCleanup (far-future / far-past days values).
        from backend import db
        removed = await db.cleanup_old_events(pg_test_conn, days=365)
        assert isinstance(removed, int)


class TestReplayEndpoint:

    @pytest.mark.asyncio
    async def test_replay_returns_list(self, client):
        resp = await client.get("/api/v1/events/replay")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_replay_with_type_filter(self, client):
        resp = await client.get("/api/v1/events/replay?types=agent_update&limit=5")
        assert resp.status_code == 200
        data = resp.json()
        for ev in data:
            assert ev["event"] == "agent_update"

    @pytest.mark.asyncio
    async def test_replay_with_limit(self, client):
        resp = await client.get("/api/v1/events/replay?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) <= 3


class TestNotificationDLQ:

    @pytest.mark.asyncio
    async def test_dispatch_status_columns_exist(self, pg_test_conn):
        # SP-3.4 (2026-04-20): pg_test_conn savepoint-wraps the insert
        # + update + SELECT so this column-presence smoke test never
        # leaks rows into sibling tests.
        from backend import db
        import uuid
        nid = f"notif-dlq-{uuid.uuid4().hex[:6]}"
        await db.insert_notification(pg_test_conn, {
            "id": nid, "level": "warning", "title": "Test DLQ",
            "message": "test", "source": "test", "timestamp": "2026-01-01T00:00:00",
            "read": 0, "action_url": None, "action_label": None,
            "auto_resolved": 0, "dispatch_status": "pending",
            "send_attempts": 0, "last_error": None,
        })
        await db.update_notification_dispatch(
            pg_test_conn, nid, "failed", attempts=3, error="slack down",
        )
        failed = await db.list_failed_notifications(pg_test_conn, limit=5)
        assert any(f["id"] == nid for f in failed)
        match = next(f for f in failed if f["id"] == nid)
        assert match["dispatch_status"] == "failed"


class TestEventBusQueueLimit:

    def test_subscribe_creates_bounded_queue(self):
        from backend.events import EventBus
        bus = EventBus()
        q = bus.subscribe()
        assert q.maxsize == 1000

    def test_persist_event_types(self):
        from backend.events import _PERSIST_EVENT_TYPES
        assert "agent_update" in _PERSIST_EVENT_TYPES
        assert "task_update" in _PERSIST_EVENT_TYPES
        assert "simulation" in _PERSIST_EVENT_TYPES
        assert "invoke" in _PERSIST_EVENT_TYPES
        # debug_finding and notification have their own dedicated persistence
        assert "debug_finding" not in _PERSIST_EVENT_TYPES
        assert "notification" not in _PERSIST_EVENT_TYPES
        # High-frequency events should NOT be persisted
        assert "tool_progress" not in _PERSIST_EVENT_TYPES
        assert "heartbeat" not in _PERSIST_EVENT_TYPES
        assert "pipeline" not in _PERSIST_EVENT_TYPES


class TestNotificationConfig:

    def test_retry_config_exists(self):
        from backend.config import settings
        assert hasattr(settings, "notification_max_retries")
        assert hasattr(settings, "notification_retry_backoff")
        assert settings.notification_max_retries == 3
        assert settings.notification_retry_backoff == 30
