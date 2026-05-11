# Frontend Audit Runbook

**Purpose:** reproducible procedure for running a frontend type-and-test audit in a
worktree that may not have npm dependencies installed. Originally written as the
deliverable for OP-919 (AUDIT-5, 2026-05-11) after that audit discovered two distinct
Node-version frictions that future auditors should not re-discover.

**Audience:** any agent or operator who needs to run `npx tsc --noEmit` and `npx vitest
run` against the OmniSight frontend.

---

## TL;DR

```bash
# 1. Use Node 20.17.0 for install + tsc (matches .nvmrc / engines.node floor).
# 2. Use Node 24.x (host default) for vitest (works around F-ENV-2).
# 3. Capture both logs to /tmp and grep the summary.

export PATH=$HOME/.nvm/versions/node/v20.17.0/bin:$PATH
npm install --no-audit --no-fund 2>&1 | tee /tmp/fe-audit-install.log
npx tsc --noEmit 2>&1 | tee /tmp/fe-audit-tsc.log

# Drop back to host Node (24.x) for vitest:
unset PATH; export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.nvm/versions/node/v24.14.1/bin
npx vitest run 2>&1 | tee /tmp/fe-audit-vitest.log
```

---

## Why this runbook exists

The OP-919 audit discovered three environment frictions that made the AC ("just run `npm
install` then `npx tsc` then `npx vitest`") more subtle than it looked:

1. **F-ENV-1** — transitive deps now want Node ≥20.19.0 or ≥24.0.0, but
   `package.json#engines.node = ">=20.17.0 <21"`. Install succeeds with 13
   `EBADENGINE` warnings.
2. **F-ENV-2** — under Node 20.17.0, `npx vitest run` fails immediately with
   `ERR_REQUIRE_ESM` loading `std-env/dist/index.mjs`. Node 24.x fixes this.
3. **F-ENV-3** — `test/alias-sync.test.ts` invokes esbuild under jsdom and trips
   esbuild's `TextEncoder().encode("") instanceof Uint8Array` invariant. This is a known
   esbuild ≥0.25 + jsdom interaction and is *not* a real product defect.

Future audits should not have to re-derive these. Until F-ENV-1/2 are fixed at the
`package.json` level, the dual-Node procedure below is the correct way to run the
audit.

---

## Prerequisites

| Item | Version | Where to install |
|---|---|---|
| Node 20.17.0 | exact (matches `.nvmrc`) | `nvm install 20.17.0` (any nvm ≥0.40 works) |
| Node 24.x | latest 24 (host default acceptable; tested with 24.14.1) | `nvm install 24` or system package |
| `npm` | comes with each Node | — |
| Free disk | ~1.5 GB | `node_modules/` is ~1.1 GB; vitest cache adds ~200 MB |
| Repo HEAD | clean worktree on `develop` or audit branch | `git status` |

Verify:

```bash
~/.nvm/versions/node/v20.17.0/bin/node --version    # → v20.17.0
~/.nvm/versions/node/v24.14.1/bin/node --version    # → v24.14.x
which pnpm                                          # corepack-managed pnpm 9.x
```

> **Note:** the project's canonical package manager is `pnpm@9.15.4` (declared via
> `packageManager` field). The OP-919 ticket AC explicitly told the auditor to use
> `npm install`, and that's what this runbook documents. `pnpm install --frozen-lockfile`
> also works and is what CI uses for non-audit installs.

---

## Procedure

### Step 1 — Install dependencies under Node 20.17.0

```bash
cd <repo-root>
export PATH=$HOME/.nvm/versions/node/v20.17.0/bin:$PATH
node --version       # must print v20.17.0
npm --version        # 10.8.2 ships with that Node
npm install --no-audit --no-fund 2>&1 | tee /tmp/fe-audit-install.log
```

**Expected:** 727+ packages installed in ~60 s. Up to ~15 `EBADENGINE` warnings
(F-ENV-1). Exit code 0.

**Failure modes:**

- `EBADENGINE` errors with exit code != 0 → Node version not matching `engines.node`.
  Either bump Node or run `npm install --engine-strict=false`.
- Network errors → re-run; npm retries on its own.
- `EACCES` on `node_modules/` → wipe `node_modules/` and retry; never `sudo`.

