"""AB.8 — Subscription ↔ API mode manager / wizard / rollback tests.

Locks:
  - validate_api_key: format match, empty rejected, garbage rejected,
    trimmed-whitespace accepted
  - fingerprint_api_key: shows last 8 chars, never the full key
  - WizardState defaults sensible (subscription, NOT_STARTED, fallback kept)
  - 5-step happy path: NOT_STARTED → KEY_OBTAINED → SPEND_LIMITS_SET
    → MODE_SWITCHED → SMOKE_TEST_PASSED → CONFIRMED with state +
    side effects per step
  - Idempotence: re-calling submit_api_key with same key doesn't move
    backward; re-calling already-passed steps no-ops
  - Out-of-order: configure_spend_limits before key submission raises,
    switch_mode before spend limits raises, run_smoke_test before
    switch raises, confirm before smoke test raises
  - Smoke test failure does not auto-advance; success does
  - Rollback from CONFIRMED state succeeds (mode → subscription),
    rollback from MODE_SWITCHED succeeds, rollback after finalize raises
  - finalize_disable_subscription: requires CONFIRMED, requires grace
    elapsed (or 0-day grace operator override path), drops fallback flag
  - cancel_wizard: resets state but keeps mode

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §7
"""

from __future__ import annotations


import pytest

from backend.agents.anthropic_mode_manager import (
    AnthropicMode,
    AnthropicModeManager,
    InvalidApiKeyError,
    SmokeTestResult,
    WizardAlreadyConfirmedError,
    WizardError,
    WizardOutOfOrderError,
    WizardStep,
    fingerprint_api_key,
    validate_api_key,
)
from backend.agents.cost_guard import CostGuard, ScopeKey


# ─── validate_api_key ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_accepts_canonical_anthropic_key():
    key = "sk-ant-" + "abcdef0123_-XYZ" * 4
    validate_api_key(key)

    mgr = AnthropicModeManager()
    state = await mgr.submit_api_key(key)
    assert state.current_step == WizardStep.KEY_OBTAINED
    assert state.api_key_fingerprint == "…123_-XYZ"


def test_validate_rejects_empty():
    with pytest.raises(InvalidApiKeyError, match="empty"):
        validate_api_key("")


def test_validate_rejects_wrong_prefix():
    with pytest.raises(InvalidApiKeyError, match="does not match Anthropic format"):
        validate_api_key("sk-openai-1234567890ABCDEFGHIJ1234567890")


def test_validate_rejects_too_short():
    with pytest.raises(InvalidApiKeyError, match="does not match Anthropic format"):
        validate_api_key("sk-ant-short")


def test_validate_accepts_trimmed_whitespace():
    """User pasting from console may include surrounding whitespace."""
    # The validate function strips before checking
    validate_api_key("  sk-ant-" + "A" * 30 + "  ")


# ─── fingerprint_api_key ─────────────────────────────────────────


def test_fingerprint_shows_last_8():
    key = "sk-ant-VERYLONGSECRET12345678ABCDEF"
    fp = fingerprint_api_key(key)
    # Last 8 chars of input, prefixed with ellipsis. Locks the
    # "never log full key" invariant.
    assert fp == "…78ABCDEF"
    assert "VERYLONG" not in fp
    assert "SECRET" not in fp


def test_fingerprint_short_input():
    fp = fingerprint_api_key("xy")
    assert fp == "<short>"


# ─── Default state ───────────────────────────────────────────────


def test_default_state_is_subscription_not_started():
    mgr = AnthropicModeManager()
    s = mgr.state
    assert s.mode == AnthropicMode.SUBSCRIPTION
    assert s.current_step == WizardStep.NOT_STARTED
    assert not s.api_key_configured
    assert s.fallback_subscription_kept


