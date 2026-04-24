"""I10 — Tests for cross-worker shared state primitives.

Tests run without Redis (in-memory fallback) by default.
"""

import pytest
from backend.shared_state import (
    SessionPresence,
    SharedCounter,
    SharedFlag,
    SharedHaltFlag,
    SharedHourlyLedger,
    SharedKV,
    SharedLogBuffer,
    SharedTokenUsage,
    publish_cross_worker,
    register_cross_worker_callback,
    session_presence,
)


class TestSharedCounter:
    def test_initial_value(self):
        c = SharedCounter("test_init", initial=0)
        assert c.get() == 0

    def test_increment(self):
        c = SharedCounter("test_inc", initial=0)
        c.set(0)
        assert c.increment() == 1
        assert c.increment() == 2
        assert c.get() == 2

    def test_decrement(self):
        c = SharedCounter("test_dec", initial=5)
        c.set(5)
        assert c.decrement() == 4
        assert c.decrement(2) == 2

    def test_decrement_floor_zero(self):
        c = SharedCounter("test_floor", initial=0)
        c.set(0)
        val = c.decrement()
        assert val >= 0

    def test_set_and_get(self):
        c = SharedCounter("test_set", initial=0)
        c.set(42)
        assert c.get() == 42


class TestSharedKV:
    def test_set_get(self):
        kv = SharedKV("test_kv")
        kv.set("key1", "value1")
        assert kv.get("key1") == "value1"

    def test_get_default(self):
        kv = SharedKV("test_kv_default")
        assert kv.get("nonexistent", "fallback") == "fallback"

    def test_get_all(self):
        kv = SharedKV("test_kv_all")
        kv.set("a", "1")
        kv.set("b", "2")
        all_vals = kv.get_all()
        assert all_vals["a"] == "1"
        assert all_vals["b"] == "2"

    def test_delete(self):
        kv = SharedKV("test_kv_del")
        kv.set("to_delete", "yes")
        kv.delete("to_delete")
        assert kv.get("to_delete", "gone") == "gone"


class TestSharedFlag:
    def test_initial_false(self):
        f = SharedFlag("test_flag_init", initial=False)
        assert f.get() is False

    def test_set_true(self):
        f = SharedFlag("test_flag_set")
        f.set(True)
        assert f.get() is True
        f.set(False)
        assert f.get() is False


class TestSharedLogBuffer:
    def test_append_and_get(self):
        buf = SharedLogBuffer("test_log", maxlen=10)
        buf.clear()
        buf.append({"msg": "hello"})
        buf.append({"msg": "world"})
        logs = buf.get_all()
        assert len(logs) == 2
        assert logs[0]["msg"] == "hello"
        assert logs[1]["msg"] == "world"

    def test_get_recent(self):
        buf = SharedLogBuffer("test_log_recent", maxlen=100)
        buf.clear()
        for i in range(10):
            buf.append({"i": i})
        recent = buf.get_recent(3)
        assert len(recent) == 3
        assert recent[-1]["i"] == 9

    def test_maxlen_enforcement(self):
        buf = SharedLogBuffer("test_log_maxlen", maxlen=5)
        buf.clear()
        for i in range(10):
            buf.append({"i": i})
        logs = buf.get_all()
        assert len(logs) <= 5


