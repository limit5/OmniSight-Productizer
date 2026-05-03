# SKILL-NEXTJS — W6 #280 (pilot)

First web-vertical skill pack. Generates a Next.js 16 App Router
project that exercises every W0-W5 capability the framework ships.

## Why this skill exists

Priority W built five scaffolding layers — platform profile schema
(W0), web profiles (W1), simulate-track (W2), role skills (W3), deploy
adapters (W4), and compliance gates (W5). On their own those layers
are a framework; they become load-bearing only after a real skill pack
consumes every one of them. SKILL-NEXTJS is that pack — same pilot
pattern D1 set for C5 and D29 set for C26.

## Outputs

A rendered project tree that:

- boots with `next dev` / `next dev --turbopack` without the
  workspace-root panic (we pin `turbopack.root` in the scaffold)
- passes `scripts/simulate.sh --type=web --web-profile=web-vercel`
- can render the FS.7.1 full-stack bundle: Next.js + Prisma +
  Auth.js + tRPC + Resend
- can render the FS.7.4 todo example app inside that full-stack
  bundle and keep W5 compliance green
- passes the three W5 compliance gates on a fresh render
- can be `provision()` + `deploy()`'d through `backend/deploy/vercel.py`
  or `backend/deploy/cloudflare_pages.py`

## Choice knobs

| Knob        | Values                 | Default       |
|-------------|------------------------|---------------|
| `auth`      | `nextauth` \| `clerk`  | `nextauth`    |
| `trpc`      | `on` \| `off`          | `off`         |
| `prisma`    | `on` \| `off`          | `off`         |
| `resend`    | `on` \| `off`          | `off`         |
| `target`    | `vercel` \| `cloudflare` \| `both` | `both` |
| `compliance`| `on` \| `off`          | `on`          |
| `example_app`| `none` \| `todo`      | `none`        |

See `configs/skills/skill_nextjs/tasks.yaml` for the DAG that each
knob routes through.

## How to render

```python
from backend.nextjs_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/my-app"),
    options=ScaffoldOptions(
        project_name="my-app",
        auth="nextauth",
        trpc=True,
        prisma=True,
        resend=True,
        target="both",
        compliance=True,
        example_app="todo",
    ),
)
```

## Framework gates covered

- **W0** platform profile schema — scaffold reads `web-vercel` /
  `web-edge-cloudflare` profiles, honours `bundle_size_budget`.
- **W1** web platform profiles — `vercel.json` + `wrangler.toml`
  generated from profile fields.
- **W2** simulate-track — Playwright + Vitest wired, Lighthouse-ready
  build output layout.
- **W3** role skills — project style honours `frontend-react` +
  `a11y` + `seo` + `perf` role prompts.
- **W4** deploy adapters — `nextjs_scaffolder.dry_run_deploy()`
  exercises Vercel and Cloudflare Pages adapters.
- **W5** compliance — WCAG / GDPR / SPDX stubs shipped; green on
  first render.
- **FS.7.1** full-stack bundle — Prisma schema/client helper,
  Auth.js credentials scaffold, tRPC root router, and Resend contact
  route render together behind explicit knobs.
- **FS.7.4** example app bundle — optional `/todos` route plus
  `TodoApp` Client Component and unit test render with the FS.7.1
  full-stack knobs and pass the same pilot compliance bundle.
