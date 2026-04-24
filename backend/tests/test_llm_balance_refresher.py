"""Z.2 (#291) — Unit tests for the balance refresher background loop.

What's locked
─────────────
1. **Happy path** — configured key + successful fetcher → SharedKV
   slot populated with the ``BalanceInfo`` JSON envelope; backoff
   resets to the base interval.
2. **No key configured** — resolver returns ``None`` → no fetch,
   no cache write, no backoff advance; next tick re-evaluates.
3. **Fetch error → exponential backoff** — ``BalanceFetchError``
   bumps the per-provider delay to ``base × 2^failures`` and does
   not touch the cache (so the previous snapshot keeps serving the
   ``stale_since`` envelope the endpoint will surface).
4. **Auth failure (None) → same backoff treatment** — a revoked key
   must not be hammered, but the cache is not overwritten either
   (operator may rotate the key; next success lands cleanly).
5. **Backoff gate** — while ``next_attempt_at > now``, the provider
   is skipped without a resolver or fetcher call.
6. **Backoff cap at 1 hour** — after enough failures the delay
   saturates at ``MAX_BACKOFF_S`` (3600 s). Matches the Z.2
   checkbox spec.
7. **Backoff reset on success** — a successful fetch after N
   failures clears ``consecutive_failures`` back to 0.
8. **Providers independent** — DeepSeek failure does not affect
   OpenRouter's backoff and vice versa.
9. **Defensive: unexpected exception in fetcher** — not a
   BalanceFetchError but still gets caught; provider backs off,
   loop continues.
10. **run_refresh_loop — cancellation cleanup + singleton guard** —
    matches the Phase-52 DLQ / Phase-63-E memory-decay contracts:
    ``_LOOP_RUNNING`` flips on entry and resets in ``finally``;
    second concurrent call returns immediately.

Module-global audit (SOP Step 1, 2026-04-21 rule)
─────────────────────────────────────────────────
Each test constructs its own ``state`` dict for ``refresh_once``
(no cross-test leak). The loop-singleton tests touch
``llm_balance_refresher._LOOP_RUNNING`` and reset it in
``finally``-style teardown so a cancelled task in one test doesn't
leak into the next.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import llm_balance_refresher as lbr
from backend.llm_balance import BalanceFetchError, BalanceInfo
from backend.shared_state import SharedKV


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _ok_balance(
    provider: str = "deepseek", amount: float = 10.0, when: float = 0.0,
) -> BalanceInfo:
    """Minimal ``BalanceInfo`` for happy-path assertions."""
    return BalanceInfo(
        currency="USD",
        balance_remaining=amount,
        granted_total=amount,
        usage_total=None,
        last_refreshed_at=when,
        raw={"provider": provider, "amount": amount},
    )


def _make_kv() -> SharedKV:
    """Fresh SharedKV namespace per test — the in-memory fallback is
    per-instance so tests cannot cross-pollute unless the suite is
    running against a shared Redis. We use a unique namespace to
    belt-and-brace that.
    """
    import uuid
    return SharedKV(f"provider_balance_test_{uuid.uuid4().hex[:8]}")


def _make_fetcher_ok(amount: float = 10.0):
    """Return an async fetcher that always yields an ``ok`` BalanceInfo."""

    async def fetcher(api_key: str, *, now: float | None = None, **_):
        return _ok_balance(amount=amount, when=now or 0.0)

    return fetcher


def _make_fetcher_raises(reason: str = "boom"):
    """Return an async fetcher that raises ``BalanceFetchError``."""

    async def fetcher(api_key: str, *, now: float | None = None, **_):
        raise BalanceFetchError("test-provider", reason)

    return fetcher


def _make_fetcher_auth_fail():
    """Return an async fetcher that returns ``None`` (401/403)."""

    async def fetcher(api_key: str, *, now: float | None = None, **_):
        return None

    return fetcher


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  refresh_once — happy path + SharedKV envelope
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRefreshOnceHappyPath:

    async def test_success_writes_to_sharedkv(self):
        kv = _make_kv()
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state,
            base_interval_s=600,
            now=1_000.0,
            fetchers={"deepseek": _make_fetcher_ok(amount=42.5)},
            key_resolver=lambda p: "sk-testing",
            kv=kv,
        )

        assert outcomes == {"deepseek": "ok"}
        raw = kv.get("deepseek")
        assert raw, "SharedKV slot must be populated on success"
        parsed = json.loads(raw)
        assert parsed["currency"] == "USD"
        assert parsed["balance_remaining"] == 42.5
        assert parsed["last_refreshed_at"] == 1_000.0
        # raw vendor body must round-trip
        assert parsed["raw"]["provider"] == "deepseek"

    async def test_success_resets_backoff_to_base(self):
        state = {
            "deepseek": lbr._ProviderBackoff(
                consecutive_failures=3, next_attempt_at=0.0,
            ),
        }

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={"deepseek": _make_fetcher_ok()},
            key_resolver=lambda p: "sk",
            kv=_make_kv(),
        )

        assert outcomes["deepseek"] == "ok"
        assert state["deepseek"].consecutive_failures == 0
        assert state["deepseek"].next_attempt_at == 1_000.0 + 600

    async def test_success_writes_envelope_round_trippable(self):
        """The JSON envelope must be ``json.loads``-able by the
        upcoming endpoint without extra decoding.
        """
        kv = _make_kv()
        state: dict[str, lbr._ProviderBackoff] = {}
        await lbr.refresh_once(
            state=state, base_interval_s=600, now=2_000.0,
            fetchers={"openrouter": _make_fetcher_ok(amount=7.0)},
            key_resolver=lambda p: "sk",
            kv=kv,
        )
        assert json.loads(kv.get("openrouter"))["balance_remaining"] == 7.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  refresh_once — no-key branch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRefreshOnceNoKey:

    async def test_missing_key_skips_silently(self):
        kv = _make_kv()
        state: dict[str, lbr._ProviderBackoff] = {}
        called: list[str] = []

        async def fetcher(api_key, *, now=None, **_):
            called.append(api_key)
            return _ok_balance()

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=5_000.0,
            fetchers={"deepseek": fetcher},
            key_resolver=lambda p: None,
            kv=kv,
        )

        assert outcomes == {"deepseek": "no_key"}
        assert not called, "Fetcher must not run when no key is configured"
        assert kv.get("deepseek") == "", "No cache write on no_key"
        # No backoff bump — next tick re-evaluates immediately.
        assert state["deepseek"].consecutive_failures == 0
        assert state["deepseek"].next_attempt_at == 0.0

    async def test_empty_string_treated_as_missing(self):
        state: dict[str, lbr._ProviderBackoff] = {}
        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=5_000.0,
            fetchers={"deepseek": _make_fetcher_ok()},
            key_resolver=lambda p: "",
            kv=_make_kv(),
        )
        assert outcomes == {"deepseek": "no_key"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  refresh_once — fetch error → backoff
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRefreshOnceFetchError:

    async def test_fetch_error_does_not_write_cache(self):
        kv = _make_kv()
        # Pre-populate a prior snapshot; fetch error must NOT overwrite.
        kv.set("deepseek", json.dumps({"previous": "snapshot"}))
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={"deepseek": _make_fetcher_raises("upstream 502")},
            key_resolver=lambda p: "sk",
            kv=kv,
        )

        assert outcomes == {"deepseek": "fetch_error"}
        assert json.loads(kv.get("deepseek")) == {"previous": "snapshot"}

    async def test_fetch_error_bumps_backoff_to_2x_base(self):
        state: dict[str, lbr._ProviderBackoff] = {}
        await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={"deepseek": _make_fetcher_raises()},
            key_resolver=lambda p: "sk",
            kv=_make_kv(),
        )
        bo = state["deepseek"]
        assert bo.consecutive_failures == 1
        # base × 2^1 = 1200 s (20 min)
        assert bo.next_attempt_at == 1_000.0 + 1200.0

    async def test_repeated_failures_double_delay(self):
        state: dict[str, lbr._ProviderBackoff] = {}
        fetcher = _make_fetcher_raises()
        kv = _make_kv()

        # Fire the fetcher 4 times in a row (each tick past the previous
        # backoff window) — should give delays of 1200, 2400, 3600 (cap).
        expected = [1200.0, 2400.0, 3600.0, 3600.0]
        t = 0.0
        for i, expected_delay in enumerate(expected):
            # Advance ``now`` just past the previous backoff window so
            # the gate lets us through.
            t = state.get("deepseek", lbr._ProviderBackoff()).next_attempt_at
            t = max(t, 0.0) + 1.0  # +1 s to clear the gate
            await lbr.refresh_once(
                state=state, base_interval_s=600, now=t,
                fetchers={"deepseek": fetcher},
                key_resolver=lambda p: "sk",
                kv=kv,
            )
            bo = state["deepseek"]
            assert bo.consecutive_failures == i + 1
            assert bo.next_attempt_at == pytest.approx(t + expected_delay), (
                f"After {i + 1} failures expected delay {expected_delay}, "
                f"got next_attempt_at={bo.next_attempt_at}, t={t}"
            )

    async def test_unexpected_exception_also_backs_off(self):
        """A fetcher that raises something other than
        :class:`BalanceFetchError` must still be caught — the loop
        cannot be killed by a single bad vendor schema."""

        async def bad_fetcher(api_key, *, now=None, **_):
            raise RuntimeError("surprise")

        state: dict[str, lbr._ProviderBackoff] = {}
        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={"deepseek": bad_fetcher},
            key_resolver=lambda p: "sk",
            kv=_make_kv(),
        )
        assert outcomes == {"deepseek": "fetch_error"}
        assert state["deepseek"].consecutive_failures == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  refresh_once — auth failure (fetcher returns None)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRefreshOnceAuthFail:

    async def test_auth_fail_does_not_overwrite_cache(self):
        """The operator may rotate the key between ticks — we must NOT
        stamp over the previous good snapshot with an auth_failed
        envelope."""
        kv = _make_kv()
        kv.set("deepseek", json.dumps({"previous": "ok"}))
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={"deepseek": _make_fetcher_auth_fail()},
            key_resolver=lambda p: "sk-revoked",
            kv=kv,
        )

        assert outcomes == {"deepseek": "auth_fail"}
        assert json.loads(kv.get("deepseek")) == {"previous": "ok"}

    async def test_auth_fail_applies_exponential_backoff(self):
        state: dict[str, lbr._ProviderBackoff] = {}
        await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={"deepseek": _make_fetcher_auth_fail()},
            key_resolver=lambda p: "sk-revoked",
            kv=_make_kv(),
        )
        assert state["deepseek"].consecutive_failures == 1
        assert state["deepseek"].next_attempt_at == 1_000.0 + 1200.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  refresh_once — backoff gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRefreshOnceBackoffGate:

    async def test_within_backoff_window_skips_fetch(self):
        state = {
            "deepseek": lbr._ProviderBackoff(
                consecutive_failures=2, next_attempt_at=10_000.0,
            ),
        }
        called: list[str] = []

        async def fetcher(api_key, *, now=None, **_):
            called.append(api_key)
            return _ok_balance()

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=5_000.0,
            fetchers={"deepseek": fetcher},
            key_resolver=lambda p: "sk",
            kv=_make_kv(),
        )

        assert outcomes == {"deepseek": "backoff"}
        assert not called, "Fetcher must not run within backoff window"
        # Counters unchanged.
        assert state["deepseek"].consecutive_failures == 2
        assert state["deepseek"].next_attempt_at == 10_000.0

    async def test_past_backoff_window_retries(self):
        state = {
            "deepseek": lbr._ProviderBackoff(
                consecutive_failures=2, next_attempt_at=1_000.0,
            ),
        }
        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_500.0,
            fetchers={"deepseek": _make_fetcher_ok()},
            key_resolver=lambda p: "sk",
            kv=_make_kv(),
        )
        assert outcomes == {"deepseek": "ok"}
        # Success resets counters.
        assert state["deepseek"].consecutive_failures == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  refresh_once — multi-provider independence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRefreshOnceProvidersIndependent:

    async def test_one_fails_other_succeeds(self):
        kv = _make_kv()
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={
                "deepseek": _make_fetcher_raises("502"),
                "openrouter": _make_fetcher_ok(amount=5.25),
            },
            key_resolver=lambda p: "sk",
            kv=kv,
        )

        assert outcomes == {
            "deepseek": "fetch_error",
            "openrouter": "ok",
        }
        # Deepseek backed off, openrouter did not.
        assert state["deepseek"].consecutive_failures == 1
        assert state["openrouter"].consecutive_failures == 0
        # Openrouter cache written; deepseek cache untouched.
        assert json.loads(kv.get("openrouter"))["balance_remaining"] == 5.25
        assert kv.get("deepseek") == ""

    async def test_each_provider_resolves_own_key(self):
        resolved: list[str] = []

        def resolver(provider):
            resolved.append(provider)
            return f"sk-{provider}"

        state: dict[str, lbr._ProviderBackoff] = {}
        await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={
                "deepseek": _make_fetcher_ok(),
                "openrouter": _make_fetcher_ok(),
            },
            key_resolver=resolver,
            kv=_make_kv(),
        )
        # Both providers looked up their own key.
        assert set(resolved) == {"deepseek", "openrouter"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _ProviderBackoff — direct unit tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestProviderBackoff:

    def test_reset_clears_counters_and_sets_base_gate(self):
        bo = lbr._ProviderBackoff(
            consecutive_failures=5, next_attempt_at=9_999.0,
        )
        bo.reset(now=100.0, base_interval_s=600)
        assert bo.consecutive_failures == 0
        assert bo.next_attempt_at == 100.0 + 600

    def test_record_failure_doubles_then_caps(self):
        bo = lbr._ProviderBackoff()
        delays = []
        t = 0.0
        # 7 failures: 1200, 2400, 4800→3600 (cap), 3600, ...
        expected = [1200, 2400, 3600, 3600, 3600, 3600, 3600]
        for _ in range(7):
            d = bo.record_failure(now=t, base_interval_s=600)
            delays.append(d)
        assert delays == expected
        # Counter continued climbing even while delay capped.
        assert bo.consecutive_failures == 7

    def test_max_backoff_exact_cap(self):
        bo = lbr._ProviderBackoff()
        # 600 × 2^3 = 4800 → caps to 3600.
        for _ in range(3):
            bo.record_failure(now=0.0, base_interval_s=600)
        assert bo.next_attempt_at == lbr.MAX_BACKOFF_S


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Serialiser
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSerialiseBalance:

    def test_preserves_none_numeric_fields(self):
        info = BalanceInfo(
            currency="USD", balance_remaining=None,
            granted_total=None, usage_total=12.3,
            last_refreshed_at=1.0, raw={},
        )
        out = json.loads(lbr._serialise_balance(info))
        assert out["balance_remaining"] is None
        assert out["granted_total"] is None
        assert out["usage_total"] == 12.3

    def test_non_json_types_in_raw_degrade_to_string(self):
        """``default=str`` keeps the serialiser robust against exotic
        vendor types."""
        import datetime

        info = BalanceInfo(
            currency="USD", balance_remaining=1.0,
            granted_total=None, usage_total=None,
            last_refreshed_at=0.0,
            raw={"ts": datetime.datetime(2026, 4, 24, 0, 0, 0)},
        )
        out = json.loads(lbr._serialise_balance(info))
        assert "2026-04-24" in out["raw"]["ts"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Key resolver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolveApiKey:

    def test_reads_settings_scalar(self, monkeypatch):
        from backend.config import settings

        monkeypatch.setattr(
            settings, "deepseek_api_key", "sk-from-settings",
            raising=False,
        )
        assert lbr._resolve_api_key("deepseek") == "sk-from-settings"

    def test_empty_returns_none(self, monkeypatch):
        from backend.config import settings

        monkeypatch.setattr(
            settings, "openrouter_api_key", "", raising=False,
        )
        assert lbr._resolve_api_key("openrouter") is None

    def test_whitespace_only_returns_none(self, monkeypatch):
        from backend.config import settings

        monkeypatch.setattr(
            settings, "deepseek_api_key", "   ", raising=False,
        )
        assert lbr._resolve_api_key("deepseek") is None

    def test_unknown_provider_returns_none(self):
        # Unknown providers have no Settings attr → None, not an error.
        assert lbr._resolve_api_key("nonexistent-provider") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  run_refresh_loop — cancellation cleanup + singleton guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def reset_loop_flag():
    """Ensure _LOOP_RUNNING does not leak across tests."""
    lbr._LOOP_RUNNING = False
    yield
    lbr._LOOP_RUNNING = False


@pytest.fixture
def isolated_providers(monkeypatch):
    """Replace SUPPORTED_BALANCE_PROVIDERS with an empty dict so the
    loop's internal refresh_once call is a no-op (no HTTP, no Settings
    read). Tests that care about outcomes call refresh_once directly
    with explicit injections."""
    monkeypatch.setattr(
        lbr, "SUPPORTED_BALANCE_PROVIDERS", {}, raising=True,
    )
    yield


class TestRunRefreshLoop:

    async def test_exits_cleanly_on_cancel(
        self, reset_loop_flag, isolated_providers,
    ):
        # Short interval so the loop enters asyncio.sleep quickly.
        task = asyncio.create_task(lbr.run_refresh_loop(interval_s=60))
        await asyncio.sleep(0.05)
        assert lbr._LOOP_RUNNING is True

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert lbr._LOOP_RUNNING is False
        assert task.done()

    async def test_second_start_is_noop(
        self, reset_loop_flag, isolated_providers,
    ):
        t1 = asyncio.create_task(lbr.run_refresh_loop(interval_s=60))
        await asyncio.sleep(0.05)
        assert lbr._LOOP_RUNNING is True

        # Second call must return immediately — the singleton guard
        # short-circuits before entering the while loop.
        result = await asyncio.wait_for(
            lbr.run_refresh_loop(interval_s=60), timeout=0.5,
        )
        assert result is None

        t1.cancel()
        try:
            await t1
        except asyncio.CancelledError:
            pass

    async def test_initial_tick_runs_before_first_sleep(
        self, reset_loop_flag, monkeypatch,
    ):
        """The loop runs one immediate refresh so dashboards are not
        blank for the first `interval_s` seconds after boot."""
        ticked: list[float] = []

        original = lbr.refresh_once

        async def counting_refresh(*, state, base_interval_s, **_kw):
            ticked.append(base_interval_s)
            return {}

        monkeypatch.setattr(lbr, "refresh_once", counting_refresh)

        # Use a very long interval — if the initial tick is gated on
        # sleep, we'd never see it within the test window.
        task = asyncio.create_task(
            lbr.run_refresh_loop(interval_s=3600),
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert ticked, "Initial refresh_once call must fire before sleep"
        assert ticked[0] == 3600

    async def test_initial_tick_exception_does_not_crash_loop(
        self, reset_loop_flag, monkeypatch,
    ):
        calls = {"n": 0}

        async def flaky_refresh(*, state, base_interval_s, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom on first tick")
            return {}

        monkeypatch.setattr(lbr, "refresh_once", flaky_refresh)

        task = asyncio.create_task(
            lbr.run_refresh_loop(interval_s=3600),
        )
        # Give the task time to process the first tick and enter sleep.
        await asyncio.sleep(0.05)
        # Loop is still running despite the initial-tick exception.
        assert lbr._LOOP_RUNNING is True

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry integration — wired to SUPPORTED_BALANCE_PROVIDERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Z.2 boundary — stale marker write / clear contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# The boundary row in TODO.md locks two distinct failure contracts at
# the refresher layer:
#   * 5xx / transport / malformed → write stale marker at ``now`` so
#     the endpoint's next cache-hit read surfaces ``stale_since``.
#   * Auth-fail (None) → leave stale marker alone (401/403 is operator
#     side; misrepresenting it as "provider down" would mislead the
#     dashboard).
# Successful refresh clears any prior marker so the stale badge flips
# off on the next read.


class TestRefreshOnceStaleMarker:

    async def test_fetch_error_writes_stale_marker_at_now(self):
        kv = _make_kv()
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        state: dict[str, lbr._ProviderBackoff] = {}

        await lbr.refresh_once(
            state=state, base_interval_s=600, now=7_777.0,
            fetchers={"deepseek": _make_fetcher_raises("upstream 502")},
            key_resolver=lambda p: "sk",
            kv=kv, stale_kv=stale,
        )

        marker = stale.get("deepseek")
        assert marker, "BalanceFetchError must write stale marker"
        assert float(marker) == pytest.approx(7_777.0)

    async def test_unexpected_exception_writes_stale_marker(self):
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")

        async def bad(api_key, *, now=None, **_):
            raise RuntimeError("vendor schema drift")

        state: dict[str, lbr._ProviderBackoff] = {}
        await lbr.refresh_once(
            state=state, base_interval_s=600, now=2_000.0,
            fetchers={"openrouter": bad},
            key_resolver=lambda p: "sk",
            kv=_make_kv(), stale_kv=stale,
        )
        assert float(stale.get("openrouter")) == pytest.approx(2_000.0)

    async def test_success_clears_prior_stale_marker(self):
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        # Pretend a prior failure stamped a marker.
        stale.set("deepseek", "3333.0")
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=9_000.0,
            fetchers={"deepseek": _make_fetcher_ok()},
            key_resolver=lambda p: "sk",
            kv=_make_kv(), stale_kv=stale,
        )
        assert outcomes["deepseek"] == "ok"
        assert stale.get("deepseek") == "", (
            "Successful fetch must clear prior stale marker"
        )

    async def test_auth_fail_does_not_touch_stale_marker(self):
        """Prior marker stays; no new marker is written on auth-fail.
        Locks both halves in one assertion: (a) auth-fail doesn't write
        where there was none, (b) auth-fail doesn't clear where there
        was one either."""
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        stale.set("deepseek", "4444.0")
        state: dict[str, lbr._ProviderBackoff] = {}

        await lbr.refresh_once(
            state=state, base_interval_s=600, now=9_000.0,
            fetchers={"deepseek": _make_fetcher_auth_fail()},
            key_resolver=lambda p: "sk-revoked",
            kv=_make_kv(), stale_kv=stale,
        )
        # Unchanged.
        assert stale.get("deepseek") == "4444.0"

    async def test_auth_fail_does_not_write_stale_when_none(self):
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        state: dict[str, lbr._ProviderBackoff] = {}

        await lbr.refresh_once(
            state=state, base_interval_s=600, now=9_000.0,
            fetchers={"deepseek": _make_fetcher_auth_fail()},
            key_resolver=lambda p: "sk-revoked",
            kv=_make_kv(), stale_kv=stale,
        )
        assert stale.get("deepseek") == "", (
            "Auth-fail must not synthesise a stale marker where none "
            "existed"
        )

    async def test_no_key_does_not_touch_stale_marker(self):
        """No HTTP attempt means no 'provider is down' signal to
        record. Existing marker (if any) must survive — a prior 5xx
        followed by an operator clearing the key doesn't mean the
        provider recovered."""
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        stale.set("deepseek", "5555.0")
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=9_000.0,
            fetchers={"deepseek": _make_fetcher_ok()},
            key_resolver=lambda p: None,
            kv=_make_kv(), stale_kv=stale,
        )
        assert outcomes["deepseek"] == "no_key"
        assert stale.get("deepseek") == "5555.0"

    async def test_backoff_gate_does_not_touch_stale_marker(self):
        """Within the backoff window we skip the fetch entirely — the
        marker set by the previous failure must survive so the endpoint
        keeps rendering ``stale_since`` until the next successful
        fetch."""
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        stale.set("deepseek", "6666.0")
        state = {
            "deepseek": lbr._ProviderBackoff(
                consecutive_failures=2, next_attempt_at=10_000.0,
            ),
        }

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=5_000.0,
            fetchers={"deepseek": _make_fetcher_ok()},
            key_resolver=lambda p: "sk",
            kv=_make_kv(), stale_kv=stale,
        )
        assert outcomes["deepseek"] == "backoff"
        assert stale.get("deepseek") == "6666.0"

    async def test_multi_provider_stale_markers_independent(self):
        """Refresher writes per-provider markers; one failing vendor
        cannot accidentally mark the other stale."""
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        state: dict[str, lbr._ProviderBackoff] = {}

        await lbr.refresh_once(
            state=state, base_interval_s=600, now=1_000.0,
            fetchers={
                "deepseek": _make_fetcher_raises("502"),
                "openrouter": _make_fetcher_ok(amount=5.25),
            },
            key_resolver=lambda p: "sk",
            kv=_make_kv(), stale_kv=stale,
        )
        assert float(stale.get("deepseek")) == pytest.approx(1_000.0)
        assert stale.get("openrouter") == "", (
            "Successful provider must not inherit another's stale "
            "marker"
        )


class TestStaleMarkerHelpers:
    """Direct unit tests for the helpers so the endpoint + refresher
    share a single well-understood contract."""

    def test_write_and_read_round_trip(self):
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        lbr._write_stale_marker(stale, "deepseek", 12345.6789)
        assert lbr._read_stale_marker(stale, "deepseek") == pytest.approx(
            12345.6789,
        )

    def test_read_empty_returns_none(self):
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        assert lbr._read_stale_marker(stale, "deepseek") is None

    def test_clear_removes_entry(self):
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        lbr._write_stale_marker(stale, "deepseek", 1.0)
        lbr._clear_stale_marker(stale, "deepseek")
        assert lbr._read_stale_marker(stale, "deepseek") is None

    def test_unparseable_marker_self_heals(self):
        """Corrupted entry gets deleted on read so the next successful
        fetch can stop rendering stale without a manual intervention."""
        import uuid
        stale = SharedKV(f"stale_{uuid.uuid4().hex[:8]}")
        stale.set("deepseek", "not-a-float")
        result = lbr._read_stale_marker(stale, "deepseek")
        assert result is None
        assert stale.get("deepseek") == "", (
            "Unparseable entry must be self-healed (deleted)"
        )

    def test_stale_namespace_constant_is_distinct_from_balance(self):
        """Cache + stale live in separate SharedKV namespaces so a
        read of one cannot surface the other's data."""
        assert lbr.STALE_NAMESPACE != lbr.BALANCE_NAMESPACE
        assert lbr.STALE_NAMESPACE == "provider_balance_stale"


