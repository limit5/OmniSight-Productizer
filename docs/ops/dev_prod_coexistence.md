# Dev + Prod on the Same WSL2 Host — Transitional Runbook

**Status**: 2026-04-19 → Ubuntu-26.04 release (transitional)
**Canonical topology** (future state): [`multi-wsl-deployment.md`](./multi-wsl-deployment.md)

---

## Why this document exists

The project's documented topology separates dev, staging, and prod onto
three different WSL2 distros:

| Role | WSL | Status today |
|---|---|---|
| Development + Testing | Ubuntu-26.04 | ⏳ **Not yet installed** — waiting for 26.04 GA |
| Staging | Ubuntu-22.04 | Present, idle |
| **Production** | **Ubuntu-24.04** | **Live — serving `ai.sora-dev.app`** |

Until 26.04 ships and we can move dev off, **Ubuntu-24.04 is doing
double duty**: the prod docker-compose stack runs on it AND whoever
wants to edit code does it in the same repo on the same disk. This is
not the intended steady state. It's working, but it has a specific set
of footguns that a future operator (including future-you) needs to know
about.

**Read this before**:
- Running anything more ambitious than editing a single component
- Installing a new dev-only tool
- Cleaning up disk space
- Being tempted to `docker system prune`

---

## Current coexistence rules

### ✅ Safe operations (no prod risk)

- **Edit files** in `/home/user/work/sora/OmniSight-Productizer/`
  — the running containers are using the BUILT images, not the live
  source tree. A rebuild is required for your edits to reach prod.
  Until you rebuild, prod stays on whatever image was last built.
- **Run tests** — `pytest backend/tests/`, `pnpm run test`,
  `pnpm run lint`. Tests run in-process against fixtures, don't touch
  the production docker volumes.
- **`pnpm run build`** locally to type-check — this writes to `.next/`
  on disk but that's ignored by compose; prod frontend uses the
  container's own `.next/standalone/` built at image build time.
- **`git` operations** — commit, branch, rebase. Nothing here touches
  docker state.
- **`docker ps` / `docker logs` / `docker inspect`** — read-only Docker
  API calls.

### ⚠️ Dangerous operations (will affect prod)

| Operation | What breaks |
|---|---|
| `docker compose -f docker-compose.prod.yml down` | Stops the live prod stack. Users immediately see 502s via the tunnel. |
| `docker compose … down -v` | The `-v` wipes volumes. DB gone. Settings gone. Session tokens gone. Users have to re-bootstrap. |
| `docker system prune -a --volumes` | Removes unused images + volumes. If it runs when any prod service is stopped (e.g. during `bootstrap_prod.sh --fresh`), the volumes go. |
| Editing `.env` | Live backends re-read env on container restart. Changing `OMNISIGHT_ADMIN_PASSWORD` here NOW doesn't kick out existing admins, but it does change who can bootstrap on next container restart. |
| `pnpm dev` / `uvicorn --reload` on default ports | Would bind `:3000` / `:8000`, which are **already taken** by prod containers. Fails to start (good — explicit error), but if you add `-p 3000` override etc. you'd collide. |
| Running any agent / task manually via the backend API | If the docker-socket-proxy is reachable (it is, see below), agent sandboxes spawn as prod containers on the shared daemon. Test containers with names clashing with prod would fail; attacker-style test payloads against `run_bash` would run in prod context. |
| `OMNISIGHT_ENV=development` + importing `backend.*` directly in Python | The config's strict gate relaxes to warnings; secrets validation weakens. DO NOT do this from the prod repo root — your env leaks into what the next `uvicorn` run sees. |
| Touching anything in `data/omnisight.db`, `data/backups/*` | The prod backend reads this file constantly. A parallel sqlite writer (`sqlite3 data/omnisight.db ".backup …"` is fine; a script that *writes* is not) can corrupt it. |

### 🔐 The one sharp edge: Docker daemon

There is ONE docker daemon on the host, and everything that uses
`docker` CLI or `docker-compose` talks to it. That means:

- Dev experiment: `docker run -d nginx` → runs alongside prod, on the
  same daemon. Won't conflict unless ports/names collide.
- But `docker compose -f some-file.yml` WILL see prod containers as
  "other people's services" and may refuse to stop/recreate them
  cleanly if names are similar.
- `docker volume ls` shows prod volumes named
  `omnisight-productizer_*`. Don't accidentally `docker volume rm` them.

**Mental model**: treat the host's docker daemon as shared
infrastructure, like a prod Kubernetes cluster that you happen to
have `kubectl` against. Not your playground.

