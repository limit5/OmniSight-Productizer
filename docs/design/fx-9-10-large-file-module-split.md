# ADR FX.9.10 — Post-Deploy Refresh of the >2000-LOC Module-Split Plan (Frontend + Cross-Stack)

> **Status:** Accepted (planning only — execution scheduled per §7).
> **Date:** 2026-05-04 (post first prod deploy at master `9676d17e`).
> **Owner:** Claude / Opus.
> **Snapshot commit:** `d3efa820` (after FX.9.9 i18n landing).
> **Companion ADR:** `docs/design/fx-7-3-large-file-module-split.md` (covers 9 *backend Python* files; written 2026-05-04 morning, before deploy).
> **TODO row:** Priority FX → `FX.9.10`.

## 0. Scope and relationship to FX.7.3

FX.7.3 (the morning-of-deploy ADR) froze 9 backend Python files >2000 LOC, decomposed each, and pinned a 9-wave schedule (W1-W9) over 7 calendar weeks. The list was deliberately Python-only: the audit row that motivated FX.7.3 (`docs/audit/2026-05-03-deep-audit.md` §DT14-DT18) excluded TypeScript by design — the audit pass was scoped to backend.

The 2026-05-04 production deploy uncovered a parallel concern: **frontend type drift** (React 19 + `.ts` extension issues, i18n scaffold-only landing in FX.7.11, then the FX.9.9 catch-up translation). When the deploy postmortem and the FX.9 follow-up batch were drafted, the question "are there frontend files we should also be planning to split?" was deferred — and FX.9.10 was opened to answer it.

This ADR therefore complements (does not supersede) FX.7.3:

- FX.7.3 owns the **9 backend Python** decomposition and W1-W9 schedule.
- FX.9.10 owns the **3 frontend giants** decomposition + a **post-deploy refresh** of the full >2000-LOC list (so the watch list is current and includes everything, not just what existed pre-deploy).
- Where the two overlap (a file appears on both lists because it was already in FX.7.3), FX.9.10 cross-references FX.7.3 instead of duplicating the design — the contract is "FX.7.3 is the source of truth for those 6 files; FX.9.10 just re-confirms they are still on the schedule".

What this ADR is **not**:

- It does **not** re-open the FX.7.3 decisions. The 6 overlap files keep their FX.7.3 wave assignments and target dates.
- It does **not** force frontend splits to follow Python-style splitting rules. Section 3 introduces TS-specific rules (named imports + barrel files, no star re-export, ESM tree-shaking).
- It does **not** dictate final module names. Like FX.7.3, the wave PR may rename if the actual code reads better.

## 1. The frozen 9 files (snapshot 2026-05-04 @ `d3efa820`)

Top-9 by LOC across the whole repo, excluding (a) auto-generated files (`lib/generated/api-types.ts` 46 099 LOC — banned from hand-edits, see §1.2) and (b) test files (5 files >2000 LOC — see §1.3 for why excluded).

| # | Path | LOC | Stack | FX.7.3 ID | Owner ADR | Notes |
|---|------|----:|-------|-----------|-----------|-------|
| **G1** | `lib/api.ts` | **7 223** | TS | — | **FX.9.10** | API client. 589 top-level exports; **151 importers**. Largest hand-written file in the repo. |
| **G2** | `components/omnisight/integration-settings.tsx` | **4 680** | TSX | — | **FX.9.10** | Settings dialog. 6 test importers. |
| **G3** | `app/bootstrap/page.tsx` | **4 215** | TSX | — | **FX.9.10** | Bootstrap wizard page. 2 test importers. |
| G4 | `backend/routers/tenant_projects.py` | 3 878 | Py | F4 | FX.7.3 | Already on FX.7.3 W4 (2026-05-22 → 25). |
| G5 | `backend/db.py` | 3 639 | Py | F3 | FX.7.3 | Already on FX.7.3 W3 (2026-05-12 → 14). |
| G6 | `backend/routers/bootstrap.py` | 3 351 | Py | F5 | FX.7.3 | Already on FX.7.3 W5 (2026-05-26 → 29). |
| G7 | `backend/depth_sensing.py` | 3 215 | Py | F2 | FX.7.3 | Already on FX.7.3 W2 (2026-05-09 → 11). |
| G8 | `backend/routers/invoke.py` | 2 923 | Py | F8 | FX.7.3 | Already on FX.7.3 W8 (2026-06-16 → 19). |
| G9 | `backend/routers/system.py` | 2 530 | Py | F6 | FX.7.3 | Already on FX.7.3 W6 (2026-05-30 → 06-03). |

**Where FX.9.10 actually adds new work:** the 3 frontend giants (G1, G2, G3). The remaining 6 (G4-G9) are already covered by FX.7.3 and are listed here only so the post-deploy reader sees the *complete* top-9 in one place.

