"""M3 — Per-tenant per-provider per-key circuit breaker tests.

Covers:

  * record_failure → is_open True for the (tenant, provider, fingerprint)
    triple, but stays False for any other tenant or any other key.
  * Recovery: record_success closes the circuit and emits a transition.
  * Cooldown elapse auto half-opens (is_open → False) without an explicit
    success call.
  * snapshot() filters by tenant / provider correctly.
  * Audit log writes ``circuit.open`` and ``circuit.close`` rows under
    the affected tenant's chain.
  * SSE bus publishes a ``circuit_state`` event on transitions only
    (not on repeated failures while already open).
  * /providers/circuits HTTP endpoint scopes to current tenant by default
    and returns ``scope=all`` for admin diagnostics.
  * /providers/circuits/reset clears the entries it should and leaves
    the others untouched.
  * model_router._is_provider_available honours the per-tenant breaker.
  * /providers/health surfaces the per-tenant cooldown when it exceeds
    the legacy global cooldown (overlap behaviour).
  * Active fingerprint resolution returns the configured key fingerprint
    or the ``no-key`` sentinel.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend import circuit_breaker as cb
from backend.agents import circuit_breaker as runner_cb
from backend.db_context import set_tenant_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _reset_circuit_state():
    cb._reset_for_tests()
    runner_cb.reset_for_tests()
    set_tenant_id(None)
    yield
    cb._reset_for_tests()
    runner_cb.reset_for_tests()
    set_tenant_id(None)


class TestRunnerCircuitBreaker:

    @settings(
        max_examples=75,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        name=st.text(max_size=256),
        threshold=st.integers(min_value=1, max_value=20),
        failures=st.integers(min_value=0, max_value=100),
        state=st.sampled_from(["closed", "half-open"]),
        result=st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.text(max_size=4096),
            st.lists(st.integers(), max_size=128),
            st.dictionaries(st.text(max_size=32), st.integers(), max_size=32),
        ),
    )
    def test_successful_call_property_returns_value_and_closes_probe(
        self,
        name,
        threshold,
        failures,
        state,
        result,
        monkeypatch,
    ):
        now = {"value": 1000.0}
        breaker = runner_cb.CircuitBreaker(
            name,
            failure_threshold=threshold,
            state=state,
            consecutive_failures=failures,
        )
        monkeypatch.setattr(runner_cb.time, "time", lambda: now["value"])

        assert breaker.call(lambda: result) == result
        assert breaker.consecutive_failures == 0
        assert breaker.state == "closed"

    @settings(
        max_examples=75,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        name=st.text(max_size=256),
        threshold=st.integers(min_value=1, max_value=20),
        attempts=st.integers(min_value=0, max_value=40),
        exc_kind=st.sampled_from(["url", "connection", "timeout", "http_500"]),
    )
    def test_transport_failures_property_open_at_threshold(
        self,
        name,
        threshold,
        attempts,
        exc_kind,
        monkeypatch,
    ):
        now = {"value": 1000.0}
        alerts: list[str] = []
        breaker = runner_cb.CircuitBreaker(name, failure_threshold=threshold)
        monkeypatch.setattr(runner_cb.time, "time", lambda: now["value"])
        monkeypatch.setattr(runner_cb, "_notify_operator", alerts.append)

        def fail():
            if exc_kind == "url":
                raise urllib.error.URLError("down")
            if exc_kind == "connection":
                raise ConnectionError("down")
            if exc_kind == "timeout":
                raise TimeoutError("down")
            raise urllib.error.HTTPError(
                url="https://example.invalid",
                code=500,
                msg="server error",
                hdrs=None,
                fp=None,
            )

        for _ in range(attempts):
            try:
                breaker.call(fail)
            except (
                runner_cb.CircuitBreakerOpen,
                urllib.error.HTTPError,
                urllib.error.URLError,
                ConnectionError,
                TimeoutError,
            ):
                pass

        recorded_failures = min(attempts, threshold)
        assert breaker.consecutive_failures == recorded_failures
        assert breaker.is_open() is (attempts >= threshold)
        assert breaker.state == ("open" if attempts >= threshold else "closed")
        assert len(alerts) == (1 if attempts >= threshold else 0)

    @settings(
        max_examples=75,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        name=st.text(max_size=256),
        threshold=st.integers(min_value=1, max_value=20),
        recovery_timeout=st.integers(min_value=0, max_value=1_000),
        failures=st.integers(min_value=0, max_value=100),
        opened_at=st.floats(
            min_value=-1_000_000,
            max_value=1_000_000,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_reset_property_is_idempotent(
        self,
        name,
        threshold,
        recovery_timeout,
        failures,
        opened_at,
        monkeypatch,
    ):
        now = {"value": 1000.0}
        breaker = runner_cb.CircuitBreaker(
            name,
            failure_threshold=threshold,
            recovery_timeout=recovery_timeout,
            state="open",
            consecutive_failures=failures,
            opened_at=opened_at,
        )
        monkeypatch.setattr(runner_cb.time, "time", lambda: now["value"])

        breaker.reset()
        first = (
            breaker.state,
            breaker.consecutive_failures,
            breaker.opened_at,
            breaker.is_open(),
        )
        breaker.reset()
        second = (
            breaker.state,
            breaker.consecutive_failures,
            breaker.opened_at,
            breaker.is_open(),
        )

        assert first == ("closed", 0, 0, False)
        assert second == first

    @settings(
        max_examples=75,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        open_names=st.lists(
            st.sampled_from(list(runner_cb.BREAKERS)),
            unique=True,
            max_size=len(runner_cb.BREAKERS),
        ),
        elapsed=st.integers(min_value=0, max_value=60),
    )
    def test_open_services_property_reports_open_breakers_and_reset_clears(
        self,
        open_names,
        elapsed,
        monkeypatch,
    ):
        now = {"value": 1000.0}
        monkeypatch.setattr(runner_cb.time, "time", lambda: now["value"])
        runner_cb.reset_for_tests()
        for name in open_names:
            breaker = runner_cb.BREAKERS[name]
            breaker.state = "open"
            breaker.opened_at = now["value"] - elapsed

        expected = [name for name in runner_cb.BREAKERS if name in open_names]
        assert runner_cb.open_services() == expected
        assert isinstance(runner_cb.open_services(), list)

        runner_cb.reset_for_tests()
        assert runner_cb.open_services() == []

    def test_jira_503_five_times_opens_then_half_open_success_closes(self, monkeypatch):
        now = {"value": 1000.0}
        alerts: list[dict] = []
        breaker = runner_cb.CircuitBreaker("jira_rest")

        monkeypatch.setattr(runner_cb.time, "time", lambda: now["value"])
        monkeypatch.setattr(runner_cb, "_notify_operator", lambda service: alerts.append({"service": service}))

        def fail():
            raise ConnectionError("jira 503")

        for _ in range(5):
            try:
                breaker.call(fail)
            except ConnectionError:
                pass

        assert breaker.state == "open"
        assert breaker.consecutive_failures == 5
        assert alerts == [{"service": "jira_rest"}]

        try:
            breaker.call(lambda: {"ok": False})
        except runner_cb.CircuitBreakerOpen as exc:
            assert str(exc) == "jira_rest"
        else:  # pragma: no cover - assertion guard
            raise AssertionError("open breaker must block calls")

        now["value"] += 61
        assert breaker.call(lambda: {"ok": True}) == {"ok": True}
        assert breaker.state == "closed"
        assert breaker.consecutive_failures == 0

    def test_half_open_failure_reopens(self, monkeypatch):
        now = {"value": 1000.0}
        breaker = runner_cb.CircuitBreaker("gerrit_ssh", failure_threshold=1)
        monkeypatch.setattr(runner_cb.time, "time", lambda: now["value"])
        monkeypatch.setattr(runner_cb, "_notify_operator", lambda service: None)

        try:
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        except ConnectionError:
            pass
        now["value"] += 61

        try:
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("still down")))
        except ConnectionError:
            pass

        assert breaker.state == "open"
        assert breaker.consecutive_failures == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core isolation: A's failure does not affect B
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPerTenantIsolation:

    def test_failure_only_opens_target_key(self):
        cb.record_failure("tenant-A", "openai", "…aaaa", reason="401")
        assert cb.is_open("tenant-A", "openai", "…aaaa") is True
        # Different tenant, same provider+key → still closed
        assert cb.is_open("tenant-B", "openai", "…aaaa") is False
        # Same tenant, same provider, different key → still closed
        assert cb.is_open("tenant-A", "openai", "…bbbb") is False
        # Same tenant, different provider → still closed
        assert cb.is_open("tenant-A", "anthropic", "…aaaa") is False

    def test_two_tenants_share_provider_independently(self):
        cb.record_failure("tenant-A", "openai", "…shared")
        cb.record_failure("tenant-B", "openai", "…shared")
        assert cb.is_open("tenant-A", "openai", "…shared")
        assert cb.is_open("tenant-B", "openai", "…shared")
        # Closing A leaves B open
        cb.record_success("tenant-A", "openai", "…shared")
        assert cb.is_open("tenant-A", "openai", "…shared") is False
        assert cb.is_open("tenant-B", "openai", "…shared") is True

    def test_no_provider_no_op(self):
        # Empty provider name shouldn't blow up or store state
        cb.record_failure("tenant-A", "", "…aaaa")
        assert cb.is_open("tenant-A", "", "…aaaa") is False
        assert cb.snapshot() == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Recovery + half-open
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRecovery:

    def test_record_success_closes_open_circuit(self):
        cb.record_failure("t-A", "openai", "…ab", reason="500")
        assert cb.is_open("t-A", "openai", "…ab")
        cb.record_success("t-A", "openai", "…ab")
        assert cb.is_open("t-A", "openai", "…ab") is False
        snap = cb.snapshot(tenant_id="t-A")
        assert snap[0]["failure_count"] == 0
        assert snap[0]["closed_at"] is not None

    def test_cooldown_elapse_auto_half_opens(self, monkeypatch):
        cb.record_failure("t-A", "openai", "…ab")
        assert cb.is_open("t-A", "openai", "…ab")
        # Fast-forward time past the cooldown window
        future = time.time() + cb.COOLDOWN_SECONDS + 1
        monkeypatch.setattr(cb, "_now", lambda: future)
        # is_open should now report False even without a success call
        assert cb.is_open("t-A", "openai", "…ab") is False
        # And cooldown_remaining should be 0
        assert cb.cooldown_remaining("t-A", "openai", "…ab") == 0

    def test_repeat_failures_refresh_cooldown(self):
        cb.record_failure("t-A", "openai", "…ab")
        first_remaining = cb.cooldown_remaining("t-A", "openai", "…ab")
        # Second failure shouldn't drop remaining time below the first
        cb.record_failure("t-A", "openai", "…ab")
        second_remaining = cb.cooldown_remaining("t-A", "openai", "…ab")
        assert second_remaining >= first_remaining - 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  snapshot() filter behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSnapshot:

    def test_filter_by_tenant(self):
        cb.record_failure("t-A", "openai", "…1")
        cb.record_failure("t-B", "openai", "…2")
        cb.record_failure("t-B", "anthropic", "…3")
        only_b = cb.snapshot(tenant_id="t-B")
        assert {r["tenant_id"] for r in only_b} == {"t-B"}
        assert len(only_b) == 2

    def test_filter_by_provider(self):
        cb.record_failure("t-A", "openai", "…1")
        cb.record_failure("t-B", "openai", "…2")
        cb.record_failure("t-A", "anthropic", "…3")
        only_openai = cb.snapshot(provider="openai")
        assert {r["provider"] for r in only_openai} == {"openai"}
        assert len(only_openai) == 2

    def test_snapshot_includes_fingerprint_and_reason(self):
        cb.record_failure("t-A", "openai", "…fp1", reason="rate limit")
        snap = cb.snapshot()
        assert len(snap) == 1
        row = snap[0]
        assert row["fingerprint"] == "…fp1"
        assert row["reason"] == "rate limit"
        assert row["failure_count"] == 1
        assert row["open"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  reset()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestReset:

    def test_reset_by_tenant_only(self):
        cb.record_failure("t-A", "openai", "…1")
        cb.record_failure("t-B", "openai", "…2")
        cleared = cb.reset(tenant_id="t-A")
        assert cleared == 1
        assert cb.snapshot(tenant_id="t-A") == []
        assert len(cb.snapshot(tenant_id="t-B")) == 1

    def test_reset_by_provider(self):
        cb.record_failure("t-A", "openai", "…1")
        cb.record_failure("t-A", "anthropic", "…2")
        cleared = cb.reset(tenant_id="t-A", provider="openai")
        assert cleared == 1
        snap = cb.snapshot(tenant_id="t-A")
        assert len(snap) == 1
        assert snap[0]["provider"] == "anthropic"

    def test_reset_all(self):
        cb.record_failure("t-A", "openai", "…1")
        cb.record_failure("t-B", "openai", "…2")
        cleared = cb.reset()
        assert cleared == 2
        assert cb.snapshot() == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Memory bound (LRU eviction)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMemoryBound:

    def test_eviction_keeps_dict_bounded(self, monkeypatch):
        monkeypatch.setattr(cb, "_MAX_KEYS", 8)
        monkeypatch.setattr(cb, "_EVICT_TARGET", 4)
        for i in range(20):
            cb.record_failure(f"t-{i}", "openai", f"…fp{i}")
        snap = cb.snapshot()
        assert len(snap) <= 8


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSE event bus integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSSEBus:

    def test_open_emits_event(self, monkeypatch):
        captured: list[tuple] = []

        from backend import events
        original = events.bus.publish

        def fake_publish(event_type, payload, **kwargs):
            captured.append((event_type, payload, kwargs))
            return original(event_type, payload, **kwargs)

        monkeypatch.setattr(events.bus, "publish", fake_publish)
        cb.record_failure("t-A", "openai", "…fp", reason="401")
        types = [c[0] for c in captured]
        assert "circuit_state" in types
        idx = types.index("circuit_state")
        assert captured[idx][1]["transition"] == "open"
        assert captured[idx][1]["tenant_id"] == "t-A"
        assert captured[idx][1]["provider"] == "openai"

    def test_repeat_failure_emits_only_once(self, monkeypatch):
        captured: list[str] = []
        from backend import events
        monkeypatch.setattr(
            events.bus, "publish",
            lambda et, p, **kw: captured.append(et),
        )
        cb.record_failure("t-A", "openai", "…fp")
        cb.record_failure("t-A", "openai", "…fp")
        cb.record_failure("t-A", "openai", "…fp")
        # Only the first transition should publish
        assert captured.count("circuit_state") == 1

    def test_close_emits_event(self, monkeypatch):
        cb.record_failure("t-A", "openai", "…fp")
        captured: list[dict] = []
        from backend import events
        monkeypatch.setattr(
            events.bus, "publish",
            lambda et, p, **kw: captured.append({"event": et, "payload": p}),
        )
        cb.record_success("t-A", "openai", "…fp")
        events_seen = [c for c in captured if c["event"] == "circuit_state"]
        assert len(events_seen) == 1
        assert events_seen[0]["payload"]["transition"] == "close"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit log integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAuditIntegration:

    @pytest.mark.asyncio
    async def test_open_and_close_appear_in_audit(self, client):
        # Use the seeded t-default tenant so the audit FK constraint
        # (audit_log.tenant_id → tenants.id) is satisfied.
        set_tenant_id("t-default")
        cb.record_failure("t-default", "openai", "…fp", reason="401")
        cb.record_success("t-default", "openai", "…fp")
        # log_sync schedules log() onto the running loop; give it a few
        # ticks so the async tasks complete before we query.
        for _ in range(5):
            await asyncio.sleep(0.02)

        from backend import audit
        rows = await audit.query(entity_kind="circuit", limit=20)
        actions = [r["action"] for r in rows]
        assert "circuit.open" in actions, f"audit rows: {rows}"
        assert "circuit.close" in actions, f"audit rows: {rows}"
        # entity_id encodes provider/fingerprint
        circuit_rows = [r for r in rows if r["entity_kind"] == "circuit"]
        assert any(r["entity_id"] == "openai/…fp" for r in circuit_rows)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Active fingerprint resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestActiveFingerprint:

    def test_no_key_returns_sentinel(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
        assert cb.active_fingerprint("openai") == cb.NO_KEY_FINGERPRINT

    def test_configured_key_returns_fingerprint(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(
            settings, "openai_api_key", "sk-test-AAAAAAAAAAAA-XYZW",
            raising=False,
        )
        fp = cb.active_fingerprint("openai")
        assert fp.endswith("XYZW")

    def test_unknown_provider_returns_sentinel(self):
        assert cb.active_fingerprint("not_a_real_provider") == cb.NO_KEY_FINGERPRINT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP API: /providers/circuits + reset
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCircuitsEndpoint:

    @pytest.mark.asyncio
    async def test_get_circuits_default_scope_tenant(self, client):
        cb.record_failure("t-default", "openai", "…fp1", reason="401")
        cb.record_failure("t-other", "openai", "…fp2", reason="401")
        resp = await client.get("/api/v1/providers/circuits")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "tenant"
        assert data["tenant_id"] == "t-default"
        assert data["cooldown_seconds"] == cb.COOLDOWN_SECONDS
        # Only the tenant-scoped row appears
        tenants = {c["tenant_id"] for c in data["circuits"]}
        assert tenants == {"t-default"}

    @pytest.mark.asyncio
    async def test_get_circuits_scope_all(self, client):
        cb.record_failure("t-default", "openai", "…fp1")
        cb.record_failure("t-other", "openai", "…fp2")
        resp = await client.get("/api/v1/providers/circuits?scope=all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "all"
        tenants = {c["tenant_id"] for c in data["circuits"]}
        assert tenants == {"t-default", "t-other"}

    @pytest.mark.asyncio
    async def test_reset_only_clears_calling_tenant_by_default(self, client):
        cb.record_failure("t-default", "openai", "…fp1")
        cb.record_failure("t-other", "openai", "…fp2")
        resp = await client.post(
            "/api/v1/providers/circuits/reset",
            json={"provider": "openai"},
        )
        assert resp.status_code == 200
        # Only t-default's circuit cleared; t-other's remains
        assert cb.snapshot(tenant_id="t-default") == []
        assert len(cb.snapshot(tenant_id="t-other")) == 1

    @pytest.mark.asyncio
    async def test_reset_scope_all(self, client):
        cb.record_failure("t-default", "openai", "…fp1")
        cb.record_failure("t-other", "openai", "…fp2")
        resp = await client.post(
            "/api/v1/providers/circuits/reset",
            json={"scope": "all"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cleared"] == 2
        assert cb.snapshot() == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /providers/health overlap with per-tenant cooldown
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestProviderHealthIntegration:

    @pytest.mark.asyncio
    async def test_health_reports_per_tenant_cooldown(self, client, monkeypatch):
        from backend.config import settings
        # Configure an OpenAI key so its fingerprint is stable
        monkeypatch.setattr(settings, "openai_api_key", "sk-fake-key-XYZW", raising=False)
        # Trip the circuit for the default tenant + that key
        fp = cb.active_fingerprint("openai")
        cb.record_failure("t-default", "openai", fp, reason="401")
        resp = await client.get("/api/v1/providers/health")
        assert resp.status_code == 200
        data = resp.json()
        openai_row = next((h for h in data["health"] if h["id"] == "openai"), None)
        assert openai_row is not None
        # cooldown_remaining should be > 0 once the per-tenant breaker trips
        assert openai_row["cooldown_remaining"] > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  model_router._is_provider_available consults breaker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestModelRouterIntegration:

    def test_breaker_open_blocks_availability(self, monkeypatch):
        from backend import model_router
        from backend.config import settings
        # Make the provider's key resolvable
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-XYZW", raising=False)
        # Force validate_model_spec to report the spec as valid
        monkeypatch.setattr(
            "backend.agents.llm.validate_model_spec",
            lambda spec: {"valid": True, "provider": "anthropic", "model": "x", "configured": True, "warning": ""},
        )
        # Without a tripped breaker, available
        cb._reset_for_tests()
        assert model_router._is_provider_available("anthropic:claude-sonnet-4-20250514") is True
        # Trip the breaker for the default tenant + active key
        fp = cb.active_fingerprint("anthropic")
        cb.record_failure("t-default", "anthropic", fp, reason="401")
        assert model_router._is_provider_available("anthropic:claude-sonnet-4-20250514") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  get_llm() failover path uses per-tenant breaker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestGetLLMFailover:

    def test_per_tenant_circuit_skips_fallback(self, monkeypatch):
        """When fallback X's per-tenant circuit is open, get_llm should
        skip X and fall through to the next provider."""
        from backend.agents import llm as llm_mod
        from backend.config import settings

        monkeypatch.setattr(settings, "llm_provider", "primary_x", raising=False)
        monkeypatch.setattr(settings, "llm_fallback_chain", "fallback_a,fallback_b", raising=False)

        # Trip the breaker for fallback_a in the default tenant
        cb.record_failure("t-default", "fallback_a", cb.NO_KEY_FINGERPRINT, reason="forced")

        attempts: list[str] = []

        def fake_create(provider, model):
            attempts.append(provider)
            if provider == "fallback_b":
                # Return a sentinel non-None object to count as success
                class FakeLLM:
                    def with_config(self, **kwargs): return self
                return FakeLLM()
            return None

        monkeypatch.setattr(llm_mod, "_create_llm", fake_create)
        monkeypatch.setattr(
            "backend.routers.system.is_token_frozen",
            lambda: False,
        )
        # Clear cache so we hit _create_llm
        llm_mod._cache.clear()

        result = llm_mod.get_llm()
        assert result is not None
        # primary failed → recorded; fallback_a skipped (circuit open) →
        # fallback_b attempted and succeeded
        assert "primary_x" in attempts
        assert "fallback_a" not in attempts
        assert "fallback_b" in attempts
