# anthropic_mode_manager

**Purpose**: Backend state machine driving the AB.8 5-step wizard that migrates OmniSight from a Claude Code subscription (OAuth) to an Anthropic API key, with a built-in rollback safety net (AB.8.4).

**Key types / public surface**:
- `AnthropicModeManager` — the state machine; exposes `start_wizard`, `submit_api_key`, `configure_spend_limits`, `switch_mode`, `run_smoke_test`, `confirm`, `rollback`, `finalize_disable_subscription`.
- `WizardState` (dataclass) — accumulated state returned by every advance method.
- `AnthropicMode`, `WizardStep` — enums for the two auth paths and the linear 5-step progression.
- `SmokeTestResult` — frozen dataclass capturing step-4 outcome (success, latency, cost, excerpt).
- `validate_api_key` / `fingerprint_api_key` — format check (regex `^sk-ant-…`) and last-8-char display fingerprint.

**Key invariants**:
- Every advance is idempotent and monotonic: `_max_step` never moves the wizard backwards just because a step is re-called; out-of-order calls raise `WizardOutOfOrderError`.
- Wizard state is **not** persisted across worker restarts in v1 — recovery relies on operators re-entering steps interactively.
- `fallback_subscription_kept=True` is the rollback contract: `rollback()` works any time post-`MODE_SWITCHED` until `finalize_disable_subscription()` flips it off (one-way; re-enrollment then requires manual Anthropic auth).
- `finalize_disable_subscription()` requires `OMNISIGHT_AB_API_MODE_ENABLED=true` in the deployed worker env after the rollback grace window; every worker independently derives the same lock value from env.
- Smoke-test failure does **not** auto-rollback — operator chooses retry vs. `rollback()`. Only the fingerprint of the key is ever stored; full key never logged.

**Cross-module touchpoints**:
- Imports `CostGuard` / `ScopeKey` from `backend.agents.cost_guard` (AB.6) for spend caps, and `WorkspaceKind`/`WorkspaceConfig` from `backend.agents.rate_limiter`.
- Designed to be wired by a composition root with a `KeyVaultWriter` (production: `backend.security.token_vault.encrypt_for_user`) and a `SmokeTestRunner` (production: `AnthropicClient.simple()`).
- Intended consumer: Settings → Provider Keys UI (not yet landed); ADR at `docs/operations/anthropic-api-migration-and-batch-mode.md §7`.
