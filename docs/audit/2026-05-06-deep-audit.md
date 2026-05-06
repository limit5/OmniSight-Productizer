# OmniSight Deep Audit — 2026-05-06

**Sister to**: [2026-05-03 deep audit](./2026-05-03-deep-audit.md) (8-dim, 150+ findings, all BLOCKERs cleared via Priority FX 110/110)
**Methodology**: 9 parallel Explore subagents, 1 dimension each
**Scope**: Full system after this session's 14 epic merges (~95+ commits, alembic head 0197)
**Audit lead**: Claude
**Run date**: 2026-05-06

---

## Executive summary

| Dimension | Grade | Critical findings |
|---|---|---|
| **D1 — Backend code quality** | 🟢 Clean | 0 BLOCKERs, 2 minor DEBT items |
| **D2 — Frontend code quality** | 🟢 Clean | 0 BLOCKERs, type safety smell in 1 component |
| **D3 — Database / schema** | 🟠 Amber | **7 sensitive tables NOT KS envelope encrypted** (only sessions.token migrated post-FX.11) |
| **D4 — Auth / security** | 🔴 Red | **MFA enforcement gap** + **API key expiry missing** (both SOC 2 blockers) |
| **D5 — Observability** | 🟡 Yellow | No OpenTelemetry tracing + no Sentry error aggregation |
| **D6 — Test coverage** | 🟠 Amber | **57% backend modules untested** (268/463); BP.L marker <4% adoption |
| **D7 — Docs / spec / code** | 🟡 Yellow | 1 stale HANDOFF manifest warning; ADR 0002 GitLab-primary "interim" by design |
| **D8 — Deploy / DR / governance** | 🟡 Yellow | **1 CI workflow drift**: `frontend-stale-detector.yml` still has `[main, master]` |
| **D9 — UI/UX (static)** | 🔴 Red | **Icon-only button WCAG AA click-target** + **dark mode <1% coverage** |

