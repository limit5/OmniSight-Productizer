# SKILL-NUXT — W7 #281

Second web-vertical skill pack. Generates a Nuxt 4 project whose
single source tree ships to four Nitro presets (node-server / vercel
/ cloudflare-pages / bun), re-validating the W0-W5 framework on a
non-React stack so we can claim the framework is stack-agnostic.

## Why this skill exists

SKILL-NEXTJS (W6 #280) was the pilot — any bug in the W0-W5 layers
surfaced there. SKILL-NUXT is the *confirmation* skill. A framework
that only works for a single consumer is not a framework. By
rendering a Vue 3 / Nitro tree through the same scaffolder contract,
the same profile dispatch, the same W4 adapter smoke, and the same
W5 compliance bundle, we prove the abstractions hold across stacks.

## Outputs

A rendered project tree that:

- boots with `nuxt dev` (Nitro dev server, Vite HMR)
- passes `scripts/simulate.sh --type=web --web-profile=web-ssr-node`
  for node-server target, and the corresponding profiles for the
  other presets
- passes the three W5 compliance gates on a fresh render
- can be `provision()` + `deploy()`'d through `backend/deploy/*.py`
  for whichever target(s) the render selected

## Choice knobs

| Knob         | Values                                    | Default   |
|--------------|-------------------------------------------|-----------|
| `auth`       | `sidebase` \| `clerk` \| `none`           | `sidebase`|
| `pinia`      | `on` \| `off`                             | `on`      |
| `target`     | `node` \| `vercel` \| `cloudflare` \| `bun` \| `all` | `all` |
| `compliance` | `on` \| `off`                             | `on`      |

`sidebase` is [sidebase/nuxt-auth](https://sidebase.io/nuxt-auth) —
the Vue/Nuxt equivalent of next-auth for SKILL-NEXTJS. `clerk` is
the same [@clerk/nuxt](https://clerk.com/docs/references/nuxt)
module for cross-stack parity.

See `configs/skills/skill-nuxt/tasks.yaml` for the DAG that each
knob routes through.

## How to render

```python
from pathlib import Path
from backend.nuxt_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/my-nuxt-app"),
    options=ScaffoldOptions(
        project_name="my-nuxt-app",
        auth="sidebase",
        pinia=True,
        target="all",
        compliance=True,
    ),
)
```

## Nitro preset ↔ W1 profile mapping

| `target` value | Nitro preset       | W1 profile              |
|----------------|--------------------|-------------------------|
| `node`         | `node-server`      | `web-ssr-node`          |
| `vercel`       | `vercel`           | `web-vercel`            |
| `cloudflare`   | `cloudflare-pages` | `web-edge-cloudflare`   |
| `bun`          | `bun`              | `web-ssr-node`          |
| `all`          | all of the above   | all three profiles      |

Bun reuses `web-ssr-node` because, from the W1 budget perspective,
Bun is a long-running server runtime with the same 5 MiB server
bundle ceiling; the difference from Node is runtime flags, not
platform invariants.

## Framework gates covered

- **W0** platform profile schema — scaffolder reads `web-ssr-node` /
  `web-vercel` / `web-edge-cloudflare` profiles, honours
  `bundle_size_budget` and `memory_limit_mb`.
- **W1** web platform profiles — `nuxt.config.ts` embeds the resolved
  Nitro preset(s); `vercel.json` + `wrangler.toml` generated from
  profile fields for targets that need them.
- **W2** simulate-track — Playwright + Vitest wired, Nuxt build
  output layout aligned with Lighthouse / bundle gate expectations.
- **W3** role skills — project style honours `frontend-vue` + `a11y`
  + `seo` + `perf` role prompts (Composition API, `<script setup>`,
  auto-imported composables, proper landmarks).
- **W4** deploy adapters — `nuxt_scaffolder.dry_run_deploy()`
  exercises VercelAdapter / CloudflarePagesAdapter / DockerNginxAdapter.
- **W5** compliance — WCAG / GDPR / SPDX stubs shipped; green on
  first render.
