# Render deploy (L11 #338 #3)

One-click deploy to [Render](https://render.com):

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/limit5/OmniSight-Productizer)

The button sends Render's Blueprint wizard at this repo; it auto-detects
`render.yaml` at the project root, prompts for the unset `sync: false`
secrets, and provisions **two** services wired together. Because this
spec lives under `deploy/render/` (not at the repo root), the Render
dashboard may ask you to confirm the Blueprint path on first import —
point it at `deploy/render/render.yaml`.

## Topology

| Service              | Type    | Dockerfile             | Port    | Health check      | Plan      |
| -------------------- | ------- | ---------------------- | ------- | ----------------- | --------- |
| `omnisight-backend`  | `pserv` | `Dockerfile.backend`   | `8000`  | `/api/v1/health`  | `starter` |
| `omnisight-frontend` | `web`   | `Dockerfile.frontend`  | `$PORT` | `/`               | `starter` |

The backend is a **private service** (`pserv`) — only the frontend can
reach it, via the internal hostname `omnisight-backend:8000`. The frontend
is the only internet-facing service; browser `/api/v1/*` calls get
proxied to the backend through Next.js rewrites (see `next.config.mjs`
+ `BACKEND_URL` below).

## Post-deploy steps

Render's one-click Blueprint flow does most of the wiring, but three
values need human attention because Render Blueprint env vars don't
support string-templating across services.

### Stage 1 — First deploy (fill secrets)

When you click **Apply Blueprint** the dashboard shows every `sync: false`
variable and asks for a value. Fill these:

| Variable                          | Stage  | Where / notes                                                                |
| --------------------------------- | ------ | ---------------------------------------------------------------------------- |
| `OMNISIGHT_ANTHROPIC_API_KEY` 🔒  | backend | Paste your real `sk-ant-...` key (or use `OMNISIGHT_OPENAI_API_KEY` / `OMNISIGHT_GOOGLE_API_KEY` depending on `OMNISIGHT_LLM_PROVIDER`) |
| `OMNISIGHT_ADMIN_PASSWORD` 🔒     | backend | ≥ 12 chars. K1's `must_change_password` flag forces a rotation on first login — still don't reuse a personal password |
| `OMNISIGHT_FRONTEND_ORIGIN`       | backend | Leave blank for now — we fill it in Stage 2                                  |
| `NEXT_PUBLIC_API_URL`             | frontend | Leave blank for now — we fill it in Stage 2                                 |

Click **Deploy**. The frontend will come up first (Next.js rewrites have
`BACKEND_URL` baked in from the Blueprint), backend will come up second.
The CORS-gated endpoints will return 403 on cross-origin calls until
Stage 2 — this is expected.

### Stage 2 — Note the assigned URL + re-deploy

1. In the dashboard open `omnisight-frontend` and copy the
   `*.onrender.com` URL Render assigned (top of the service page). For
   example: `https://omnisight-frontend.onrender.com`.

2. On the **backend** service → **Environment** tab, set:

       OMNISIGHT_FRONTEND_ORIGIN = https://omnisight-frontend.onrender.com

3. On the **frontend** service → **Environment** tab, set:

       NEXT_PUBLIC_API_URL = https://omnisight-frontend.onrender.com

   (Same-origin — the browser calls `/api/v1/*` on the frontend host and
   Next.js rewrites proxy to the backend via `BACKEND_URL`.)

4. Click **Manual Deploy → Deploy latest commit** on both services so
   the new env vars take effect. Frontend **must** rebuild because
   `NEXT_PUBLIC_*` is inlined into the client JS bundle at build time.

Your demo is live at the frontend's `onrender.com` URL. K1's bootstrap
admin seed will create an account from `OMNISIGHT_ADMIN_EMAIL` +
`OMNISIGHT_ADMIN_PASSWORD` on first boot; log in and the
`must_change_password` flag will force a rotation.

## Full env matrix

Values in the **Source** column marked `spec` are pinned in
`render.yaml` and applied automatically on Blueprint apply. Values
marked `prompt` are `sync: false` — Render will ask the operator for
each during the Blueprint-apply wizard.

### Backend (`omnisight-backend`)