# ─── Happy path: 5 steps end-to-end ──────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_5_steps():
    vault: dict[str, str] = {}

    async def vault_writer(workspace: str, key: str) -> None:
        vault[workspace] = key

    smoke_calls: list[tuple[str, str]] = []

    async def smoke_runner(fingerprint: str, workspace: str) -> SmokeTestResult:
        smoke_calls.append((fingerprint, workspace))
        return SmokeTestResult(
            call_id="smoke_1",
            success=True,
            latency_ms=420,
            cost_usd=0.001,
            response_excerpt="hi",
        )

    cost_guard = CostGuard()
    mgr = AnthropicModeManager(
        key_vault_writer=vault_writer,
        smoke_test_runner=smoke_runner,
        cost_guard=cost_guard,
    )

    await mgr.start_wizard(target_workspace="batch")
    assert mgr.state.target_workspace == "batch"
    assert mgr.state.current_step == WizardStep.NOT_STARTED
    assert mgr.state.started_at is not None

    # Step 1
    s1 = await mgr.submit_api_key("sk-ant-" + "A" * 40)
    assert s1.current_step == WizardStep.KEY_OBTAINED
    assert s1.api_key_configured
    assert s1.api_key_fingerprint.startswith("…")
    assert vault["batch"].startswith("sk-ant-")

    # Step 2
    s2 = await mgr.configure_spend_limits(daily_usd=30.0, monthly_usd=500.0)
    assert s2.current_step == WizardStep.SPEND_LIMITS_SET
    assert s2.spend_daily_usd == 30.0
    assert s2.spend_monthly_usd == 500.0
    cap = await cost_guard.store.get_budget(ScopeKey("workspace", "batch"))
    assert cap is not None
    assert cap.daily_limit_usd == 30.0
    assert cap.monthly_limit_usd == 500.0

    # Step 3
    s3 = await mgr.switch_mode()
    assert s3.current_step == WizardStep.MODE_SWITCHED
    assert s3.mode == AnthropicMode.API
    assert s3.fallback_subscription_kept  # AB.8.4 safety net

    # Step 4
    s4 = await mgr.run_smoke_test()
    assert s4.current_step == WizardStep.SMOKE_TEST_PASSED
    assert s4.smoke_test is not None
    assert s4.smoke_test.success
    assert smoke_calls == [(s1.api_key_fingerprint, "batch")]

    # Step 5
    s5 = await mgr.confirm()
    assert s5.current_step == WizardStep.CONFIRMED
    assert s5.completed_at is not None
    assert s5.rollback_grace_until is not None
    assert s5.rollback_grace_until > s5.completed_at


# ─── Idempotence ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_api_key_idempotent():
    mgr = AnthropicModeManager()
    await mgr.submit_api_key("sk-ant-" + "B" * 30)
    state_first = mgr.state
    # Calling with the SAME key should advance the step (already past)
    # but not regress.
    await mgr.submit_api_key("sk-ant-" + "B" * 30)
    assert mgr.state.current_step == WizardStep.KEY_OBTAINED


@pytest.mark.asyncio
async def test_advancing_already_passed_step_no_regress():
    """Calling submit_api_key after spend limits set must not regress."""
    mgr = AnthropicModeManager()
    await mgr.submit_api_key("sk-ant-" + "C" * 30)
    await mgr.configure_spend_limits(daily_usd=10.0)
    # Re-calling step 1
    await mgr.submit_api_key("sk-ant-" + "D" * 30)
    # Step should remain SPEND_LIMITS_SET (no regression)
    assert mgr.state.current_step == WizardStep.SPEND_LIMITS_SET


# ─── Out-of-order ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spend_limits_before_key_raises():
    mgr = AnthropicModeManager()
    with pytest.raises(WizardOutOfOrderError, match=">= KEY_OBTAINED"):
        await mgr.configure_spend_limits(daily_usd=10.0)


@pytest.mark.asyncio
async def test_switch_mode_before_spend_limits_raises():
    mgr = AnthropicModeManager()
    await mgr.submit_api_key("sk-ant-" + "X" * 30)
    with pytest.raises(WizardOutOfOrderError, match=">= SPEND_LIMITS_SET"):
        await mgr.switch_mode()


