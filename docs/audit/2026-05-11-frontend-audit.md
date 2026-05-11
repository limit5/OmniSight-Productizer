# Frontend Audit — 2026-05-11

**Spec source:** `docs/audit/2026-05-11-deep-system-audit-late.md` §P1-2
**JIRA ticket:** OP-919 (AUDIT-5)
**Auditor:** claude-bot (re-classed from codex; codex stopped until 2026-05-12 08:11 weekly reset)
**Branch:** `feature/OP-919-runner-fresh`
**Commit pinned:** `b52e0b15` (top of `develop` at audit time)

---

## Executive summary

| Check | Result |
|---|---|
| `npm install` (Node 20.17.0, npm 10.8.2) | ✓ 727 packages installed in ~1 min; 13 `EBADENGINE` warnings (transitive deps now want ≥20.19.0 or ≥24.0.0 — see Finding F-ENV-1) |
| `npx tsc --noEmit` | **✓ 0 errors** |
| `npx vitest run` | **✗ 9 file failed / 218 passed (227 files), 64 tests failed / 3598 passed (3662 tests), 10 uncaught exceptions, 20.15 s wall** |

TypeScript is clean. Vitest reveals one confirmed real bug (un-guarded property access in
`app/bootstrap/page.tsx`) plus a pile of pre-existing test/component drift that has been
carried on `develop` since at least the BS.9.5 commit (`e5634ac7`), which already noted
"55 passed / 10 failed = baseline 完全相同 10 failed pre-existing".

---

## Environment

| Item | Value |
|---|---|
| Host node (default) | v24.14.1 |
| Host npm | 11.11.0 |
| `.nvmrc` / `.node-version` | 20.17.0 |
| `package.json` engines.node | `>=20.17.0 <21` |
| `packageManager` field | `pnpm@9.15.4` |
| Lockfile present | `pnpm-lock.yaml` only (no `package-lock.json` until this audit) |
| Tooling used for audit | Node 20.17.0 (via `~/.nvm`) for `npm install` and `npx tsc`; Node 24.14.1 for `npx vitest run` (see F-ENV-2) |

The audit deliberately followed the ticket's AC verbatim — `npm install` then `npx tsc`
then `npx vitest`. That works, but reveals two environment frictions documented as
findings F-ENV-1 and F-ENV-2 below, and codified in
`docs/operations/frontend-audit-runbook.md`.

---

## Results — `npx tsc --noEmit`

Exit code: **0**. Output: **empty**. No TypeScript errors under `tsconfig.json` strict mode.

`tsconfig.json` excludes `backend/`, `scripts/`, `deploy/`, `test/`, `e2e/`, `tests/`,
`templates/`, `test_assets/`, `configs/skills/*/scaffolds`. Frontend coverage is
`next-env.d.ts`, `**/*.ts`, `**/*.tsx`, plus generated `.next/types` and `.next/dev/types`.

---

## Results — `npx vitest run`

Summary:

```
 Test Files  9 failed | 218 passed (227)
      Tests  64 failed | 3598 passed (3662)
     Errors  10 errors
   Duration  20.15s (transform 43.64s, setup 43.34s, import 89.02s, tests 163.60s, environment 238.49s)
```

### Per-file failure classification

Classification key:
- **REAL BUG** — product code defect; tracked as a follow-up ticket recommendation
- **TEST DRIFT** — component shipped but `data-testid` / textContent / mock surface diverged
- **ENV/CONFIG** — broken because of toolchain/Node/jsdom mismatch, not product code

