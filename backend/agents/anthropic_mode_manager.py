"""AB.8 — Subscription ↔ API mode wizard + rollback state machine.

Backend state machine driving the 5-step migration wizard from
Claude Code subscription (Pro / Max) to Anthropic API key + Batch
mode. Surface mirrors what the Settings → Provider Keys frontend
will call (UI lands separately; this is the contract).

Five steps (idempotent re-entry — caller can poll state, repeat
any step that hasn't advanced):

  1. KEY_OBTAINED        — API key submitted + format-validated +
                           encrypted into AS Token Vault
  2. SPEND_LIMITS_SET    — daily / monthly cap configured via
                           AB.6 CostGuard
  3. MODE_SWITCHED       — OmniSight default LLM path flipped from
                           subscription to API; subscription
                           credentials retained as AB.8.4 fallback
  4. SMOKE_TEST_PASSED   — single small API call exercises the new
                           path end-to-end (token use loop,
                           tracker, cost guard) before opening to
                           production traffic
  5. CONFIRMED           — operator has reviewed observations;
                           wizard completes; subscription fallback
                           still kept for ``rollback_grace_days``
                           (default 30) before final
                           ``finalize_disable_subscription()``

Rollback (AB.8.4): the wizard explicitly keeps subscription
credentials around (``fallback_subscription_kept=True``) until the
operator runs ``finalize_disable_subscription()``. Until then,
``rollback()`` flips ``mode`` back to ``subscription`` in one call.
After finalization, rollback is a destructive operation (would
require re-enrolling with Anthropic).

Storage: in-memory ships for tests; production wires a small
PG-backed implementation when Settings UI lands. Wizard state is
explicitly NOT persisted across worker restarts in v1 — operators
run the wizard once interactively, and a half-completed wizard is
recoverable by re-entering any step (every advance method is
idempotent against the already-applied effects).

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §7
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum

from backend.agents.cost_guard import CostGuard, ScopeKey
from backend.agents.rate_limiter import WorkspaceKind

logger = logging.getLogger(__name__)


# ─── Enums ────────────────────────────────────────────────────────


class AnthropicMode(str, Enum):
    """Which auth path OmniSight uses for Anthropic calls."""

    SUBSCRIPTION = "subscription"  # Claude Code Pro / Max via OAuth (CLI)
    API = "api"                     # Anthropic API key (programmatic)


class WizardStep(str, Enum):
    """Linear progression of the 5-step migration wizard.

    Re-entrancy: every advance method is safe to call when already at
    or past the target step — it returns the existing state unchanged.
    Going backwards beyond the current step is a no-op (use
    ``rollback()`` instead).
    """

    NOT_STARTED = "not_started"
    KEY_OBTAINED = "key_obtained"
    SPEND_LIMITS_SET = "spend_limits_set"
    MODE_SWITCHED = "mode_switched"
    SMOKE_TEST_PASSED = "smoke_test_passed"
    CONFIRMED = "confirmed"


# Ordered tuple defines linear precedence; index = "depth into wizard".
_STEP_ORDER: tuple[WizardStep, ...] = (
    WizardStep.NOT_STARTED,
    WizardStep.KEY_OBTAINED,
    WizardStep.SPEND_LIMITS_SET,
    WizardStep.MODE_SWITCHED,
    WizardStep.SMOKE_TEST_PASSED,
    WizardStep.CONFIRMED,
)


def _step_index(step: WizardStep) -> int:
    return _STEP_ORDER.index(step)


# ─── Errors ──────────────────────────────────────────────────────


class WizardError(RuntimeError):
    """Base wizard error."""


class InvalidApiKeyError(WizardError):
    """API key format validation failed."""


class WizardOutOfOrderError(WizardError):
    """Caller invoked a step before its prerequisites completed."""


class WizardAlreadyConfirmedError(WizardError):
    """No further state changes allowed once wizard is confirmed
    (apart from explicit rollback / finalize)."""


# ─── State + config ──────────────────────────────────────────────


@dataclass(frozen=True)
class SmokeTestResult:
    """Outcome of the AB.8 step 4 smoke test."""

    call_id: str
    success: bool
    latency_ms: int
    cost_usd: float
    error_message: str | None = None
    response_excerpt: str = ""


@dataclass
class WizardState:
    """All state accumulated through the 5-step wizard."""

    mode: AnthropicMode = AnthropicMode.SUBSCRIPTION
    current_step: WizardStep = WizardStep.NOT_STARTED
    target_workspace: WorkspaceKind = "production"
    api_key_configured: bool = False
    api_key_fingerprint: str = ""
    """Last 8 chars of the API key for display only — never the full key."""

    spend_daily_usd: float | None = None
    spend_monthly_usd: float | None = None
    fallback_subscription_kept: bool = True
    """AB.8.4 — true until ``finalize_disable_subscription()`` runs."""

    smoke_test: SmokeTestResult | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    rollback_grace_until: datetime | None = None


# ─── Validation helpers ──────────────────────────────────────────


# Anthropic API keys start with "sk-ant-" and are 100+ chars URL-safe base64.
_API_KEY_PATTERN = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")


def validate_api_key(api_key: str) -> None:
    """Raises InvalidApiKeyError on malformed key (no API call made)."""
    if not api_key:
        raise InvalidApiKeyError("API key is empty")
    if not _API_KEY_PATTERN.match(api_key.strip()):
        raise InvalidApiKeyError(
            "API key does not match Anthropic format ('sk-ant-' + ≥20 URL-safe chars). "
            "Check console.anthropic.com → API Keys."
        )


def fingerprint_api_key(api_key: str) -> str:
    """Last 8 chars for display. Never store / log the full key."""
    stripped = api_key.strip()
    return f"…{stripped[-8:]}" if len(stripped) >= 8 else "<short>"


# ─── Manager ─────────────────────────────────────────────────────


KeyVaultWriter = Callable[[str, str], Awaitable[None]]
"""Callback that persists ``(workspace_kind, api_key)`` into AS Token
Vault. Production: wraps ``backend.security.token_vault.encrypt_for_user``
with the operator's tenant context. Tests: in-memory stub."""