@pytest.mark.asyncio
async def test_smoke_test_before_switch_raises():
    mgr = AnthropicModeManager(smoke_test_runner=_unreachable_smoke)
    await mgr.submit_api_key("sk-ant-" + "Y" * 30)
    await mgr.configure_spend_limits(daily_usd=10.0)
    with pytest.raises(WizardOutOfOrderError, match=">= MODE_SWITCHED"):
        await mgr.run_smoke_test()


@pytest.mark.asyncio
async def test_confirm_before_smoke_test_raises():
    mgr = AnthropicModeManager()
    await mgr.submit_api_key("sk-ant-" + "Z" * 30)
    await mgr.configure_spend_limits(daily_usd=10.0)
    await mgr.switch_mode()
    with pytest.raises(WizardOutOfOrderError, match=">= SMOKE_TEST_PASSED"):
        await mgr.confirm()


async def _unreachable_smoke(fingerprint, workspace):  # pragma: no cover
    raise AssertionError("smoke runner should not be reached")


# ─── Smoke test failure handling ─────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_test_failure_does_not_advance():
    async def failing_smoke(fp, ws):
        return SmokeTestResult(
            call_id="bad", success=False, latency_ms=0, cost_usd=0.0,
            error_message="auth failed", response_excerpt="",
        )

    mgr = AnthropicModeManager(smoke_test_runner=failing_smoke)
    await mgr.submit_api_key("sk-ant-" + "F" * 30)
    await mgr.configure_spend_limits(daily_usd=10.0)
    await mgr.switch_mode()
    s = await mgr.run_smoke_test()
    assert s.smoke_test is not None
    assert not s.smoke_test.success
    # Step did NOT advance to SMOKE_TEST_PASSED.
    assert s.current_step == WizardStep.MODE_SWITCHED
    # Operator can retry — smoke runner is called again
    # (idempotent advance attempt).


@pytest.mark.asyncio
async def test_smoke_test_runner_required():
    mgr = AnthropicModeManager(smoke_test_runner=None)
    await mgr.submit_api_key("sk-ant-" + "S" * 30)
    await mgr.configure_spend_limits(daily_usd=10.0)
    await mgr.switch_mode()
    with pytest.raises(WizardError, match="smoke_test_runner not configured"):
        await mgr.run_smoke_test()


# ─── Rollback (AB.8.4) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_from_confirmed_state_works():
    async def smoke_ok(fp, ws):
        return SmokeTestResult(call_id="ok", success=True, latency_ms=10, cost_usd=0.001)

    mgr = AnthropicModeManager(smoke_test_runner=smoke_ok)
    await mgr.submit_api_key("sk-ant-" + "R" * 30)
    await mgr.configure_spend_limits(daily_usd=10.0)
    await mgr.switch_mode()
    await mgr.run_smoke_test()
    await mgr.confirm()
    assert mgr.mode == AnthropicMode.API

    s = await mgr.rollback()
    assert s.mode == AnthropicMode.SUBSCRIPTION
    assert s.current_step == WizardStep.NOT_STARTED
    # API key remains configured for fast re-migration
    assert s.api_key_configured


@pytest.mark.asyncio
async def test_rollback_from_mode_switched_works():
    """Rollback allowed mid-wizard (post-MODE_SWITCHED, before CONFIRM)."""
    mgr = AnthropicModeManager()
    await mgr.submit_api_key("sk-ant-" + "M" * 30)
    await mgr.configure_spend_limits(daily_usd=10.0)
    await mgr.switch_mode()
    s = await mgr.rollback()
    assert s.mode == AnthropicMode.SUBSCRIPTION


@pytest.mark.asyncio
async def test_rollback_idempotent_when_already_subscription():
    mgr = AnthropicModeManager()
    s = await mgr.rollback()
    assert s.mode == AnthropicMode.SUBSCRIPTION


# ─── finalize_disable_subscription ───────────────────────────────


@pytest.mark.asyncio
async def test_finalize_requires_confirmed():
    mgr = AnthropicModeManager()
    await mgr.submit_api_key("sk-ant-" + "T" * 30)
    with pytest.raises(WizardError, match="requires CONFIRMED"):
        await mgr.finalize_disable_subscription()