### Step 2 — TypeScript check under Node 20.17.0

```bash
npx tsc --noEmit 2>&1 | tee /tmp/fe-audit-tsc.log
echo "exit=$?"
```

**Expected:** exit 0, empty log. Strict mode is on (`strict: true` in `tsconfig.json`);
any new diagnostic should be triaged in the audit report under the same classification
scheme as OP-919:

- **real bug** → file a follow-up ticket against the owning area (frontend / backend)
- **intentional `any`** → leave in place but document
- **config issue** → bump tooling / tsconfig

If diagnostics count > 20, file ONE meta ticket grouping by file (per the
`TSCErrorsTooManyToFile` clause in OP-919).

### Step 3 — Vitest under Node 24.x

```bash
# Reset PATH to host default (Node 24.x). Adjust if your host Node lives elsewhere.
unset PATH
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.nvm/versions/node/v24.14.1/bin
node --version       # must print v24.x

npx vitest run 2>&1 | tee /tmp/fe-audit-vitest.log
echo "exit=$?"
```

**Expected:** exit 0 if the suite is green, exit 1 with a summary line otherwise. As of
2026-05-11 the develop baseline is `9 files failed / 218 passed (227) — 64 tests failed
/ 3598 passed (3662) — 10 uncaught exceptions`.

**Failure modes:**

- `Error [ERR_REQUIRE_ESM]: require() of ES Module .../std-env/dist/index.mjs` → you
  are still on Node ≤20.18. Switch to Node 24.x. This is F-ENV-2.
- `failed to load config from .../vitest.config.ts` with any other error → check the
  config file for typos before suspecting the environment.
- No tests run / `0 passed` → `--reporter=verbose` and inspect; almost always means
  the test glob is empty under the current `cwd`.

### Step 4 — Triage and write the audit report

Use the OP-919 audit report as the template:
[`docs/audit/2026-05-11-frontend-audit.md`](../audit/2026-05-11-frontend-audit.md).
Key sections to fill:

1. **Executive summary** — pass/fail counts for both checks.
2. **Per-file failure classification** — one row per failing test file, classify each
   as REAL BUG / TEST DRIFT / ENV-CONFIG.
3. **Findings detail** — per-finding section with location, root cause, fix shape,
   evidence path (e.g. `/tmp/fe-audit-vitest.log:NNNN`).
4. **Follow-up tickets** — enumerate. Note: the audit-mode JIRA helpers only expose
   `add_comment` and `transition_back_to_todo`, so an audit cannot programmatically file
   new tickets; the operator picks them up from this table.

---

## Quick-summary one-liners

After running the procedure, extract the result lines:

```bash
# tsc result:
( wc -l /tmp/fe-audit-tsc.log; echo "exit code captured in tee pipeline" )

# vitest result:
grep -E "^(Test Files|     Tests|     Errors|   Duration)" /tmp/fe-audit-vitest.log | tail -10

# failed test files:
grep -E "^ FAIL" /tmp/fe-audit-vitest.log | awk -F'>' '{print $1}' | sort -u
```

---

## Cleanup

`node_modules/` is gitignored but ~1.1 GB. Delete after the audit if disk is tight:

```bash
rm -rf node_modules .next package-lock.json
```

The `package-lock.json` that `npm install` generates does not match `pnpm-lock.yaml` and
should NOT be committed. Remove it before any commit lands.

---

## Known follow-ups blocking a "clean" audit

Tracked in the OP-919 audit report. The four highest-leverage:

| Finding | Estimated fail-count reduction |
|---|---|
| F-TEST-3 — add `createShareableObject` to LivePreviewPanel test mock | -26 |
| F-BUG-1 — guard `frontend_freshness` optional chain | -10 vitest fails + -10 uncaught |
| F-TEST-1 — re-anchor bootstrap-page testids | -7 |
| F-TEST-2 — re-anchor force-turbo button text assertions | -7 |

After F-TEST-3 alone, the develop baseline would drop from 64 → 38 fails.

---

## Change log

| Date | Author | Change |
|---|---|---|
| 2026-05-11 | claude-bot (OP-919) | Initial version. Covers F-ENV-1/2/3 frictions and the dual-Node 20.17/24.14 procedure. |