class TestSharedTokenUsage:
    def test_track_and_get(self):
        usage = SharedTokenUsage()
        usage.clear()
        entry = usage.track("test-model", 100, 50, 200.0, 0.001)
        assert entry["input_tokens"] == 100
        assert entry["output_tokens"] == 50
        assert entry["request_count"] == 1

    def test_track_accumulates(self):
        usage = SharedTokenUsage()
        usage.clear()
        usage.track("test-model2", 100, 50, 200.0, 0.001)
        entry = usage.track("test-model2", 200, 100, 300.0, 0.002)
        assert entry["input_tokens"] == 300
        assert entry["output_tokens"] == 150
        assert entry["request_count"] == 2

    def test_total_cost(self):
        usage = SharedTokenUsage()
        usage.clear()
        usage.track("m1", 100, 50, 100.0, 0.01)
        usage.track("m2", 200, 100, 150.0, 0.02)
        total = usage.total_cost()
        assert total == pytest.approx(0.03, abs=0.001)

    def test_set_all_and_get_all(self):
        usage = SharedTokenUsage()
        usage.clear()
        data = {
            "model-a": {"model": "model-a", "input_tokens": 500, "cost": 0.05},
        }
        usage.set_all(data)
        result = usage.get_all()
        assert "model-a" in result

    # ─── P7 Fix B regression guards ───────────────────────────
    # These lock in the canonical 8-field shape returned by
    # ``/api/v1/runtime/tokens`` so the frontend TokenUsage interface
    # (lib/api.ts) never again sees an undefined ``total_tokens`` /
    # ``request_count`` / ``avg_latency`` / ``last_used`` — which is what
    # caused the dashboard's TypeError cascade at P6.

    _CANONICAL_FIELDS = {
        "model", "input_tokens", "output_tokens", "total_tokens",
        "cost", "request_count", "avg_latency", "last_used",
    }

    def test_track_returns_full_canonical_shape(self):
        """Every field the frontend TokenUsage interface expects is
        present — and the internal ``_total_latency`` bookkeeping is
        stripped from the public projection."""
        usage = SharedTokenUsage()
        usage.clear()
        entry = usage.track("canonical-model", 100, 50, 200.0, 0.001)
        assert self._CANONICAL_FIELDS.issubset(entry.keys())
        assert "_total_latency" not in entry
        assert entry["total_tokens"] == 150
        assert entry["request_count"] == 1
        assert entry["avg_latency"] == 200  # int ms
        assert entry["last_used"]  # non-empty "HH:MM:SS"

    def test_get_all_strips_internal_bookkeeping(self):
        usage = SharedTokenUsage()
        usage.clear()
        usage.track("m", 100, 50, 200.0, 0.001)
        for entry in usage.get_all().values():
            assert "_total_latency" not in entry
            assert self._CANONICAL_FIELDS.issubset(entry.keys())

    def test_get_all_backfills_legacy_redis_payload(self):
        """A Redis payload written by a pre-P7 worker uses the legacy
        ``requests`` / ``avg_latency_ms`` names and has no
        ``total_tokens`` / ``last_used``. ``get_all()`` must rewrite
        those in place so the caller never sees the legacy shape."""
        usage = SharedTokenUsage()
        usage.clear()
        # Seed the in-memory store with a legacy-shaped entry as if it
        # had been restored from Redis written by an old worker.
        usage._local["legacy-model"] = {
            "model": "legacy-model",
            "input_tokens": 1000,
            "output_tokens": 500,
            "cost": 0.123,
            "requests": 3,              # legacy key
            "avg_latency_ms": 180.5,    # legacy key
            "_total_latency": 541.5,
            # NB: no total_tokens, no last_used
        }
        result = usage.get_all()["legacy-model"]
        assert self._CANONICAL_FIELDS.issubset(result.keys())
        assert "requests" not in result
        assert "avg_latency_ms" not in result
        assert "_total_latency" not in result
        assert result["total_tokens"] == 1500  # synthesised
        assert result["request_count"] == 3     # renamed
        assert result["avg_latency"] == 180     # renamed + int-coerced
        assert result["last_used"] == ""        # synthesised default

    def test_track_after_legacy_read_produces_canonical(self):
        """A track() call that finds a legacy-shaped entry in Redis
        must upgrade it rather than mix the two schemas."""
        usage = SharedTokenUsage()
        usage.clear()
        usage._local["legacy-model"] = {
            "model": "legacy-model",
            "input_tokens": 100,
            "output_tokens": 50,
            "cost": 0.01,
            "requests": 1,
            "avg_latency_ms": 200.0,
            "_total_latency": 200.0,
        }
        entry = usage.track("legacy-model", 10, 5, 100.0, 0.001)
        assert self._CANONICAL_FIELDS.issubset(entry.keys())
        assert entry["input_tokens"] == 110
        assert entry["output_tokens"] == 55
        assert entry["total_tokens"] == 165
        assert entry["request_count"] == 2
        # Stored copy should also be canonical (no stale legacy keys).
        stored = usage._local["legacy-model"]
        assert "requests" not in stored
        assert "avg_latency_ms" not in stored

    # ─── ZZ.A1 (#303-1) regression guards ───────────────────────
    # Prompt-cache observability: three new fields on every entry
    # — ``cache_read_tokens`` / ``cache_create_tokens`` /
    # ``cache_hit_ratio`` — and NULL is the canonical marker for
    # "pre-ZZ legacy row, no cache data" so a dashboard can render
    # an em-dash instead of misleading zeros.

    def test_track_records_cache_tokens_and_ratio(self):
        """A ZZ-era track() call records cache_read / cache_create and
        derives ``cache_hit_ratio = cache_read / (input + cache_read)``.
        """
        usage = SharedTokenUsage()
        usage.clear()
        # 800 cache-read + 200 fresh input ⇒ hit ratio 0.8.
        entry = usage.track(
            "zz-cache-model", 200, 50, 100.0, 0.001,
            cache_read_tokens=800, cache_create_tokens=120,
        )
        assert entry["cache_read_tokens"] == 800
        assert entry["cache_create_tokens"] == 120
        assert entry["cache_hit_ratio"] == pytest.approx(0.8, abs=1e-6)

    def test_track_cache_tokens_accumulate(self):
        """Second call adds to the lifetime total and recomputes ratio
        from the running sums rather than the last-turn snapshot."""
        usage = SharedTokenUsage()
        usage.clear()
        usage.track(
            "zz-accum", 100, 0, 100.0, 0.0,
            cache_read_tokens=100, cache_create_tokens=20,
        )
        entry = usage.track(
            "zz-accum", 100, 0, 100.0, 0.0,
            cache_read_tokens=300, cache_create_tokens=30,
        )
        # Lifetime totals: 200 input, 400 cache_read, 50 cache_create.
        assert entry["cache_read_tokens"] == 400
        assert entry["cache_create_tokens"] == 50
        assert entry["cache_hit_ratio"] == pytest.approx(
            400 / (200 + 400), abs=1e-6,
        )

    def test_track_zero_cache_defaults_to_zero_ratio(self):
        """When no cache tokens are seen (legacy caller, or provider
        with no cache signal), the ratio is 0.0 — not NaN, not None."""
        usage = SharedTokenUsage()
        usage.clear()
        entry = usage.track("zz-no-cache", 100, 50, 100.0, 0.001)
        assert entry["cache_read_tokens"] == 0
        assert entry["cache_create_tokens"] == 0
        assert entry["cache_hit_ratio"] == 0.0

    def test_track_cache_only_no_input_guards_division(self):
        """Defensive: if a provider reports cache_read with zero fresh
        input on a first call (hypothetical — wouldn't happen in prod
        since prompt_tokens always includes the cache-hit count), the
        ratio is still a sane float, never a ZeroDivisionError."""
        usage = SharedTokenUsage()
        usage.clear()
        entry = usage.track(
            "zz-cache-only", 0, 0, 100.0, 0.0,
            cache_read_tokens=500, cache_create_tokens=0,
        )
        # denominator = 0 input + 500 cache_read = 500 > 0 → 1.0 ratio.
        assert entry["cache_hit_ratio"] == pytest.approx(1.0)
        # And with neither input nor cache_read, the ratio is 0.0.
        entry2 = usage.track("zz-all-zero", 0, 100, 100.0, 0.0)
        assert entry2["cache_hit_ratio"] == 0.0

    def test_get_all_preserves_null_on_pre_zz_payload(self):
        """A Redis payload written by a pre-ZZ worker has no cache
        fields at all. ``get_all()`` must surface ``None`` (not 0)
        so the dashboard can distinguish "no data" from "zero hits".
        """
        usage = SharedTokenUsage()
        usage.clear()
        usage._local["pre-zz-model"] = {
            "model": "pre-zz-model",
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "cost": 0.123,
            "request_count": 3,
            "avg_latency": 180,
            "last_used": "10:20:30",
            # no cache_* fields whatsoever
        }
        result = usage.get_all()["pre-zz-model"]
        assert result["cache_read_tokens"] is None
        assert result["cache_create_tokens"] is None
        assert result["cache_hit_ratio"] is None

    def test_track_upgrades_pre_zz_null_to_numeric(self):
        """First ZZ-era track() on a pre-ZZ row must upgrade the NULL
        cache counters to numeric so subsequent reads see the canonical
        shape. NULL is a one-way marker — once a worker observes cache
        data the row is committed to ZZ-era semantics."""
        usage = SharedTokenUsage()
        usage.clear()
        usage._local["upgrading"] = {
            "model": "upgrading",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cost": 0.01,
            "request_count": 1,
            "avg_latency": 100,
            "last_used": "10:00:00",
            # no cache_* fields → NULL
        }
        entry = usage.track(
            "upgrading", 100, 50, 100.0, 0.01,
            cache_read_tokens=400, cache_create_tokens=40,
        )
        # After upgrade, NULL → numeric; cache_read is the ZZ-turn value.
        assert entry["cache_read_tokens"] == 400
        assert entry["cache_create_tokens"] == 40
        assert entry["cache_hit_ratio"] == pytest.approx(
            400 / (200 + 400), abs=1e-6,
        )
        # Stored copy also upgraded.
        stored = usage._local["upgrading"]
        assert stored["cache_read_tokens"] == 400
        assert stored["cache_create_tokens"] == 40

    def test_fresh_entry_starts_at_zero_not_null(self):
        """A brand-new model goes through _fresh_token_entry — cache
        fields start at 0 (not NULL) because a fresh track() call
        means ZZ-era semantics are authoritative from turn 1."""
        usage = SharedTokenUsage()
        usage.clear()
        entry = usage.track("fresh-zz", 100, 50, 100.0, 0.001)
        # No cache tokens reported by this call, but the fields are
        # numeric zero — "genuine zero hits", not "no data".
        assert entry["cache_read_tokens"] == 0
        assert entry["cache_create_tokens"] == 0
        assert entry["cache_hit_ratio"] == 0.0