SmokeTestRunner = Callable[[str, WorkspaceKind], Awaitable[SmokeTestResult]]
"""Callback that performs a real Anthropic API call to verify the new
mode works. Production: wraps ``AnthropicClient.simple()``. Tests:
deterministic stub."""


class AnthropicModeManager:
    """Drive the 5-step migration wizard.

    Composition root: caller wires:
      - ``key_vault_writer`` — persists API key to AS Token Vault
      - ``smoke_test_runner`` — executes the post-switch verification call
      - ``cost_guard`` — AB.6 CostGuard for spend cap configuration

    All advance methods return the post-call ``WizardState``. They are
    idempotent: calling ``submit_api_key`` when already past
    KEY_OBTAINED returns existing state unchanged unless the key value
    differs (which re-runs validation + persistence).
    """

    def __init__(
        self,
        *,
        key_vault_writer: KeyVaultWriter | None = None,
        smoke_test_runner: SmokeTestRunner | None = None,
        cost_guard: CostGuard | None = None,
        rollback_grace_days: int = 30,
    ) -> None:
        self._key_vault_writer = key_vault_writer
        self._smoke_test_runner = smoke_test_runner
        self._cost_guard = cost_guard
        self._rollback_grace_days = rollback_grace_days
        self._state = WizardState()

    # ── Read state ──────────────────────────────────────────

    @property
    def state(self) -> WizardState:
        return self._state

    @property
    def mode(self) -> AnthropicMode:
        return self._state.mode

    # ── Lifecycle ───────────────────────────────────────────

    async def start_wizard(self, target_workspace: WorkspaceKind = "production") -> WizardState:
        """Reset wizard to NOT_STARTED → KEY_OBTAINED is next.

        Idempotent: calling on an in-progress wizard returns the
        current state unchanged. To restart from scratch, call
        ``cancel_wizard()`` first.
        """
        if self._state.current_step != WizardStep.NOT_STARTED:
            return self._state
        self._state = WizardState(
            mode=self._state.mode,
            target_workspace=target_workspace,
            current_step=WizardStep.NOT_STARTED,
            started_at=datetime.now(timezone.utc),
        )
        return self._state

    async def cancel_wizard(self) -> WizardState:
        """Abort + reset. Does NOT change ``mode`` (use rollback for that)."""
        self._state = WizardState(
            mode=self._state.mode,
            target_workspace=self._state.target_workspace,
            current_step=WizardStep.NOT_STARTED,
        )
        return self._state

    # ── Step 1: API key ─────────────────────────────────────

    async def submit_api_key(self, api_key: str) -> WizardState:
        """Step 1 of 5. Validates format, persists via key_vault_writer."""
        self._guard_not_confirmed()
        validate_api_key(api_key)

        if self._key_vault_writer:
            await self._key_vault_writer(self._state.target_workspace, api_key.strip())

        self._state = replace(
            self._state,
            api_key_configured=True,
            api_key_fingerprint=fingerprint_api_key(api_key),
            current_step=self._max_step(WizardStep.KEY_OBTAINED),
        )
        return self._state

    # ── Step 2: spend limits ────────────────────────────────

    async def configure_spend_limits(
        self,
        *,
        daily_usd: float | None = None,
        monthly_usd: float | None = None,
    ) -> WizardState:
        """Step 2 of 5. Persists caps via AB.6 CostGuard against the
        ``workspace`` scope of the wizard's target_workspace."""
        self._guard_not_confirmed()
        if self._step_index() < _step_index(WizardStep.KEY_OBTAINED):
            raise WizardOutOfOrderError(
                "configure_spend_limits requires step >= KEY_OBTAINED"
            )

        if daily_usd is not None and daily_usd < 0:
            raise WizardError("daily_usd must be >= 0")
        if monthly_usd is not None and monthly_usd < 0:
            raise WizardError("monthly_usd must be >= 0")

        if self._cost_guard:
            await self._cost_guard.configure_budget(
                ScopeKey(kind="workspace", key=self._state.target_workspace),
                daily_limit_usd=daily_usd,
                monthly_limit_usd=monthly_usd,
            )

        self._state = replace(
            self._state,
            spend_daily_usd=daily_usd,
            spend_monthly_usd=monthly_usd,
            current_step=self._max_step(WizardStep.SPEND_LIMITS_SET),
        )
        return self._state

    # ── Step 3: switch mode ─────────────────────────────────

    async def switch_mode(self) -> WizardState:
        """Step 3 of 5. Flips OmniSight default mode to API.

        Subscription fallback retained (``fallback_subscription_kept=True``)
        until ``finalize_disable_subscription()`` runs after grace
        period. This is the AB.8.4 rollback safety net.
        """
        self._guard_not_confirmed()
        if self._step_index() < _step_index(WizardStep.SPEND_LIMITS_SET):
            raise WizardOutOfOrderError(
                "switch_mode requires step >= SPEND_LIMITS_SET"
            )

        self._state = replace(
            self._state,
            mode=AnthropicMode.API,
            current_step=self._max_step(WizardStep.MODE_SWITCHED),
            fallback_subscription_kept=True,
        )
        return self._state

    # ── Step 4: smoke test ─────────────────────────────────

    async def run_smoke_test(self) -> WizardState:
        """Step 4 of 5. Executes a small real API call to verify the
        full path (auth + tools + tracker + cost) end-to-end.

        On smoke test failure the wizard does NOT auto-rollback —
        operator decides whether to retry (re-call this method) or
        ``rollback()`` to subscription mode. State carries the
        SmokeTestResult so the UI can surface details.
        """
        self._guard_not_confirmed()
        if self._step_index() < _step_index(WizardStep.MODE_SWITCHED):
            raise WizardOutOfOrderError(
                "run_smoke_test requires step >= MODE_SWITCHED"
            )

        if self._smoke_test_runner is None:
            raise WizardError(
                "smoke_test_runner not configured; cannot exercise API path"
            )

        result = await self._smoke_test_runner(
            self._state.api_key_fingerprint,
            self._state.target_workspace,
        )

        next_step = (
            self._max_step(WizardStep.SMOKE_TEST_PASSED)
            if result.success
            else self._state.current_step
        )
        self._state = replace(
            self._state,
            smoke_test=result,
            current_step=next_step,
        )
        return self._state

    # ── Step 5: confirm ─────────────────────────────────────

    async def confirm(self) -> WizardState:
        """Step 5 of 5. Wizard complete; rollback grace clock starts."""
        self._guard_not_confirmed()
        if self._step_index() < _step_index(WizardStep.SMOKE_TEST_PASSED):
            raise WizardOutOfOrderError(
                "confirm requires step >= SMOKE_TEST_PASSED"
            )

        now = datetime.now(timezone.utc)
        self._state = replace(
            self._state,
            current_step=WizardStep.CONFIRMED,
            completed_at=now,
            rollback_grace_until=now + timedelta(days=self._rollback_grace_days),
        )
        return self._state

    # ── Rollback / finalize (AB.8.4) ────────────────────────

    async def rollback(self) -> WizardState:
        """Switch ``mode`` back to ``subscription``. Preserves API key
        configuration so a future re-run of the wizard can skip step 1
        if desired.

        Allowed at ANY point post-MODE_SWITCHED (including post-CONFIRM)
        as long as ``fallback_subscription_kept=True``. Once
        ``finalize_disable_subscription()`` has run, rollback raises.
        """
        if not self._state.fallback_subscription_kept:
            raise WizardError(
                "Rollback unavailable — fallback_subscription_kept=False. "
                "Re-enrolling Claude Code subscription requires manual auth."
            )
        if self._state.mode == AnthropicMode.SUBSCRIPTION:
            return self._state  # idempotent

        self._state = replace(
            self._state,
            mode=AnthropicMode.SUBSCRIPTION,
            current_step=WizardStep.NOT_STARTED,
            completed_at=None,
            rollback_grace_until=None,
        )
        return self._state

    async def finalize_disable_subscription(self) -> WizardState:
        """One-way: drop the subscription fallback. After this,
        ``rollback()`` is no longer available without manual re-enrollment.

        Operator typically calls this after ``rollback_grace_days``
        elapsed and confirms the API path is healthy in production.
        """
        if self._state.current_step != WizardStep.CONFIRMED:
            raise WizardError(
                "finalize_disable_subscription requires CONFIRMED state"
            )
        if self._state.rollback_grace_until and datetime.now(timezone.utc) < self._state.rollback_grace_until:
            raise WizardError(
                "Grace period not yet elapsed. Override by passing a fresh "
                "rollback_grace_days=0 manager (operator decision)."
            )
        self._state = replace(
            self._state,
            fallback_subscription_kept=False,
        )
        return self._state

    # ── Helpers ─────────────────────────────────────────────

    def _step_index(self) -> int:
        return _step_index(self._state.current_step)

    def _max_step(self, candidate: WizardStep) -> WizardStep:
        """Return the further-progressed of (current, candidate).
        Idempotent advance — never go backwards just because a re-call
        passes the same step."""
        if _step_index(candidate) > self._step_index():
            return candidate
        return self._state.current_step

    def _guard_not_confirmed(self) -> None:
        if self._state.current_step == WizardStep.CONFIRMED:
            raise WizardAlreadyConfirmedError(
                "Wizard already confirmed. Use rollback() or "
                "start_wizard() to begin a new migration."
            )
