# SKILL-NEXTJS Integration Guide

W6 #280. Produced by the pilot skill that validates the W0-W5 framework.

## Render a project

```python
from pathlib import Path
from backend.nextjs_scaffolder import ScaffoldOptions, render_project

outcome = render_project(
    out_dir=Path("/tmp/acme-portal"),
    options=ScaffoldOptions(
        project_name="acme-portal",
        auth="nextauth",       # or "clerk"
        trpc=True,
        target="both",         # vercel + cloudflare
        compliance=True,
    ),
)
print(f"Rendered {len(outcome.files_written)} files, {outcome.bytes_written} bytes")
```

The output tree contains:

```
acme-portal/
├── package.json          (from package.json.j2)
├── next.config.mjs       (turbopack.root pinned — important!)
├── tsconfig.json
├── .env.example
├── .gitignore
├── app/
│   ├── layout.tsx
│   ├── page.tsx          (Server Component)
│   ├── globals.css
│   ├── actions.ts        ("use server")
│   ├── api/
│   │   ├── health/route.ts
│   │   ├── v1/[...slug]/route.ts   (backend proxy)
│   │   ├── auth/[...nextauth]/route.ts  (if auth=nextauth)
│   │   └── trpc/[trpc]/route.ts         (if trpc)
│   └── privacy/erasure/route.ts    (compliance=on)
├── components/
│   ├── Counter.tsx       (Client Component)
│   └── consent/CookieBanner.tsx    (compliance=on)
├── auth/
│   ├── nextauth.config.ts          (auth=nextauth)
│   ├── middleware.nextauth.ts      (auth=nextauth)
│   ├── clerk.middleware.ts         (auth=clerk)
│   └── clerk.example.tsx           (auth=clerk)
├── server/
│   ├── trpc.ts                     (trpc=on)
│   └── trpc.client.tsx             (trpc=on)
├── e2e/smoke.spec.ts
├── playwright.config.ts
├── vitest.config.ts
├── tests/unit/
│   ├── setup.ts
│   └── counter.test.tsx
├── docs/privacy/
│   ├── retention.md                (compliance=on)
│   └── dpa.md                      (compliance=on)
├── spdx.allowlist.json             (compliance=on)
├── vercel.json                     (target includes vercel)
└── wrangler.toml                   (target includes cloudflare)
```

## Why `turbopack.root` matters

Next 16 introduced a workspace-root detector for Turbopack that, when
the generated project sits inside a larger monorepo, panics with
`workspace root is ambiguous`. OmniSight itself hit this in its own
`next.config.mjs` — we pin `turbopack.root = __dirname` at the project
root to suppress the detector. Every SKILL-NEXTJS render inherits the
fix; do **not** delete the block without re-reproducing the panic first.

## W0-W5 framework bindings

| Framework gate         | How SKILL-NEXTJS uses it                                   |
|------------------------|-------------------------------------------------------------|
| W0 `target_kind: web`  | Scaffolder reads the profile YAML via `backend.platform`    |
| W1 `web-vercel`        | `vercel.json` memory limit = profile's `memory_limit_mb`    |
| W1 `web-edge-cloudflare` | `wrangler.toml` compat flags + 1 MiB edge ceiling          |
| W2 Lighthouse / Bundle | Rendered app passes default thresholds when deployed        |
| W2 Playwright / Vitest | Test skeletons ship alongside the app                       |
| W3 frontend-react      | Server/Client split + no `useEffect` data fetching          |
| W3 a11y / seo / perf   | Labels, landmarks, meta tags baked into scaffolds           |
| W4 Vercel adapter      | `dry_run_deploy()` constructs + validates artifact          |
| W4 Cloudflare adapter  | Same, with account_id placeholder                           |
| W5 WCAG                | axe-core scan passes on rendered project                    |
| W5 GDPR                | retention.md / DPA / erasure handler shipped                |
| W5 SPDX                | allowlist.json narrows deny list                            |

## Pilot validation

```python
from backend.nextjs_scaffolder import pilot_report, ScaffoldOptions
report = pilot_report(Path("/tmp/acme-portal"), options=...)
assert report["w5_compliance"]["passed"]
```

If `pilot_report()` returns `w5_compliance.passed == True` and all
entries under `w4_deploy.*.artifact_valid == True`, the W0-W5
framework has been validated end-to-end, which is the bar set by
D1 SKILL-UVC on C5 and D29 SKILL-HMI-WEBUI on C26.