class TestSharedHourlyLedger:
    def test_record_and_total(self):
        ledger = SharedHourlyLedger(window_seconds=3600.0)
        ledger.clear()
        ledger.record(0.01)
        ledger.record(0.02)
        total = ledger.total_in_window()
        assert total == pytest.approx(0.03, abs=0.001)

    def test_clear(self):
        ledger = SharedHourlyLedger(window_seconds=3600.0)
        ledger.record(0.05)
        ledger.clear()
        assert ledger.total_in_window() == 0.0


class TestSharedHaltFlag:
    def test_initial_running(self):
        flag = SharedHaltFlag("test_halt")
        flag.resume()
        assert flag.is_running() is True

    def test_halt_and_resume(self):
        flag = SharedHaltFlag("test_halt2")
        flag.halt()
        assert flag.is_running() is False
        flag.resume()
        assert flag.is_running() is True


class TestCrossWorkerPubSub:
    def test_publish_no_redis(self):
        result = publish_cross_worker("test_event", {"key": "value"})
        assert result is False

    def test_register_callback(self):
        received = []
        def cb(event, data):
            received.append((event, data))
        register_cross_worker_callback(cb)


class TestDecisionEngineSharedState:
    """Verify decision engine uses shared state correctly."""

    def test_mode_persists_across_set_get(self):
        from backend import decision_engine as de
        de._reset_for_tests()
        assert de.get_mode() == de.OperationMode.supervised
        de.set_mode("full_auto")
        assert de.get_mode() == de.OperationMode.full_auto
        de._reset_for_tests()
        assert de.get_mode() == de.OperationMode.supervised

    def test_parallel_in_flight_shared(self):
        from backend import decision_engine as de
        de._reset_for_tests()
        assert de.parallel_in_flight() == 0

    @pytest.mark.asyncio
    async def test_parallel_slot_increments_shared(self):
        from backend import decision_engine as de
        de._reset_for_tests()
        de.set_mode("full_auto")
        slot = de.parallel_slot()
        await slot.__aenter__()
        assert de.parallel_in_flight() == 1
        await slot.__aexit__(None, None, None)
        assert de.parallel_in_flight() == 0
        de._reset_for_tests()


