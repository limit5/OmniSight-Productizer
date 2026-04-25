"""R9 row 2935 (#315) — contract tests for the severity-tag spec.

Locks the design-decision row's three deliverables:

  1. ``Severity`` is exactly the enum {P1, P2, P3} — no broader, no
     narrower (a future P0 / P4 must be a deliberate spec change with
     a corresponding TODO row, not silent expansion).
  2. ``SEVERITY_TIER_MAPPING`` matches the row 2935 sub-bullets
     verbatim — P1 reaches L4 PagerDuty + L4 SMS + L3 Jira + L2 IM
     (Slack/Discord @everyone); P2 reaches L3 Jira + L2 ChatOps
     interactive; P3 reaches L1 log + email digest.
  3. ``Notification.severity`` is an *optional* tag (None for legacy
     callers) — locking this guards against a future "let's just make
     it required" PR that would silently break 40+ existing
     ``notify(level=...)`` call-sites.
  4. ``notify(severity=...)`` plumbs the tag through to
     ``Notification.severity`` *and* the ``bus.publish('notification',
     ...)`` SSE payload so the frontend notification-center can
     render the badge without a follow-up REST round-trip. None
     callers must produce ``severity=None`` on the wire (NOT missing
     key, NOT empty string) — frontend `data.severity ?? null` parses
     either but the explicit-None form is the contract.
  5. ``insert_notification`` persists the tag to PG (covered by the
     PG-backed lifecycle test below; skipped without OMNI_TEST_PG_URL).

This file is intentionally split from ``test_db_notifications.py``
because the spec assertions (#1, #2, #3) are pure-Python and run on
every CI matrix; only the PG-backed wire-through (#4, #5) needs the
test PG.

SOP Step 4 drift-guard: assertion #2 below is the ONLY place the
mapping is mirrored in test code. If a future PR edits
``SEVERITY_TIER_MAPPING`` without a matching TODO row entry, this
test breaks loudly.
"""

from __future__ import annotations

import pytest

from backend import severity as sev
from backend.models import Notification, Severity
from backend.severity import (
    L1_LOG_EMAIL,
    L2_CHATOPS_INTERACTIVE,
    L2_IM_WEBHOOK,
    L3_JIRA,
    L4_PAGERDUTY,
    L4_SMS,
    SEVERITY_LEVEL_FLOOR,
    SEVERITY_TIER_MAPPING,
    level_floor_for,
    tiers_for,
)


# ─────────────────────────────────────────────────────────────────
#  #1 — Severity enum shape
# ─────────────────────────────────────────────────────────────────


class TestSeverityEnum:
    def test_exactly_three_severities(self) -> None:
        # Lock the cardinality. A future P0 / P4 must be a deliberate
        # spec change — silent additions that don't update the
        # mapping below would silently leak unmapped severities to
        # the dispatcher.
        assert {s.value for s in Severity} == {"P1", "P2", "P3"}

    def test_severity_values_are_strings(self) -> None:
        # str-Enum so JSON serialisation is human-readable AND
        # round-trip-safe via Pydantic. Locks against a future "let's
        # make it IntEnum for sort order" refactor that would break
        # the wire format.
        for s in Severity:
            assert isinstance(s.value, str)
            assert s.value == s.name  # P1.value == "P1", not "p1" or "1"

    def test_models_severity_is_same_enum(self) -> None:
        # ``backend.models.Severity`` and ``backend.severity.Severity``
        # must be the same object — duplicating the enum across
        # modules creates the classic "isinstance check fails because
        # two distinct classes happen to have the same name" footgun.
        assert Severity is sev.Severity


# ─────────────────────────────────────────────────────────────────
#  #2 — SEVERITY_TIER_MAPPING matches row 2935 verbatim
# ─────────────────────────────────────────────────────────────────


