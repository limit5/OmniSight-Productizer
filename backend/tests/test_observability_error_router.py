"""W10 #284 — ErrorToIntentRouter tests."""

from __future__ import annotations

import asyncio

import pytest

from backend import intent_source
from backend.intent_source import (
    IntentStory,
    SubtaskPayload,
    SubtaskRef,
)
from backend.observability import (
    ErrorEvent,
    ErrorToIntentRouter,
    build_subtask_payload,
    get_default_router,
    reset_default_router,
)


# ── Fake IntentSource ────────────────────────────────────────────


class FakeIntentSource:
    """Recording in-process IntentSource fake. Implements the protocol
    surface ErrorToIntentRouter touches: create_subtasks + comment."""

    def __init__(self, vendor="jira"):
        self.vendor = vendor
        self.created: list[tuple[str, list[SubtaskPayload]]] = []
        self.comments: list[tuple[str, str]] = []
        self.next_id = 1
        self.fail_create = False
        self.fail_comment = False

    async def fetch_story(self, ticket: str) -> IntentStory:
        return IntentStory(vendor=self.vendor, ticket=ticket, summary="")

    async def create_subtasks(self, parent, payloads):
        if self.fail_create:
            raise intent_source.AdapterError(self.vendor, "create_subtasks",
                                             "boom")
        out = []
        for p in payloads:
            ref = SubtaskRef(
                vendor=self.vendor,
                ticket=f"OMNI-{self.next_id}",
                url=f"https://jira.example.com/browse/OMNI-{self.next_id}",
                parent=parent,
            )
            self.next_id += 1
            out.append(ref)
        self.created.append((parent, list(payloads)))
        return out

    async def update_status(self, ticket, status, *, comment=""):
        return {"ok": True}

    async def comment(self, ticket, body):
        if self.fail_comment:
            raise intent_source.AdapterError(self.vendor, "comment", "no")
        self.comments.append((ticket, body))
        return {"ok": True}

    async def verify_webhook(self, headers, body):
        return True

    def parse_webhook(self, body):
        return ("", "")


@pytest.fixture
def fake_jira():
    intent_source.reset_registry_for_tests()
    fake = FakeIntentSource(vendor="jira")
    intent_source.register_source(fake)
    yield fake
    intent_source.reset_registry_for_tests()


@pytest.fixture
def make_router():
    def _factory(**kw):
        kw.setdefault("vendor", "jira")
        kw.setdefault("comment_on_duplicate", False)
        return ErrorToIntentRouter(**kw)
    return _factory


# ── Construction ─────────────────────────────────────────────────


class TestConstruction:

    def test_defaults(self):
        r = ErrorToIntentRouter()
        assert r._min_level == "error"
        assert r._dedup_window == 86_400
        assert r._comment_on_duplicate is True

    def test_invalid_min_level(self):
        with pytest.raises(ValueError):
            ErrorToIntentRouter(min_level="trace")

    def test_invalid_dedup_window(self):
        with pytest.raises(ValueError):
            ErrorToIntentRouter(dedup_window_seconds=0)

    @pytest.mark.parametrize("level", ["debug", "info", "warning",
                                       "warn", "error", "fatal"])
    def test_accepted_levels(self, level):
        r = ErrorToIntentRouter(min_level=level)
        assert r._min_level == level


# ── Routing happy path ───────────────────────────────────────────


class TestRouteHappyPath:

    async def test_creates_subtask_for_new_fingerprint(self, fake_jira, make_router):
        router = make_router()
        ev = ErrorEvent(message="boom", page="/x", level="error",
                        release="1.0", stack="app.js:1:2")
        ref = await router.route(ev)
        assert ref is not None
        assert ref.ticket == "OMNI-1"
        assert len(fake_jira.created) == 1
        parent, payloads = fake_jira.created[0]
        assert parent == "OMNI-RUM-1.0"
        assert payloads[0].title.startswith("[browser-error]")
        assert "boom" in payloads[0].title
        m = router.metrics()
        assert m["routed"] == 1
        assert m["deduped"] == 0
        assert m["last_routed_ticket"] == "OMNI-1"

    async def test_payload_includes_fingerprint_and_release(self, fake_jira, make_router):
        router = make_router()
        ev = ErrorEvent(message="TypeError: x", page="/blog",
                        level="error", release="2.0",
                        environment="staging",
                        stack="at app.js:5:10\nat react.js:1:1")
        await router.route(ev)
        _, payloads = fake_jira.created[0]
        ac = payloads[0].acceptance_criteria
        assert "TypeError: x" in ac
        assert "/blog" in ac
        assert "2.0" in ac
        assert "staging" in ac
        assert ev.fingerprint in ac
        assert payloads[0].domain_context == "web/staging"
        assert "browser-error" in payloads[0].labels


# ── Dedup ────────────────────────────────────────────────────────