| Variable                         | Value                                | Source | Why |
| -------------------------------- | ------------------------------------ | ------ | --- |
| `OMNISIGHT_DEBUG`                | `false`                              | spec   | `.env.example` production hard-pin |
| `OMNISIGHT_AUTH_MODE`            | `strict`                             | spec   | `.env.example` production hard-pin |
| `OMNISIGHT_COOKIE_SECURE`        | `true`                               | spec   | `.env.example` production hard-pin |
| `OMNISIGHT_ENV`                  | `production`                         | spec   | |
| `OMNISIGHT_DATABASE_PATH`        | `/var/data/omnisight.db`             | spec   | Persistent disk mount |
| `OMNISIGHT_WORKERS`              | `2`                                  | spec   | Matches `starter` tier sizing |
| `OMNISIGHT_FRONTEND_ORIGIN`      | `https://omnisight-frontend.onrender.com` (actual URL after Stage 2) | prompt | CORS origin — can't be templated from the Blueprint |
| `OMNISIGHT_LLM_PROVIDER`         | `anthropic`                          | spec   | |
| `OMNISIGHT_ANTHROPIC_API_KEY` 🔒 | `sk-ant-...`                         | prompt | Secret — Render prompts on Blueprint apply |
| `OMNISIGHT_OPENAI_API_KEY` 🔒    | (if switching provider)              | prompt | Secret |
| `OMNISIGHT_GOOGLE_API_KEY` 🔒    | (if switching provider)              | prompt | Secret |
| `OMNISIGHT_ADMIN_EMAIL`          | `admin@example.com`                  | spec   | K1 bootstrap admin email |
| `OMNISIGHT_ADMIN_PASSWORD` 🔒    | `<strong passphrase>`                | prompt | K1 bootstrap — `must_change_password` forces rotation on first login |

### Frontend (`omnisight-frontend`)

| Variable              | Value                                                          | Source | Why |
| --------------------- | -------------------------------------------------------------- | ------ | --- |
| `NODE_ENV`            | `production`                                                   | spec   | |
| `BACKEND_URL`         | `http://omnisight-backend:8000`                                | spec   | Next.js rewrites target — pserv internal hostname is deterministic |
| `NEXT_PUBLIC_API_URL` | `https://omnisight-frontend.onrender.com` (actual URL after Stage 2) | prompt | Baked into client JS at build time — operator redeploys after setting |

The spec values are visible in [`deploy/render/render.yaml`](render.yaml)
and pinned by the contract test suite (`tests/test_render_blueprint.py`)
so drift shows up on the next CI run.

## Custom domain

Render mints a managed TLS cert automatically when you attach a custom
domain under **Settings → Custom Domains**. After the CNAME propagates
(Render shows a green check), update `OMNISIGHT_FRONTEND_ORIGIN` +
`NEXT_PUBLIC_API_URL` to the new domain and redeploy the frontend so
the client bundle picks up the new `NEXT_PUBLIC_API_URL`.

## Caveats

- **Persistent disk is paid-tier only.** The Blueprint attaches a 1 GB
  disk at `/var/data` (where `omnisight.db` lives) which requires
  `plan: starter` ($7/mo/service minimum). The free tier cannot hold
  disks — on free, SQLite gets wiped every redeploy. Either stay on
  starter or provision a Render Postgres addon + teach the backend to
  read `OMNISIGHT_DATABASE_URL` (SQLAlchemy URL).
- **No Docker-in-Docker.** Render's runtime doesn't expose the host
  Docker socket. The `ContainerManager` agent sandbox path (used for
  cross-compilation + multi-agent tool isolation) is inoperative.
  Render is fine for the single-tenant tool-less demo; full multi-agent
  runs need a VPS-style host.
- **Free tier spins down idle services.** `plan: free` is fine for an
  always-on internal demo — first request after 15 min idle takes ~30s
  to cold-start. Use `starter` for user-facing demos.
- **Healthcheck is deploy-time only.** Render's `healthCheckPath` gates
  whether a new revision flips to live; it is not a periodic liveness
  probe. Render's platform layer handles crash-restart automatically.
- **Blueprint env vars can't template strings.** Hence the 2-stage
  post-deploy walkthrough above. Render is tracking this limitation
  ([community post](https://community.render.com)); if they ship URL
  templating later, we can drop Stage 2 and point `OMNISIGHT_FRONTEND_ORIGIN`
  at a `fromService` reference.

## Updating the spec

`deploy/render/render.yaml` is the **seed template**. After first
apply, the live Blueprint in Render's control plane becomes the SSOT.
To push local edits:

- Commit + push to `master` — Render auto-detects the change and prompts
  to re-apply the Blueprint on next visit to the dashboard (or
- enable **Settings → Blueprint → Auto-sync** on the workspace to apply
  updates automatically on every push).

See also: `deploy/digitalocean/` (DigitalOcean App Platform) ·
`deploy/railway/` (Railway).