> **Net new LOC under planning by FX.9.10:** 16 118 (G1+G2+G3). Total >2000-LOC source LOC across both ADRs (FX.7.3 + FX.9.10 net new): 26 531 + 16 118 = **42 649 LOC** under formal split planning.

### 1.1 Below-the-focal-9 watch list (still >2000 LOC, not focal in FX.9.10)

Six more source files exceed 2000 LOC but ranked below G9. They are **on the watch list** and the §6.1 drift guard prevents them from growing further; their split decisions go to follow-up ADRs as capacity allows.

| Path | LOC | Owner ADR | Disposition |
|------|----:|-----------|-------------|
| `backend/agents/tools.py` | 2 437 | FX.7.3 (F7) | On FX.7.3 W7 (2026-06-11 → 15). Covered. |
| `backend/onvif_device.py` | 2 389 | FX.7.3 (F1) | On FX.7.3 W1 (2026-05-07 → 08). Covered. |
| `backend/enterprise_web_stack.py` | 2 169 | **deferred** | New since FX.7.3 audit. Add to future Priority MS sub-epic; no FX.9.10 design today. |
| `backend/auth.py` | 2 169 | FX.7.3 (F9) | On FX.7.3 W9 (2026-06-20 → 24). Covered. |
| `backend/routers/integration.py` | 2 110 | **deferred** | New since FX.7.3 audit. Add to Priority MS. |
| `backend/routers/admin_tenants.py` | 2 067 | **deferred** | New since FX.7.3 audit. Add to Priority MS. |

The 3 *deferred* rows (enterprise_web_stack, routers/integration, routers/admin_tenants) are flagged so the next ADR-author knows exactly which files are unplanned. Adding them is not in scope for FX.9.10 because (a) they are smaller than the focal 9, (b) post-deploy frontend was the explicit motivator for FX.9.10, and (c) the SOP §2 anti-bulldozer rule says don't expand scope mid-row.

### 1.2 `lib/generated/api-types.ts` is special — never split, never grow by hand

`lib/generated/api-types.ts` is **46 099 LOC**, ~6× the next biggest hand-written file. It is regenerated by `npm run gen:openapi` from `backend`'s OpenAPI export. Splitting it would (a) break the generator's single-file output contract, (b) require the generator to learn package layouts, and (c) break the 151 `import { ... } from "@/lib/generated/api-types"` call sites for zero refactor value (the file is read-only to humans).

The §6.1 drift guard exempts this file by exact path. A separate guard (§6.3) confirms the file is still produced by the generator and has not been hand-edited (checks the `// AUTO-GENERATED — do not edit` header).

### 1.3 Test files >2000 LOC are excluded — same precedent as FX.7.3 §9.3

Five test files exceed 2000 LOC: `test_integration_settings.py` (2 456), `test_mobile_build_error_autofix.py` (2 338), `test_bot_challenge.py` (2 153), `test_web_sandbox.py` (2 068), `test/components/bootstrap-page.test.tsx` (2 323). The FX.7.3 ADR §9.3 explicitly excluded test files from the focal list because tests are leaf nodes — splitting them is a separate quality concern (FX.5 territory). FX.9.10 inherits that decision unchanged.

The §6.1 drift guard still globs all `*.py` / `*.ts` / `*.tsx` and applies the 2000 LOC cap, but uses a *separate* `ALLOWED_FROZEN_TESTS` list so test growth does not relax production-file rules.

## 2. Why FX.9.10 exists at all (post-deploy motivation)

The 2026-05-04 prod deploy revealed three uncomfortable facts about the frontend codebase that FX.7.3 had implicitly assumed away:

1. **`lib/api.ts` is the single largest hand-written file in the entire repo (7 223 LOC).** It got past the FX.7.3 audit cutoff because the audit was Python-only. Every UI feature touches it; PR conflicts cluster on it; the function-call surface (589 exports) is now larger than the entire `backend/auth.py` *and* `backend/agents/tools.py` combined.
2. **`integration-settings.tsx` and `app/bootstrap/page.tsx` are dialog/page-level monoliths** carrying ~10 distinct sub-features each (LLM provisioning + Cloudflare tunnel + git-forge + smoke + service health + ... in `bootstrap/page.tsx`; tenant-secrets + LLM credentials + Gerrit wizard + storage quota + circuit breaker + network egress + ... in `integration-settings.tsx`). The 4-locale i18n translation in FX.9.9 had to touch hundreds of strings inside these files, and the merge-conflict surface against in-flight feature work was visible.
3. **No drift guard caps frontend LOC.** FX.7.3's `test_large_file_drift_guard.py` (specified in §6.1, lands W0) globs `backend/**/*.py` only. Without a TS-side equivalent, frontend files can keep growing and the next "frontend giants" ADR will be forced to absorb whatever new monsters land between now and then.

