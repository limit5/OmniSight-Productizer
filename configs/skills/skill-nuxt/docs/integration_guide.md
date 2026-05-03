# SKILL-NUXT Integration Guide

W7 #281. Second web skill — re-validates the W0-W5 framework on a
Vue 3 / Nitro stack after W6 SKILL-NEXTJS proved it on React.

## Render a project

```python
from pathlib import Path
from backend.nuxt_scaffolder import ScaffoldOptions, render_project

outcome = render_project(
    out_dir=Path("/tmp/acme-nuxt"),
    options=ScaffoldOptions(
        project_name="acme-nuxt",
        auth="sidebase",        # or "clerk" or "none"
        pinia=True,
        drizzle=True,
        postmark=True,
        target="all",           # node + vercel + cloudflare + bun
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

The output tree (with `target=all`, `pinia=on`, `auth=sidebase`,
`compliance=on`) contains:

```
acme-nuxt/
├── package.json
├── nuxt.config.ts            (nitro.preset pinned from target)
├── tsconfig.json
├── .env.example
├── .gitignore
├── app.vue                   (<NuxtPage> shell)
├── pages/
│   ├── index.vue             (home — demonstrates useFetch)
│   └── about.vue
├── layouts/
│   └── default.vue
├── components/
│   ├── Counter.vue           (Composition API, <script setup>)
│   └── consent/CookieBanner.vue    (compliance=on)
├── composables/
│   └── useBackend.ts         (typed $fetch wrapper)
├── drizzle/
│   └── schema.ts             (drizzle=on)
├── stores/
│   └── counter.ts            (pinia=on)
├── server/
│   └── api/
│       ├── health.get.ts
│       ├── contact.post.ts   (postmark=on)
│       ├── v1/
│       │   └── [...slug].ts  (backend proxy)
│       └── privacy/
│           └── erasure.post.ts    (compliance=on)
│   ├── db.ts                 (drizzle=on)
│   └── email.ts              (postmark=on)
├── middleware/
│   └── auth.global.ts        (auth=sidebase)
├── auth/
│   ├── nuxt-auth.config.ts   (auth=sidebase)
│   └── clerk.example.vue     (auth=clerk)
├── e2e/smoke.spec.ts
├── playwright.config.ts
├── vitest.config.ts
├── tests/unit/
│   ├── setup.ts
│   └── counter.test.ts
├── docs/privacy/
│   ├── retention.md          (compliance=on)
│   └── dpa.md                (compliance=on)
├── spdx.allowlist.json       (compliance=on)
├── Dockerfile                (target includes node OR bun)
├── vercel.json               (target includes vercel)
├── wrangler.toml             (target includes cloudflare)
└── bunfig.toml               (target includes bun)
```

## Nitro preset selection

`nuxt.config.ts` reads `process.env.NITRO_PRESET` at build time and
falls back to the preset pinned by the scaffolder when the env var
is unset. That means the same source tree serves all four presets
without branching at runtime:

```ts
// nuxt.config.ts (excerpt)
export default defineNuxtConfig({
  nitro: {
    preset: process.env.NITRO_PRESET || "node-server",
  },
})
```

To build for each target:

```bash
# Node server (containers / PaaS)
NITRO_PRESET=node-server npm run build

# Vercel (Build Output API)
NITRO_PRESET=vercel npm run build

# Cloudflare Pages (V8 isolate)
NITRO_PRESET=cloudflare-pages npm run build

# Bun runtime
NITRO_PRESET=bun npm run build
```

## W0-W5 framework bindings

| Framework gate             | How SKILL-NUXT uses it                                        |
|----------------------------|----------------------------------------------------------------|
| W0 `target_kind: web`      | Scaffolder reads profile YAML via `backend.platform`           |
| W1 `web-ssr-node`          | 5 MiB server-bundle ceiling enforced for node/bun presets      |
| W1 `web-vercel`            | `vercel.json` memory limit = profile's `memory_limit_mb`       |
| W1 `web-edge-cloudflare`   | `wrangler.toml` compat flags + 1 MiB edge ceiling              |
| W2 Lighthouse / Bundle     | Rendered app passes default thresholds when deployed           |
| W2 Playwright / Vitest     | Test skeletons ship alongside the app                          |
| W3 frontend-vue            | `<script setup>`, Composition API, auto-imports, no fetch in setup |
| W3 a11y / seo / perf       | Landmarks, meta, NuxtImage baked into scaffolds                |
| W4 Vercel adapter          | `dry_run_deploy()` constructs + validates artifact             |
| W4 Cloudflare adapter      | Same, with account_id placeholder                              |
| W4 DockerNginx adapter     | Used when `target` includes `node` or `bun` (container path)   |
| W5 WCAG                    | axe-core scan passes on rendered project                       |
| W5 GDPR                    | retention.md / DPA / erasure Nitro route shipped               |
| W5 SPDX                    | allowlist.json narrows deny list                               |
| FS.7.2 bundle              | Drizzle schema/client + sidebase auth + Postmark contact route |

## Cross-stack validation

```python
from backend.nuxt_scaffolder import pilot_report, ScaffoldOptions
report = pilot_report(Path("/tmp/acme-nuxt"), options=...)
assert report["skill"] == "skill-nuxt"
assert report["w5_compliance"]["failed_count"] == 0
```

If SKILL-NUXT's `pilot_report()` returns a green bundle under exactly
the same contract SKILL-NEXTJS used, the W framework survived its
second consumer without framework-level changes — which is the
practical bar for promoting W0-W5 from "pilot-backed convention" to
"load-bearing cross-stack framework".

## Why Nuxt 4 (and not 3)

Nuxt 4 is the version the framework ships as the current major at
generation time. The scaffold pins `"nuxt": "^4.0.0"` in
`package.json`. If an operator needs Nuxt 3 instead, they can
downshift that single semver pin — every other file in the tree is
written to APIs (`defineNuxtConfig`, `<script setup>`, `useFetch`,
`definePageMeta`) that are stable across Nuxt 3 and 4.