class TestSeverityTierMappingSpec:
    def test_p1_activates_l4_pagerduty_l4_sms_l3_jira_l2_im(self) -> None:
        # Row 2935 sub-bullet 1: P1 → L4 PagerDuty + L3 Jira (severity:
        # P1) + L2 Slack/Discord @everyone + SMS.
        assert SEVERITY_TIER_MAPPING[Severity.P1] == frozenset({
            L4_PAGERDUTY, L4_SMS, L3_JIRA, L2_IM_WEBHOOK,
        })

    def test_p2_activates_l3_jira_l2_chatops_interactive(self) -> None:
        # Row 2935 sub-bullet 2: P2 → L3 Jira (severity: P2, label:
        # blocked) + L2 ChatOps interactive (R1).
        assert SEVERITY_TIER_MAPPING[Severity.P2] == frozenset({
            L3_JIRA, L2_CHATOPS_INTERACTIVE,
        })

    def test_p3_activates_only_l1_log_email(self) -> None:
        # Row 2935 sub-bullet 3: P3 → L1 log + email digest. No L2/L3/L4
        # because P3 is "auto-recovery in progress" — operators do not
        # need to be paged or even Slack-pinged for these.
        assert SEVERITY_TIER_MAPPING[Severity.P3] == frozenset({L1_LOG_EMAIL})

    def test_p1_l2_chatops_interactive_is_NOT_set(self) -> None:
        # Negative assertion: P1 fans out to ``@everyone`` Slack
        # webhook, NOT the ChatOps interactive bridge — a P1 system
        # crash needs a broadcast, not an interactive button card.
        # ChatOps interactive is the *P2* fan-out destination.
        assert L2_CHATOPS_INTERACTIVE not in SEVERITY_TIER_MAPPING[Severity.P1]

    def test_p2_im_webhook_is_NOT_set(self) -> None:
        # Negative assertion: P2 goes through ChatOps interactive (R1
        # bridge), NOT the plain Slack/Discord IM webhook. Locking
        # this guard prevents a "well, both are Slack, just use the
        # webhook" simplification that would lose the
        # interactive-button affordance for P2 task-stuck cases.
        assert L2_IM_WEBHOOK not in SEVERITY_TIER_MAPPING[Severity.P2]

    def test_mapping_is_immutable(self) -> None:
        # MappingProxyType wraps the spec — any runtime mutation
        # attempt should raise. Locks against a future "let's just
        # patch it for tests" anti-pattern that would let the spec
        # drift silently between test and prod.
        with pytest.raises(TypeError):
            SEVERITY_TIER_MAPPING[Severity.P1] = frozenset()  # type: ignore[index]

    def test_mapping_covers_every_severity(self) -> None:
        # Drift guard: every Severity enum value must have an entry.
        # If a future PR adds Severity.P0 without updating the
        # mapping, this test catches the gap before the dispatcher
        # silently drops P0 events.
        for s in Severity:
            assert s in SEVERITY_TIER_MAPPING
            assert isinstance(SEVERITY_TIER_MAPPING[s], frozenset)
            assert len(SEVERITY_TIER_MAPPING[s]) > 0

    def test_level_floor_covers_every_severity(self) -> None:
        for s in Severity:
            assert s in SEVERITY_LEVEL_FLOOR
        assert SEVERITY_LEVEL_FLOOR[Severity.P1] == "critical"
        assert SEVERITY_LEVEL_FLOOR[Severity.P2] == "action"
        assert SEVERITY_LEVEL_FLOOR[Severity.P3] == "info"


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────


class TestSeverityHelpers:
    def test_tiers_for_accepts_enum(self) -> None:
        assert tiers_for(Severity.P1) == SEVERITY_TIER_MAPPING[Severity.P1]

    def test_tiers_for_accepts_string(self) -> None:
        # Ergonomic: callers reading the tag from JSON / DB get strings,
        # not enum instances — accept both.
        assert tiers_for("P1") == SEVERITY_TIER_MAPPING[Severity.P1]
        assert tiers_for("P2") == SEVERITY_TIER_MAPPING[Severity.P2]
        assert tiers_for("P3") == SEVERITY_TIER_MAPPING[Severity.P3]

    def test_tiers_for_unknown_returns_empty(self) -> None:
        # Unknown / typo'd severity → empty set, NOT exception. The
        # dispatcher then falls back to plain level routing — the
        # honest "no severity tag" semantics.
        assert tiers_for("P0") == frozenset()
        assert tiers_for("garbage") == frozenset()
        assert tiers_for("") == frozenset()

    def test_level_floor_for_accepts_string(self) -> None:
        assert level_floor_for("P1") == "critical"
        assert level_floor_for("P2") == "action"
        assert level_floor_for("P3") == "info"

    def test_level_floor_for_unknown_returns_none(self) -> None:
        assert level_floor_for("P0") is None
        assert level_floor_for("garbage") is None


# ─────────────────────────────────────────────────────────────────
#  #3 — Notification.severity is optional
# ─────────────────────────────────────────────────────────────────


class TestNotificationModelSeverity:
    def test_severity_defaults_to_none(self) -> None:
        # Legacy callers without severity awareness must keep working.
        # If this defaults to anything else, 40+ existing
        # ``notify(level=...)`` call-sites would suddenly carry an
        # unintended P-tag.
        n = Notification(
            id="n-default",
            level="info",
            title="t",
            message="m",
        )
        assert n.severity is None

    def test_severity_accepts_enum(self) -> None:
        n = Notification(
            id="n-enum",
            level="critical",
            title="crash",
            message="",
            severity=Severity.P1,
        )
        assert n.severity == Severity.P1

    def test_severity_accepts_string(self) -> None:
        n = Notification(
            id="n-str",
            level="action",
            title="stuck",
            message="",
            severity="P2",
        )
        # Pydantic coerces str → Severity for str-Enum fields — locking
        # so we can rely on enum equality downstream.
        assert n.severity == Severity.P2

    def test_severity_rejects_unknown_string(self) -> None:
        # Pydantic str-Enum rejects values outside the enum set. Tests
        # against a future "let's accept any string for forward
        # compat" softening that would silently let typo'd severities
        # land in the DB.
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            Notification(
                id="n-bad",
                level="info",
                title="t",
                message="",
                severity="P9",  # type: ignore[arg-type]
            )

    def test_notification_dump_round_trips_severity(self) -> None:
        n = Notification(
            id="n-rt",
            level="critical",
            title="t",
            message="",
            severity="P1",
        )
        dumped = n.model_dump()
        assert dumped["severity"] == "P1"
        # Round-trip back through the model.
        n2 = Notification(**dumped)
        assert n2.severity == Severity.P1

    def test_notification_dump_round_trips_none_severity(self) -> None:
        n = Notification(id="n-rt", level="info", title="t", message="")
        dumped = n.model_dump()
        # None must round-trip as None (not missing key, not empty
        # string) — frontend contract.
        assert "severity" in dumped
        assert dumped["severity"] is None


