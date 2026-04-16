# Framework Fallback Branches (N9)

> **Purpose**: declarative source-of-truth for OmniSight's framework
> fallback branches. Read by `.github/workflows/fallback-branches.yml`,
> `.github/workflows/major-upgrade-gate.yml`, `scripts/fallback_rebase.py`,
> and the shape-guards in `backend/tests/test_dependency_governance.py`.

## What lives in this directory

| File | Role |
|---|---|
| `README.md` (this file) | Human-readable policy summary. |
| `manifests/<branch>.toml` | One TOML file per fallback branch — the **only** place a branch's pin / scope / freshness window is declared. Tools and CI read it; reviewers edit it. |

## Why a declarative manifest (and not "just create the branch")

A long-running fallback branch is a **policy artefact**, not a snapshot.
The branch name (`compat/nextjs-15`) tells you the major version it
holds; the manifest tells you everything else:

* Which **upstream commit** master must be ahead of for the manifest to
  still apply (so a stale fallback whose target framework has been long
  deprecated is detectable from CI alone).
* Which **paths to skip** when weekly rebase pulls master commits in
  (any path under `frontend/` that re-imports a Next 16-only API would
  break the Next 15 branch — the rebase tool reads the skip glob from
  here).
* Which **package pins** define "this branch is still on the fallback
  version" (used by the freshness gate: if `compat/nextjs-15` has been
  rebased to a master commit that bumped `next` to 16.x, the gate
  fails).
* Which **upstream major** triggers the fallback's relevance window
  (`compat/nextjs-15` is meaningful only while master sits on 16.x or
  17.x; if master ever rolls back to 15.x, this branch retires).

Without the manifest, every change to the fallback policy would require
editing three files (workflow / rebase tool / tests) in lockstep. The
manifest collapses that into one.

## Lifecycle

```
master pinned at framework v[N]
        │
        │  N9 setup (one-shot)
        ▼
fallback branch compat/<framework>-<N-1> created
   (manifest declares it pins framework <N-1>.x)
        │
        │  weekly cron rebases non-framework commits in
        ▼
fallback branch stays evergreen, CI green
        │
        │  major upgrade PR for framework v[N+1] opens on master
        ▼
major-upgrade-gate workflow checks: fallback branch's last green CI ≤ 14 days?
        │ yes              │ no
        ▼                  ▼
PR allowed to merge    PR blocked — operator runs scripts/fallback_rebase.py first
        │
        │  master ships v[N+1]
        ▼
if production explodes:
   * checkout fallback tag (`compat/<framework>-<N-1>@latest-green`)
   * redeploy from that ref
   * triage forward-fix on master at leisure
```

## Why **next 15** + **pydantic v2** are the first two

* **Next.js 15** — master is on 16.x today; 17 is the next breaking
  major. App Router and middleware semantics have a history of breaking
  cross-major; a green Next 15 fallback is the cheapest insurance.
* **Pydantic v2** — master is on 2.11.x today; v3 will land. The v1→v2
  migration was a 6-month industry-wide pain event. Declaring the
  fallback **before** v3 ships means the day v3 is announced we already
  have a working v2 branch we can keep alive while the v3 PR cooks.

Both manifests are committed today even though Pydantic v3 hasn't
shipped — see *Lifecycle* above for why pre-declaration matters.

## Operator handoff

The setup script `scripts/fallback_setup.sh` materialises the local
branches the first time it's run. Pushing them to `origin` is a
one-shot operator action (requires push credentials):

```bash
bash scripts/fallback_setup.sh        # creates local branches
git push -u origin compat/nextjs-15
git push -u origin compat/pydantic-v2
```

After the initial push, weekly maintenance is fully driven by the
workflow + rebase script — operators only re-touch the branches when
the gate fails or Renovate flags a fallback drift.

See `docs/ops/fallback_branches.md` for the full SOP.
