# Dependency Upgrade Policy (N10)

> Source of truth for **how often** dependency PRs ship, **who must sign
> off**, and **which deploy path** they take. This doc closes the last
> leg of the N-series: N2 defines how Renovate opens PRs,
> `docs/ops/dependency_upgrade_runbook.md` (N6) defines the four-phase
> upgrade SOP, and this policy (N10) defines the **cadence + deploy
> gate** so every tier lands the same way every time.
>
> N10 pairs with G3 blue-green: **major** upgrades are physically
> incapable of reaching production without the blue-green ceremony
> (standby → smoke → cutover → 24h rollback window).

## TL;DR — cadence matrix

| Tier | Batch cadence | Reviewers | Soak before merge | Deploy path | Rollback window |
|---|---|---|---|---|---|
| **Patch** (incl. CVE) | weekly | 0 — auto-merge on CI green | 3 days since upstream release | direct (rolling restart) | standard backup + git revert |
| **Minor** | bi-weekly | 1 CODEOWNERS | 5 days + staging 24h | staging soak → prod direct | standard |
| **Major** | **quarterly** | 2 CODEOWNERS | 14 days + staging 48h | **G3 blue-green mandatory** (standby upgrade → smoke → traffic cut-over → **old version kept hot for 24h**) | dedicated rollback-to-fallback tag |
| **Engines** (Node / pnpm / Python) | quarterly | 2 CODEOWNERS | 14 days | treated as major — blue-green | as major |

**PR packaging rule** — enforced by Renovate grouping + reviewer
discipline: **one package per PR**, or *one tight dependency group* per
PR (see [`renovate_policy.md`](renovate_policy.md) for the N2 group
rules and the permitted families: `radix-ui`, `ai-sdk`,
`langchain-py`, `types`). **Never** mix
unrelated packages in one PR. The reason is single-revert: when a
major upgrade breaks prod, `git revert <sha>` must remove *exactly*
the failing package and nothing else.

## Blue-green requirement (G3 coupling)

This section is the load-bearing contract for the CI/deploy gate.
Changes here must be matched in
[`.github/workflows/blue-green-gate.yml`](../../.github/workflows/blue-green-gate.yml),
[`scripts/check_bluegreen_gate.py`](../../scripts/check_bluegreen_gate.py),
and the N10 shape tests in
`backend/tests/test_dependency_governance.py`.

A PR is **blue-green required** (gets the `requires-blue-green` label)
if **any** of the following holds:

1. Renovate labelled it `tier/major` (per the N2 rule in
   `renovate.json`). Renovate *also* adds `deploy/blue-green-required`
   — we treat that as an alias.
2. The PR title matches the Renovate major-bump pattern
   `Update <dep> to v<N>` *and* `<N>` is a major semver bump.
3. A human manually bumped a framework in `package.json`,
   `backend/requirements.in`, `.nvmrc`, `.node-version`, or
   `pyproject.toml` such that the first semver component changed
   (detected by the label workflow diffing base ↔ head).
4. A maintainer manually applies the `requires-blue-green` label
   (escape hatch for ambiguous cases — e.g. a minor bump of
   `langchain-core` that Renovate classified as minor but the reviewer
   knows is internally breaking).

Once labelled, the PR carries the label through to merge. The
**deploy-time gate** then refuses to ship to prod unless:

* the merge commit's PR had `requires-blue-green`, **and**
* the release artefact's metadata records a completed blue-green run
  (standby upgrade logged, smoke test green, cut-over timestamp, old
  version tag retained for 24h rollback).

### Blue-green ceremony (operator SOP)

The ceremony runs on the **standby** side of the blue-green pair; the
active side keeps serving traffic until the final cut-over. All five
steps are mandatory — skipping any of them invalidates the blue-green
claim and the deploy gate will reject the cut-over.

1. **Standby upgrade**
   * `scripts/deploy.sh staging <ref>` on the standby host.
   * Wait for `/api/v1/health` to report `UP` on the standby port.
2. **Smoke test**
   * `python3 scripts/prod_smoke_test.py --target standby` (end-to-end
     golden paths: auth, artifact pipeline, DAG submit, SSE, cross-agent
     router). A green run is a hard prerequisite for step 3.
   * If any smoke check flaps, do **not** retry silently — diagnose
     the flap first, *then* re-run the whole smoke set. Flapping a
     smoke test and re-running until it passes defeats the whole gate.
3. **Traffic cut-over**
   * Flip the upstream (nginx / cloudflared / LB) from blue → green
     atomically. Record the cut-over timestamp in the rollback ledger.
4. **Old version hot for 24h**
   * The previous active side stays running, fully populated, with
     the old image / git ref, for **at least 24 hours**. This is the
     rollback window — it is non-negotiable.
   * Monitor error rate, latency p99, memory, and any domain-specific
     SLO (DAG completion rate, SSE reconnect, auth success) against
     the 72h baseline from the N6 runbook.
5. **24h rollback window expiry**
   * After 24h green, the old side may be torn down. Record the
     expiry in the rollback ledger (even if no rollback was needed —
     a clean close-out is the success signal for the quarterly review).

