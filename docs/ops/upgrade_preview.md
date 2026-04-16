# Nightly Dependency Upgrade Preview (N5)

> Purpose-built early-warning system for the weekend Renovate batch.
> Implementation lives in
> [`.github/workflows/upgrade-preview.yml`](../../.github/workflows/upgrade-preview.yml)
> and [`scripts/upgrade_preview.py`](../../scripts/upgrade_preview.py).
> Companion to the N2 Renovate policy (`docs/ops/renovate_policy.md`).

## TL;DR

Every night at **01:00 Asia/Taipei** (`0 17 * * *` UTC) GitHub Actions:

1. Installs the **committed** lockfiles, then asks pip / pnpm what is
   outdated relative to upstream.
2. Trial-runs `pip-compile --upgrade` and `pnpm update` in a scratch
   directory, capturing the diffs that Renovate would propose.
3. Installs the upgraded lockfiles in the same fresh runner image and
   runs the **full backend pytest suite + Chromium Playwright suite**
   against them.
4. Closes any prior open issue labelled `dependency-preview`, then opens
   a new issue containing: outdated tables, suspected-breaking callouts,
   diffs (truncated to 200 lines each, full version in the artifact),
   and the tail logs of the test runs.
5. Uploads the full set of artifacts (raw JSON, full diffs, full logs)
   under the workflow run's `upgrade-preview-${run_id}` artifact bundle
   for 14 days.

The workflow **never auto-merges anything** and **never mutates any
committed file in `master`**. The only persistent side effect is one
open issue at a time.

## What lands in the issue

```
# Nightly Dependency Upgrade Preview — YYYY-MM-DD
## Summary       — table of step outcomes (pip/pnpm install + pytest + playwright)
## Suspected breaking — bullet list of major bumps and 0.x minors
## pip outdated   — markdown table, 60-row cap (full list in artifact)
## pnpm outdated  — markdown table, 60-row cap
## pip-compile --upgrade diff   — first 200 lines of unified diff
## pnpm update diff             — first 200 lines of unified diff
## pytest tail   — last 80 lines of stdout
## playwright tail — last 80 lines of stdout
```

The "suspected breaking" classifier (`scripts/upgrade_preview.py`) flags
a bump as breaking when:

* the leading SemVer integer changes (`1.x → 2.x`), or
* the package is on `0.x` and the **minor** version changes (the
  pre-1.0 SemVer convention — most LangChain releases live here), or
* the package belongs to a hand-curated watchlist of strategic
  dependencies (`langchain*`, `langgraph`, `fastapi`, `pydantic`,
  `sqlalchemy`, `alembic` on the Python side; `next`, `react`,
  `react-dom`, `@radix-ui/*`, `@ai-sdk/*`, `ai`, `playwright`,
  `vitest`, `msw`, `openapi-typescript` on the JS side), or
* the version string cannot be parsed (rare; better safe than silent).

The watchlist exists because some bumps that look "minor" on paper are
load-bearing — bump these without explicit human review even if they
pass CI green.

## Triage workflow (operator, every Monday)

1. Open the most recent `dependency-preview` issue.
2. Scan the **Summary** table — anything red?
   * pip / pnpm install ❌ → upgraded lockfile is uninstallable; the
     Renovate PR for this week will hit the same wall.
   * pytest ❌ → at least one upgraded dep broke a Python contract.
     Open the workflow artifact, grep the full pytest log for the
     first failure, identify the offending package via `pip.diff`.
   * playwright ❌ → an upgraded JS dep broke an E2E flow. Same
     procedure with `pnpm.diff` + `playwright.log`.
3. Scan **Suspected breaking** — pre-emptively read release notes for
   any package listed here.
4. Decide per package:
   * **Safe** → let the weekend Renovate PR auto-merge per N2 tier
     rules (patches) or approve when it opens (minor).
   * **Hold** → comment `@renovate-bot ignore this PR` on the
     incoming Renovate PR (or pre-emptively pin the package via a
     `packageRule` with `enabled: false`).
   * **Coordinate** → if the bump is a major (e.g. `next` 16→17),
     route to the blue-green deploy lane per N2 major-tier policy.