A "lint at 1 000 LOC" rule for frontend has the same problem FX.7.3 §2 cited for backend: it would either be set so loose it doesn't bind, or force splits along arbitrary boundaries instead of *semantic* seams. This ADR records the semantic seams; the lint rule (§6.1) only protects them after the work is done.

## 3. Splitting rules for frontend (TS-specific addendum to FX.7.3 §3)

FX.7.3 §3.1-3.6 covered Python: re-export shims, router `APIRouter` preservation, `_DUMMY_PASSWORD_HASH`-style import-time invariants. The frontend ecosystem has different load-bearing constraints. The rules below apply to any wave that splits a TS/TSX file under FX.9.10 scope; they are **additive** to FX.7.3 §3.5 (no behaviour change in same commit) and §3.6 (pre-commit fingerprint grep).

### 3.1 Public API is frozen — exports stay reachable from the original path

Any symbol currently importable as `import { Y } from "@/lib/api"` (or `from "@/components/omnisight/integration-settings"`, etc.) **must remain importable from that path** for at least one full release after the split. Mechanism:

- Move the implementation to `lib/api/<group>.ts` (or `components/omnisight/integration-settings/<group>.tsx`).
- In the original `lib/api.ts`, replace the moved code with **explicit named re-exports**: `export { listAgents, createAgent, deleteAgent } from "./api/agents"`. **No `export *`** — TS namespace pollution risks colliding with same-name symbols across groups, and IDE go-to-definition gets noisier when star re-export hides the source.
- A new test (`test/lib/api-public-surface-drift-guard.test.ts`) snapshots `Object.keys(import * as api from "@/lib/api")` *before* the split and asserts the post-split surface is a superset.

### 3.2 Tree-shaking must not regress

`lib/api.ts` is bundled into the Next.js client. Today, even though the file is 7 223 LOC, ESM tree-shaking lets the bundler include only the named imports a page actually uses (verified informally via `next build && du -sh .next/static/chunks`). A split that introduces *side-effectful* module evaluation in a sub-module (e.g. a top-level `subscribeEvents()` call, a singleton SSE manager that `setInterval`s on import) breaks tree-shaking and can balloon bundle size.

**Rule:** every new `lib/api/<group>.ts` must be marked `"sideEffects": false`-compatible. Translation: no top-level statements with side effects. The existing global SSE manager state (lines 346-570 of current `lib/api.ts`) stays in `lib/api/_sse.ts` and is initialised lazily on first `subscribeEvents()` call, not at module import.

A bundle-size drift guard (§6.4) takes a baseline `du -sh .next/static/chunks` (or equivalent — the actual metric is "biggest chunk byte size"); wave PRs that grow the metric >10 % must justify in the PR description.

### 3.3 React component splits preserve the default export

`app/bootstrap/page.tsx` is a Next.js App Router page; the default export **is** the route handler. Splitting must keep `export default function BootstrapPage()` at `app/bootstrap/page.tsx`. The sub-step components (`AdminPasswordStep`, `LlmProviderStep`, `InitTenantStep`, `CfTunnelStep`, `GitForgeStep`, `VerticalSetupStep`, `ServiceHealthStep`, `SmokeSubsetStep`) move to `app/bootstrap/_steps/<id>.tsx` — note the `_` prefix, which Next.js treats as "this directory is not a route". The default-export page imports each step and renders by `currentStep` switch.

`components/omnisight/integration-settings.tsx` exports two named functions (`IntegrationSettings`, `SettingsButton`); both stay at the original path and re-export from the now-package layout `components/omnisight/integration-settings/index.tsx`.

### 3.4 Test-file rewiring is bounded and explicit

The 6+2+? test importers must update one import line per file. The wave PR includes the test rewires; **do not** ship a `// TODO: re-import paths` shim. The same `git diff -M50` rename-similarity check from FX.7.3 §3.5 applies: most of the diff should be `R` (rename), not `M` (modify).

### 3.5 No styling / a11y / behaviour changes in the same commit

Same anti-bulldozer rule as FX.7.3 §3.5. A frontend split that "happens to also" reformat a Tailwind class string, fix an a11y warning, or upgrade a dependency is rejected. Open a follow-up row.

### 3.6 SOP §3 fingerprint grep still runs (ported to TS)

Frontend has its own legacy fingerprints. Pre-commit grep should catch:

```
grep -nE "useEffect\(\(\) => \{[^}]*setState[^}]*\}, \[\]\)|getServerSideProps|jest\.fn\(\)" <file>
```

— legacy `useEffect → setState` patterns (React 19 deprecated, see FX.9 deploy postmortem), `getServerSideProps` (Pages Router holdover; App Router uses async server components), `jest.fn()` (this repo uses Vitest; `jest.fn` only exists via shim and indicates a copy-pasted test from a different project). Hits do not block commit but file a follow-up FX.9.x row, same anti-bulldozer rule as FX.7.3 §3.6.

