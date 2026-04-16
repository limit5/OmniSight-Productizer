# SKILL-ASTRO Integration Guide

W8 #282. Third web skill — extends the W0-W5 framework onto the
content-heavy vertical (Astro 5 SSG-by-default, optional SSR,
Islands architecture) after W6 (SKILL-NEXTJS, React pilot) and W7
(SKILL-NUXT, Vue cross-stack validator).

## Render a project

```python
from pathlib import Path
from backend.astro_scaffolder import ScaffoldOptions, render_project

outcome = render_project(
    out_dir=Path("/tmp/acme-astro"),
    options=ScaffoldOptions(
        project_name="acme-astro",
        islands="react",        # or "vue" or "svelte" or "none"
        cms="sanity",           # or "contentful" or "none"
        target="all",           # or "static" / "node" / "vercel" / "cloudflare"
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

The output tree (with `target=all`, `islands=react`, `cms=sanity`,
`compliance=on`) contains:

```
acme-astro/
├── package.json
├── astro.config.mjs            (output + adapter pinned from target)
├── tsconfig.json
├── .env.example
├── .gitignore
├── src/
│   ├── env.d.ts
│   ├── content/
│   │   ├── config.ts           (Zod schema for blog collection)
│   │   └── blog/
│   │       └── hello-world.mdx (seed post)
│   ├── components/
│   │   ├── Counter.jsx         (React island, client:visible)
│   │   └── ConsentBanner.astro (compliance=on)
│   ├── layouts/
│   │   └── BaseLayout.astro    (semantic landmarks, meta, OG)
│   ├── pages/
│   │   ├── index.astro         (home — demonstrates <CollectionList>)
│   │   ├── about.astro
│   │   ├── rss.xml.ts          (RSS feed generator)
│   │   ├── blog/
│   │   │   └── [...slug].astro (dynamic content-collection route)
│   │   └── api/
│   │       ├── privacy/
│   │       │   └── erasure.ts  (compliance=on)
│   │       └── webhooks/
│   │           └── sanity.ts   (cms=sanity)
│   └── lib/
│       └── cms/
│           └── sanity.ts       (cms=sanity)
├── e2e/smoke.spec.ts
├── playwright.config.ts
├── vitest.config.ts
├── tests/unit/
│   ├── setup.ts
│   └── cms.test.ts             (cms != none)
├── docs/privacy/
│   ├── retention.md            (compliance=on)
│   └── dpa.md                  (compliance=on)
├── spdx.allowlist.json         (compliance=on)
├── Dockerfile                  (target includes static OR node)
├── vercel.json                 (target includes vercel)
└── wrangler.toml               (target includes cloudflare)
```

## Target selection

`astro.config.mjs` reads `process.env.ASTRO_TARGET` at build time
and falls back to the target pinned by the scaffolder when the env
var is unset. That means the same source tree builds for every
target without branching at runtime:

```js
// astro.config.mjs (excerpt)
const target = process.env.ASTRO_TARGET || "static"
```

To build for each target:

```bash
# Static (SSG export to dist/)
ASTRO_TARGET=static npm run build

# Node server (containers / PaaS)
ASTRO_TARGET=node npm run build

# Vercel (Build Output API)
ASTRO_TARGET=vercel npm run build

# Cloudflare Pages (V8 isolate)
ASTRO_TARGET=cloudflare npm run build
```

## W0-W5 framework bindings

| Framework gate             | How SKILL-ASTRO uses it                                      |
|----------------------------|---------------------------------------------------------------|
| W0 `target_kind: web`      | Scaffolder reads profile YAML via `backend.platform`          |
| W1 `web-static`            | 500 KiB critical-path budget enforced for static target       |
| W1 `web-ssr-node`          | 5 MiB server bundle for node adapter                          |
| W1 `web-vercel`            | `vercel.json` memory limit = profile's `memory_limit_mb`      |
| W1 `web-edge-cloudflare`   | `wrangler.toml` compat flags + 1 MiB edge ceiling             |
| W2 Lighthouse / Bundle     | Rendered site passes default thresholds when deployed         |
| W2 Playwright / Vitest     | Test skeletons ship alongside the site                        |
| W3 frontend-react/vue/svelte | Island hydration framework selected by knob                 |
| W3 a11y / seo / perf       | Landmarks, OG meta, sitemap, RSS baked into scaffolds         |
| W4 Vercel adapter          | `dry_run_deploy()` constructs + validates artifact            |
| W4 Cloudflare adapter      | Same, with account_id placeholder                             |
| W4 DockerNginx adapter     | For static (serve dist/ from nginx) + node targets            |
| W5 WCAG                    | axe-core scan passes on rendered project                      |
| W5 GDPR                    | retention.md / DPA / erasure endpoint shipped                 |
| W5 SPDX                    | allowlist.json narrows deny list                              |

## Content-vertical validation

```python
from backend.astro_scaffolder import pilot_report, ScaffoldOptions
report = pilot_report(Path("/tmp/acme-astro"), options=...)
assert report["skill"] == "skill-astro"
assert report["w5_compliance"]["failed_count"] == 0
```

If SKILL-ASTRO's `pilot_report()` returns a green bundle under
exactly the same contract SKILL-NEXTJS and SKILL-NUXT used, the W
framework survived its third consumer (and its first content-first
consumer) without framework-level changes — which promotes W0-W5
from "cross-stack framework" (n=2) to "cross-stack AND cross-shape
framework" (n=3).

## CMS adapter choice

- **`cms=sanity`** — ships `src/lib/cms/sanity.ts` + webhook route.
  Uses GROQ queries via `@sanity/client`. Preview mode reads
  `SANITY_PREVIEW_TOKEN` from env.
- **`cms=contentful`** — ships `src/lib/cms/contentful.ts` + webhook
  route. Uses the Contentful Delivery API with `contentful` SDK.
  Preview reads `CONTENTFUL_PREVIEW_TOKEN`.
- **`cms=none`** — content comes entirely from the local
  `src/content/blog/*.mdx` files; no external CMS dependency. The
  Astro idiom for this is a Zod-typed content collection, which the
  scaffold ships regardless of CMS choice.

The adapters are thin by design: they expose `fetchEntries(query)` +
`verifyWebhook(signature, body)`, and the Astro pages call those.
Swapping CMS means regenerating + reconnecting — the pages do not
encode CMS-specific assumptions.

## Why Astro 5 (and not 4)

Astro 5 is the current major at generation time. `package.json`
pins `"astro": "^5.0.0"`. If an operator needs Astro 4, they can
downshift that single semver pin — every other file in the tree is
written to APIs that are stable across Astro 4 and 5
(`defineCollection`, `getCollection`, `<Fragment slot>`,
`Astro.props`, content-layer API).
