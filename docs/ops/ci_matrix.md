# Multi-version CI Matrix (N7)

> Source of truth for which interpreter / framework versions are
> exercised in CI, when each cell runs, and how its output reaches the
> operator. Implementation lives in
> [`.github/workflows/multi-version-matrix.yml`](../../.github/workflows/multi-version-matrix.yml).

## TL;DR

| Cell | Runs on | Status | Why |
|---|---|---|---|
| Python **3.12** | every PR (`ci.yml`) + nightly | **gate** | pinned production interpreter; matches `Dockerfile.backend` |
| Python **3.13** | nightly only (`multi-version-matrix.yml`) | **advisory** | next stable interpreter — surfaces deprecations months before we upgrade |
| Node **20.17.x** | every PR (`ci.yml`) + nightly | **gate** | pinned via `.nvmrc` and `package.json` `engines.node` |
| Node **22.x** | nightly only | **advisory** | next LTS — same rationale as Python 3.13 |
| FastAPI **pinned** (`requirements.txt`) | every PR + nightly | **gate** | the version we actually deploy |
| FastAPI **latest minor** | nightly only | **advisory** | pre-1.0 (minor = breaking by SemVer convention) — forecasts what the next pin bump will require |

PR latency is unchanged: the existing `ci.yml` keeps shipping the
single-version primary pipeline. The matrix workflow is **append-only
nightly**.

## Tier rationale

Two failure modes were possible, and the design picks one:

1. **Run the full matrix on every PR.** Maximises coverage but ~4×
   the wall-clock; advisory cells fail intermittently from upstream
   churn we can't fix in a PR; the red X conditions reviewers to
   ignore CI signal. **Rejected.**
2. **Layered: primary on PR, broad nightly.** PRs stay fast and
   deterministic. Forward-look failures land at most 24 h later, in a
   workflow whose entire point is "things that aren't blocking yet".
   This is what we ship.

Operators who need to verify a planned bump *now* (e.g. before
opening a PR that pins to Python 3.13) can dispatch the matrix
workflow manually:

```bash
gh workflow run "Multi-version CI Matrix"
```

## What gets surfaced

Every advisory cell pipes its captured pytest / vitest / tsc log
through [`scripts/surface_deprecations.py`](../../scripts/surface_deprecations.py).
The script:

- Emits one `::warning ...` GitHub Actions annotation per *unique*
  deprecation message (capped at 30 — full list always lands in the
  step summary). Annotations show up in the run page sidebar, the same
  place lint warnings appear, so they aren't buried in 5 000 lines of
  pytest noise.
- Appends a deduplicated table (count + message) to the per-job
  `GITHUB_STEP_SUMMARY` so the run summary tells the operator at a
  glance which package families are about to deprecate APIs we depend
  on.
- Always exits 0 — surfacing is advisory by design. The matrix itself
  is non-gating; only `ci.yml` blocks merges.

The script is **stdlib-only** for the same self-defense reason
[`scripts/upgrade_preview.py`](../../scripts/upgrade_preview.py) and
[`scripts/check_eol.py`](../../scripts/check_eol.py) are: a script that
runs as the last step of every matrix cell cannot itself be broken by
the dep upgrade it is trying to summarise.

Captured logs are uploaded as workflow artifacts with 14-day retention,
so when an advisory cell goes red the operator can pull the raw output
without re-running the matrix.

## How each cell installs deps

| Cell | Install command | Why |
|---|---|---|
| Python 3.12 (gate) | `pip install --require-hashes -r backend/requirements.txt` | identical to PR + production |
| Python 3.13 (advisory) | `pip install -r backend/requirements.in` | the hashed lockfile pins wheels to py3.12 ABI tags and would refuse to resolve on py3.13; the `lockfile-drift` job in `ci.yml` already gates `.in` ↔ `.txt` consistency, so installing from `.in` is safe here |
| Node 20.17 / 22 | `pnpm install --frozen-lockfile` with `npm_config_engine_strict=false` | Node 22 violates `engines.node "<21"`; engine-strict=false downgrades that to a warning (the matrix is advisory anyway) |
| FastAPI pinned | `pip install --require-hashes -r backend/requirements.txt` | identical to PR |
| FastAPI latest-minor | hashed install, then `pip install --upgrade --no-deps fastapi starlette` | upgrade only the layer under test; `--no-deps` keeps everything else hash-locked so we measure the FastAPI delta in isolation |

## When to act on a red advisory cell

The matrix going amber should never block a PR. Use it as planning
input:

- **Python 3.13 cell red** → file a follow-up tracking what blocks the
  3.13 bump. Don't change the PR pipeline until 3.13 is the planned
  next pin.
- **Node 22 cell red** → same — log it; the next `engines.node` bump
  PR is where the fix belongs.
- **FastAPI latest-minor cell red** → check whether the failures are
  starlette transitive breakage or our own code. If ours, file an
  N4-style issue (the LangChain firewall pattern); FastAPI minor
  bumps land via Renovate and we want to land them quickly.

## Related workflows

- [`ci.yml`](../../.github/workflows/ci.yml) — primary PR pipeline
  (single-version, gating).
- [`upgrade-preview.yml`](../../.github/workflows/upgrade-preview.yml)
  — N5 nightly Renovate preview.
- [`eol-check.yml`](../../.github/workflows/eol-check.yml) — N6 monthly
  EOL warnings; complements N7 by warning months before a forward-look
  cell goes from advisory to mandatory.