class TestEventBusCrossWorker:
    """Verify EventBus cross-worker delivery setup."""

    def test_bus_has_worker_id(self):
        from backend.events import bus
        assert bus._worker_id.startswith("w-")

    def test_deliver_local(self):
        from backend.events import bus
        q = bus.subscribe()
        bus._deliver_local("test_event", '{"msg": "hello"}')
        msg = q.get_nowait()
        assert msg["event"] == "test_event"
        bus.unsubscribe(q)

    def test_cross_worker_callback_skips_same_worker(self):
        from backend.events import _on_cross_worker_event, bus
        q = bus.subscribe()
        _on_cross_worker_event("sse", {
            "event": "test",
            "data_json": '{"x":1}',
            "broadcast_scope": "global",
            "tenant_id": "",
            "origin_worker": bus._worker_id,
        })
        assert q.empty()
        bus.unsubscribe(q)

    def test_cross_worker_callback_delivers_from_other_worker(self):
        from backend.events import _on_cross_worker_event, bus
        q = bus.subscribe()
        _on_cross_worker_event("sse", {
            "event": "test_cross",
            "data_json": '{"y":2}',
            "broadcast_scope": "global",
            "tenant_id": "",
            "origin_worker": "w-other-worker",
        })
        msg = q.get_nowait()
        assert msg["event"] == "test_cross"
        bus.unsubscribe(q)