## 4. Per-file split design

### G1. `lib/api.ts` (7 223 LOC, **highest-priority wave**)

**Why first among frontend:** 151 direct importers — every frontend route reads it. The 50+ `// ─── <Section> ───` comment headers already mark de facto seams; the split is more "promote each section to its own file" than "find new seams". 589 top-level exports.

**Target package:** `lib/api/` (rename from `lib/api.ts` to package; old import stays via re-export shim per §3.1).

| Submodule | Rough lines (current) | Content |
|-----------|----------------------:|---------|
| `_base.ts` | 1–28 | `_resolveApiBase()`, `API_V1` constant |
| `_sse.ts` | 29–610 | `SSEEvent` discriminated union (~200 variants), global SSE manager, `subscribeEvents`, filter mode, host metrics tick types |
| `_errors.ts` | 610–970 | `ApiError`, `ApiErrorKind`, `onApiError` listener, error helper functions |
| `agents.ts` | 993–1031 | Agents CRUD |
| `tasks.ts` | 1032–1090 | Tasks CRUD |
| `handoffs.ts` | 1091–1106 | Handoffs |
| `chat.ts` | 1107–1189 | Chat send/stream/history |
| `providers.ts` | 1190–1458 | Provider config, balance, ollama compat |
| `system.ts` | 1459–1572 | System info, spec, repos, logs |
| `tokens.ts` | 1574–1762 | Token usage daily / hourly / heatmap / burn-rate |
| `simulations.ts` | 1763–1805 | Simulations + dashboard aggregator |
| `integration.ts` | 1874–2444 | Integration settings + git-forge probe + git_accounts CRUD + llm_credentials CRUD + Gerrit setup |
| `tenants.ts` | 2445–2536 | Tenant secrets + per-tenant disk quota |
| `events_artifacts.ts` | 2537–2578 | Event replay + artifacts |
| `auth.ts` | 2579–2904 | Phase 54 auth + internet-exposure hardening |
| `admin_tenants.ts` | 2905–3122 | Admin tenant CRUD |
| `tenant_settings.ts` | 2978–3227 | Tenant settings page (Y8 row 4) |
| `project_settings.ts` | 3228–3427 | Project settings page (Y8 row 5) |
| `sessions.ts` | 3428–3722 | Session management + Q.5 device presence |
| `profile.ts` | 3723–3878 | AS.7.7 profile/account settings + audit log + user prefs |
| `ops.ts` | 3879–3988 | L1-04 ops summary |
| `monitors.ts` | 3989–4088 | R2 semantic-entropy + O9 orchestration observability |
| `runs.ts` | 4089–4184 | Workflow runs + project runs |
| `intent_repos.ts` | 4185–4337 | Intent parser + repo ingest + DAG authoring + turn.complete |
| `npi.ts` | 4502–4548 | NPI lifecycle |
| `notifications_invoke.ts` | 4549–4736 | Token budget + notifications + invoke (Singularity Sync) |
| `decisions.ts` | 4737–4865 | Phase 47 autonomous decision engine |
| `pep_chatops.ts` | 4866–5042 | R0 PEP gateway + R1 ChatOps |
| `pipeline.ts` | 5043–5160 | Phase 50A timeline + 50B decision rules editor |
| `report_keys.ts` | 5161–5249 | Project report + API keys |
| `bootstrap_wizard.ts` | 5250–6121 | L1-L7 bootstrap wizard (admin password + ollama probe + LLM provisioning + tenant init + service health + smoke + cf-tunnel + vertical) |
| `installer.ts` | 6122–6659 | BS.7.1 installer + BS.8.x catalog |
| `web_sandbox_misc.ts` | 7026–7223 | W14 web sandbox + N3 OpenAPI tripwire + Z.7.7 LLM live integration |

**Risk:** the global SSE manager (`subscribeEvents`, internal `EventSource` cache, retry/backoff state) is the single most cross-cutting piece of state in the file. It **must not duplicate** when split — exactly one module (`_sse.ts`) owns the singleton; every other group that emits SSE-derived types imports the type-only declaration from `_sse.ts`. The `SSEEvent` discriminated union also carries domain types from many sub-modules (e.g. `OperationMode`, `BudgetStrategyId`, `OrchestrationQueueSnapshot`); these must move with the *event* declaration to `_sse.ts`, not back to the domain module — the union is the single source of truth.

**Estimate:** 4 days. Mostly mechanical, but the type-import surgery in `_sse.ts` is the slow part. Test rewire is light because most tests import the *re-export* surface (`@/lib/api`), not specific paths.

---

### G2. `components/omnisight/integration-settings.tsx` (4 680 LOC)