@pytest.mark.asyncio
async def test_finalize_blocked_during_grace_period():
    async def smoke_ok(fp, ws):
        return SmokeTestResult(call_id="ok", success=True, latency_ms=10, cost_usd=0.001)

    mgr = AnthropicModeManager(smoke_test_runner=smoke_ok, rollback_grace_days=30)
    await _drive_through_confirm(mgr)
    with pytest.raises(WizardError, match="Grace period not yet elapsed"):
        await mgr.finalize_disable_subscription()


@pytest.mark.asyncio
async def test_finalize_succeeds_with_zero_grace():
    """rollback_grace_days=0 lets operator finalize immediately."""
    async def smoke_ok(fp, ws):
        return SmokeTestResult(call_id="ok", success=True, latency_ms=10, cost_usd=0.001)

    mgr = AnthropicModeManager(smoke_test_runner=smoke_ok, rollback_grace_days=0)
    await _drive_through_confirm(mgr)
    s = await mgr.finalize_disable_subscription()
    assert not s.fallback_subscription_kept


@pytest.mark.asyncio
async def test_rollback_after_finalize_raises():
    async def smoke_ok(fp, ws):
        return SmokeTestResult(call_id="ok", success=True, latency_ms=10, cost_usd=0.001)

    mgr = AnthropicModeManager(smoke_test_runner=smoke_ok, rollback_grace_days=0)
    await _drive_through_confirm(mgr)
    await mgr.finalize_disable_subscription()
    with pytest.raises(WizardError, match="fallback_subscription_kept=False"):
        await mgr.rollback()


async def _drive_through_confirm(mgr: AnthropicModeManager) -> None:
    await mgr.submit_api_key("sk-ant-" + "Z" * 40)
    await mgr.configure_spend_limits(daily_usd=10.0)
    await mgr.switch_mode()
    await mgr.run_smoke_test()
    await mgr.confirm()


# ─── Confirmed-state guard ───────────────────────────────────────


@pytest.mark.asyncio
async def test_advance_after_confirmed_raises():
    async def smoke_ok(fp, ws):
        return SmokeTestResult(call_id="ok", success=True, latency_ms=10, cost_usd=0.001)

    mgr = AnthropicModeManager(smoke_test_runner=smoke_ok)
    await _drive_through_confirm(mgr)
    with pytest.raises(WizardAlreadyConfirmedError):
        await mgr.submit_api_key("sk-ant-" + "Q" * 30)
    with pytest.raises(WizardAlreadyConfirmedError):
        await mgr.configure_spend_limits(daily_usd=5.0)


# ─── Validation in configure_spend_limits ────────────────────────


@pytest.mark.asyncio
async def test_negative_spend_limit_rejected():
    mgr = AnthropicModeManager()
    await mgr.submit_api_key("sk-ant-" + "N" * 30)
    with pytest.raises(WizardError, match="daily_usd must be"):
        await mgr.configure_spend_limits(daily_usd=-1.0)
    with pytest.raises(WizardError, match="monthly_usd must be"):
        await mgr.configure_spend_limits(monthly_usd=-100.0)


# ─── cancel_wizard ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_wizard_resets_step_keeps_mode():
    mgr = AnthropicModeManager()
    await mgr.submit_api_key("sk-ant-" + "K" * 30)
    await mgr.configure_spend_limits(daily_usd=10.0)
    await mgr.switch_mode()
    assert mgr.mode == AnthropicMode.API

    s = await mgr.cancel_wizard()
    assert s.current_step == WizardStep.NOT_STARTED
    # Mode unchanged — cancel is "abort wizard", not "rollback"
    assert s.mode == AnthropicMode.API


# ─── Bad API key formats ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_invalid_api_key_raises_no_persist():
    persisted = []

    async def writer(ws, key):
        persisted.append((ws, key))

    mgr = AnthropicModeManager(key_vault_writer=writer)
    with pytest.raises(InvalidApiKeyError):
        await mgr.submit_api_key("not-a-key")
    assert persisted == []
    assert not mgr.state.api_key_configured