If step 4 trips any SLO: flip upstream back to the old side, tag the
incident as a **rollback event** in
[`docs/ops/upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md),
and follow `dependency_upgrade_runbook.md` Phase 4 (Rollback). The
failed PR is then auto-reclassified to the quarterly review agenda.

## Cadence rationale

Why these numbers (and not weekly-for-everything):

* **Patch weekly** — CVE-class patches *must* ship fast; non-CVE
  patches are close enough to no-risk that batching them weekly keeps
  signal-to-noise high without sitting on security fixes.
* **Minor bi-weekly** — one human reviewer per minor PR means
  reviewer bandwidth is the constraint. A weekly minor batch consumes
  ~2× the reviewer time for ~1× the risk reduction; bi-weekly is the
  Pareto point from the last quarter's review log.
* **Major quarterly** — blue-green ceremony is ~1 engineer-day
  end-to-end. Doing it monthly would starve feature work; yearly
  lets majors stack up and compound risk. Quarterly (4× / year) is
  the bounded middle: one major at a time, one quarter's worth of
  upstream soak per bump.
* **Engines as major** — Node, pnpm, and Python runtime bumps fan
  out through dev images, CI matrix, Docker base images, and every
  deploy target. Treating them as majors is the only way the N7
  multi-version CI matrix keeps converging on a single green cell.

## Single-revert discipline

Why "one package per PR" is in the matrix at all:

* A single-package PR is **physically revertible** by `git revert
  <sha>`. A multi-package PR — even if CI green — is not, because
  the revert pulls back bystander changes that may have already
  shipped to users or been stamped into other PRs.
* The N2 `groupSlug` rules carve out **tight** coupling (Radix,
  `@ai-sdk`, LangChain providers, `@types/*`) where mixing is the
  *safer* option because partial upgrades cause peer-dep hell. Those
  carve-outs are the only mixes allowed; everything else must be
  opened as its own PR.
* Renovate enforces this by default (`separateMajorReleases` +
  per-package PRs). If you catch a human bundling unrelated majors,
  bounce the PR with a pointer to this section.

## Quarterly review

Every quarter, on the first working day of the new quarter, the
maintainer:

1. Pulls the last 3 months of entries from
   [`docs/ops/upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md).
2. Computes: **majors shipped**, **rollbacks triggered**, **mean
   soak-to-cutover** (hours).
3. Publishes a one-paragraph summary in HANDOFF.md and files an
   issue tagged `policy-review` if *either*:
   * rollback rate > 25 % (too many majors break), or
   * mean soak < 24h (cadence discipline slipping).

A rollback rate of 0 is **not** automatically good — it can mean
operators are suppressing the SLO triggers. Cross-check with the
runbook's 72h monitoring windows for the same PRs.

## How this policy is enforced

| Mechanism | File / job | What it enforces |
|---|---|---|
| Renovate tier labels | `renovate.json` → `packageRules` | Opens major PRs with `tier/major` + `deploy/blue-green-required` (N2) |
| Auto-label workflow | `.github/workflows/blue-green-gate.yml` → job `auto-label` | Adds `requires-blue-green` to any PR (human or bot) that bumps a major |
| Major upgrade gate | `.github/workflows/major-upgrade-gate.yml` | N9 — refuses merge if the framework's fallback branch CI is stale |
| PR merge gate | `.github/workflows/blue-green-gate.yml` → job `pr-check` | Status check "N10 / blue-green-label" that stays red until the label is present *and* the PR body carries the G3 ceremony checklist |
| Deploy-time gate | `scripts/check_bluegreen_gate.py` (called by `scripts/deploy.sh` when `ENV=prod`) | Queries `gh pr list` for the last commit's PR; refuses `deploy.sh prod` if the label is present but the blue-green artefact is not |
| Ledger | `docs/ops/upgrade_rollback_ledger.md` | Records every major + every rollback; the quarterly review reads from here |
| Shape tests | `backend/tests/test_dependency_governance.py` → N10 section | Pins file presence, doc phrases, workflow job names — so a careless edit cannot silently disarm the gate |

## Escape hatches

These exist so the policy stays workable under real incident pressure.
All three require a HANDOFF.md note so the next maintainer understands
why the gate was opened manually.

* **`deploy/bluegreen-waived` label** — set by a maintainer with a
  short rationale in the PR body. `scripts/check_bluegreen_gate.py`
  will honour the waiver but log it as a **waived** entry in the
  ledger so the quarterly review counts it separately from green
  cut-overs.
* **`OMNISIGHT_BLUEGREEN_OVERRIDE=1`** — environment variable read
  by `scripts/deploy.sh`. Intended only for disaster recovery when
  the gate script itself is broken. Using this *without* a matching
  waiver label turns into a quarterly-review audit item.
* **Quarterly policy amendment** — the cadence numbers above are
  deliberately opinionated but not immutable. If the rollback rate
  stays < 5 % for two consecutive quarters, the review may propose
  dropping majors to **bi-monthly** — but the amendment must land in
  this file + ledger + test before the new cadence takes effect.

## Change log

* **2026-04-16** — N10 initial policy. Quarterly majors + mandatory
  G3 blue-green. Auto-label workflow + deploy-time gate. Ledger +
  quarterly review. One-package-per-PR discipline codified.