---

## Current-state-specific decisions / deviations

These are active decisions that differ from what the future
separated-host topology will do, explicitly because dev + prod share
this host:

### D-1 — `.env` is prod-only, no `.env.dev`

- Current `.env` holds the real prod secrets (Anthropic API key,
  admin password, CF Tunnel token, decision bearer).
- Old `.env.bak-*` and `.env.secrets.bak-*` backups (pre-consolidation,
  from the 2026-04-19 go-live) are kept but chmod 600.
- **Do not create a `.env.dev` and start running dev services here.**
  The ports collide with prod and the shared daemon surface-of-attack
  grows. Wait until 26.04 is up, do dev there with `.env.dev`.

### D-2 — dev-only tools are absent from this host

- No `pnpm dev` running. No `uvicorn --reload`. No pytest daemon.
- If you WANT to test a change, build a container for it:
  ```bash
  docker compose -f docker-compose.prod.yml build backend-a
  # ↑ the build stage runs on this host but produces an image; the
  #   running prod containers are unaffected until you recreate them.
  ```
- This is slower than `pnpm dev` HMR, but it's the cost of running
  dev + prod on one box safely.

### D-3 — no horizontal Docker isolation yet

- Both `ai_*` stack (pre-existing: gemma4 tunnel etc.) and
  `omnisight-productizer_*` stack share the same daemon, same
  host networking. They are on separate compose project networks
  (`omnisight-ai-core_omnisight_net` vs `omnisight-productizer_default`)
  so service-name DNS doesn't cross-pollinate. Containers can still
  see each other by raw IP but don't advertise it.
- When 26.04 dev lands, DEV moves to a different distro → different
  docker daemon → true isolation.

### D-4 — WSL resource limits are shared

- `.wslconfig` memory=68GB is shared between all WSL distros.
  Production has 5 healthy containers using ~4GB; there's >60GB
  headroom. Dev on this distro would compete.
- On 26.04 migration, dev gets its own slice.

### D-5 — backup + restore assumes single-host

- `bootstrap_prod.sh --fresh` moves the DB aside to
  `data/backups/pre-fresh-*.db`. Don't wipe those during cleanup
  sprees; they're your only rollback for bootstrap regressions.
- `scripts/deploy-prod.sh` does a WAL-safe `.backup` before rolling.

---

## Known footguns (things that will probably bite someone)

### F-1 — "I ran `docker compose down` to fix something"

**Consequence**: users immediately see 502 on `ai.sora-dev.app`.
Caddy's `fail_duration 30s` in Caddyfile means the tunnel also starts
failing its health probes.

**Recovery**: `docker compose -f docker-compose.prod.yml --profile tunnel up -d`
brings everything back in <30 s (images cached, DB intact).

**Prevention**: before any `docker compose down`, pause and ask "does
this need to ALSO be prod-down?" 99% of the time the answer is "I
want to re-create service X, not stop everything". Use
`docker compose up -d --force-recreate --no-deps <svc>` for that.

### F-2 — `.env` gets edited for an experiment and left in a weird state

**Consequence**: next backend restart (can happen at any time —
`restart: always` + dockerd bug + WSL reboot) picks up the broken env.
strict-mode `validate_startup_config()` refuses to boot → prod down.