**Overall health: 6.5/10** (improved from 5.2/10 at 2026-05-03 — 22 BLOCKERs cleared by Priority FX, but new BLOCKERs surfaced from this session's expanded surface area + UI/UX dimension added).

**Total findings: ~80** distributed:

- **🔴 BLOCKERS: 5** — must fix before next prod ship / Phase D legal review
- **🟠 DEFECTS: 8** — functional bugs / partial implementations
- **🟡 DEBT: ~15** — will worsen without intervention
- **🟢 COSMETIC: ~50** — style / minor nits

---

## D1 — Backend code quality

**Grade: 🟢 Green**

Strong defensive practices. Zero injection vectors detected.

- **🟡 D1.1 DEBT** — `sandbox_capacity.py:1025-1040` cancels watchdog tasks but doesn't await on cancel (theoretical race)
- **🟡 D1.2 DEBT** — `notifications.py` fire-and-forget `asyncio.create_task` for external dispatch; per-task try/except mitigates but no aggregate failure metric

Module-global state audit clean: 7 globals identified, all justified per SOP §1 acceptable-answer #1/#2/#3.

---

## D2 — Frontend code quality

**Grade: 🟢 Green**

React 19 + Next.js 16 ready. Build-time TS check enforced.

- **🟡 D2.1 DEBT** — `components/omnisight/spec-template-editor.tsx` has 7 `as any` instances (field-merging without discriminated union); refactor to typed field union
- **🟡 D2.2 DEBT** — `hooks/use-workspace-persistence.ts` 3× `as unknown` (envelope casting); should add type guard
- **🟢 D2.3 COSMETIC** — `first-run-tour.tsx:193` `.then()` chain lacks error boundary on `getUserPreference()` reload

---

## D3 — Database / schema

**Grade: 🟠 Amber**

Alembic chain integrity ✓, RLS app-layer ✓, FK consistency ✓, pgvector ✓.

- **🟠 D3.1 DEFECT** — **7 sensitive tables NOT KS envelope encrypted** (only Fernet via `backend.secret_store` legacy):
  - `api_keys.key` (PAT plaintext)
  - `oauth_tokens.{access_token, refresh_token}` (alembic 0057)
  - `tenant_secrets.encrypted_value` (alembic 0013)
  - `git_accounts.encrypted_{token, ssh_key, webhook_secret}` (alembic 0027)
  - `llm_credentials.encrypted_value` (alembic 0029)
  - `provisioned_databases.connection_url_enc` (alembic 0061)
  - `provisioned_storage` (alembic 0062, no encryption column at all)
  
  KS envelope landed (FX.11 / 0189 sessions backfill) but only `sessions.token` migrated. Need batch-migrate matching the 0189 pattern.

  → **Repair: FX2.D3.1 — KS envelope migration sweep for 7 tables (alembic + backfill, ~3 weeks effort)**

---

## D4 — Auth / security

**Grade: 🔴 Red**

Mostly solid (Q.1 peer rotation ✓, FX.11 sessions.token KS envelope ✓, K7 lockout ✓, KS.1.7 hash chain ✓), but 3 high-impact gaps.

- **🔴 D4.1 BLOCKER** — **MFA enforcement gap**: `OMNISIGHT_REQUIRE_MFA` defaults `false`. Login flow's `require_mfa_for_user()` defined but NOT called in `routers/auth.py:424-465` — only `has_verified_mfa` short-circuits. Admins / operators with role requiring MFA can skip enrollment indefinitely. **SOC 2 Type II audit-blocker**.
  
  → **Repair: FX2.D4.1 — wire `require_mfa_for_user()` into login flow (~1 day)**

- **🔴 D4.2 BLOCKER** — **API key expiry missing**: `api_keys` table has no `expires_at` field; `ApiKey` dataclass + `create_key()` don't accept TTL. PATs live indefinitely. SOC 2 control gap.
  
  → **Repair: FX2.D4.2 — alembic migration adding `expires_at` column + create_key() TTL parameter + validate_bearer() expiry check (~2 days)**

- **🟠 D4.3 DEFECT** — **New-device session not rotated**: Q.2 sends new-device alert but doesn't revoke / shorten session TTL. Attacker on new device gets full 8-hour session immediately upon login.
  
  → **Repair: FX2.D4.3 — Q.2 hook auto-shortens session TTL to 1h on new-device fingerprint, force re-auth thereafter (~1 day)**

- **🟡 D4.4 DEBT** — `OMNISIGHT_DECISION_BEARER` legacy env var fallback in `_has_valid_bearer_token()` line 328; deprecated pattern, sunset in next major

- **🟡 D4.5 DEBT** — Webhook secrets default to empty string + 503 at runtime if missing; should fail-fast at app startup

---

## D5 — Observability

**Grade: 🟡 Yellow**

Logging universal + clean, metrics avoid cardinality bombs, audit chains cryptographically sound, SSE scope strictly enforced, ZZ series complete.

- **🟡 D5.1 DEBT** — **No OpenTelemetry / W3C trace context**. LLM turns have semantic SSE event IDs but inter-service requests lack distributed tracing spans
- **🟡 D5.2 DEBT** — **No backend error aggregation** (Sentry / DataDog). Errors visible in logs + counter metrics but no central dashboard / alerting

→ **Repair: FX2.D5 — add OpenTelemetry exporter (Phase 5+ when prod env stable; not BLOCKER)**

---

## D6 — Test coverage

**Grade: 🟠 Amber**

Strong fixture quality (savepoint isolation, PG pool guards), 12 Playwright e2e files, 23k+ test functions. But:

- **🟠 D6.1 DEFECT** — **57% backend modules untested** (268/463 lack `test_<module>.py`). Highest-risk untested: `auth.py`, `bootstrap.py`, `db_pool.py`, `db_context.py`, `tenant_secrets.py` (latter 5 are `pytest.ini §8.1 95% gate` modules).
  
  → **Repair: FX2.D6.1 — write test_<module>.py for the 5 critical untested modules first (~1 week, codex-suitable)**

- **🟠 D6.2 DEFECT** — **BP.L marker coverage <4%** (722 / 23,703 marked). Marker tier system shipped but adoption pending.
  
  → **Repair: FX2.D6.2 — sweep mark existing tests via conftest auto-categorisation (~3 days, codex-suitable)**

- **🟡 D6.3 DEBT** — 159 runtime `pytest.skip()` calls (env-gate noise). Some legitimate (Docker / Node twin not present in CI) but 159 is a lot to audit.

---

## D7 — Docs / spec / code alignment

**Grade: 🟡 Yellow**

Generally well-aligned. ADR 0001-0006 cross-link consistently. Sample env vars + alembic numbers match code. No dead docs.

- **🟡 D7.1 DEBT** — `docs/status/handoff_status.yaml` has 1 normalisation warning at HANDOFF.md:4899 ("planning + audit doc landed" — pre-schema legacy entry). Non-blocking, tidy on next batch edit.

- **Note (not a finding)**: ADR 0002 says GitLab-primary in Phase 2 but origin still GitHub — this is **by design**, Phase 1 just completed, Phase 2 entry-gate observation window runs to 2026-05-12.

---

## D8 — Deploy / DR / governance

**Grade: 🟡 Yellow**

Production-grade deploy infrastructure: GPG release-signer ✓, FX.7.9 ref allowlist ✓, FX.9.7 backup pipeline + S3 immutable + DLP gate ✓, DR runbook 36KB ✓, drift guards ✓, alembic 0197 in TABLES_IN_ORDER ✓.

- **🔴 D8.1 BLOCKER** — **`frontend-stale-detector.yml` still triggers on `[main, master]`**. Phase 1 sweep step 1.4 missed this one workflow. Trivial fix but breaks Phase 1 step 1.6 validation contract (CI runs on main only post-rename).
  
  → **Repair: FX2.D8.1 — `sed -i` flip + commit (~5 min, Tier S)**

---

## D9 — UI/UX (static portion)

**Grade: 🔴 Red**

(Visual portion deferred — operator screenshots needed for full audit per Plan v2.1.)

- **🔴 D9.1 BLOCKER** — **Icon-only button WCAG AA click-target violation** in `components/omnisight/workspace-chat.tsx`. "Attach image" + "Send message" use `<Button size="icon">` without `min-h-[44px]` enforcement. Default Button likely 32-40px = below AA 44×44 minimum.
  
  → **Repair: FX2.D9.1 — Button `size="icon"` variant adds min-h-[44px]/min-w-[44px] (~2 hours)**

- **🔴 D9.2 BLOCKER** — **Dark mode <1% coverage** (31 `dark:` classes / 3,405 color properties). Light-mode-only styling — when user toggles dark mode, most surfaces unstyled. Accessibility + brand consistency failure.
  
  → **Repair: FX2.D9.2 — dark mode sweep on 50+ omnisight components (~2 weeks, codex+human-review). Could scope to Phase 2-3 timeline.**

- **🟠 D9.3 DEFECT** — **ARIA label coverage 47%** (296 aria-label / ~625 interactive elements). High-traffic chat OK; admin/settings surfaces gap.
  
  → **Repair: FX2.D9.3 — eslint jsx-a11y `aria-label` rule from warn → error + sweep (~1 week)**

- **🟠 D9.4 DEFECT** — **Focus-visible 6% coverage** (8 uses across 130 omnisight components). Insufficient keyboard navigation visual feedback.

- **🟡 D9.5 DEBT** — `.toLocaleString()` non-locale-aware in admin/tenants page; should use `Intl.DateTimeFormat`

- **🟢 D9.6 i18n PASS** — 4-locale parity (en/zh-CN/zh-TW/ja) verified, no hardcoded strings detected

---

## Findings rollup

| Severity | Count | Distribution |
|---|---|---|
| 🔴 **BLOCKER** | **5** | D4 (×2) + D8 (×1) + D9 (×2) |
| 🟠 **DEFECT** | **8** | D3 (×1) + D4 (×1) + D6 (×2) + D9 (×2) + D2/D5/D7 (×0 each, all promoted to DEBT) |
| 🟡 **DEBT** | **~15** | spread across all dimensions |
| 🟢 **COSMETIC** | **~50** | mostly D1 + D9.5 + D2 minor |

---

# Priority FX2 — Repair plan

Based on findings + ADR 0005 Tier system. FX2 is sister to original FX (which cleared 110/110 BLOCKERs on 2026-05-03).

## FX2.W1 — BLOCKERs (must clear before next prod ship)

5 items, ~1.5 weeks, mix of Tier S/M/L:

| ID | Tier | Effort | Description |
|---|---|---|---|
| FX2.D8.1 | S | 5 min | `frontend-stale-detector.yml` `[main, master]` → `[main]` (pure CI sweep) |
| FX2.D9.1 | M | 2 h | Button `size="icon"` adds min-h/min-w-[44px] |
| FX2.D4.1 | L | 1 day | Wire `require_mfa_for_user()` into login flow + tests |
| FX2.D4.2 | L | 2 days | API key `expires_at` column + TTL flow + validate_bearer expiry check |
| FX2.D9.2 | L | 2 weeks | Dark mode sweep across 50+ components |

## FX2.W2 — High-impact DEFECTs (clear before SOC 2 prep / Phase 2 stabilisation)

8 items, ~3 weeks:

| ID | Tier | Effort | Description |
|---|---|---|---|
| FX2.D3.1 | L | 3 weeks | KS envelope migration for 7 tables (api_keys, oauth_tokens, tenant_secrets, git_accounts, llm_credentials, provisioned_databases, provisioned_storage) |
| FX2.D4.3 | M | 1 day | Q.2 new-device session TTL shortening |
| FX2.D6.1 | M | 1 week | test_<module>.py for 5 critical untested modules (auth/bootstrap/db_pool/db_context/tenant_secrets) |
| FX2.D6.2 | M | 3 days | BP.L marker auto-categorisation sweep |
| FX2.D9.3 | M | 1 week | ARIA label sweep + jsx-a11y rule promotion warn→error |
| FX2.D9.4 | S | 2 days | Focus-visible class addition across all interactive components |
| FX2.D7.1 | S | 30 min | Cleanup HANDOFF.md:4899 legacy status entry |
| FX2.D2.1 | S | 1 day | spec-template-editor type union refactor |

## FX2.W3 — DEBT (rolling, no hard deadline)

~15 items including OpenTelemetry / Sentry / sandbox_capacity await-cancel / notifications dispatch metric / etc. — track for v0.5.0 / v1.0.0 milestones.

## FX2.W4 — COSMETIC (passes, low priority)

~50 items, codex-bulk-cleanup-friendly, do as opportunity arises.

---

## Codex assignment matrix

| FX2 item | Tier | agent_class | Why |
|---|---|---|---|
| FX2.D8.1 | S | subscription-codex | trivial sed edit |
| FX2.D9.1 | M | subscription-codex | well-scoped React component change |
| FX2.D4.1 | L | api-anthropic | login flow critical, need 1M context for full review |
| FX2.D4.2 | L | api-anthropic | alembic + dataclass + validator + tests, large blast radius |
| FX2.D9.2 | L | subscription-codex × N | 50 components batch parallelisable |
| FX2.D3.1 | L | api-anthropic | 7-table envelope migration too risky for subscription-tier |
| FX2.D6.1 | M | subscription-codex | testing pattern well-known |
| FX2.D6.2 | M | subscription-codex | sweep with conftest auto-categorisation |
| FX2.D9.3 | M | subscription-codex | sweep work |
| FX2.D4.3 | M | subscription-codex | scoped Q.2 hook change |

→ subscription-codex can handle 7/10; api-anthropic recommended for the 3 highest-blast-radius (D4.1, D4.2, D3.1).

---

## Phase D / SOC 2 alignment

This audit's FX2.W1 BLOCKERs map directly to Phase D (Commercial Launch) prerequisites:

| FX2 BLOCKER | SOC 2 control | ISO 27001 clause |
|---|---|---|
| D4.1 MFA enforcement | CC6.1 — Logical access | A.9.4 |
| D4.2 API key expiry | CC6.2 — User access mgmt | A.9.2 |
| D9.1 + D9.2 + D9.3 a11y | n/a directly but customer review | n/a |
| D8.1 CI gate consistency | CC8.1 — Change management | A.14.2 |

**Implication**: clearing FX2.W1 (~1.5 weeks) opens Phase D prep window. FX2.W2 KS envelope migration (D3.1) is the longest pole at ~3 weeks — should start now in parallel.

---

## Rollout cadence proposal

```
Week 1 (now → 2026-05-13):
  ├── FX2.D8.1 (Mon, 5 min)
  ├── FX2.D9.1 (Mon, 2 hr)
  ├── FX2.D7.1 (Mon, 30 min)
  ├── FX2.D2.1 (Tue, 1 day)
  ├── FX2.D9.4 (Wed, 2 days)
  └── FX2.D4.3 (Thu, 1 day)
  → Most W1 BLOCKERs + most-cheap W2 done by Friday.

Week 2-3 (2026-05-13 → 2026-05-27):
  ├── FX2.D4.1 (api-anthropic, 1 day → 3 days incl review)
  ├── FX2.D4.2 (api-anthropic, 2 days → 5 days incl review)
  ├── FX2.D6.1 (5 critical test modules, 1 week)
  └── FX2.D6.2 (BP.L marker sweep, 3 days)
  → SOC 2 BLOCKERs cleared by Phase 2 entry day.

Week 3-5 (2026-05-27 → 2026-06-10):
  ├── FX2.D3.1 (KS envelope migration sweep, 7 tables, 3 weeks; api-anthropic)
  ├── FX2.D9.2 (dark mode sweep, 2 weeks; subscription-codex × 10 batches)
  └── FX2.D9.3 (ARIA sweep, 1 week)
  → Phase D legal-review-readiness.
```

Hard milestone: **2026-06-02 v0.4.0 cut** (per governance migration plan) — FX2.W1 must be 100% done by then.

---

## Phase 2 D9 visual portion (deferred — operator screenshots needed)

When operator has time:
1. Run dev server (`pnpm dev`)
2. Walk through 5 flows: login → dashboard → orchestrator → settings (MFA enrollment) → admin
3. Screenshot each main page (light + dark mode)
4. Send to me — I correlate against code findings + add visual-only findings

Estimated 30 min operator time + 1 hr Claude correlation. Adds maybe 5-10 visual-only findings to FX2 backlog.

---

## Cross-link

- [2026-05-03 deep audit](./2026-05-03-deep-audit.md) — predecessor
- [Priority FX](../../TODO.md) — original FX series (110/110 done)
- [governance plan memory](../../README.md) — phase 0-5 timeline + Tier S/M/L/X
- ADR 0001 / 0003 / 0005 — branch / review / Tier authority
- ADR 0006 — TLS termination architecture (post-2026-05-05)