5. If the install / test failures are spurious (flake, GitHub
   infrastructure outage), trigger another preview manually via
   **Actions → Nightly Upgrade Preview → Run workflow**.

## Why this exists (rationale)

* **N1** locks the lockfile. **N2** automates upgrades on weekends.
  Together they create a window where the weekend bot can land a PR
  that tsc / pytest never saw because the existing CI runs against the
  *committed* lockfile, not the upgraded one. N5 closes that window
  with a 24-hour heads-up.
* Running the full suite *against the upgraded deps* is the only way
  to catch combos like "langchain-core 0.3.74 + pydantic 2.11" that
  pass each library's own tests but blow up at the integration seam.
* Posting an issue (rather than pinging Slack or emailing) gives a
  durable audit trail: every weekend's "what could have broken" is a
  search away under `label:dependency-preview`.

## What N5 does NOT do

* **Does not open Renovate PRs.** That's still N2 / Renovate itself
  (weekend schedule, `every weekend` cron in `renovate.json`).
* **Does not block any PR.** Existing CI gates (`lockfile-drift`,
  `openapi-contract`, `llm-adapter-firewall`, `lint`, `backend-tests`,
  `frontend-unit`, `frontend-e2e`) remain authoritative. N5 is purely
  informational.
* **Does not consume LLM credentials.** Pytest runs with
  `OMNISIGHT_DEBUG=true`, which bypasses startup credential checks;
  no provider keys are used because no agent is exercised.
* **Does not push the upgraded lockfile anywhere.** The trial
  `pnpm-lock.yaml` is restored from a backup before the artifact is
  uploaded; Renovate is the only path that modifies the canonical
  lockfile in `master`.

## Disabling / overriding

* **Skip a single night** — let the workflow run; it's a no-op on
  master. To suppress the issue specifically, comment out the
  `open preview issue` step.
* **Disable temporarily** — `gh workflow disable "Nightly Upgrade
  Preview"` (operator action; re-enable with `gh workflow enable`).
* **Run on demand** — Actions → "Nightly Upgrade Preview" → "Run
  workflow" (uses `workflow_dispatch`).
* **Change the schedule** — edit the `cron` line. Note the cron is in
  UTC, not local time.

## Interaction with other gates

| Phase | Who runs first | What happens on a clash |
|---|---|---|
| **N1 lockfile drift** | every PR (including Renovate) | preview is read-only; never trips drift check |
| **N2 Renovate batch** | every weekend | preview from the night before tells operators what to expect |
| **N3 OpenAPI contract** | every PR | preview doesn't touch backend schemas; orthogonal |
| **N4 LangChain firewall** | every PR | preview doesn't add imports; orthogonal |
| **N6 CVE/EOL monitor** _(planned)_ | continuous | preview surfaces the same packages with longer lead time |

## Artifact retention & cost

* Workflow artifact retention: **14 days**. Pick the most recent run
  if you need the raw diffs; older artifacts roll off automatically.
* Compute: ~30–60 minutes per night on a single ubuntu-latest runner
  (90-minute hard cap). At GitHub Actions free-tier minute pricing
  this is comfortably within budget for an internal repo.
* Issue churn: at most one open `dependency-preview` issue at a time
  (the prior one is closed at the start of every run).

## Bootstrap / one-time operator steps

These are settings the workflow *assumes* are in place. They cannot be
configured from inside the repo:

1. **GitHub token permissions** — `permissions: { issues: write }`
   in the workflow header is sufficient *if* the org-level setting
   "Allow GitHub Actions to create and approve pull requests / issues"
   is enabled (Settings → Actions → General → Workflow permissions).
2. **Issue label `dependency-preview`** — auto-created by `gh issue
   create --label`; no pre-step required, but you may want to give it
   a colour/description in Issues → Labels for visibility.
3. **No secrets required** — the workflow uses only the default
   `GITHUB_TOKEN`; no third-party API keys.

## Change log

* **2026-04-16** — N5 initial implementation. Nightly cron, trial pip-
  compile + pnpm update, full pytest + chromium Playwright against
  upgraded deps, single-issue thread under `dependency-preview` label,
  watchlist-aware "suspected breaking" classifier.
