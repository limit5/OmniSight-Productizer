"""Phase-3-Runtime-v2 SP-3.4 — contract tests for ported notification
db.py functions.

Replaces the SQLite-backed ``test_notifications_full_lifecycle`` in
``test_db.py`` (moved out because the ported signatures require
asyncpg + pool — see that file's header comment for rationale).

Coverage:
  * Six functions: insert_notification / list_notifications /
    mark_notification_read / count_unread_notifications /
    update_notification_dispatch / list_failed_notifications.
  * Boolean round-trip fidelity (PG INTEGER 0/1 → Python bool via
    the _notification_row_to_dict marshaller).
  * Level filter + ORDER BY timestamp DESC + LIMIT semantics.
  * Dispatch-status lifecycle (pending → failed → dead / sent).
  * Dynamic ``IN ($1, $2, ...)`` placeholder count for
    count_unread_notifications is injection-safe (level values come
    from a hardcoded dict, not user input).
  * Error paths: NOT NULL violations (title).

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db


# ─── Helpers ─────────────────────────────────────────────────────


def _notif_fixture(**overrides) -> dict:
    base = {
        "id": "n-test",
        "level": "info",
        "title": "Test notification",
        "message": "body",
        "source": "test",
        "timestamp": "2026-04-20T00:00:00",
        "read": False,
        "action_url": None,
        "action_label": None,
        "auto_resolved": False,
    }
    base.update(overrides)
    return base


# ─── Happy path: full lifecycle round-trip ───────────────────────


class TestNotificationsLifecycle:
    @pytest.mark.asyncio
    async def test_empty_list_on_fresh_state(self, pg_test_conn) -> None:
        assert await db.list_notifications(pg_test_conn) == []

    @pytest.mark.asyncio
    async def test_insert_then_list(self, pg_test_conn) -> None:
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n1", level="warning", title="Disk almost full",
        ))
        rows = await db.list_notifications(pg_test_conn)
        assert len(rows) == 1
        assert rows[0]["id"] == "n1"
        assert rows[0]["level"] == "warning"
        assert rows[0]["title"] == "Disk almost full"

    @pytest.mark.asyncio
    async def test_bool_fields_round_trip(self, pg_test_conn) -> None:
        # read + auto_resolved are stored as INTEGER 0/1 but the
        # _notification_row_to_dict marshaller coerces to bool so
        # callers (router responses, Pydantic models) see native bools.
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-bool", read=True, auto_resolved=True,
        ))
        rows = await db.list_notifications(pg_test_conn)
        assert rows[0]["read"] is True
        assert rows[0]["auto_resolved"] is True

    @pytest.mark.asyncio
    async def test_level_filter(self, pg_test_conn) -> None:
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-info", level="info", timestamp="2026-04-20T00:00:00",
        ))
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-crit", level="critical", timestamp="2026-04-20T00:00:01",
        ))
        crit = await db.list_notifications(pg_test_conn, level="critical")
        assert len(crit) == 1 and crit[0]["id"] == "n-crit"
        info = await db.list_notifications(pg_test_conn, level="info")
        assert len(info) == 1 and info[0]["id"] == "n-info"

    @pytest.mark.asyncio
    async def test_list_orders_newest_first(self, pg_test_conn) -> None:
        # ORDER BY timestamp DESC — the column is TEXT and the caller
        # supplies ISO timestamps, so string-sort is chronological.
        for i in range(3):
            await db.insert_notification(pg_test_conn, _notif_fixture(
                id=f"n-ord-{i}", timestamp=f"2026-04-20T00:00:0{i}",
            ))
        rows = await db.list_notifications(pg_test_conn)
        assert [r["id"] for r in rows] == ["n-ord-2", "n-ord-1", "n-ord-0"]

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, pg_test_conn) -> None:
        for i in range(5):
            await db.insert_notification(pg_test_conn, _notif_fixture(
                id=f"n-lim-{i}", timestamp=f"2026-04-20T00:00:0{i}",
            ))
        rows = await db.list_notifications(pg_test_conn, limit=2)
        assert len(rows) == 2


# ─── Mark-read + unread counts ───────────────────────────────────


class TestNotificationsReadState:
    @pytest.mark.asyncio
    async def test_mark_read_returns_true_on_match(
        self, pg_test_conn,
    ) -> None:
        await db.insert_notification(pg_test_conn, _notif_fixture(id="n-mr"))
        assert await db.mark_notification_read(pg_test_conn, "n-mr") is True
        rows = await db.list_notifications(pg_test_conn)
        assert rows[0]["read"] is True

    @pytest.mark.asyncio
    async def test_mark_read_returns_false_on_miss(
        self, pg_test_conn,
    ) -> None:
        assert await db.mark_notification_read(
            pg_test_conn, "nonexistent",
        ) is False

    @pytest.mark.asyncio
    async def test_unread_count_min_level_warning(
        self, pg_test_conn,
    ) -> None:
        # warning = rank 1 → counts warning/action/critical, excludes info.
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-i", level="info",
        ))
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-w", level="warning",
        ))
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-c", level="critical",
        ))
        assert await db.count_unread_notifications(
            pg_test_conn, min_level="warning",
        ) == 2

    @pytest.mark.asyncio
    async def test_unread_count_min_level_critical(
        self, pg_test_conn,
    ) -> None:
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-w", level="warning",
        ))
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-c", level="critical",
        ))
        assert await db.count_unread_notifications(
            pg_test_conn, min_level="critical",
        ) == 1

    @pytest.mark.asyncio
    async def test_mark_read_decrements_unread_count(
        self, pg_test_conn,
    ) -> None:
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-a", level="warning",
        ))
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-b", level="warning",
        ))
        assert await db.count_unread_notifications(
            pg_test_conn, min_level="warning",
        ) == 2
        await db.mark_notification_read(pg_test_conn, "n-a")
        assert await db.count_unread_notifications(
            pg_test_conn, min_level="warning",
        ) == 1


# ─── Dispatch status + failed list (DLQ contract) ────────────────


class TestNotificationsDispatch:
    @pytest.mark.asyncio
    async def test_update_dispatch_marks_failed(
        self, pg_test_conn,
    ) -> None:
        await db.insert_notification(pg_test_conn, _notif_fixture(
            id="n-fail", level="warning",
        ))
        await db.update_notification_dispatch(
            pg_test_conn, "n-fail", "failed",
            attempts=2, error="slack timeout",
        )
        failed = await db.list_failed_notifications(pg_test_conn)
        assert len(failed) == 1
        assert failed[0]["id"] == "n-fail"
        assert failed[0]["send_attempts"] == 2
        assert failed[0]["last_error"] == "slack timeout"

    @pytest.mark.asyncio
    async def test_list_failed_excludes_sent(self, pg_test_conn) -> None:
        # Contract: list_failed_notifications returns ONLY
        # dispatch_status='failed' rows — sent / skipped / dead are
        # out. Regression guard for the WHERE clause.
        for i, (nid, status) in enumerate([
            ("n-sent", "sent"), ("n-dead", "dead"),
            ("n-skip", "skipped"), ("n-fail", "failed"),
        ]):
            await db.insert_notification(pg_test_conn, _notif_fixture(
                id=nid, timestamp=f"2026-04-20T00:00:0{i}",
            ))
            await db.update_notification_dispatch(
                pg_test_conn, nid, status, attempts=1,
            )
        failed = await db.list_failed_notifications(pg_test_conn)
        assert [r["id"] for r in failed] == ["n-fail"]

    @pytest.mark.asyncio
    async def test_list_failed_respects_limit(self, pg_test_conn) -> None:
        for i in range(5):
            await db.insert_notification(pg_test_conn, _notif_fixture(
                id=f"n-lim-{i}", timestamp=f"2026-04-20T00:00:0{i}",
            ))
            await db.update_notification_dispatch(
                pg_test_conn, f"n-lim-{i}", "failed", attempts=1,
            )
        rows = await db.list_failed_notifications(pg_test_conn, limit=3)
        assert len(rows) == 3


# ─── Concurrency + error paths ───────────────────────────────────


class TestNotificationsConcurrency:
    @pytest.mark.asyncio
    async def test_parallel_inserts(self, pg_test_pool) -> None:
        import asyncio

        async def _worker(nid: str) -> None:
            async with pg_test_pool.acquire() as conn:
                await db.insert_notification(conn, _notif_fixture(id=nid))

        await asyncio.gather(*[_worker(f"n-conc-{i}") for i in range(5)])
        async with pg_test_pool.acquire() as conn:
            rows = await db.list_notifications(conn)
            got = {r["id"] for r in rows}
            expected = {f"n-conc-{i}" for i in range(5)}
            assert expected.issubset(got)
            # Cleanup
            for nid in expected:
                await conn.execute(
                    "DELETE FROM notifications WHERE id = $1", nid,
                )


class TestNotificationsErrorPaths:
    @pytest.mark.asyncio
    async def test_insert_rejects_null_title(self, pg_test_conn) -> None:
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.insert_notification(pg_test_conn, {
                "id": "n-null", "level": "info", "title": None,
            })

    @pytest.mark.asyncio
    async def test_insert_missing_required_key_raises(
        self, pg_test_conn,
    ) -> None:
        # insert_notification expects ``id`` / ``level`` / ``title`` —
        # KeyError surfaces in Python before the DB sees a malformed
        # INSERT.
        with pytest.raises(KeyError):
            await db.insert_notification(pg_test_conn, {
                "level": "info", "title": "no-id",
            })
