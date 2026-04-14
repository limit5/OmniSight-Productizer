"""Fix-B B2/B6 — formerly silent catches now log and bump a counter."""

from __future__ import annotations

import logging
import os
import tempfile
import uuid

import pytest


@pytest.mark.asyncio
async def test_notifications_skipped_persist_failure_is_logged_and_metered(
    monkeypatch, caplog,
):
    from backend import db, metrics as m, notifications as n
    from backend.models import Notification

    # Use a real DB then break the update method to simulate persistence failure.
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", os.path.join(tmp, "t.db"))
        from backend import config as cfg
        cfg.settings.database_path = os.path.join(tmp, "t.db")
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        m.reset_for_tests()

        async def _boom(*_a, **_kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "update_notification_dispatch", _boom)

        # Ensure no external channels so we hit the "skipped" branch.
        from backend.config import settings
        monkeypatch.setattr(settings, "notification_slack_webhook", "", raising=False)
        monkeypatch.setattr(settings, "notification_jira_url", "", raising=False)
        monkeypatch.setattr(settings, "notification_pagerduty_key", "", raising=False)

        notif = Notification(
            id=f"notif-sc-{uuid.uuid4().hex[:6]}",
            level="warning", title="t", message="m", source="test",
            timestamp="2026-04-14T00:00:00",
        )
        caplog.set_level(logging.WARNING, logger="backend.notifications")
        await n._dispatch_external(notif)

        assert any("persist skipped status" in rec.message for rec in caplog.records)
        if m.is_available():
            samples = list(m.persist_failure_total.collect()[0].samples)
            total = sum(s.value for s in samples
                        if s.labels.get("module") == "notifications"
                        and s.name.endswith("_total"))
            assert total >= 1
        await db.close()


def test_persist_failure_metric_is_registered():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    # Must be callable without raising.
    m.persist_failure_total.labels(module="budget_strategy").inc()
    m.persist_failure_total.labels(module="notifications").inc()
