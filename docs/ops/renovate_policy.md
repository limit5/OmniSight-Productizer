# Renovate Policy (N2)

> Source of truth for how dependency PRs are opened, grouped, reviewed,
> and merged in this repo. Implementation lives in
> [`renovate.json`](../../renovate.json) at the repo root. CI validates
> the schema in the `renovate-config` job (`.github/workflows/ci.yml`).

## TL;DR

| Update type | Auto-merge? | Reviewers | Wait time | Extra gates |
|---|---|---|---|---|
| **CVE / vulnerability** | yes (CI green → auto) | none required | none — open immediately | none, fastest path |
| **patch** (incl. `pin`, `digest`) | yes (CI green → auto) | none | 3 days since release | — |
| **minor** | no | 1 (CODEOWNERS) | 5 days | — |
| **major** | no, **never** | 2 (CODEOWNERS) | 14 days | G3 blue-green deploy (TODO N10) + smoke-test results in PR |
| **engines** (Node/pnpm/Python) | no | 1+ | default | treat as major regardless of bump size |

Schedule: PRs are opened **only on weekends** (`every weekend` in
`Asia/Taipei`) to keep weekday signal noise low. Vulnerability PRs are
exempt — they open `at any time`.

## Group rules

Why grouping matters: peer-coupled package families break in
inconsistent ways when only some members get bumped. Renovate fans these
into a single PR so the lockfile stays internally consistent.

| Group | Pattern | Why grouped | Manager(s) |
|---|---|---|---|
| `radix-ui` | `@radix-ui/{/,}**` | All Radix primitives share a peer-dep on `react`/`react-dom`; mixing minors causes runtime warnings + style regressions | `npm` (pnpm) |
| `ai-sdk` | `@ai-sdk/{/,}**` | Vercel AI SDK provider modules ride a single core; the `ai` core and provider versions must stay in lockstep | `npm` (pnpm) |
| `langchain-python` | `/^langchain/`, `/^langgraph/` | The LangChain provider matrix (`langchain-anthropic`, `langchain-openai`, `langchain-google-genai`, …) plus `langgraph` all peer-depend on `langchain-core`. Grouping them prevents a half-upgraded matrix that imports two `langchain-core` minor versions | `pip-compile` |
| `types` | `@types/{/,}**` (`devDependencies`) | DefinitelyTyped packages are dev-only and low-risk; auto-merge minor in addition to patch (overrides the global minor tier) | `npm` (pnpm) |
| `github-actions` | all `.github/workflows/*.yml` actions | Pin to digest, group together, auto-merge minor/patch | `github-actions` |
| `docker-base-images` | `Dockerfile*` + `docker-compose*.yml` | Base-image bumps are coupled with deploy plumbing; never auto-merge | `dockerfile`, `docker-compose` |

## Tier rules in detail

### Patch (`patch` / `pin` / `digest`)

* `automerge: true` + `platformAutomerge: true` → uses GitHub's native
  auto-merge. The PR enters the auto-merge queue the moment Renovate
  opens it; GitHub merges as soon as required checks pass.
* `minimumReleaseAge: 3 days` — the upstream version must have been
  published for ≥3 days. Catches the "publisher accidentally pushes a
  broken patch and yanks within 24h" failure mode.
* Includes **security patches**: a CVE patch is still a patch update;
  the `vulnerabilityAlerts` rule simply opens the PR immediately
  (overrides the `every weekend` schedule and the 3-day wait).

### Minor (`minor`)

* `automerge: false` — a human must approve.
* `reviewersFromCodeOwners: true` — Renovate auto-requests review from
  whoever owns the changed files per `.github/CODEOWNERS`.
* `minimumReleaseAge: 5 days` — slightly longer than patch since minors
  are more likely to introduce regressions.
* Approval threshold: **1 reviewer** (matches the repo's branch
  protection setting for non-major changes).

### Major (`major`)

* `automerge: false` — disabled regardless of CI status, regardless of
  the number of approvals. Major bumps **must** ship via the
  blue-green path (see TODO N10).
* `reviewersFromCodeOwners: true` + branch-protection requires
  **2 approvals** before merge.
* `minimumReleaseAge: 14 days` — give upstream time to flush release
  regressions.
* PR body includes a hard-coded checklist (`prBodyNotes`):
  1. 2 human approvals,
  2. G3 blue-green deploy run with results pasted in PR,
  3. manual smoke-test results pasted in PR.
* The `deploy/blue-green-required` label is what couples this rule to
  the deploy gate — `scripts/deploy.sh` (or a future webhook on the
  prod-deploy job) must refuse to deploy if a merged commit's PR
  carried that label and the blue-green status is not "passed".

### Engines (Node, pnpm, Python ranges in `package.json` / `pyproject.toml`)

* Treated as major regardless of the bump size — engine drift cascades
  through dev environments, CI matrix, and Docker base images at once.
* Manual review only; no auto-merge.
* When bumping Node, remember to also update `.nvmrc`,
  `.node-version`, and the CI matrix (see N7 in TODO once it lands).

### Fallback branches (`compat/**`) — N9 carve-out

The N9 fallback branches exist precisely to **hold a previous major**
of a framework. Renovate's default behaviour would happily bump
`next` on `compat/nextjs-15`, defeating the entire point. The
`packageRules` block in `renovate.json` therefore:

* On any branch matching `compat/**`, the package whose name appears
  in the branch (`next` for `compat/nextjs-15`, `pydantic` for
  `compat/pydantic-v2`) is **excluded** from any update PR.
* All *other* packages on those branches stay in scope so the fallback
  doesn't rot — security patches and unrelated minor bumps still flow.
