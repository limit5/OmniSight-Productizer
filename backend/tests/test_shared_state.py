"""I10 — Tests for cross-worker shared state primitives.

Tests run without Redis (in-memory fallback) by default.
"""

import pytest
from backend.shared_state import (
    SharedCounter,
    SharedFlag,
    SharedHaltFlag,
    SharedHourlyLedger,
    SharedKV,
    SharedLogBuffer,
    SharedTokenUsage,
    publish_cross_worker,
    register_cross_worker_callback,
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