# ─────────────────────────────────────────────────────────────────
#  #4 — notify() plumbs severity through to SSE payload
# ─────────────────────────────────────────────────────────────────


class TestNotifyPropagation:
    @pytest.mark.asyncio
    async def test_notify_publishes_severity_on_bus(self, monkeypatch) -> None:
        from backend import notifications as _n
        from backend.events import bus as _bus

        captured: list[tuple[str, dict]] = []

        async def _no_db(conn, data):
            return None

        # Stub out PG insert + curl-based dispatchers so this test
        # runs without a live DB / network.
        monkeypatch.setattr("backend.db.insert_notification", _no_db)

        original_publish = _bus.publish

        def _capture(channel: str, payload: dict, **kw) -> None:
            captured.append((channel, payload))
            return original_publish(channel, payload, **kw)

        monkeypatch.setattr(_bus, "publish", _capture)

        await _n.notify(
            level="critical",
            title="system down",
            message="oom-killer fired",
            source="watchdog",
            severity="P1",
        )

        notif_payloads = [p for ch, p in captured if ch == "notification"]
        assert len(notif_payloads) == 1
        assert notif_payloads[0]["severity"] == "P1"
        assert notif_payloads[0]["level"] == "critical"

    @pytest.mark.asyncio
    async def test_notify_publishes_none_severity_for_legacy_caller(
        self, monkeypatch,
    ) -> None:
        from backend import notifications as _n
        from backend.events import bus as _bus

        captured: list[dict] = []

        async def _no_db(conn, data):
            return None

        monkeypatch.setattr("backend.db.insert_notification", _no_db)

        def _capture(channel: str, payload: dict, **kw) -> None:
            if channel == "notification":
                captured.append(payload)

        monkeypatch.setattr(_bus, "publish", _capture)

        # Legacy call shape — no severity kwarg.
        await _n.notify(
            level="warning",
            title="quota at 80%",
            message="",
            source="token_budget",
        )

        assert len(captured) == 1
        assert "severity" in captured[0]
        assert captured[0]["severity"] is None

    @pytest.mark.asyncio
    async def test_notify_accepts_enum_severity(self, monkeypatch) -> None:
        from backend import notifications as _n
        from backend.events import bus as _bus

        captured: list[dict] = []

        async def _no_db(conn, data):
            return None

        monkeypatch.setattr("backend.db.insert_notification", _no_db)

        def _capture(channel: str, payload: dict, **kw) -> None:
            if channel == "notification":
                captured.append(payload)

        monkeypatch.setattr(_bus, "publish", _capture)

        await _n.notify(
            level="action",
            title="task stuck",
            message="",
            source="watchdog",
            severity=Severity.P2,
        )

        assert captured[0]["severity"] == "P2"


# ─────────────────────────────────────────────────────────────────
#  #5 — Persisted round-trip (PG-backed)
# ─────────────────────────────────────────────────────────────────


class TestSeverityPersistence:
    @pytest.mark.asyncio
    async def test_insert_and_list_round_trips_severity(
        self, pg_test_conn,
    ) -> None:
        # Skipped without OMNI_TEST_PG_URL via the pg_test_conn fixture.
        from backend import db

        await db.insert_notification(pg_test_conn, {
            "id": "n-sev-p1",
            "level": "critical",
            "title": "system down",
            "message": "oom",
            "source": "watchdog",
            "timestamp": "2026-04-25T00:00:00",
            "read": False,
            "auto_resolved": False,
            "severity": "P1",
        })
        rows = await db.list_notifications(pg_test_conn)
        assert len(rows) == 1
        assert rows[0]["severity"] == "P1"

    @pytest.mark.asyncio
    async def test_legacy_insert_persists_null_severity(
        self, pg_test_conn,
    ) -> None:
        from backend import db

        # Legacy data shape — no 'severity' key at all.
        await db.insert_notification(pg_test_conn, {
            "id": "n-sev-legacy",
            "level": "info",
            "title": "legacy notification",
            "message": "",
            "source": "agent",
            "timestamp": "2026-04-25T00:00:00",
            "read": False,
            "auto_resolved": False,
        })
        rows = await db.list_notifications(pg_test_conn)
        assert len(rows) == 1
        assert rows[0].get("severity") is None