**Recovery**: restore `.env.bak-*` from `ls -t data/backups/.. or
the timestamped `.env.bak-*` in repo root.

**Prevention**: every edit to `.env` should be committed (not to git
— it's gitignored — but to a `.env.bak-$(date +%s)` locally) first.
The `bootstrap_prod.sh` script backs up automatically on a `--fresh`;
manual edits don't.

### F-3 — Testing a code change by editing files in a running-container

```bash
# Tempting:
docker compose exec backend-a vim /app/backend/some_file.py
```

**Consequence**: changes live in the container filesystem layer, NOT
the repo. They're lost on restart. Subsequent `docker compose build`
won't include them (they were never in the source tree). You'll waste
an hour wondering why "your fix didn't stick".

**Prevention**: always edit in the repo, then
`docker compose build <svc> && docker compose up -d --force-recreate --no-deps <svc>`.

### F-4 — Leaving CF Tunnel token in .env after rotation

The `OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN` has appeared in deploy logs and
this conversation's context. If/when it's rotated in CF dashboard,
`.env` must be updated AND the `cloudflared` container recreated:

```bash
# Edit .env with new token
docker compose -f docker-compose.prod.yml --profile tunnel up -d --force-recreate cloudflared
```

Otherwise the old token's connector stays registered to CF for 48h+.

### F-5 — `/app/data/omnisight.db` under docker volume, but `data/` directory exists on host too

- The prod DB is in the docker-managed volume
  `omnisight-productizer_omnisight-data`, mounted into containers at
  `/app/data`. **NOT the same as `./data/` on the host.**
- `./data/backups/*.db` (pre-deploy snapshots, `bootstrap_prod.sh` output)
  IS on the host filesystem — that's where `ls -la data/` sees them.
- Confusion point: do not copy `./data/omnisight.db` expecting it's
  the live DB — it's a stale/historical file. The live DB is only
  accessible via `docker volume` or `docker exec backend-a cat /app/data/omnisight.db`.

### F-6 — Prod containers see changes to source `configs/` because of bind mounts (they don't, but you might think they do)

None of the Caddyfile / compose service mounts the source tree live
into the running container. Changes to `deploy/reverse-proxy/Caddyfile`
DO take effect immediately because it's bind-mounted (see compose:
`./deploy/reverse-proxy/Caddyfile:/etc/caddy/Caddyfile:ro`).
Changes to `backend/` are NOT live — they go through image build.

---

## Migration playbook: when Ubuntu-26.04 ships

Do these in order. Approximate time: 1–2 hours for a careful migrator.

### Pre-migration (while 24.04 is still dev + prod)

- [ ] `git push origin main` — make sure 26.04 can `git clone` everything
- [ ] Note the current operator-visible state: `curl https://ai.sora-dev.app/ -I | head`
- [ ] Back up `.env` to `.env.bak-pre-26.04-migration` on host + a secondary
      location (team password manager)
- [ ] Confirm `data/backups/` has a recent snapshot (`bootstrap_prod.sh --backup` if not)

### On 26.04 (new distro)

- [ ] `wsl --install -d Ubuntu-26.04` from Windows PowerShell
- [ ] Inside 26.04: install dev deps (node, pnpm, python3.12, docker-ce if you want isolated docker)
- [ ] `git clone <repo> ~/work/sora/OmniSight-Productizer`
- [ ] Create a NEW `.env.dev` with dev-only values:
  - `OMNISIGHT_ENV=development`
  - `OMNISIGHT_DEBUG=true`
  - `OMNISIGHT_AUTH_MODE=open` (or `session` if you want login)
  - Fresh weak admin password like `dev-admin-pw` (never a real one)
  - A SEPARATE Anthropic key (or the same one — it's your call; don't
    burn prod rate-limit on dev)
  - Do NOT put production tunnel token or decision bearer here
- [ ] `./scripts/setup-dev-env.sh` (per multi-wsl-deployment.md §Development)
- [ ] Verify dev stack on `http://localhost:3001/` (per port allocation table)

### Cut-over

- [ ] Stop referring to the 24.04 `.env` — prod-only from now on
- [ ] Add a `.gitignore` note or `.env.example` header pointing dev
      operators to 26.04
- [ ] On 24.04, run `./scripts/clean-prod-env.sh` (per multi-wsl-deployment.md
      §123-129) to delete dev artifacts — `__pycache__/`, dev logs, etc.
- [ ] Remove any dev-only tools from 24.04 (pnpm dev, uvicorn --reload,
      anything that binds :3001 / :8002)

### Post-migration

- [ ] 24.04 is pure prod. No edits happen here. Any change goes:
      **26.04 edit → git push → 24.04 `git pull && scripts/deploy-prod.sh`**
- [ ] Update `docs/ops/dev_prod_coexistence.md` (this file) to mark
      state = `Ubuntu-26.04 active for dev, 24.04 prod-only`
- [ ] Retire this whole document → move to `docs/handoff_archive/`

---

## When to update this document

Any time a new footgun bites you (or nearly bites you). The goal is
that the next operator doesn't step on the same rake.

---

## See also

- [`multi-wsl-deployment.md`](./multi-wsl-deployment.md) — canonical
  three-host topology
- [`production_deploy.md`](./production_deploy.md) — go-live runbook
- [`deploy_postmortem_2026-04-19.md`](./deploy_postmortem_2026-04-19.md)
  — what happened during the first go-live, including 14 bugs / 3 audit
  issues found + fixed
- [`cloudflare_settings.md`](./cloudflare_settings.md) — CF dashboard
  reference
- [`autostart_wsl.md`](./autostart_wsl.md) — how the prod stack survives
  host reboot (affects migration too: 26.04 autostart needs a parallel
  Task Scheduler entry)