class TestTokenBudgetSharedState:
    """Verify token budget uses shared flags."""

    def test_is_token_frozen_default(self):
        from backend.routers.system import is_token_frozen
        from backend.routers import system as sys_mod
        sys_mod.token_frozen = False
        sys_mod._token_frozen_shared.set(False)
        assert is_token_frozen() is False

    def test_is_token_frozen_shared(self):
        from backend.routers.system import is_token_frozen
        from backend.routers import system as sys_mod
        sys_mod.token_frozen = False
        sys_mod._token_frozen_shared.set(True)
        assert is_token_frozen() is True
        sys_mod._token_frozen_shared.set(False)


class TestMultiWorkerConfig:
    """Verify uvicorn worker configuration."""

    def test_config_has_workers(self):
        from backend.config import settings
        assert hasattr(settings, "workers")
        assert isinstance(settings.workers, int)

    def test_default_workers_zero(self):
        from backend.config import settings
        assert settings.workers == 0


class TestSessionPresence:
    """Q.5 #299: SessionPresence heartbeat tracker for the active-device
    indicator. Verifies the ``SharedKV``-backed primitive in isolation —
    the SSE-endpoint wiring + ``/auth/sessions/presence`` endpoint + UI
    integration are exercised by later checkboxes of the Q.5 item."""

    def _fresh(self) -> SessionPresence:
        presence = SessionPresence()
        presence._local.clear()
        return presence

    def test_record_and_last_seen_roundtrip(self):
        presence = self._fresh()
        ts = presence.record_heartbeat("u-alice", "sess-abc", ts=1000.0)
        assert ts == 1000.0
        assert presence.last_seen("u-alice", "sess-abc") == pytest.approx(1000.0)

    def test_empty_user_or_session_is_noop(self):
        presence = self._fresh()
        assert presence.record_heartbeat("", "sid") == 0.0
        assert presence.record_heartbeat("u", "") == 0.0
        assert presence.last_seen("", "sid") is None
        assert presence.last_seen("u", "") is None

    def test_default_clock_uses_time_time(self):
        import time as _time
        presence = self._fresh()
        before = _time.time()
        stored = presence.record_heartbeat("u-clock", "sid-clock")
        after = _time.time()
        assert before - 0.1 <= stored <= after + 0.1
        got = presence.last_seen("u-clock", "sid-clock")
        assert got is not None
        assert before - 0.1 <= got <= after + 0.1

    def test_active_sessions_within_window(self):
        presence = self._fresh()
        # Three sessions: two fresh, one stale (> 60 s old).
        presence.record_heartbeat("u-bob", "fresh-a", ts=1000.0)
        presence.record_heartbeat("u-bob", "fresh-b", ts=995.0)
        presence.record_heartbeat("u-bob", "stale", ts=900.0)
        active = presence.active_sessions("u-bob", now=1010.0)
        sids = [sid for sid, _ts in active]
        assert sids == ["fresh-a", "fresh-b"]
        # Count helper stays consistent with active_sessions.
        assert presence.active_count("u-bob", now=1010.0) == 2

    def test_active_sessions_sorted_by_recency_desc(self):
        presence = self._fresh()
        presence.record_heartbeat("u-carol", "older", ts=1000.0)
        presence.record_heartbeat("u-carol", "newer", ts=1030.0)
        presence.record_heartbeat("u-carol", "newest", ts=1055.0)
        active = presence.active_sessions("u-carol", now=1060.0)
        sids = [sid for sid, _ts in active]
        assert sids == ["newest", "newer", "older"]

    def test_active_sessions_user_isolation(self):
        presence = self._fresh()
        presence.record_heartbeat("u-a", "s1", ts=1000.0)
        presence.record_heartbeat("u-b", "s2", ts=1000.0)
        assert [sid for sid, _ in presence.active_sessions("u-a", now=1000.0)] == ["s1"]
        assert [sid for sid, _ in presence.active_sessions("u-b", now=1000.0)] == ["s2"]

    def test_active_sessions_custom_window(self):
        presence = self._fresh()
        presence.record_heartbeat("u-win", "sid", ts=1000.0)
        # Tight 5 s window — record at 1000, "now" at 1010 → stale.
        assert presence.active_count("u-win", window_seconds=5.0, now=1010.0) == 0
        # Widen window to 120 s → fresh.
        assert presence.active_count("u-win", window_seconds=120.0, now=1010.0) == 1

    def test_drop_removes_entry(self):
        presence = self._fresh()
        presence.record_heartbeat("u-drop", "sid-drop", ts=1000.0)
        assert presence.last_seen("u-drop", "sid-drop") == pytest.approx(1000.0)
        presence.drop("u-drop", "sid-drop")
        assert presence.last_seen("u-drop", "sid-drop") is None
        assert presence.active_count("u-drop", now=1000.0) == 0

    def test_prune_expired_removes_stale_and_preserves_fresh(self):
        presence = self._fresh()
        presence.record_heartbeat("u-x", "fresh", ts=1000.0)
        presence.record_heartbeat("u-x", "stale", ts=900.0)
        pruned = presence.prune_expired(now=1010.0, window_seconds=60.0)
        assert pruned == 1
        active = presence.active_sessions("u-x", now=1010.0, window_seconds=60.0)
        assert [sid for sid, _ in active] == ["fresh"]

    def test_prune_expired_drops_malformed_values(self):
        presence = self._fresh()
        # Simulate a corrupt entry (non-numeric ts).
        presence.set(SessionPresence._field("u-mal", "bad"), "not-a-float")
        presence.record_heartbeat("u-mal", "good", ts=1000.0)
        pruned = presence.prune_expired(now=1000.0, window_seconds=60.0)
        assert pruned == 1
        assert presence.active_count("u-mal", now=1000.0) == 1

    def test_field_delimiter_survives_colon_in_user_id(self):
        """API-key user ids are ``apikey:<id>`` — ``|`` rather than ``:``
        is used as the ``(user_id, session_id)`` delimiter so the split
        stays unambiguous."""
        presence = self._fresh()
        uid = "apikey:abc123"
        sid = "deadbeefcafef00d"
        presence.record_heartbeat(uid, sid, ts=1000.0)
        active = presence.active_sessions(uid, now=1000.0)
        assert [s for s, _ in active] == [sid]
        # Nothing bleeds into a different user id that shares a prefix.
        assert presence.active_count("apikey", now=1000.0) == 0

    def test_record_overwrites_timestamp(self):
        presence = self._fresh()
        presence.record_heartbeat("u-over", "sid", ts=1000.0)
        presence.record_heartbeat("u-over", "sid", ts=1030.0)
        assert presence.last_seen("u-over", "sid") == pytest.approx(1030.0)
        # Same session remains 1 — overwrite, not duplicate.
        assert presence.active_count("u-over", now=1030.0) == 1

    def test_singleton_is_session_presence_instance(self):
        from backend.shared_state import session_presence as singleton
        assert isinstance(singleton, SessionPresence)
        # Same object identity as the imported singleton at module top.
        assert singleton is session_presence
