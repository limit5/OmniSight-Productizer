# Framework Fallback Branches (N9)

> **Purpose**: keep two long-running branches (`compat/nextjs-15`,
> `compat/pydantic-v2`) **always deployable** so a Next 17 / Pydantic 3
> production explosion has a one-command rollback target.

This SOP pairs with:

* `.fallback/README.md` + `.fallback/manifests/*.toml` — the declarative
  source of truth. Read by every tool and gate below.
* `.github/workflows/fallback-branches.yml` — weekly + push-trigger CI
  that certifies each fallback branch.
* `.github/workflows/major-upgrade-gate.yml` — pre-merge gate that
  blocks `tier/major` framework PRs while the relevant fallback is stale.
* `scripts/fallback_setup.sh` — one-shot bootstrap (operator).
* `scripts/fallback_rebase.py` — weekly rebase planner / applier.
* `scripts/check_fallback_freshness.py` — gate's freshness probe.
* [`docs/ops/dependency_upgrade_runbook.md`](dependency_upgrade_runbook.md)
  Phase 4.5 — invokes the fallback-tag rollback path.
* [`docs/ops/renovate_policy.md`](renovate_policy.md) — explains why
  Renovate is excluded from `compat/**` for the pinned framework.

---

## TL;DR

| Concern | Where |
|---|---|
| What pins the fallback to which version? | `.fallback/manifests/<branch>.toml` |
| Where do the branches actually live? | `compat/nextjs-15`, `compat/pydantic-v2` (origin) |
| How are they kept evergreen? | weekly `git switch` + `scripts/fallback_rebase.py --apply` |
| How is "fresh" defined? | `[gate].freshness_days` in the manifest (default 14) |
| What blocks a Next 17 PR? | major-upgrade-gate.yml (label `tier/major` + framework name in title) |
| How do I roll back production to a fallback? | runbook Phase 4.5 |

---

## Why two branches, why these two

* **`compat/nextjs-15`** — master is on Next 16. Next 17 will eventually
  ship; React 19's `use(Promise)` semantics already broke twice across
  16's minor releases. We pre-stage Next 15 (the previous stable major)
  as the rollback-target so a 16→17 explosion has a known-green
  destination, instead of a forensic "pin Next to 15.5.4 right now and
  see what else breaks" scramble during the incident.
* **`compat/pydantic-v2`** — master is on Pydantic 2.11.x. v3 has not
  shipped yet, but the v1→v2 migration cost the industry six months.
  Standing the v2 branch up **before** v3 ships means we have the
  "last known v2 green" reference the day v3 lands, not three weeks
  later when we realise v3 broke `model_config`.

The pattern is general — declare a new manifest under
`.fallback/manifests/` whenever a new framework becomes load-bearing.
The workflow + scripts pick it up automatically; only the docs need
extending.

---

## Lifecycle (state machine)

```
        ┌────────────────────┐
        │  master @ vN       │
        └─────────┬──────────┘
                  │ N9 setup (one-shot)
                  ▼
        ┌────────────────────┐
        │ compat/<fw>-<N-1>  │ ← initial copy of master HEAD
        └─────────┬──────────┘
                  │ weekly rebase (non-framework commits only)
                  ▼
        ┌────────────────────┐
        │ fallback evergreen │ ← certified GREEN by fallback-branches.yml
        └─────────┬──────────┘
                  │ master tries to merge tier/major framework PR
                  ▼
        ┌────────────────────┐
        │ gate: freshness OK?│
        └────┬───────────┬───┘
             │ yes       │ no
             ▼           ▼
        ┌────────┐  ┌──────────────────────────────┐
        │ merge  │  │ block PR; operator runs      │
        └────────┘  │ scripts/fallback_rebase.py   │
                    └──────────────────────────────┘

If production explodes after merging:

        ┌────────────────────┐
        │ rollback to        │ ← runbook Phase 4.5
        │ compat/<fw>-<N-1>  │
        │ @ latest-green tag │
        └────────────────────┘
```

---

## Operator playbooks

### One-shot bootstrap (do this once, after merging N9)

```bash
bash scripts/fallback_setup.sh             # creates local branches at master HEAD
git push -u origin compat/nextjs-15
git push -u origin compat/pydantic-v2
```

The first push triggers `fallback-branches.yml`. Wait for green
(~10 min) before relying on the gate.

### Weekly maintenance (Monday mornings, ~10 minutes)

```bash
git fetch origin
git switch compat/nextjs-15
git rebase origin/compat/nextjs-15           # pull any prior week

python3 scripts/fallback_rebase.py \
    --branch compat/nextjs-15 \
    --range  HEAD..origin/master \
    --plan                                    # dry-run report
# review the report; partial-skip commits need manual splits.

python3 scripts/fallback_rebase.py \
    --branch compat/nextjs-15 \
    --range  HEAD..origin/master \
    --apply                                   # actually cherry-pick
git push origin compat/nextjs-15

# Repeat for compat/pydantic-v2.
```

`fallback-branches.yml` re-runs on the push. Wait for green; if red,
inspect and either revert the offending pick or update the manifest's
`skip_globs` and re-plan.