**Why second among frontend:** 6 test importers; the dialog already has clear thematic sub-components (`SettingsSection`, `TenantSecretsSection`, `AccountManagerSection`, `LLMCredentialManagerSection`, `StorageQuotaSection`, `CircuitBreakerSection`, `NetworkEgressSection`, `GerritSetupWizardDialog`, `JiraWebhookSecretRotateDialog`).

**Target package:** `components/omnisight/integration-settings/`

| Submodule | Rough lines (current) | Content |
|-----------|----------------------:|---------|
| `index.tsx` | n/a | Re-exports `IntegrationSettings`, `SettingsButton`, `IntegrationSettingsProps` |
| `_chrome.tsx` | 1–211 | `IntegrationSettingsProps`, `STATUS_ICON`, `SettingsSection`, `SettingField`, `ToggleField`, shared types (`TestResult`, `TabConnectionStatus`, etc.) |
| `tenant-secrets.tsx` | 212–338 | `TenantSecretsSection` + `SECRET_TYPES` enum |
| `account-manager.tsx` | 339–893 | `AccountManagerSection`, `PLATFORMS`, `NewAccountForm`, `parsePatterns`, `platformMeta` |
| `llm-credentials.tsx` | 894–1350 | `LLMCredentialManagerSection`, `LLM_PROVIDERS_META`, `NewLlmCredentialForm`, `llmProviderMeta` |
| `storage-quota.tsx` | 1351–1513 | `StorageQuotaSection`, `formatBytes` |
| `circuit-breaker.tsx` | 1514–1651 | `CircuitBreakerSection` |
| `network-egress.tsx` | 1652–1923 | `NetworkEgressSection` |
| `gerrit-setup-wizard.tsx` | 1924–3291 | `GerritSetupWizardDialog`, `GERRIT_DEFAULT_SSH_PORT` |
| `_tabs.tsx` | 3292–3407 | `TabStatusBadge`, `TAB_STATUS_CONFIG`, `TAB_INTEGRATIONS` |
| `jira-webhook-rotate.tsx` | 3408–3647 | `JiraWebhookSecretRotateDialog` |
| `_root.tsx` | 3648–4669 | `IntegrationSettings` main component (the orchestrator that mounts all sections) |
| `settings-button.tsx` | 4670–4680 | `SettingsButton` |

**Risk:** the test files use highly specific selectors (e.g. `screen.getByTestId("integration-settings-llm-credential-form")`); splitting must preserve every `data-testid`, `role`, and visible text exactly. A drift guard (§6.5) snapshots all `data-testid` attributes in the dialog tree pre-split and asserts post-split parity. The Gerrit wizard `useState` chain is dense (~30 fields across multi-step flow); moving it to its own file is the riskiest piece — the `useEffect` cleanup ordering must remain identical (React 19 strict-mode double-mount + this dialog already had FX.9 deploy issues).

**Estimate:** 3 days.

---

### G3. `app/bootstrap/page.tsx` (4 215 LOC)

**Why third among frontend:** 2 test importers; each step component is self-contained (`AdminPasswordStep`, `LlmProviderStep`, `InitTenantStep`, `CfTunnelStep`, `GitForgeStep`, `VerticalSetupStep`, `ServiceHealthStep`, `SmokeSubsetStep`). The page's job is exactly "render the right step by `currentStep`"; splitting is natural.

**Target package:** `app/bootstrap/` (the file becomes a thin shell, sub-steps move to `app/bootstrap/_steps/`).

| Submodule | Rough lines (current) | Content |
|-----------|----------------------:|---------|
| `page.tsx` | shell | `BootstrapPage` default export — step orchestrator + global state |
| `_steps/_types.ts` | 103–218 | `StepId`, `StepDef`, `STEPS` constant |
| `_steps/_chrome.tsx` | 219–311 | `StepPill` + step-pill list rendering |
| `_steps/admin-password.tsx` | 274–557 | `AdminPasswordErrorBanner`, `AdminPasswordStep` |
| `_steps/llm-provider.tsx` | 558–1133 | `LlmProviderStep`, `LLM_PROVIDERS`, `OllamaDetectPanel`, `ProvisionErrorBanner`, `OLLAMA_KIND_HINTS`, `OLLAMA_DEFAULT_BASE_URL`, `LlmProviderId`, `LlmProviderOption` |
| `_steps/init-tenant.tsx` | 1134–1541 | `InitTenantErrorBanner`, `InitTenantStep`, `_slugifyPreview` |
| `_steps/cf-tunnel.tsx` | 1542–1718 | `CfTunnelStep` |
| `_steps/git-forge.tsx` | 1719–2449 | `GitForgeStep`, `GitHubTokenForm`, `GitLabTokenForm`, `GerritSshForm`, `GIT_FORGE_TABS`, `GitForgeTab`, `DEFAULT_GERRIT_SSH_PORT` |
| `_steps/vertical-setup.tsx` | 2450–2683 | `VerticalSetupStep` |
| `_steps/service-health.tsx` | 2684–3286 | `HealthRow`, `HEALTH_ROWS`, `HealthRowItem`, `StartServicesErrorBanner`, `_startServicesOkCopy`, `StartServicesPanel`, `ServiceHealthStep`, `SERVICE_HEALTH_POLL_MS`, `HEALTH_ROW_RED_STRIKES`, `HEALTH_XHR_ERROR_STRIKES` |
| `_steps/smoke-subset.tsx` | 3287–3779 | `SMOKE_JUMP_BACK_STEPS`, `_diagnoseSmokeFailure`, `SmokeSubsetStep` |
| `_steps/_placeholder.tsx` | 3780–3859 | `StepBodyPlaceholder` |