class TestDedup:

    async def test_same_fingerprint_dedups(self, fake_jira, make_router):
        router = make_router()
        ev = ErrorEvent(message="boom", release="1.0", stack="a.js:1:2")
        ref1 = await router.route(ev)
        ref2 = await router.route(ev)
        assert ref1 is not None
        assert ref2 is not None
        assert ref1.ticket == ref2.ticket
        # Only one tracker call.
        assert len(fake_jira.created) == 1
        m = router.metrics()
        assert m["routed"] == 1
        assert m["deduped"] == 1

    async def test_different_release_creates_new_ticket(self, fake_jira, make_router):
        router = make_router()
        ev1 = ErrorEvent(message="boom", release="1.0", stack="a.js:1:2")
        ev2 = ErrorEvent(message="boom", release="1.1", stack="a.js:1:2")
        ref1 = await router.route(ev1)
        ref2 = await router.route(ev2)
        assert ref1.ticket != ref2.ticket
        assert len(fake_jira.created) == 2

    async def test_dedup_eviction_after_window(self, fake_jira, make_router):
        clock = [1000.0]
        router = make_router(dedup_window_seconds=10, clock=lambda: clock[0])
        ev = ErrorEvent(message="boom", release="1.0", stack="a.js:1:2",
                        timestamp=clock[0])
        await router.route(ev)
        # Advance past the dedup window.
        clock[0] += 30
        ev2 = ErrorEvent(message="boom", release="1.0", stack="a.js:1:2",
                         timestamp=clock[0])
        await router.route(ev2)
        # New ticket — old fingerprint evicted.
        assert len(fake_jira.created) == 2

    async def test_comment_on_duplicate_appends_when_enabled(self, fake_jira):
        router = ErrorToIntentRouter(vendor="jira",
                                     comment_on_duplicate=True)
        ev = ErrorEvent(message="boom", release="1.0", stack="a.js:1:2")
        await router.route(ev)
        await router.route(ev)
        # Comment append is fire-and-forget — yield to event loop.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(fake_jira.comments) == 1
        ticket, body = fake_jira.comments[0]
        assert ticket == "OMNI-1"
        assert "duplicate occurrence #2" in body


# ── Min-level gate ───────────────────────────────────────────────


class TestMinLevelGate:

    async def test_warn_below_error_min_dropped(self, fake_jira, make_router):
        router = make_router(min_level="error")
        ev = ErrorEvent(message="just a warning", level="warning")
        ref = await router.route(ev)
        assert ref is None
        assert len(fake_jira.created) == 0
        assert router.metrics()["dropped_below_min_level"] == 1

    async def test_warning_min_includes_warnings(self, fake_jira, make_router):
        router = make_router(min_level="warning")
        ev = ErrorEvent(message="warn", level="warning")
        ref = await router.route(ev)
        assert ref is not None
        assert len(fake_jira.created) == 1

    async def test_fatal_min_excludes_error(self, fake_jira, make_router):
        router = make_router(min_level="fatal")
        ev = ErrorEvent(message="just an error", level="error")
        assert await router.route(ev) is None
        assert router.metrics()["dropped_below_min_level"] == 1


# ── Failure modes ────────────────────────────────────────────────


class TestFailureModes:

    async def test_no_intent_source_registered_returns_none(self, make_router):
        intent_source.reset_registry_for_tests()
        router = make_router(vendor="jira")
        ev = ErrorEvent(message="boom")
        ref = await router.route(ev)
        assert ref is None
        assert router.metrics()["adapter_unavailable"] == 1

    async def test_adapter_error_swallowed(self, fake_jira, make_router):
        fake_jira.fail_create = True
        router = make_router()
        ev = ErrorEvent(message="boom", release="1.0", stack="a.js:1:2")
        ref = await router.route(ev)
        assert ref is None
        m = router.metrics()
        assert m["adapter_errors"] == 1
        assert "boom" in m["last_error"]

    async def test_comment_failure_swallowed(self, fake_jira):
        router = ErrorToIntentRouter(vendor="jira",
                                     comment_on_duplicate=True)
        ev = ErrorEvent(message="boom", release="1.0", stack="a.js:1:2")
        await router.route(ev)
        fake_jira.fail_comment = True
        await router.route(ev)
        await asyncio.sleep(0); await asyncio.sleep(0)
        # Errors counted but no exception bubbled.
        m = router.metrics()
        assert m["adapter_errors"] >= 1


# ── List recent ─────────────────────────────────────────────────


class TestListRecent:

    async def test_lists_most_recent_first(self, fake_jira, make_router):
        router = make_router()
        clock = [1000.0]
        router._clock = lambda: clock[0]
        for i in range(3):
            await router.route(ErrorEvent(
                message=f"e{i}", release="1.0",
                stack=f"a{i}.js:1:2",
                timestamp=clock[0],
            ))
            clock[0] += 1
        rows = router.list_recent(limit=10)
        assert [r["message"] for r in rows] == ["e2", "e1", "e0"]
        assert all(r["ticket"].startswith("OMNI-") for r in rows)


# ── build_subtask_payload ───────────────────────────────────────


class TestBuildSubtaskPayload:

    def test_pins_static_fields(self):
        ev = ErrorEvent(message="boom", page="/x", level="error",
                        release="1.0", environment="prod",
                        stack="a.js:1:2")
        payload = build_subtask_payload(ev)
        assert isinstance(payload, SubtaskPayload)
        assert payload.title.startswith("[browser-error]")
        assert "rum" in payload.labels
        assert "browser-error" in payload.labels
        assert "error" in payload.labels
        assert payload.domain_context == "web/prod"
        assert "test_assets/" in payload.impact_scope_forbidden
        assert payload.extra["fingerprint"] == ev.fingerprint
        assert payload.extra["page"] == "/x"

    def test_long_message_truncated_in_title(self):
        long_msg = "x" * 500
        ev = ErrorEvent(message=long_msg)
        payload = build_subtask_payload(ev)
        assert len(payload.title) <= len("[browser-error] ") + 120

    def test_empty_message_falls_back_to_unknown(self):
        ev = ErrorEvent(message="")
        payload = build_subtask_payload(ev)
        assert "unknown error" in payload.title


# ── Singleton ───────────────────────────────────────────────────


class TestDefaultSingleton:

    def setup_method(self):
        reset_default_router()

    def teardown_method(self):
        reset_default_router()

    def test_singleton_returns_same_instance(self):
        a = get_default_router()
        b = get_default_router()
        assert a is b

    def test_reset_creates_new(self):
        a = get_default_router()
        reset_default_router()
        b = get_default_router()
        assert a is not b