| # | File | Failures | Root signal | Class | Follow-up |
|---|------|----------|-------------|-------|-----------|
| 1 | `test/alias-sync.test.ts` | 1 | `Invariant violation: "new TextEncoder().encode("") instanceof Uint8Array" is incorrectly false` thrown from `esbuild/lib/main.js:201` when the test asks esbuild to resolve `@/*` under jsdom | **ENV/CONFIG** | F-ENV-3 |
| 2 | `test/components/bootstrap-page.test.tsx` | 7 | `TestingLibraryElementError: Unable to find an element by: [data-testid="bootstrap-cf-tunnel-complete"]` (×1), `[data-testid="bootstrap-git-forge-step"]` (×4), `AssertionError: expected 'green' to be 'red'` (×1), `expect(element).toHaveTextContent(...)` mismatch (×1) | **TEST DRIFT** (pre-existing per BS.9.5 commit note) | F-TEST-1 |
| 3 | `test/components/bootstrap-vertical-setup-wiring.test.tsx` | 10 + 10 uncaught | All ten cases throw `TypeError: Cannot read properties of undefined (reading 'status')` at `app/bootstrap/page.tsx:210:15` inside the `frontend_freshness` STEP `isGreen` callback | **REAL BUG** — chained optional access mid-expression | F-BUG-1 |
| 4 | `test/components/h3-coordinator-transparency-flow.test.tsx` | 3 | `expected "vi.fn()" to be called 1 times, but got 0 times` (force-turbo handler not firing); `Unable to find [data-testid="ops-force-turbo-msg"]` | **TEST DRIFT** (likely same root as #6) | F-TEST-2 |
| 5 | `test/components/live-preview-panel.test.tsx` | 26 | `Error: [vitest] No "createShareableObject" export is defined on the "@/lib/api" mock` — `LivePreviewPanel` now defaults a prop to `createShareableObject` from `@/lib/api`, but the test's `vi.mock("@/lib/api", ...)` does not expose that symbol | **TEST DRIFT** — mock surface stale vs new component prop | F-TEST-3 |
| 6 | `test/components/ops-summary-panel.test.tsx` | 4 | `expected 'FORCE TURBO OVERRIDEbypass H2 auto-derate & DRF capacity-derate · OOM risk under sustained load' to contain 'Force turbo'` — button text was upper-cased + got a long sub-label glued in; downstream confirm-dialog tests then see 0 invocations | **TEST DRIFT** (case-sensitive `.toContain('Force turbo')` vs `'FORCE TURBO …'`) | F-TEST-2 |
| 7 | `test/components/session-heatmap-checkbox5.test.tsx` | 5 | `expected undefined to be defined` on cell lookups by `data-hour` / `data-dayKey`; covers log-scale, sparse cells, and operator-local-TZ paths | **TEST DRIFT** (DOM shape changed) | F-TEST-4 |
| 8 | `test/components/session-heatmap.test.tsx` | 2 | Same family — `expected undefined to be defined` on hover/tokens-by-cell lookups | **TEST DRIFT** | F-TEST-4 |
| 9 | `test/components/skill-review-panel.test.tsx` | 1 | `expected vi.fn() to be called with: [ 'ads-1', { …(4) } ]` — review-call payload diverges from expectation | **TEST DRIFT** | F-TEST-5 |

Total: **9 files / 64 tests / 10 uncaught exceptions**. Of those:
- **1 confirmed real product bug** (F-BUG-1)
- **5 test-drift clusters** (F-TEST-1 … F-TEST-5)
- **3 environment findings** (F-ENV-1 … F-ENV-3)

---

## Findings detail

### F-BUG-1 — `BootstrapPage` crashes when `status.frontend_freshness` is absent

**Severity:** P1 (component crash → ErrorBoundary required, but no boundary is installed
in test render — production users will see the same crash if the backend status payload
omits `frontend_freshness`).

**Location:** `app/bootstrap/page.tsx:209-210`

```ts
isGreen: (_g, _finalized, _localGreen, status) =>
  status?.frontend_freshness.status === "fresh",
```

**Bug:** the optional chain on `status?.` only guards the outer `status` reference. The
next access `.frontend_freshness.status` is non-optional. If `status` is defined but
`status.frontend_freshness` is missing (e.g. older backend builds, or status request
in-flight returns a partial object), the chain throws
`TypeError: Cannot read properties of undefined (reading 'status')`.

**Fix shape (not applied — audit-only ticket):**

```ts
isGreen: (_g, _finalized, _localGreen, status) =>
  status?.frontend_freshness?.status === "fresh",
```

**Evidence — uncaught exception stack:** `/tmp/op919-vitest-n24.log:3820,3844,3868,3892,3916,3940,3964,3988,4012,4036` (one stack per test in `bootstrap-vertical-setup-wiring.test.tsx`).

**Recommended follow-up ticket:** create `[OP-???]` "BS.9.5 fix — guard frontend_freshness optional chain in app/bootstrap/page.tsx". Area: frontend. Estimate: ≤1 LOC change + regression test.

---

### F-ENV-1 — Transitive deps want Node ≥20.19.0 / ≥24.0.0

`npm install` under Node 20.17.0 (the pinned `.nvmrc` / `engines.node` floor) emits 13
`EBADENGINE` warnings:

| Package | Required |
|---|---|
| `undici@7.25.0` | `>=20.18.1` |
| `html-encoding-sniffer@6.0.0` | `^20.19.0 \|\| ^22.12.0 \|\| >=24.0.0` |
| `whatwg-url@16.0.1` | `^20.19.0 \|\| ^22.12.0 \|\| >=24.0.0` |
| `@asamuzakjp/generational-cache@1.0.1` | `^20.19.0 \|\| ^22.12.0 \|\| >=24.0.0` |
| `@csstools/css-calc@3.2.0` | `>=20.19.0` |
| `@csstools/css-parser-algorithms@4.0.0` | `>=20.19.0` |
| `@csstools/css-tokenizer@4.0.0` | `>=20.19.0` |
| `@csstools/css-color-parser@4.1.0` | `>=20.19.0` |
| `@csstools/color-helpers@6.0.2` | `>=20.19.0` |
| `entities@8.0.0` | `>=20.19.0` |
| `eslint-visitor-keys@5.0.1` | `^20.19.0 \|\| ^22.13.0 \|\| >=24` |
| (others) | similar |

**Class:** ENV/CONFIG. Install completes but vitest fails to load its config under
20.17.0 (see F-ENV-2). The repo `engines.node = >=20.17.0 <21` is now stricter than the
floor the dep tree needs.

**Recommended follow-up:** bump the floor to `>=20.19.0 <21 || >=22.12.0 <23 || >=24` (or
align with the most common transitive floor `>=20.19.0`), update `.nvmrc` / `.node-version`,
and re-run CI. Area: tooling. Out of scope for this audit ticket (OP-919 area = docs +
frontend + tests).

---

### F-ENV-2 — Vitest 4.x cannot load its config under Node 20.17.0

Running `npx vitest run` under Node 20.17.0 fails before any test discovers:

```
failed to load config from /home/user/work/sora/OmniSight-claude-worktree/vitest.config.ts

Error [ERR_REQUIRE_ESM]: require() of ES Module
  /home/user/work/sora/OmniSight-claude-worktree/node_modules/std-env/dist/index.mjs
  not supported.
```

`std-env` is ESM-only; vitest's CJS bridge needs Node's newer `require(ESM)` support,
which lands in 20.19.0. Switching to Node 24.14.1 (the host default) gets vitest past
config load and yields the 9/64 failure numbers above.

**Class:** ENV/CONFIG. Same root as F-ENV-1.

**Mitigation in this audit:** vitest was run under Node 24.14.1. Documented in runbook.

---

### F-ENV-3 — `test/alias-sync.test.ts` invokes esbuild under jsdom + Node 24 and trips its TextEncoder invariant

```
Error: Invariant violation: "new TextEncoder().encode("") instanceof Uint8Array"
  is incorrectly false

This indicates that your JavaScript environment is broken. ...
 ❯ Object.<anonymous> node_modules/esbuild/lib/main.js:201:9
```

jsdom's `TextEncoder` polyfill produces a `Uint8Array` whose prototype chain is not the
same realm's `Uint8Array.prototype`, so esbuild's `instanceof` invariant check fails.
This is a known interaction between recent esbuild (≥0.25) and jsdom.

**Class:** ENV/CONFIG. The actual purpose of the test — verifying tsconfig `@/*` and
vitest `@/` resolve to the same root — is sound; the implementation just chose esbuild
as the resolver oracle.

**Recommended follow-up:** swap the esbuild-driven check for a string-compare of the two
resolved paths read directly from `tsconfig.json` and `vitest.config.ts`. Area: tests.

---

### F-TEST-1 — bootstrap-page.test.tsx (7 failures) — pre-existing test drift

| Case | Symptom |
|---|---|
| `Step 3 shows a completion card when the gate is already green` | `bootstrap-cf-tunnel-complete` testid not in DOM |
| `B14 Part A Step 3.5 Git Forge tabs > GitHub tab token probe > Save & Continue persists ...` | `bootstrap-git-forge-step` testid not rendered after `Save & Continue` |
| `... > GitLab tab token probe > Save & Continue persists gitlab_token + gitlab_url and flips complete` | same |
| `... > Gerrit tab SSH probe > Save & Continue persists gerrit_* settings and flips complete` | same |
| `... > Skip button flips the step to complete without touching finalize gates` | same |
| `L8 #3 Step 4 start-services kind-keyed error banners > dev-mode no-op: launcher reports already_running without an error banner` | `expected 'green' to be 'red'` — state machine assertion inverted |
| `Step 4 marks the failing row red and keeps polling for recovery` | `toHaveTextContent` mismatch |

The BS.9.5 landing commit (`e5634ac7`) already documented:
> "full bootstrap-page.test.tsx 65 cases → 55 passed / 10 failed = baseline 完全相同 10 failed pre-existing on master、本 row 非 regression、BS.9.6 規劃同步修正"

So most of these were known and slated for "BS.9.6". This audit confirms 7 of those
10 still fail on `develop` HEAD `b52e0b15`.

**Class:** TEST DRIFT.

**Recommended follow-up:** roll into existing BS.9.6 planning. Estimate: 7 cases × ~0.5 LOC of testid wiring each.

---

### F-TEST-2 — Force-turbo button text drift (h3 + ops-summary, 7 failures)

The `ops-force-turbo-btn` button used to render literal text `Force turbo`. It now
renders `FORCE TURBO OVERRIDE` plus a sub-label `bypass H2 auto-derate & DRF
capacity-derate · OOM risk under sustained load` concatenated into the same `textContent`.
Two test files assert against the lowercase string:

- `ops-summary-panel.test.tsx`: `expect(btn.textContent).toContain("Force turbo")` (case-sensitive)
- `h3-coordinator-transparency-flow.test.tsx`: clicks the button by testid, then expects the
  POST handler to fire once — fails because the click handler chain depends on a confirm
  dialog payload that no longer matches the new button label.

Also: `ops-force-turbo-msg` testid is no longer in the component after a successful POST.

**Class:** TEST DRIFT.

**Recommended follow-up:** one ticket covering both files. Either re-case the assertion
(`Force turbo` → `FORCE TURBO`) or apply a case-insensitive match. Re-add `ops-force-turbo-msg`
or update the test to look for whatever stable signal now exists. Area: tests + frontend.

---

### F-TEST-3 — `LivePreviewPanel` mock missing `createShareableObject` (26 failures)

Every `live-preview-panel.test.tsx` case fails at component mount with:

```
Error: [vitest] No "createShareableObject" export is defined on the "@/lib/api" mock.
Did you forget to return it from "vi.mock"?
```

The component recently grew a `createShare = createShareableObject` default-parameter
import from `@/lib/api`. The test's `vi.mock("@/lib/api", () => ({ ... }))` stub returns
a partial set that does NOT include `createShareableObject`, so destructuring at line 98
throws.

**Class:** TEST DRIFT — mock surface stale vs component prop.

**Recommended follow-up:** one ticket. Either:
1. Add `createShareableObject: vi.fn()` to the live-preview-panel mock, OR
2. Switch the test mock to the `importOriginal()` partial-mock pattern vitest suggests.

Area: tests. Estimate: ~3 LOC.

This is the single highest-leverage follow-up — fixing it unblocks 26 cases.

---

### F-TEST-4 — Session heatmap DOM probes returning undefined (7 failures)

`session-heatmap.test.tsx` and `session-heatmap-checkbox5.test.tsx` both look up cells via
`Array.from(...).find(el => el.getAttribute("data-hour") === ...)` and assert
`expect(target).toBeDefined()`. The lookups return `undefined`, meaning the cells either
no longer carry those data attributes, or render at a different `(dayKey, hour)` than the
fixture expects (likely a timezone or sparse-cell ordering change).

**Class:** TEST DRIFT.

**Recommended follow-up:** one ticket. Inspect the rendered DOM under `data-testid=
"session-heatmap"`, compare to the test fixture, and re-anchor the probes. Cover
log-scale, sparse-cell, and TZ branches. Area: tests + possibly frontend (TZ may be a real
bug — needs investigation).

---

### F-TEST-5 — `SkillReviewPanel` review-call payload diverged (1 failure)

`expected vi.fn() to be called with: [ 'ads-1', { …(4) } ]`. The first call to the mock
fired with a different shape than expected. Without a deeper dive the test could be
either a real-bug surface (panel now sends extra fields) or a test that needs updating.

**Class:** TEST DRIFT (best guess; could escalate to REAL BUG on inspection).

**Recommended follow-up:** investigate which side moved — bisect with `git log -p
components/omnisight/skill-review-panel.tsx`. Area: tests + possibly frontend.

---

## Follow-up tickets

Per the OP-919 spec, real bugs should each get their own follow-up ticket and test-drift
should get triage tickets. Because the audit-mode helpers in
`backend/agents/jira_dispatch.py` expose only `add_comment` and `transition_back_to_todo`
(no `create_issue`), this audit **enumerates** the recommended tickets rather than
programmatically filing them. Operator action: create the following tickets in OP project.

| ID | Title | Area | Severity | Closes |
|----|-------|------|----------|--------|
| FU-1 | `BS.9.5 fix — guard frontend_freshness optional chain in app/bootstrap/page.tsx` | frontend | P1 | F-BUG-1 (10 vitest fails + 10 uncaught) |
| FU-2 | `BS.9.6 — re-anchor bootstrap-page.test.tsx testids (cf-tunnel-complete, git-forge-step, services_ready states)` | tests | P2 | F-TEST-1 (7 fails) |
| FU-3 | `Re-anchor force-turbo assertions across h3 + ops-summary tests` | tests | P2 | F-TEST-2 (7 fails) |
| FU-4 | `Add createShareableObject to LivePreviewPanel test mock` | tests | P2 | F-TEST-3 (26 fails) |
| FU-5 | `Re-anchor session-heatmap cell-lookup probes (incl. TZ branch)` | tests + frontend | P2 | F-TEST-4 (7 fails) |
| FU-6 | `Investigate SkillReviewPanel review-call payload divergence` | tests + frontend | P3 | F-TEST-5 (1 fail) |
| FU-7 | `Replace esbuild-driven alias-sync test with tsconfig/vitest string compare` | tests | P3 | F-ENV-3 (1 fail) |
| FU-8 | `Bump Node floor to >=20.19.0 to match transitive deps; update .nvmrc` | tooling | P2 | F-ENV-1, F-ENV-2 |

Total expected fail-count reduction after FU-1 through FU-8 ship: **64 → 0** (if no new
regressions land in the meantime).

---

## Reproducibility

See `docs/operations/frontend-audit-runbook.md` for the exact procedure used. Raw logs
were captured at `/tmp/op919-tsc.log` (empty — no errors) and `/tmp/op919-vitest-n24.log`
(4066 lines). Logs are not committed; the runbook explains how to regenerate.

---

## Sign-off

- TypeScript: clean (✓ AC#1a satisfied).
- Vitest: pass/fail counts recorded above (✓ AC#1b satisfied).
- Triage tables completed for TS (no items) and vitest (9 files / 64 fails / 10 uncaught) (✓ AC#2 + AC#3).
- This audit report (✓ AC#4).
- Runbook written at `docs/operations/frontend-audit-runbook.md` (✓ AC#5).

Real-bug follow-up ticket enumeration provided above. Audit-mode JIRA helpers do not
include `create_issue`, so programmatic ticket filing is deferred to operator.
