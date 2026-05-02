# SKILL-ASTRO — W8 #282

Third web-vertical skill pack. Generates an Astro 5 project whose
single source tree ships to four targets (static / @astrojs/node /
@astrojs/vercel / @astrojs/cloudflare), with Islands architecture
+ MDX + optional Sanity / Contentful CMS source adapters — the
"content-heavy" vertical that W6 (SKILL-NEXTJS, pilot) and W7
(SKILL-NUXT, cross-stack) did not address.

## Why this skill exists

- W6 SKILL-NEXTJS established the W0-W5 pilot contract (n=1).
- W7 SKILL-NUXT showed the contract is not React-specific (n=2).
- **W8 SKILL-ASTRO shows the contract is not whole-app-framework-
  specific either** (n=3). Astro is SSG-by-default with opt-in SSR,
  its "islands" hydration model is unlike the Next/Nuxt full-app
  hydration model, and its first-class CMS integration story
  belongs to a different class of site than the siblings (marketing,
  docs, blog, e-commerce catalogue).

If the same ScaffoldOptions / render_project / dry_run_deploy /
pilot_report API that covers React and Vue also covers a
content-first static generator with optional islands, the W0-W5
framework is not "the framework of one or two stacks" but
genuinely stack-agnostic for the web vertical.

It can render the **FS.7.3** bundle: Astro + typed content
collections + Sanity, using `islands=react`, `cms=sanity`,
`target=all`, and compliance gates on.

## Outputs

A rendered project tree that:

- boots with `astro dev` (Vite HMR, islands auto-configured)
- passes `scripts/simulate.sh --type=web` with the right web-*
  profile for whichever target was selected
- passes the three W5 compliance gates on a fresh render
- can be `provision()` + `deploy()`'d through `backend/deploy/*.py`
  for whichever target(s) the render selected

## Choice knobs

| Knob         | Values                                              | Default   |
|--------------|-----------------------------------------------------|-----------|
| `islands`    | `react` \| `vue` \| `svelte` \| `none`              | `react`   |
| `cms`        | `sanity` \| `contentful` \| `none`                  | `none`    |
| `target`     | `static` \| `node` \| `vercel` \| `cloudflare` \| `all` | `static` |
| `compliance` | `on` \| `off`                                       | `on`      |

`islands=none` produces a pure-static site — no client-side JS at
all beyond what MDX / Astro emits intrinsically. `cms=none` ships
the tree with content-collections reading local `.mdx` files (the
Astro-idiomatic starting point); `sanity` / `contentful` additionally
emit the CMS source adapter under `src/lib/cms/` and a webhook route
for on-publish revalidation.

See `configs/skills/skill-astro/tasks.yaml` for the DAG that each
knob routes through.

## How to render

```python
from pathlib import Path
from backend.astro_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/my-astro-site"),
    options=ScaffoldOptions(
        project_name="my-astro-site",
        islands="react",
        cms="sanity",
        target="all",
        compliance=True,
    ),
)
```

## Target ↔ W1 profile mapping

| `target` value | Astro output mode | Adapter              | W1 profile              |
|----------------|-------------------|----------------------|-------------------------|
| `static`       | `static`          | *(none)*             | `web-static`            |
| `node`         | `server`          | `@astrojs/node`      | `web-ssr-node`          |
| `vercel`       | `server`          | `@astrojs/vercel`    | `web-vercel`            |
| `cloudflare`   | `server`          | `@astrojs/cloudflare`| `web-edge-cloudflare`   |
| `all`          | defaults to `static` | (adapters shipped, not active) | all four profiles |

The "all" render keeps the source tree shippable to every target by
gating the adapter selection on `process.env.ASTRO_TARGET` in
`astro.config.mjs` — same pattern W7's `NITRO_PRESET` pivot uses.

## Framework gates covered

- **W0** platform profile schema — scaffolder reads `web-static` /
  `web-ssr-node` / `web-vercel` / `web-edge-cloudflare` profiles,
  honours `bundle_size_budget` and `memory_limit_mb`.
- **W1** web platform profiles — `astro.config.mjs` embeds the
  resolved output mode + adapter for each target; `vercel.json` +
  `wrangler.toml` generated from profile fields for targets that
  need them.
- **W2** simulate-track — Playwright + Vitest wired, Astro build
  output layout aligned with Lighthouse / bundle gate expectations.
- **W3** role skills — project style honours
  `frontend-{react,vue,svelte}` + `a11y` + `seo` + `perf` role
  prompts (islands only where needed, semantic landmarks, Open
  Graph / RSS / sitemap).
- **W4** deploy adapters — `astro_scaffolder.dry_run_deploy()`
  exercises VercelAdapter / CloudflarePagesAdapter /
  DockerNginxAdapter. `static` dry-runs DockerNginxAdapter as a
  "serve from nginx" reference — the W4 family's static-site path.
- **W5** compliance — WCAG / GDPR / SPDX stubs shipped; green on
  first render.
- **FS.7.3** full-stack bundle — content collection config + seed
  MDX, Sanity source adapter, Sanity webhook route, CMS unit test,
  and all selected target configs in one contract render.

## Anti-patterns the scaffold bakes in

Astro-specific:

- Do NOT hydrate an island with `client:load` unless you actually
  need synchronous hydration — the scaffold defaults to
  `client:visible` so islands only hydrate when they enter the
  viewport (and never on the `/about` / docs pages that don't
  include them).
- Do NOT call `Astro.fetch()` from a `.mdx` frontmatter block — MDX
  runs at build time in SSG mode, not request time; fetch in the
  surrounding `.astro` page instead.
- Do NOT commit CMS API tokens to `.env` — the scaffold's
  `.env.example` enumerates them but the real values belong in the
  host's secret store (Vercel env vars / Cloudflare Pages secrets /
  Docker env file).
