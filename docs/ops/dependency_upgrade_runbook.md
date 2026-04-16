# Dependency Upgrade Runbook (N6)

> Step-by-step procedure for safely shipping a Renovate / manual
> dependency bump from PR to production. Pairs with
> [`docs/ops/renovate_policy.md`](renovate_policy.md) (N2 — how PRs are
> opened) and [`docs/ops/upgrade_preview.md`](upgrade_preview.md) (N5 —
> how upcoming bumps are forecast).

The runbook is **mandatory** for any PR that:

- touches `backend/requirements.in` / `backend/requirements.txt`
- touches `package.json` / `pnpm-lock.yaml`
- touches `.nvmrc` / `.node-version` / `Dockerfile*` base images
- is labelled `tier/major` or `deploy/blue-green-required` by Renovate

Patch-tier auto-merge PRs (per N2) skip the 24h staging soak but still
inherit the rollback section — a CI-green patch can still regress at
runtime.

---

## TL;DR — the four phases

| Phase | Owner | Gate | Typical duration |
|---|---|---|---|
| **1. Pre-upgrade** | author | checklist in PR description | 15 min |
| **2. During upgrade** | reviewer + oncall | staging 24h + smoke | 24 h |
| **3. Post-upgrade** | oncall | metrics stay green 72 h | 72 h |
| **4. Rollback** _(if needed)_ | oncall | revert + redeploy | ≤15 min |

If any gate fails, **stop and escalate** — do not try to fix forward
under time pressure on a dependency bump.

---

## Phase 1 — Pre-upgrade (before merging the PR)

All items are author-owned and must be ticked in the PR description
before a reviewer approves.

### 1.1 Image snapshot

The deploy image for the *current* (pre-upgrade) version must exist as a
named tag so rollback is a single `docker compose pull`:

```bash
# On the deploy host, before merging:
docker compose -f docker-compose.prod.yml pull
CURRENT_SHA="$(git rev-parse --short HEAD)"
docker tag ghcr.io/omnisight/backend:latest \
           ghcr.io/omnisight/backend:rollback-${CURRENT_SHA}
docker tag ghcr.io/omnisight/frontend:latest \
           ghcr.io/omnisight/frontend:rollback-${CURRENT_SHA}
docker push ghcr.io/omnisight/backend:rollback-${CURRENT_SHA}
docker push ghcr.io/omnisight/frontend:rollback-${CURRENT_SHA}
```

Record `CURRENT_SHA` in the PR body under **Rollback tag:** so Phase 4
doesn't require spelunking the deploy host bash history.

### 1.2 Database backup

Even for "just a library bump" PRs — `alembic upgrade head` runs on
first boot and a failed migration is exactly when you want a known-good
dump:

```bash
# On the deploy host, before merging:
scripts/backup_selftest.py  # verifies backup chain is healthy
cp data/omnisight.db data/omnisight.db.pre-upgrade-${CURRENT_SHA}
sha256sum data/omnisight.db.pre-upgrade-${CURRENT_SHA} \
    > data/omnisight.db.pre-upgrade-${CURRENT_SHA}.sha256
```

For Postgres deployments (future G4):

```bash
pg_dump --format=custom --file=omnisight-${CURRENT_SHA}.pgdump omnisight
```

### 1.3 Lockfile-clean verification

Regenerate lockfiles locally and confirm the PR only bumps what it
*claims* to bump:

```bash
# Python
pip install "pip-tools==7.5.3"
pip-compile --quiet --generate-hashes \
    --output-file=backend/requirements.txt backend/requirements.in
git diff --stat backend/requirements.txt

# JavaScript
pnpm install --frozen-lockfile
# `--frozen-lockfile` must succeed with zero diff; if pnpm prints
# "ERR_PNPM_OUTDATED_LOCKFILE", the PR is missing a lockfile update.
```

The `lockfile-drift` CI job (N1) catches this automatically, but doing
it locally first avoids the round-trip.

### 1.4 Preview-issue sanity check

Open the most recent [`dependency-preview` issue](upgrade_preview.md)
and confirm the packages in this PR do not appear under **Suspected
breaking**. If any do, the PR must:

- link to upstream release notes for each flagged package,
- list the user-visible impact (if any) in the PR body, and
- be labelled `tier/major` regardless of the SemVer bump size.

### 1.5 Pre-upgrade checklist template

Paste this into the PR description and tick each box:

```markdown
## Pre-upgrade (Runbook §1)

- [ ] Rollback tag recorded: `ghcr.io/omnisight/*:rollback-<SHA>`
- [ ] DB snapshot taken: `data/omnisight.db.pre-upgrade-<SHA>` + sha256
- [ ] `pip-compile` + `pnpm install --frozen-lockfile` clean locally
- [ ] Preview issue checked — no load-bearing package under
      "Suspected breaking", OR release notes linked below
- [ ] Release notes: <paste links>
```

---

## Phase 2 — During upgrade (merge → staging → production)

### 2.1 Merge path

| Tier | Auto-merge? | Path |
|---|---|---|
| Vulnerability / CVE | yes (N2) | direct to `master`, staging 1h soak, prod deploy |
| Patch | yes (N2) | direct to `master`, staging 4h soak, prod deploy |
| Minor | no (1 reviewer) | staging 24h soak, prod deploy |
| Major / engines | no (2 reviewers + blue-green) | staging 24h soak, **G3 blue-green** to prod |

### 2.2 Staging soak (24h — minor/major only)

After merge, the deploy pipeline pushes to staging automatically. The
oncall watches:

- **error rate** on staging: must stay ≤ 1.5× the pre-upgrade baseline
  for a contiguous 24h window
- **latency p99** on `/api/v1/*`: must stay ≤ 1.2× the pre-upgrade
  baseline
- **memory RSS** per container: must stay ≤ 1.2× pre-upgrade baseline
- **Zero** Sentry-level exceptions that weren't present pre-upgrade
  (filter by release tag)

If any threshold trips, jump to Phase 4 before the 24h window closes.

### 2.3 Smoke test checklist (run before promoting staging → prod)

Execute `scripts/prod_smoke_test.py --base-url https://staging.<env>`
and verify each of the following manually in a browser pointed at
staging:

- [ ] Login flow (bootstrap admin + one MFA-enabled user)
- [ ] Dashboard loads, live metrics stream renders
- [ ] `/events` SSE connection stays open for ≥ 2 minutes
- [ ] Create a task → run it → confirm artifact pipeline produces output
- [ ] `/api/v1/decisions/` returns HTTP 200 with test bearer token
- [ ] Circuit breaker panel in Integration Settings renders without JS
      error
- [ ] Export an audit chain — verify hash continuity via
      `scripts/audit_archive.py --verify`
- [ ] MFA enrollment (TOTP + WebAuthn) + challenge round-trip
- [ ] Blue-green only: old version still reachable via
      `https://blue.<env>` for rollback

### 2.4 Production cut-over

- **Patch / CVE / minor**: standard `scripts/deploy.sh` (recreates
  containers in-place)
- **Major / engines / blue-green-required**: G3 blue-green path
  (standby upgrades first, smoke runs against standby, cut traffic
  only on green smoke, keep old version hot for 24h)

Record the deploy start timestamp in the PR body under **Production
cut-over:** for the Phase 3 monitoring window.

---

## Phase 3 — Post-upgrade (72h monitoring window)

The oncall watches three metrics on the production dashboard at roughly
these checkpoints: +1h, +6h, +24h, +72h. The window closes at +72h
**only if all three metrics remained within threshold at every
checkpoint.**

### 3.1 Error rate

- Source: backend 5xx counter + frontend unhandled-rejection counter
- Threshold: ≤ 1.5× pre-upgrade 7-day rolling baseline
- Action on breach:
  - +1h breach → rollback immediately (Phase 4)
  - +6h breach → open incident, rollback if no root cause in 15 min
  - +24h breach → open incident, triage on a best-effort basis
  - +72h breach → file issue, do not rollback (likely unrelated)

### 3.2 Latency p99

- Source: request-duration histogram on `/api/v1/*`
- Threshold: ≤ 1.2× pre-upgrade 7-day rolling baseline
- A new dependency's cold-start overhead commonly adds 10–20% to p99
  for the first hour; the threshold accounts for this.

### 3.3 Memory RSS

- Source: per-container `docker stats` memory sampled every 60s
- Threshold: ≤ 1.2× pre-upgrade baseline **after** the first hour (the
  first hour is noisy from cache warm-up)
- Memory regressions from dependency bumps are usually silent — a
  monotonic RSS climb over 6h is a strong rollback signal.

### 3.4 Sentry / log-based alerts

- Any **new** exception class that wasn't present in the 7 days before
  the deploy: investigate within 1h
- Any **pre-existing** exception whose count per minute > 2× its
  pre-upgrade rate: investigate within 6h

### 3.5 Metrics summary template

Record in the PR body (or a follow-up comment on the deploy PR) once
the 72h window closes:

```markdown
## Post-upgrade 72h review (Runbook §3)

- Error rate: peak 1.NNx baseline at +NNh (within 1.5x threshold ✅ / ❌)
- Latency p99: peak 1.NNx baseline at +NNh (within 1.2x threshold ✅ / ❌)
- Memory RSS: peak 1.NNx baseline at +NNh (within 1.2x threshold ✅ / ❌)
- New Sentry exceptions: <count> — links: ...
- Decision: ACCEPT / ROLL BACK / WATCH
```

If the decision is ACCEPT, the rollback image tags from Phase 1.1 can
be deleted after +7d (retention policy — gives time for delayed
regressions to surface).

---

## Phase 4 — Rollback

Rollback is designed to take **≤ 15 minutes from decision to restored
service**. There are two paths depending on deploy mode.

### 4.1 Rollback decision criteria

Trigger a rollback when *any* of the following holds during Phase 3:

- Phase 3.1 error-rate threshold breach at +1h or +6h with no
  actionable root cause within 15 minutes
- Authentication broken (any `/auth/*` endpoint 5xx rate > 5%)
- Data integrity issue (audit chain verification fails, migrations
  left the DB in an inconsistent state)
- Oncall judgement call — err on the side of rolling back. Rolling
  forward under pressure is how 2am incidents become 6am incidents.

### 4.2 Path A — In-place rollback (patch / minor deploys)

```bash
# Identify the rollback tag from the PR body (see §1.1).
ROLLBACK_SHA="<short-sha-from-pr-body>"

# 1. Revert the merge commit on master (opens trace in git log).
git revert --no-edit <merge-commit-sha>
git push origin master

# 2. On the deploy host, pull the pinned rollback images.
docker compose -f docker-compose.prod.yml pull \
    --policy always
# If CI hasn't rebuilt `:latest` yet, point compose at the rollback tag:
BACKEND_IMAGE=ghcr.io/omnisight/backend:rollback-${ROLLBACK_SHA} \
FRONTEND_IMAGE=ghcr.io/omnisight/frontend:rollback-${ROLLBACK_SHA} \
docker compose -f docker-compose.prod.yml up -d --no-deps backend frontend

# 3. Verify service is restored.
curl -fsS https://prod.<env>/api/v1/health
scripts/prod_smoke_test.py --base-url https://prod.<env>
```

### 4.3 Path B — Blue-green rollback (major / engines / blue-green-required)

If the upgrade rode the G3 blue-green lane, the previous version is
still running on the `blue.<env>` DNS name. Rollback is a DNS / LB cut:

```bash
# 1. Flip the traffic manager back to blue.
scripts/deploy.sh --switch-active blue

# 2. Verify traffic is served from blue.
curl -sS https://prod.<env>/api/v1/health | jq .git_sha
# Expect: <pre-upgrade sha>, not the new deploy sha.

# 3. Tear down green so it can't race with fresh writes.
docker compose -f docker-compose.prod.yml --profile green down

# 4. Revert the master commit (same as Path A step 1).
git revert --no-edit <merge-commit-sha>
git push origin master
```

### 4.4 Database rollback

**Only** restore the DB snapshot if the migration actually ran and left
the DB in an inconsistent state. For pure library-version bumps that
don't touch `backend/alembic/versions/`, the DB is untouched — skip
this section.

```bash
# Stop the stack first — a live DB + overwrite is a recipe for
# corruption.
docker compose -f docker-compose.prod.yml down

# Verify the pre-upgrade snapshot hash.
cd data
sha256sum -c omnisight.db.pre-upgrade-${ROLLBACK_SHA}.sha256
# Must print: omnisight.db.pre-upgrade-<sha>: OK

# Restore.
cp omnisight.db.pre-upgrade-${ROLLBACK_SHA} omnisight.db

# Restart.
cd ..
docker compose -f docker-compose.prod.yml up -d
```

For Postgres (future G4):

```bash
pg_restore --clean --if-exists --dbname=omnisight omnisight-${ROLLBACK_SHA}.pgdump
```

### 4.5 Path C — Fallback-branch rollback (framework major explosions)

If the failed upgrade was a framework major (`next`, `pydantic`) and a
declared fallback branch exists for the previous major
(`compat/<framework>-<N-1>`), prefer this path over Path A/B — it
ships the *previous* framework version that has live CI proving it
still builds with our codebase, instead of relying on the rollback
image tag (which may have been built before any of the master commits
that landed since the fallback was last rebased).

```bash
# 1. On the deploy host, fetch the latest fallback ref.
git fetch origin
git switch --detach origin/compat/nextjs-15      # or compat/pydantic-v2
LATEST_GREEN_SHA="$(git rev-parse HEAD)"

# 2. Rebuild and redeploy from the fallback ref.
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

# 3. Tag the fallback ref so forensics has an anchor.
git tag "rollback-to-fallback-$(date +%Y%m%d)" "${LATEST_GREEN_SHA}"
git push origin "rollback-to-fallback-$(date +%Y%m%d)"

# 4. Revert the failed major-bump merge commit on master so the next
#    deploy does not re-attempt it.
git switch master
git revert --no-edit <merge-commit-sha>
git push origin master
```

Decision rule:

* Fallback exists and `fallback-branches.yml` shows GREEN within
  freshness window → **Path C** (preferred).
* No fallback (e.g. brand-new framework adoption) → Path A or B.
* Fallback exists but is stale → Path A or B; do *not* deploy a stale
  fallback to prod (the whole point of N9's freshness gate is to keep
  this from being the rollback target).

See [`docs/ops/fallback_branches.md`](fallback_branches.md) for the
fallback branches' lifecycle, weekly maintenance SOP, and the
freshness window definition.

### 4.6 Post-rollback hygiene

- File an incident report in the PR body (what failed, what got
  rolled back, next steps) — even if the fix is "reopen the PR with a
  version pin one minor lower".
- Keep the rollback image tags for an additional 14 days so a second
  re-rollback is available if a new root cause surfaces.
- If the underlying package is CVE-driven, coordinate with security on
  a hotfix path — a rolled-back CVE patch is still an open
  vulnerability.

---

## Related automation

| Phase | Automation | Source |
|---|---|---|
| §1.3 lockfile | `lockfile-drift` CI job | `.github/workflows/ci.yml` |
| §1.4 preview | Nightly preview issue | `.github/workflows/upgrade-preview.yml` (N5) |
| §2.1 vulnerability tier | Renovate `vulnerabilityAlerts` | `renovate.json` (N2) |
| §2.3 smoke | `scripts/prod_smoke_test.py` | N/A — standalone script |
| §3.x metrics | CVE daily scan | `.github/workflows/cve-scan.yml` (N6) |
| §1.x planning | EOL monthly check | `.github/workflows/eol-check.yml` (N6) |
| §2.x major-bump gate | Major Upgrade Gate | `.github/workflows/major-upgrade-gate.yml` (N9) |
| §4.5 framework rollback | Fallback branches | `.github/workflows/fallback-branches.yml` + `docs/ops/fallback_branches.md` (N9) |

## Change log

- **2026-04-16** — N6 initial runbook. Four phases (pre / during /
  post / rollback), 24h staging soak, 72h production monitoring,
  blue-green-aware rollback paths, DB snapshot-and-restore procedure.
- **2026-04-16** — N9 added Phase 4.5 (Path C — fallback-branch
  rollback) and the major-upgrade gate row in the automation table.