* The fallback's `[pin].version` in `.fallback/manifests/<branch>.toml`
  is the source of truth for what version is held; reviewers reading
  Renovate's PR list should never see a `next` bump targeting
  `compat/nextjs-15`.

If you do see one, the `packageRules` carve-out has drifted — fix it
in `renovate.json` and the shape-guard test in
`backend/tests/test_dependency_governance.py` will start passing again.

## Vulnerability handling

`vulnerabilityAlerts` short-circuits the schedule and the patch wait:

* `schedule: ["at any time"]` — opens immediately when GitHub /
  Renovate detect a CVE on a tracked package.
* `automerge: true` + `prPriority: 100` — the dedicated
  `isVulnerabilityAlert: true` rule in `packageRules` ensures the PR
  jumps the queue and merges as soon as CI is green.
* `osvVulnerabilityAlerts: true` — additionally enables Renovate's
  scan against the OSV database (catches CVEs that GitHub's advisory
  feed has not picked up yet).
* Labels: `security`, `priority/critical`, `auto-merge`.

Operator action: if CI fails on a security PR, fix it as the highest
priority of the day — do not let it linger past the next business day.

## How the policy interacts with the rest of the repo

* **N1 lockfile drift**: Renovate updates the source-of-truth files
  (`package.json`, `backend/requirements.in`) and *also* regenerates
  `pnpm-lock.yaml` / `backend/requirements.txt` in the same commit, so
  the `lockfile-drift` CI gate stays green. If you ever see Renovate
  open a PR that fails `lockfile-drift`, the bot is misconfigured —
  fix `renovate.json`, don't paper over it in CI.
* **N5 nightly upgrade-preview**: the nightly preview job tells you
  *what* the upcoming weekend's Renovate batch will try to merge;
  this policy tells you *how* it will be merged. Use the preview to
  pre-empt breakage on major-bump candidates before Saturday.
* **BP.I SecOps Intel**: pre-install and pre-blueprint Intel briefs may
  flag recent CVEs or exploited-in-the-wild signals before a dependency
  enters the repo. Once a tracked dependency needs remediation, this N2
  policy remains the owner of the fix PR and lockfile regeneration; see
  [`secops_intel_overlap.md`](secops_intel_overlap.md).
* **CODEOWNERS**: `reviewersFromCodeOwners: true` requires a populated
  `.github/CODEOWNERS`. If a path has no owner, Renovate falls back to
  whoever the repo's default reviewers are (currently the maintainer).

## Disabling / overriding

* To skip a single PR: comment `@renovate-bot ignore this PR` on the PR
  itself. The PR closes and Renovate won't reopen it for that version.
* To pin (block all updates) for a package: add a packageRule with
  `enabled: false` under `packageRules` keyed on the package name.
* To force-open a previously-skipped PR: tick its checkbox in the
  Dependency Dashboard issue (`Renovate Dependency Dashboard (N2
  policy)`) — the bot reopens on its next run.
* Schedule overrides for an emergency batch: edit `renovate.json`'s
  top-level `schedule`, merge the change, then revert after the batch
  runs.

## Bootstrap checklist (operator)

These are the **one-time** repo settings the policy relies on. They
cannot be set in `renovate.json` — they live in GitHub's repo
configuration and must be flipped by an admin (Operator-blocked):

1. Install the [Renovate GitHub App](https://github.com/apps/renovate)
   on this repo (or the org).
2. **Settings → General → Pull Requests → Allow auto-merge**: on.
   `platformAutomerge: true` is a no-op without this.
3. **Settings → Branches → master**: require 1 review for all PRs;
   require 2 reviews for PRs labelled `tier/major` (if your branch
   protection plan supports label-conditional rules; otherwise leave
   at 1 and rely on the policy doc + CODEOWNERS).
4. **Settings → Code security → Dependabot alerts**: on (Renovate
   reads GitHub's advisory feed via this).
5. Create a `.github/CODEOWNERS` entry covering at least the repo
   root so Renovate has someone to request review from on minor/major
   PRs. Empty-CODEOWNERS deployments are explicitly supported (the
   policy degrades gracefully) but you'll want owners before the first
   weekend batch lands.

## Validation

`renovate.json` is validated on every PR by the `renovate-config` job
in `.github/workflows/ci.yml`:

```bash
npx --yes --package renovate@39 -- renovate-config-validator --strict renovate.json
```

Run this locally before committing changes to `renovate.json`. The
validator catches typos in field names, deprecated patterns, and
schema-incompatible combinations — fast feedback before the bot tries
to interpret a broken config in production.

## Cross-reference

* **Cadence + deploy gate (N10)**:
  [`dependency_upgrade_policy.md`](dependency_upgrade_policy.md) is
  the authoritative doc for **how often** majors ship and the
  **blue-green ceremony** a major PR must complete before it can hit
  prod. This file (`renovate_policy.md`, N2) describes how Renovate
  *opens* the PRs; the policy doc describes how they *land*.
* **Runbook**: [`dependency_upgrade_runbook.md`](dependency_upgrade_runbook.md)
  — the four-phase upgrade + rollback SOP.
* **SecOps Intel overlap**:
  [`secops_intel_overlap.md`](secops_intel_overlap.md) — how BP.I,
  N2, and S2-8 avoid duplicate CVE/secret-scanning ownership.
* **Ledger**: [`upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md)
  — append-only ledger read by the quarterly policy review.

## Change log

* **2026-04-16** — N2 initial policy. Group rules for `@radix-ui/*`,
  `@ai-sdk/*`, `langchain*` / `langgraph`, `@types/*`. Tiered
  auto-merge (patch auto, minor 1-rev, major 2-rev + blue-green).
  Security PRs immediate + auto-merge. Schedule `every weekend`.