class TestRegistryIntegration:

    async def test_default_fetcher_set_is_supported_providers(
        self, monkeypatch,
    ):
        """Passing no ``fetchers`` kwarg must iterate the real
        SUPPORTED_BALANCE_PROVIDERS set — a regression here would
        silently stop refreshing providers added later."""
        from backend import llm_balance

        iterated: list[str] = []

        async def tracking_fetcher(api_key, *, now=None, **_):
            # We can't know which provider this is from the closure,
            # so the key_resolver plays that role.
            return _ok_balance(amount=1.0, when=now or 0.0)

        # Monkeypatch the registry entries to our tracker.
        monkeypatch.setattr(
            llm_balance, "SUPPORTED_BALANCE_PROVIDERS",
            {"deepseek": tracking_fetcher, "openrouter": tracking_fetcher},
            raising=True,
        )
        # The module-level alias in lbr was imported at module load —
        # refresh the reference so it picks up our patch.
        monkeypatch.setattr(
            lbr, "SUPPORTED_BALANCE_PROVIDERS",
            {"deepseek": tracking_fetcher, "openrouter": tracking_fetcher},
            raising=True,
        )

        def resolver(provider):
            iterated.append(provider)
            return f"sk-{provider}"

        state: dict[str, lbr._ProviderBackoff] = {}
        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600, now=0.0,
            key_resolver=resolver,
            kv=_make_kv(),
        )
        assert set(outcomes) == {"deepseek", "openrouter"}
        assert outcomes["deepseek"] == "ok"
        assert outcomes["openrouter"] == "ok"
        assert set(iterated) == {"deepseek", "openrouter"}
