# Railway deploy (L11 #338 #2)

One-click deploy to [Railway](https://railway.com):

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new/template?template=https%3A%2F%2Fgithub.com%2Flimit5%2FOmniSight-Productizer)

The button sends the Railway template wizard at this repo. Because Railway's
`railway.json` schema is **single-service** (top-level `build` + `deploy`
only — no `services` block), a full OmniSight deploy needs **two** Railway
services wired together. `deploy/railway/railway.json` configures the
backend (the tricky one with the hardcoded :8000 in `Dockerfile.backend`);
the frontend uses Railway's Dockerfile auto-detect with sensible defaults.

## Topology

| Service    | Config file                             | Dockerfile           | Listens on    | Health check      |
| ---------- | --------------------------------------- | -------------------- | ------------- | ----------------- |
| `backend`  | `deploy/railway/railway.json`           | `Dockerfile.backend` | `$PORT`       | `/api/v1/health`  |
| `frontend` | (none — Railway defaults)               | `Dockerfile.frontend`| `$PORT` (Next standalone auto-reads) | `/` (implicit) |

The backend's `deploy.startCommand` in `railway.json` overrides the
Dockerfile `CMD` so uvicorn binds Railway's injected `$PORT` instead of
the hardcoded `8000` that docker-compose relies on. `OMNISIGHT_WORKERS`
is still honored with a default of 2.

## Post-deploy steps

After clicking the button the wizard will create the project but NOT
the two services — Railway's template flow is repo-scoped, not
multi-service. From the project dashboard:

1. **Add the backend service**
   - **New → GitHub Repo → `limit5/OmniSight-Productizer`**.
   - **Settings → Source → Config-as-Code Path** = `deploy/railway/railway.json`.
   - **Settings → Networking** — enable **Public Networking** so the
     browser can reach the API through Next.js rewrites (Railway will
     mint a `*.up.railway.app` hostname).

2. **Add the frontend service**
   - **New → GitHub Repo → same repo**.
   - **Settings → Source → Root Directory** = `/` (default).
   - **Settings → Build → Custom Dockerfile Path** = `Dockerfile.frontend`.
   - **Settings → Networking** — enable **Public Networking**.

3. **Fill required Variables on each service** (see matrix below).

4. **Trigger a new deploy** on both services so the reference
   variables (`${{ ... }}` tokens below) resolve.

## Required env vars

Set these under **Service → Variables** in the Railway dashboard — they
cannot be declared in `railway.json` (Railway config-as-code has no env
schema; it is deploy-policy only).

### Backend service

| Variable                          | Value                                              | Why |
| --------------------------------- | -------------------------------------------------- | --- |
| `OMNISIGHT_DEBUG`                 | `false`                                            | `.env.example` production hard-pin |
| `OMNISIGHT_AUTH_MODE`             | `strict`                                           | `.env.example` production hard-pin |
| `OMNISIGHT_COOKIE_SECURE`         | `true`                                             | `.env.example` production hard-pin |
| `OMNISIGHT_ENV`                   | `production`                                       | |
| `OMNISIGHT_DATABASE_PATH`         | `/app/data/omnisight.db`                           | See ephemeral-FS caveat below |
| `OMNISIGHT_WORKERS`               | `2`                                                | Matches `basic` tier sizing |
| `OMNISIGHT_FRONTEND_ORIGIN`       | `https://${{frontend.RAILWAY_PUBLIC_DOMAIN}}`      | CORS; Railway resolves the reference at runtime |
| `OMNISIGHT_LLM_PROVIDER`          | `anthropic` (or another supported provider)        | |
| `OMNISIGHT_ANTHROPIC_API_KEY` 🔒  | `sk-ant-...`                                       | Secret — paste your real key |
| `OMNISIGHT_ADMIN_EMAIL`           | `admin@example.com`                                | K1 bootstrap |
| `OMNISIGHT_ADMIN_PASSWORD` 🔒     | `<strong password>`                                | K1 bootstrap — operator must change on first login |

### Frontend service

| Variable                          | Value                                              | Why |
| --------------------------------- | -------------------------------------------------- | --- |
| `NODE_ENV`                        | `production`                                       | |
| `BACKEND_URL`                     | `http://${{backend.RAILWAY_PRIVATE_DOMAIN}}:${{backend.PORT}}` | Next.js `rewrites()` proxy target — uses Railway private networking (IPv6) |
| `NEXT_PUBLIC_API_URL`             | `https://${{RAILWAY_PUBLIC_DOMAIN}}`               | Same-origin; the browser calls `/api/v1/*` and Next.js rewrites to backend |

`${{ backend.RAILWAY_PRIVATE_DOMAIN }}` is Railway's private-networking
placeholder (IPv6 hostname like `backend.railway.internal`). It only
resolves inside the Railway VPC, which is what we want — the backend
must not be hit directly from the browser.

## Caveats

- **Ephemeral filesystem.** Railway service filesystems reset on every
  deploy. The default SQLite DB at `/app/data/omnisight.db` disappears
  when you push a new image. For durable storage, either:
  - attach a **Railway Volume** to the backend service mounted at
    `/app/data` (Settings → Volumes; ~$0.25/GB/month at the time of
    writing), or
  - provision a **Postgres** plugin and teach the backend to read
    `OMNISIGHT_DATABASE_URL` (SQLAlchemy URL).
- **No Docker-in-Docker.** Railway does not expose the host Docker
  socket. The `ContainerManager` agent sandbox path is inoperative —
  Railway is fine for the single-tenant tool-less demo, but full
  multi-agent runs need a VPS-style host.
- **Cost.** Railway's free tier is $5/month execution credit (≈500
  hours of a basic `hobby` service). Two services ×
  `0.5 vCPU / 512 MB` easily fit the tier for a demo; scale up via
  **Settings → Deploy → Resources**.
- **Healthcheck scope is deploy-only.** Railway's `healthcheckPath` is
  consulted during deploy rollout (to decide when a new revision is
  healthy). It is not a periodic liveness probe — Railway's platform
  layer handles crash-restart via the `restartPolicy` already set.

## Updating the spec

`deploy/railway/railway.json` is the SSOT for the backend service's
deploy policy. Changes take effect on the next push. You can also push
the spec imperatively:

```bash
railway up --detach                                  # full redeploy
railway variables --set OMNISIGHT_WORKERS=4          # env update
```

See also: `deploy/digitalocean/` (DigitalOcean App Platform) ·
`deploy/render/` (Render — pending).