### When the gate fails on a major-upgrade PR

The PR's `Major Upgrade Gate / freshness (...)` job is red.

1. Click into the job's step summary — it prints a recovery block
   with the exact `scripts/fallback_rebase.py` invocation.
2. Run the recovery commands (above weekly maintenance section).
3. Push the fallback branch.
4. The gate re-evaluates on next PR sync — push an empty commit on the
   PR if you want it now, or wait for the next CI tick.

### Production rollback (incident — see runbook Phase 4.5, "Path C")

```bash
# On the deploy host:
git fetch origin
git switch --detach origin/compat/nextjs-15      # or compat/pydantic-v2
LATEST_GREEN_SHA="$(git rev-parse HEAD)"

# Rebuild + redeploy from the fallback ref:
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

# Tag the fallback ref so the next forensic step has an anchor:
git tag "rollback-to-fallback-$(date +%Y%m%d)" "${LATEST_GREEN_SHA}"
git push origin "rollback-to-fallback-$(date +%Y%m%d)"
```

Then file an incident ticket and start root-cause analysis on master
**without** time pressure — production is on the fallback.

---

## Design decisions

* **Why a TOML manifest, not just naming convention?** A name encodes
  one fact (the framework + major). The manifest encodes six —
  `freshness_days`, `skip_globs`, `required_check_name`, `pin`,
  `manifest_paths`, `retire`. Three different consumers (workflow,
  rebase script, gate) need this metadata. Without the manifest, every
  policy edit ripples through three files; with it, one file does.
* **Why per-branch concurrency in `fallback-branches.yml`?** A push to
  `compat/nextjs-15` should not cancel an in-flight nightly-cron run on
  `compat/pydantic-v2`. The concurrency key embeds the ref/input, so
  cancellation is scoped to the actual contended branch.
* **Why does the rebase tool refuse `partial-skip` by default?** A
  commit that touches `next.config.ts` AND `backend/api_keys.py` cannot
  be safely auto-split — splitting changes the commit's atomic intent
  (and breaks `git bisect` later). The tool reports it, and the
  operator splits manually with `git checkout -p`. `--allow-partial-skip`
  exists for the rare case where the operator decides the framework
  delta dominates and the safe paths are noise.
* **Why a 14-day freshness window in the gate?** Weekly cron + push
  triggers means a freshness violation can happen only if (a) the
  weekly run failed two cycles in a row AND (b) no one rebased in
  between. Both conditions failing for two weeks means the fallback is
  unmaintained — the right answer at that point is *not* to merge a
  framework major upgrade. 14 days is conservative; tune via
  `[gate].freshness_days` in the manifest if a particular fallback
  needs a tighter SLA.
* **Why exclude Renovate from `compat/**` for the pinned package?**
  Renovate's whole job is to keep `next` current. On `compat/nextjs-15`
  that defeats the branch's purpose — we want it pinned at 15.x. The
  `renovate.json` `packageRules` block (see `renovate_policy.md`)
  excludes `compat/**` from `next` / `pydantic` upgrade PRs, but lets
  Renovate keep updating *every other* package on those branches. That
  way the branch stays evergreen without losing the pin.
* **Why a stdlib-only freshness probe?** Same self-defense argument as
  N5/N6/N7/N8: the gate runs *during a major framework upgrade PR*. If
  the probe imports the framework being upgraded, the gate breaks
  exactly when we need it most. `urllib.request` + `tomllib` (Python
  3.11+ stdlib) is enough to query the GitHub Actions API.
* **Why does setup-script bootstrap branches at master HEAD instead of
  retroactively pinning?** The codebase never had Next 15 — master has
  always been on 16. Retroactive pinning would mean writing a
  Next-16-to-15 downgrade commit blind. We instead create the branch as
  "tracking master + skip-globs" and let the **first weekly rebase**
  materialise the pin (if/when 16→17 happens). This makes the branch a
  policy artefact that's deployable today (because it = master) and
  becomes a fallback the moment master moves past it.

---

## Retirement

Each manifest has a `[retire]` section. Two conditions to drop the
branch:

* **`when_master_returns_to_track`** — if master ever pins back to the
  fallback's track (e.g. master rolls back to Next 15), the fallback
  becomes redundant. Delete the branch + manifest in the same PR.
* **`when_track_eol_announced`** — once Vercel / Pydantic announces
  EOL for the fallback's major, start a wind-down clock equal to
  `freshness_days * 4` (default 56 days), then retire. Past EOL the
  fallback is no longer a viable rollback target.

The retirement PR removes:

1. `.fallback/manifests/<branch>.toml`
2. The branch on origin (`git push origin --delete compat/<fw>-<N>`)
3. Any explicit references in `renovate_policy.md`
4. The corresponding shape-guard cases in
   `backend/tests/test_dependency_governance.py`

The `discover` job in `fallback-branches.yml` reads `.fallback/manifests/`
dynamically, so removing the manifest is sufficient to drop the branch
from CI. No workflow edit needed.