**Risk:** the `BootstrapPage` shell holds the cross-step state (`stepStatuses`, `currentStep`, persistence to `localStorage`, deep links via search params). Sub-steps receive props; they must not import the shell directly (would create a cycle). A `_steps/_state.ts` may emerge during the wave PR if the shell-state is too large to keep inside `page.tsx` — defer that decision until W3-front. Also: `app/bootstrap/_steps/` directory naming follows Next.js convention (underscore prefix → not a route), but `next-intl`'s App Router integration sometimes scans directories; verify with `npm run build` that no spurious 404 routes appear.

**Estimate:** 2.5 days.

---

### G4-G9. Backend Python (already in FX.7.3)

These 6 files appear in §1's frozen list because they are still in the top-9 by LOC. Their split design is in `docs/design/fx-7-3-large-file-module-split.md` §4 (F2-F8 by FX.7.3 numbering). FX.9.10 makes **no design changes** to those entries — the wave schedule (FX.7.3 §7) and the splitting rules (FX.7.3 §3) apply unchanged. Cross-reference table:

| FX.9.10 row | FX.7.3 entry | Wave | Target date |
|-------------|--------------|------|-------------|
| G4 | F4 (`tenant_projects`) | W4 | 2026-05-22 → 25 |
| G5 | F3 (`db.py`) | W3 | 2026-05-12 → 14 |
| G6 | F5 (`bootstrap.py` router) | W5 | 2026-05-26 → 29 |
| G7 | F2 (`depth_sensing`) | W2 | 2026-05-09 → 11 |
| G8 | F8 (`invoke.py`) | W8 | 2026-06-16 → 19 |
| G9 | F6 (`system.py`) | W6 | 2026-05-30 → 06-03 |

If a wave PR for any of G4-G9 wants to revise FX.7.3 §4 design, the revision lands as an FX.7.3 amendment commit, not as a FX.9.10 update.

## 5. Risk × value × dependency ordering for the frontend waves

| Wave | File | External importers | LOC | Internal coupling | Risk | Value | Net priority |
|------|------|-------------------:|----:|-------------------|-----:|------:|-------------:|
| FW1 | G1 `lib/api.ts` | **151** prod + 50+ tests | 7 223 | medium (SSE singleton + 200-variant union) | **5** | **6** | 1 |
| FW2 | G3 `app/bootstrap/page.tsx` | 2 tests + 1 prod | 4 215 | low (each step is self-contained) | **3** | 4 | 2 |
| FW3 | G2 `components/omnisight/integration-settings.tsx` | 6 tests + 4 prod | 4 680 | medium (Gerrit wizard state, dialog mount lifecycle) | **4** | 4 | 3 |

Risk × value rationale (parallel to FX.7.3 §5):

- **Risk** = importers + state-coupling (SSE singleton / dialog state / step state machine).
- **Value** = (a) review-blast-radius reduction (G1 is the highest-conflict file in the repo), (b) bundle-size visibility (G1 split lets per-section size land in CI artifacts), (c) future feature velocity.
- FW1 is highest risk-and-value; the value justifies leading with it despite the importer count, because **every** frontend feature pays the merge-conflict tax on `lib/api.ts` until it is split. The 151-importer surgery is mechanical (re-export shim absorbs it) and the SSE singleton is the only genuinely state-coupled piece.

Within the calendar, FW1-FW3 interleave with FX.7.3's Python waves — see §7.

## 6. Drift guards

These tests are **prerequisites** for any FW1-FW3 PR being mergeable. They land as a single FX.9.10-prep commit before FW1.

### 6.1 `test/lib/large-file-drift-guard.test.ts` (frontend) + extend `backend/tests/test_large_file_drift_guard.py` (backend)

The Python guard already specified by FX.7.3 §6.1 covers `backend/**/*.py`. FX.9.10 ships the TS counterpart:

```ts
// Pseudocode — actual test file lands with FW1-prep commit.
const ALLOWED_FROZEN: Record<string, number> = {
  "lib/api.ts": 7223,
  "components/omnisight/integration-settings.tsx": 4680,
  "app/bootstrap/page.tsx": 4215,
  // generated file — exempt by name, never appears in cap check
  // "lib/generated/api-types.ts" — handled by §6.3 not this guard
};
const HARD_CAP = 2000;

test("no new TS/TSX files over 2000 LOC", () => {
  for (const path of glob("**/*.{ts,tsx}", { ignore: [...node_modules, .next, dist, lib/generated/**] })) {
    const loc = countLoc(path);
    if (loc > HARD_CAP) {
      expect(ALLOWED_FROZEN).toHaveProperty(path);
    }
  }
});

test("frozen TS/TSX files only shrink", () => {
  for (const [path, baseline] of Object.entries(ALLOWED_FROZEN)) {
    if (existsSync(path)) {
      expect(countLoc(path)).toBeLessThanOrEqual(baseline);
    }
  }
});
```

Same "only shrink" axis as FX.7.3 §6.1: post-freeze a file already over the cap can't *grow* without an ADR amendment.

### 6.2 `test/lib/api-public-surface-drift-guard.test.ts`

```ts
const EXPECTED = new Set([
  "subscribeEvents", "ApiError", "onApiError", "listAgents", "createAgent",
  /* ... all 589 exports listed here, one per line, sorted */
]);

test("lib/api public surface is preserved", async () => {
  const api = await import("@/lib/api");
  const surface = new Set(Object.keys(api).filter(k => !k.startsWith("_")));
  const missing = [...EXPECTED].filter(s => !surface.has(s));
  expect(missing).toEqual([]);
});
```

The expected list lands as a separate file (`test/lib/api-baseline-surface.txt`) regenerated only by an explicit `npm run gen:api-surface-baseline` script — never by hand.

### 6.3 `test/lib/api-types-generator-drift-guard.test.ts`

Asserts `lib/generated/api-types.ts` (a) starts with the auto-generation header (`// AUTO-GENERATED — do not edit. Run \`npm run gen:openapi\` to regenerate.`), (b) parses cleanly under `tsc --noEmit`, (c) the file's LOC count round-trips through the generator (run generator into `/tmp`, diff against committed file). If a maintainer hand-edits the file, this guard fires — the hand-edit must be re-run through the generator instead.

### 6.4 Bundle-size drift guard (informational, not blocking)

After FW1, the `next build` output's largest chunk size becomes a baseline. A nightly job records `du -sh .next/static/chunks/*` and posts to a `bundle-size:` issue if the largest chunk grows >10 % between consecutive nightly runs. **Does not block PR merge** (per FX.7.3 §6.4 nightly-cycle-check precedent) — opens a follow-up issue.

### 6.5 `data-testid` drift guard for FW3 (G2 dialog)

```ts
const EXPECTED_TESTIDS = [/* snapshot of all data-testid in mounted IntegrationSettings */];

test("integration-settings testid surface is preserved", async () => {
  render(<IntegrationSettings open onClose={() => {}} />);
  const ids = Array.from(document.querySelectorAll("[data-testid]")).map(e => e.getAttribute("data-testid"));
  for (const id of EXPECTED_TESTIDS) {
    expect(ids).toContain(id);
  }
});
```

Wave-FW3 PR ships the snapshot at the same commit as the split. After landing, the snapshot is read-only — adding new test ids requires an explicit refresh PR.

### 6.6 Inherits FX.7.3 §6.4 (nightly cyclic-import detector) for backend; TS counterpart deferred

`pylint --enable=cyclic-import backend/` from FX.7.3 §6.4 catches Python cycles. TypeScript has built-in cycle-tolerance (ESM) and `tsc` will not error on cycles, so the equivalent check needs a separate tool (`madge --circular`). FX.9.10 does **not** add the TS cycle guard today — flagged as deferred follow-up. Reasoning: G1 already has zero cycles by inspection (it is a leaf module that everything imports, but nothing in it imports back); G2 and G3 are page/component leaves; the cycle-introduction risk during FW1-FW3 is theoretical, not observed.

## 7. Schedule

Front-end waves interleave with FX.7.3's Python waves so neither stack monopolises the test infra (vitest + pytest run on the same CI host). Total calendar overhead added by FX.9.10 over FX.7.3: ~10 calendar days (frontend waves are shorter than backend per §5 risk distribution).

