# DigitalOcean App Platform deploy (L11 #338 #1)

One-click deploy to [DigitalOcean App Platform](https://www.digitalocean.com/products/app-platform):

[![Deploy to DO](https://www.deploytodo.com/do-btn-blue.svg)](https://cloud.digitalocean.com/apps/new?repo=https://github.com/limit5/OmniSight-Productizer/tree/master)

The button points the DO wizard at this repo; on first deploy the wizard
auto-detects `deploy/digitalocean/app.yaml` and provisions two services:

| Service    | Visibility | Port | Image source        | Health check         |
| ---------- | ---------- | ---- | ------------------- | -------------------- |
| `backend`  | private    | 8000 | `Dockerfile.backend`| `/api/v1/health`     |
| `frontend` | public `/` | 3000 | `Dockerfile.frontend` | `/`                |

## What you fill in post-deploy

The app.yaml ships with `EV[1:PLACEHOLDER:REPLACE_AFTER_DEPLOY]` sentinels
for every `type: SECRET` env. After the first (failing) boot:

1. **Settings → App-Level Environment Variables** — replace:
   - `OMNISIGHT_ANTHROPIC_API_KEY` (or whichever provider you picked)
   - `OMNISIGHT_ADMIN_PASSWORD`
2. **Settings → Domains** (optional) — add your custom domain; DO issues
   Let's Encrypt certs automatically.
3. Click **Deploy** again. `${APP_URL}` resolves to your public URL on
   the second deploy, which is when CORS (`OMNISIGHT_FRONTEND_ORIGIN`)
   actually starts accepting browser requests.

## Caveats

- **Ephemeral filesystem.** App Platform services don't persist files
  across deploys. The default SQLite DB at `/app/data/omnisight.db` is
  wiped on every redeploy. For durable storage, uncomment the
  `databases:` block at the bottom of `app.yaml` (adds ~$5/mo for the
  smallest Postgres) and teach the backend to read
  `OMNISIGHT_DATABASE_URL`. If you only need SQLite, use Railway or
  Render — both support persistent volumes on their free tiers.
- **No Docker-in-Docker.** App Platform doesn't expose the host Docker
  socket. The `ContainerManager` tool-sandbox path (cross-compilation
  + agent sandbox) will be inoperative. Single-tenant, tool-less demo
  works; full multi-agent runs need a VPS or droplet-based deploy.
- **Cost.** 2× `basic-xxs` ≈ $10/mo. Downsize to
  `instance_size_slug: apps-d-1vcpu-1gb` for dev-only; upsize to
  `professional-xs` for real load.

## Updating the spec

The `app.yaml` in this repo is a **seed template** — the SSOT after
first deploy is the live App spec in DO's control plane. To push local
edits:

```bash
doctl apps update <APP_ID> --spec deploy/digitalocean/app.yaml
```

See also: `deploy/railway/` (Railway) · `deploy/render/` (Render).