| Wave | Date (target) | File | Owner | Notes |
|------|---------------|------|-------|-------|
| FW0 (prep) | 2026-05-05 → 2026-05-06 | TS drift guards (§6.1-6.3, §6.5) | Claude | Ships in same prep block as FX.7.3 W0; one combined prep PR. |
| FW1 | 2026-05-15 → 2026-05-19 | G1 `lib/api.ts` | Claude | Lands during FX.7.3's "Pause week 1" — frontend doesn't compete with W1-W3 backend reviews. |
| **FX.7.3 W4-W6 run 2026-05-22 → 06-03** | — | (backend) | — | FW2/FW3 paused while backend router waves are reviewed. |
| FW2 | 2026-06-04 → 2026-06-08 | G3 `app/bootstrap/page.tsx` | Claude | Lands during FX.7.3's "Pause week 2". |
| FW3 | 2026-06-09 → 2026-06-13 | G2 `components/omnisight/integration-settings.tsx` | Claude | Pause-week 2 tail. |
| **FX.7.3 W7-W9 run 2026-06-11 → 06-24** | — | (backend) | — | Light overlap with FW3 tail; if review bandwidth tight, slip FW3 to 2026-06-25. |
| **Combined pause + shim removal** | 2026-06-25 → 2026-07-08 | Drop FW1/FW2/FW3 shims | Claude | Same window as FX.7.3 §7 final pause. Per §3.1, shims live ≥2 weeks post-split; FW1 shim is eligible to drop 2026-06-02. |

**Total calendar:** combined FX.7.3 + FX.9.10 schedule ends 2026-07-08, roughly 9 calendar weeks from 2026-05-05 prep, ~5 weeks of effort.

**Pause-week semantics** (same as FX.7.3 §7): the pause is *not* idle time. Other FX rows advance during pauses; the next wave rolls forward only after the previous wave's `[D]` (deployed) status is observation-clean.

**Cancellation criteria:** if any FW PR opens >1 production-affecting bug in the first 24 h post-deploy, halt this ADR's schedule, file a regression row, and **do not start the next wave** until the regression is closed and a retro is appended.

## 8. What FX.9.10 itself completes today

FX.9.10 is **planning**, per its TODO text. Today's row closes when:

- [x] this ADR exists and is reachable via `docs/design/README.md`,
- [x] the 9 files are frozen (the LOC table in §1; 3 net-new + 6 cross-referenced from FX.7.3),
- [x] the 3 frontend wave designs are pinned (§4 G1-G3),
- [x] the wave schedule is interleaved with FX.7.3 (§7),
- [ ] the drift guards (§6) are *specified* but not yet shipped — they ship as part of FW0 on 2026-05-05 alongside FX.7.3 W0.

The execution waves (FW0-FW3) become **new TODO rows** under the same future Priority `MS` (Module Split) sub-epic that FX.7.3 §8 created. FX.9.10's rows are tagged `MS-FW1` / `MS-FW2` / `MS-FW3` to distinguish from FX.7.3's `MS-W1` … `MS-W9`. FX.9.10 itself stays closed.

## 9. Open questions / non-decisions

These are flagged here so the FW0 author has them queued.

1. **Should `lib/api.ts` go further than the 33-submodule cut?** Per-route grouping (one file per `/api/v1/<route>`) would be ~70 sub-modules — too granular, slows IDE go-to-definition. Per-thematic-domain (the existing `// ─── ... ───` headers) is the proposed cut. **Decision deferred to FW1** — open the file, look at how the test suite imports, decide then.
2. **Should the frontend split also carve out a `lib/api/_types.ts`?** All TS interfaces (`ApiAgent`, `ApiTask`, `HostMetricsTickSample` ...) currently live next to their use site. Centralising them is tempting but contradicts §3.1's "no `export *`" — and the App Router's "use server" directive interacts oddly with type-only files. **Decision deferred to FW1.**
3. **Should `next-intl`-aware routes (`app/[locale]/...`) ever land?** FX.9.9 explicitly chose cookie-driven locale and avoided URL-segment routing. If the SEO row (deferred) opens, `app/bootstrap/page.tsx` would move to `app/[locale]/bootstrap/page.tsx`, which complicates the §4 G3 split. **No decision today** — FW2 author should check whether the SEO row is still deferred at FW2 start.
4. **Should the Storybook (currently absent from the repo) story files snapshot the dialog tree pre-split?** Adding Storybook is out of scope; the §6.5 testid guard is the pragmatic substitute.

## 10. References

- `docs/design/fx-7-3-large-file-module-split.md` — companion ADR for the 9 backend Python files.
- `docs/audit/2026-05-03-deep-audit.md` §DT14-DT18 — original audit row that motivated FX.7.3 (Python-only).
- `docs/sop/implement_phase_step.md` §1 — module-global state rule (cited in §3.6 SOP fingerprint grep).
- `docs/sop/implement_phase_step.md` §3 — pre-commit fingerprint grep rule.
- `HANDOFF.md` 2026-05-04 FX.9.9 — i18n landing entry; adjacent context for why frontend split matters now.
- `HANDOFF.md` 2026-05-04 FX.7.3 — companion ADR's HANDOFF entry.
- `TODO.md` Priority FX → FX.9.10 — owning row.
