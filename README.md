# OmniSight Productizer

![Next.js](https://img.shields.io/badge/Next.js-16.2-black?logo=next.js)
![React](https://img.shields.io/badge/React-19.2-61DAFB?logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.7-3178C6?logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4.2-06B6D4?logo=tailwindcss&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.1-1C3C3C?logo=langchain&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Isolation-2496ED?logo=docker&logoColor=white)

![Anthropic](https://img.shields.io/badge/Anthropic-Claude-D97706?logo=anthropic&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT-412991?logo=openai&logoColor=white)
![Google](https://img.shields.io/badge/Google-Gemini-4285F4?logo=google&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama-Local-ffffff?logo=ollama&logoColor=black)

![Tests](https://img.shields.io/badge/Tests-678-brightgreen)
![Tools](https://img.shields.io/badge/Tools-29_Sandboxed-green)
![Agents](https://img.shields.io/badge/Roles-19_Skills-blueviolet)
![API](https://img.shields.io/badge/API-~80_Endpoints-blue)

Full-stack autonomous development command center for embedded AI cameras (UVC/RTSP). Multi-agent orchestration with isolated workspaces, real-time streaming UI, dual-track simulation, and Docker-containerized cross-compilation.

## Architecture

```
Browser (Windows/Linux)
    |
Next.js (WSL2:3000)              Frontend — Sci-Fi FUI dashboard (18 components)
    | rewrites proxy
FastAPI (WSL2:8000)               Backend — Multi-agent engine (14 routers, ~80 endpoints)
    |
    +-- LangGraph Pipeline        Orchestrator → Conversation/Specialist → Tool Executor → Summarizer
    +-- 8 LLM Providers           Anthropic, OpenAI, Google, xAI, Groq, DeepSeek, Together, Ollama
    +-- 29 Sandboxed Tools        File, Git, Bash, Simulation, Platform, Review, Report
    +-- EventBus → SSE            Real-time push + event persistence + replay API
    +-- WorkspaceManager          git worktree per agent (CODEOWNERS enforcement)
    +-- ContainerManager          Docker cross-compilation (aarch64/armv7/riscv64 + vendor SDK mount)
    +-- Multi-Track Simulation    algo / hw / npu / deploy / hmi (constrained HMI bundle)
    +-- 4-Tier Notifications      L1 log → L2 Slack → L3 Jira → L4 PagerDuty (DLQ + retry)
    +-- NPI Lifecycle             8 phases × 3 tracks × 4 business models (Timeline + Gantt)
    +-- Slash Commands            22 commands with autocomplete (/status, /build, /simulate, ...)
    +-- SQLite WAL                11 tables, event_log, debug_findings, integrity check
```

## Quick Start

```bash
# 0. Config (one-time)
cp .env.example .env
#   then edit .env — at minimum set OMNISIGHT_LLM_PROVIDER + its API key.
#   Without one, agents run in rule-based fallback mode (see below).

# 1. Backend  (Python deps are hash-locked; N1 policy)
cd OmniSight-Productizer
python3 -m pip install --upgrade pip   # avoid Python 3.12 resolver bugs
pip install --require-hashes -r backend/requirements.txt
python3 -m uvicorn backend.main:app --port 8000

# 2. Frontend (new terminal)  —  pnpm is canonical (N1 policy)
#    Requires Node 20.17.x (.nvmrc); run `nvm use` first if you have nvm.
pnpm install --frozen-lockfile
pnpm run dev

# 3. Browser
open http://localhost:3000

# Alternative: one-shot docker compose
#   docker compose up --build
#   (reads the same .env; exposes :3000 + :8000)
```

Interactive API docs live at `http://localhost:8000/docs` (FastAPI
auto-generated Swagger) once the backend is up.

### Environment Variables

Copy `.env.example` to `.env` and set your LLM API key:

```bash
OMNISIGHT_LLM_PROVIDER=anthropic
OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-...

# Or use local Ollama (no key needed)
# OMNISIGHT_LLM_PROVIDER=ollama
```

Without an API key the system runs in rule-based fallback mode — all features work, agents produce template responses instead of LLM-generated ones.

## Key Features

### Multi-Agent Pipeline
- **8 specialist agents**: firmware, software, validator, reporter, reviewer, general, custom, devops
- **19 role skill files** with domain-specific prompts (BSP, ISP, HAL, algorithm, SDET, etc.)
- **Conversational AI**: intent detection routes questions to conversation node (no tools), tasks to specialists
- **Self-healing**: error_check → retry (3x) → loop detection → human escalation
- **Verification loop**: simulation [FAIL] → auto-fix code → re-verify (2x max)

### Simulation & Verification
- **Multi-track**: algo (data-driven replay + Valgrind) / hw (mock sysfs + QEMU cross-run) / npu / deploy / **hmi** (C26 — constrained HMI bundle + IEC 62443 gate + budget gate)
- **simulate.sh**: unified test runner with JSON report, coverage enforcement, cmake toolchain support
- **4 platform profiles**: aarch64, armv7, riscv64, vendor-example (extensible for any SoC)

### HMI Embedded Web UI Framework (C26 / L4-CORE-26)
- **Constrained generator**: whitelist Preact / lit-html / vanilla JS; inlines CSS + i18n JSON; rejects CDN / analytics / `eval` / inline event attrs
- **Bundle size budget**: per-platform flash-partition-aware (aarch64 512 KiB / armv7 256 KiB / riscv64 1 MiB / host_native 4 MiB) — CI hard-fail via `BudgetExceeded`
- **IEC 62443-4-2 SL2 gate**: CSP directives + required headers + forbidden patterns + inline event attr scan
- **Binding generator**: NL prompt + HAL schema → `fastcgi` / `mongoose` / `civetweb` C handler skeleton + matching JS client
- **Shared components**: network / OTA / logs viewer — reused by D2 IPCam / D8 Router / D9 5G-GW / D17 Industrial-PC / D24 POS / D25 Kiosk
- **i18n pool**: en / zh-TW / ja / zh-CN (extensible via overrides), shared with D-series doc templates
- **ABI matrix**: frozen Chromium/WebKit compatibility table per platform (aarch64 / armv7 / riscv64 / host_native)
- **Pluggable LLM backend**: anthropic (Opus 4.7 Design Tool) / ollama / rule-based — override via `HMI_LLM_PROVIDER` env (falls back to `OMNISIGHT_LLM_PROVIDER`, then rule-based)
- **Endpoints**: 13 REST routes under `/api/v1/hmi/*` (summary, platforms, abi-check, generate, budget-check, security-scan, binding/generate, components/assemble, ...)
- **Simulation**: `scripts/simulate.sh --type=hmi` — generates bundle + runs budget + security gates + optional headless Chromium + QEMU smoke

### DevOps Integration
- **Gerrit**: AI reviewer (patchset-created → auto-review), Code-Review -1 → auto-fix task
- **GitHub/GitLab/Jira**: bidirectional webhook sync (HMAC/token validation, 5s debounce)
- **CI/CD triggers**: GitHub Actions, Jenkins, GitLab CI (fire-and-forget on merge)
- **CODEOWNERS**: file-level ownership enforcement + pre-merge conflict detection

### NPI Lifecycle
- **8 phases**: PRD → EIV → POC → HVT → EVT → DVT → PVT → MP
- **3 tracks**: Engineering, Design, Market (OBM mode)
- **4 business models**: ODM, OEM, JDM, OBM
- **Gantt chart**: horizontal progress bars with Timeline/Gantt toggle

### Multi-Tenancy
- **Tenant isolation**: schema-level (I1), RLS (I2), SSE filter (I3), secrets (I4), filesystem (I5)
- **Sandbox fair-share**: DRF per-tenant capacity (I6) — CAPACITY_MAX=12, guaranteed minimum per tenant, idle borrowing with 30s grace reclaim, turbo cap prevents single-tenant monopoly
- **Rate limiting**: 3-dimension (per-IP + per-user + per-tenant) Redis token bucket (I9) — plan-based quotas (free/starter/pro/enterprise), automatic in-memory fallback
- **Resource hard isolation**: cgroup CPU/mem (M1), per-tenant disk quota + LRU sweep (M2), per-tenant per-key LLM circuit breaker (M3)
- **Per-tenant observability + billing (M4)**: cgroup v2 scraper (`backend/host_metrics.py`) samples every running sandbox by `tenant_id` label → 7 Prometheus metrics (`tenant_cpu_percent`, `tenant_mem_used_gb`, `tenant_disk_used_gb`, `tenant_sandbox_count` + `tenant_cpu_seconds_total` / `tenant_mem_gb_seconds_total` / `tenant_derate_total`); `/host/metrics` REST with admin/user ACL; culprit-aware AIMD (`tenant_aimd.plan_derate()`) derates only the outlier tenant instead of flat host-wide; `scripts/usage_report.py` renders billing-ready text/JSON/CSV
- **Per-tenant egress allowlist (M6)**: DB-backed `tenant_egress_policies` (`allowed_hosts[]` + `allowed_cidrs[]` + `default_action`) replaces the global `OMNISIGHT_T1_EGRESS_ALLOW_HOSTS` env. Sandbox launch path consults per-tenant policy first, falls back to legacy env when DB row missing. `python -m backend.tenant_egress emit-rules` produces a JSON rule plan that `scripts/apply_tenant_egress.sh` materialises as iptables `-m owner --uid-owner <sandbox_uid>` chains. Settings → Network Egress UI lets viewer/operator file `host`/`cidr` requests; admin one-click approve merges into the live policy. Default-deny: empty allow-list → `--network none`

### Reliability & Recovery
- **Token budget**: 3-tier (80% warn → 90% downgrade → 100% freeze) + daily auto-reset
- **Provider failover**: per-tenant per-key circuit breaker (M3) — `(tenant_id, provider, api_key_fingerprint)` triple, 5min cooldown, audit on open/close, `/providers/circuits` REST + Settings UI panel; tenant A's bad key cannot derail tenant B
- **Watchdog**: 30min task timeout, 2hr stuck detection, dynamic reallocation
- **Startup cleanup**: reset stuck agents/simulations, orphan containers, git locks
- **Event persistence**: DLQ with retry (3x exponential backoff), event_log table, replay API
- **Debug blackboard**: cross-agent error tracking, loop detection, /system/debug API

### Hardware Abstraction (Layer A Core)
- **Machine Vision** (C24): GenICam + 4 transport adapters + HW trigger + multi-camera calibration
- **Depth/3D Sensing** (C23): ToF + structured light + stereo SGBM + point cloud + ICP/SLAM
- **Barcode Scanner** (C22): 4 vendor adapters + 16 symbologies + 3 decode modes
- **Motion Control** (C25): G-code interpreter + 3 stepper drivers + PID heaters + thermal runaway safety

### Skill Packs (Layer D)
- **UVC Gadget** (D1): UVC 1.5 descriptor scaffold + gadget-fs binding + UVCH264 payload + USB-CV compliance (pilot skill — validates CORE-05 framework)

### Slash Commands
Type `/` in any input field for autocomplete:

| Category | Commands |
|----------|----------|
| System | `/status` `/info` `/debug` `/logs` `/devices` |
| Dev | `/build` `/test` `/simulate` `/review` `/platform` |
| Agent | `/spawn` `/agents` `/tasks` `/assign` `/invoke` |
| Provider | `/provider` `/switch` `/budget` |
| NPI | `/npi` `/sdks` |
| Tools | `/help` `/clear` `/refresh` |

## Project Structure

```
OmniSight-Productizer/
├── app/                    # Next.js pages + error boundary
├── components/omnisight/   # 18 FUI components
├── hooks/                  # use-engine (SSE + state), use-mobile
├── lib/                    # api.ts, slash-commands.ts, i18n
├── backend/
│   ├── agents/             # graph.py, nodes.py, llm.py, tools.py, state.py
│   ├── routers/            # 14 API routers
│   ├── docker/             # Dockerfile.agent (aarch64 + Valgrind + QEMU)
│   ├── tests/              # 29 test files, 370 tests
│   ├── slash_commands.py   # 22 / command handlers
│   ├── codeowners.py       # File ownership parser
│   ├── notifications.py    # L1-L4 tiered dispatch + DLQ
│   └── ...
├── configs/
│   ├── platforms/          # 4 platform profiles (aarch64, armv7, riscv64, vendor-example)
│   ├── roles/              # 19 role skill files
│   ├── skills/             # 4 Anthropic-format task skills
│   ├── models/             # 7 LLM model rule files
│   ├── templates/          # 2 Jinja2 report templates
│   ├── CODEOWNERS          # File → agent type mapping
│   └── hardware_manifest.yaml
├── scripts/simulate.sh     # Dual-track simulation runner
├── test_assets/            # Ground truth test data
├── docs/
│   ├── design/             # 10 system design documents
│   └── sop/                # Implementation SOP
└── HANDOFF.md              # Complete project state document
```

## Development Phases

28 phases completed (Phase 1-29), covering:
- Core infrastructure (Phase 1-12)
- NPI + Artifact + Simulation (Phase 13-15)
- Error recovery + Conversational AI (Phase 16-19)
- Multi-agent patterns: 5 patterns at 80-98% coverage (Phase 20-24)
- Provider UI + Webhooks + Handoff viz + NPI Gantt (Phase 25-27)
- SoC SDK integration + Slash commands (Phase 28-29)

See [HANDOFF.md](HANDOFF.md) for detailed phase history and future roadmap.

## Prewarm Pool Multi-Tenant Safety (M5)

Speculative Tier-1 container pre-warm (Phase 67-C) is now tenant-scoped. The policy is controlled by `OMNISIGHT_PREWARM_POLICY`:

| Policy | Behavior | When to pick |
|---|---|---|
| `per_tenant` (default) | Each tenant gets its own pre-warm bucket (depth 1-2 per bucket). Tenant A's pre-warmed container can never be consumed by tenant B. | SaaS / multi-tenant — always. |
| `shared` | Single global bucket (legacy Phase 67-C behavior). Faster fan-out but cross-tenant filesystem residue risk. Emits a startup warning. | Single-tenant or fully-trusted deployments. |
| `disabled` | Pre-warm entirely off. Trade 300 ms cold-start for zero speculative-container state. | High-security customers (compliance / audit). |

Regardless of policy, every `consume()` force-clears the tenant's `/tmp/omnisight_ingest/<tid>/` namespace before handing the container to the real task — so no speculative scratch-file residue ever leaks into a real workspace. Cleanup failures are logged but never void a valid pre-warm hit.

Pre-warm itself remains opt-in via `OMNISIGHT_PREWARM_ENABLED=true`; `policy=disabled` takes precedence when both are set.

## Per-tenant Egress Allowlist (M6)

Tier-1 sandbox egress is now controlled per tenant through DB-backed policy plus an admin approval workflow. The legacy global `OMNISIGHT_T1_EGRESS_ALLOW_HOSTS` env is auto-migrated into `t-default` on first boot and remains a fallback when no DB row exists.

| Layer | What happens |
|---|---|
| `tenant_egress_policies` table | One row per tenant: `allowed_hosts[]`, `allowed_cidrs[]`, `default_action` (`deny` recommended). DB is the source of truth. |
| Sandbox launch | `start_container` resolves `tenant_id` → `sandbox_net.resolve_network_arg(tenant_id=…)` consults the policy. Empty allow-list = `--network none` (full air-gap). Any allowed host/CIDR opens the bridge. |
| Iptables installer | Operator runs `sudo scripts/apply_tenant_egress.sh --tenant <tid> --uid <sandbox_uid>` (or `--all`). The script reads the JSON rule plan from `python -m backend.tenant_egress emit-rules` and installs an `OMNISIGHT-EGRESS-<tid>` chain hooked into `OUTPUT -m owner --uid-owner <uid>`, with a terminal `DROP` (when `default_action=deny`). |
| Approval workflow | Viewer/operator file additions via the Settings → Network Egress UI. The request lands as `pending` in `tenant_egress_requests`. Admin clicks `approve` (or `reject`); approval merges the value into the live policy and audits the decision. |
| Audit chain | `tenant_egress.upsert`, `request_submit`, `request_approve`, `request_reject` all enter the per-tenant `audit_log` hash chain — answers "who allowed `evil.com` for tenant X" via a single `audit.query(entity_kind='tenant_egress')`. |

Operators wanting to keep the pre-M6 single-tenant flow do nothing — the legacy env still works and the t-default policy gets a `legacy-migration` audit row on first upgrade.

## Dependency Governance (N1)

Dependencies are fully locked and every lockfile drift fails CI before the rest of the pipeline runs. Use this section when onboarding or upgrading.

| Layer | Tool / file | What changes through it |
|---|---|---|
| Node version | `.nvmrc` + `.node-version` (both `20.17.0`) + `package.json` `engines.node` `>=20.17.0 <21` | `nvm use` / `fnm use` / `asdf install` / Volta / `actions/setup-node@v4` `node-version-file: .nvmrc` all resolve to the same version |
| JS package manager | `package.json` `packageManager: pnpm@9.15.4` + `engines.pnpm: >=9` | Node 20's built-in `corepack` downloads and pins pnpm automatically — contributors don't need a global install |
| JS dependency graph | `pnpm-lock.yaml` (canonical, committed) | `pnpm install --frozen-lockfile` everywhere (local dev, Dockerfile.frontend, CI, release). `package-lock.json` and `yarn.lock` are both `.gitignore`d and the CI drift gate rejects any stray copy |
| Python ranges | `backend/requirements.in` (human-readable) | Edit this file to add/remove/bump a package, then regenerate |
| Python lock | `backend/requirements.txt` (pip-compile output with `--generate-hashes`) | Regenerate with `pip-compile --generate-hashes backend/requirements.in` — every pin carries at least one `sha256:` hash |
| Python install | `pip install --require-hashes -r backend/requirements.txt` | Used in `Dockerfile.backend`, `scripts/deploy.sh`, and every CI job. A missing/mismatched hash aborts install |
| CI drift gate | `.github/workflows/ci.yml` `lockfile-drift` job | Runs first. Rejects stray `package-lock.json`/`yarn.lock`, re-runs `pnpm install --frozen-lockfile` + `pip-compile`, fails the build if anything diffs |

**Typical flows:**

```bash
# Add a Python dep
echo "somelib==1.2.3" >> backend/requirements.in
pip-compile --generate-hashes backend/requirements.in
git add backend/requirements.in backend/requirements.txt

# Bump a JS dep
pnpm update some-package
git add package.json pnpm-lock.yaml
```

If CI fails with `Lockfile drift detected`, re-run the corresponding regenerate command locally, commit the lock, push.

### Renovate auto-PRs (N2)

[Renovate](https://docs.renovatebot.com/) opens dependency PRs every weekend (`Asia/Taipei`) per the policy in [`renovate.json`](renovate.json). Full SOP — group rules, tiered auto-merge, vulnerability handling, operator bootstrap — lives in [`docs/ops/renovate_policy.md`](docs/ops/renovate_policy.md).

| Update type | Auto-merge? | Reviewers | Notes |
|---|---|---|---|
| CVE / vulnerability | yes (CI green) | none | Opens immediately, jumps the queue (`prPriority: 100`) |
| patch / pin / digest | yes (CI green) | none | 3-day upstream wait |
| minor | no | 1 (CODEOWNERS) | 5-day wait |
| major | no — never | 2 + G3 blue-green | 14-day wait, label `deploy/blue-green-required` |

Group rules keep peer-coupled families together so the lockfile stays internally consistent: `@radix-ui/*`, `@ai-sdk/*`, `langchain*`/`langgraph` (Python), `@types/*`, GitHub Actions, and Docker base images each merge as one PR.

CI validates `renovate.json` against the Renovate JSON schema in the `renovate-config` job before any other gate runs — typos cannot silently disable the bot.

### LangChain / LangGraph adapter firewall (N4)

All `langchain*` and `langgraph*` imports are funneled through a single module, [`backend/llm_adapter.py`](backend/llm_adapter.py). Every other backend module imports message classes, graph primitives, and provider factories from the adapter — never from LangChain directly.

Why: LangChain ships breaking changes at patch cadence (message shapes, tool-call formats, provider arg names). Keeping the surface contained to one file means a LangChain upgrade is a one-file change plus running the adapter test suite, not an 8-file sweep across agents/, routers/, and tests.

The adapter exposes a stable, version-decoupled API:

| Symbol | Purpose |
|---|---|
| `invoke_chat(messages, ...)` | Single synchronous chat turn → text |
| `stream_chat(messages, ...)` | Async iterator of text chunks |
| `tool_call(messages, tools, ...)` | Chat with tools bound → normalized `AdapterToolResponse` |
| `embed(texts, ...)` | Provider-agnostic embeddings (OpenAI + Ollama) |
| `build_chat_model(provider, ...)` | Provider factory (only `agents.llm` should use directly) |
| `HumanMessage` / `AIMessage` / `SystemMessage` / … | Re-exported LangChain message primitives |
| `StateGraph` / `END` / `add_messages` | Re-exported LangGraph primitives |
| `tool` | Re-exported `@tool` decorator |

A CI gate (`llm-adapter-firewall` job, runs in parallel with `lint`) enforces the firewall by scanning every `backend/**/*.py` for forbidden imports using stdlib `ast`. The script — [`scripts/check_llm_adapter_firewall.py`](scripts/check_llm_adapter_firewall.py) — exits non-zero with `::error file=...,line=...::` annotations if any file other than `backend/llm_adapter.py` (plus its own test file) imports from `langchain*` or `langgraph*`. Upgrades follow this workflow:

1. Bump the `langchain-*` / `langgraph` pin in `backend/requirements.in`.
2. Regenerate `backend/requirements.txt` via `pip-compile --generate-hashes`.
3. Run `pytest backend/tests/test_llm_adapter.py` — 50 tests cover all public symbols.
4. If any test fails, the fix is isolated to `backend/llm_adapter.py`; no other file needs changes.

### Framework fallback branches (N9)

Two long-running branches stand permanent guard over the framework rollback path: `compat/nextjs-15` (held one major behind master's Next 16) and `compat/pydantic-v2` (pre-emptive — declared today even though Pydantic v3 has not shipped yet). The branches are declared in [`.fallback/manifests/*.toml`](./.fallback/) — a single TOML per branch carries the pin, the `freshness_days` window, and the `skip_globs` that the rebase tool uses to filter master commits when keeping the fallback evergreen.

Three CI surfaces enforce the policy: ([`fallback-branches.yml`](.github/workflows/fallback-branches.yml)) re-builds + runs core tests on every `compat/**` push and on a Sunday-night cron, certifying the fallback as deployable; ([`major-upgrade-gate.yml`](.github/workflows/major-upgrade-gate.yml)) blocks any `tier/major` PR that bumps `next` or `pydantic` until the corresponding fallback branch shows a green CI run within its freshness window (defaults to 14 days); and the carve-outs in [`renovate.json`](renovate.json) prevent Renovate from bumping the pinned framework on its own fallback (which would defeat the entire point), while still flowing security patches and unrelated minor bumps so the branch doesn't rot.

Operator tooling stays stdlib-only for the same self-defense reason as N5/N6/N7/N8: when a major framework upgrade is what just exploded production, the rollback tools must not depend on the framework that's broken. [`scripts/fallback_setup.sh`](scripts/fallback_setup.sh) materialises the branches locally (one-shot), [`scripts/fallback_rebase.py`](scripts/fallback_rebase.py) plans + applies the weekly cherry-pick of non-framework commits (refuses to auto-split commits that straddle safe + skip paths), and [`scripts/check_fallback_freshness.py`](scripts/check_fallback_freshness.py) is the gate's GH Actions API probe. Full lifecycle, retirement criteria, and per-incident playbook in [`docs/ops/fallback_branches.md`](docs/ops/fallback_branches.md); production rollback path is wired into [`docs/ops/dependency_upgrade_runbook.md`](docs/ops/dependency_upgrade_runbook.md) Phase 4.5 ("Path C — Fallback-branch rollback").

### DB engine compatibility matrix (N8)

A dedicated workflow ([`.github/workflows/db-engine-matrix.yml`](.github/workflows/db-engine-matrix.yml)) exercises every committed Alembic migration against two engines ahead of the G4 Postgres cutover. **Hard gate:** SQLite 3.40.1 + 3.45.3 (the floor and ceiling of what production has ever run). **Advisory:** Postgres 15 + 16 (red-X by design today — the baseline migrations use SQLite-only idioms; the cells go hard-gate after G4 ports the SQL). **Advisory:** an engine-specific SQL linter ([`scripts/check_migration_syntax.py`](scripts/check_migration_syntax.py)) emits `::warning ...` annotations for every `AUTOINCREMENT`, `datetime('now')`, `INSERT OR IGNORE`, `CREATE VIRTUAL TABLE USING fts5`, `PRAGMA`, `BEGIN IMMEDIATE`, and related SQLite idiom it finds in migration files.

The dual-track validator ([`scripts/alembic_dual_track.py`](scripts/alembic_dual_track.py)) runs `upgrade head → step-down to revision 0001 → re-upgrade head`, then diff-checks the two schema fingerprints. An asymmetric up/down pair fails the cell. Already earned its keep on day one: caught a latent SQLAlchemy-2.x bug in migration 0014 where `conn.execute("SELECT …")` needs a `text()` wrapper. SQLite versions are pinned deterministically via `LD_PRELOAD` of a source-built `libsqlite3.so` (cached across runs); the workflow asserts `sqlite3.sqlite_version` matches the matrix pin before running migrations. Full SOP + G4 handoff plan in [`docs/ops/db_matrix.md`](docs/ops/db_matrix.md).

### Multi-version CI matrix (N7)

A nightly workflow ([`.github/workflows/multi-version-matrix.yml`](.github/workflows/multi-version-matrix.yml)) exercises the test suite against the **next** versions of every interpreter and the FastAPI minor stream — Python 3.12 (gate) + 3.13 (advisory), Node 20.x (gate) + 22.x (advisory), FastAPI pinned (gate) + latest minor (advisory). PRs continue to run only the gate cells via [`ci.yml`](.github/workflows/ci.yml), so PR latency is unchanged; advisory cells use `continue-on-error: true` so a deprecation in Python 3.13 cannot red-X a green run. The matrix's job is to surface what the next upgrade will require *before* it lands.

Every advisory cell pipes its captured pytest / vitest / tsc log through [`scripts/surface_deprecations.py`](scripts/surface_deprecations.py), which (a) emits one `::warning ...` GitHub Actions annotation per unique deprecation message — capped at 30 so a runaway log can't flood the run sidebar — and (b) appends a deduplicated count-by-message table to the per-job `GITHUB_STEP_SUMMARY`. The script is stdlib-only for the same self-defense reason `upgrade_preview.py` (N5) and `check_eol.py` (N6) are: it cannot itself be broken by the dep upgrade it summarises. Full SOP — when to act on a red advisory cell, how each install command differs from PR — lives in [`docs/ops/ci_matrix.md`](docs/ops/ci_matrix.md).

### Nightly upgrade preview (N5)

Every night at 01:00 Asia/Taipei a separate workflow ([`.github/workflows/upgrade-preview.yml`](.github/workflows/upgrade-preview.yml)) trial-upgrades the lockfiles in a fresh GitHub runner, installs the upgraded deps, runs the full backend pytest suite + Chromium Playwright suite against them, and posts a single open issue tagged `dependency-preview` with: outdated tables, suspected-breaking callouts, lockfile diffs (truncated), and the tail logs. Operators read the issue on Monday morning to decide whether to let the weekend Renovate batch land, pin a package, or coordinate a blue-green deploy. The preview never auto-merges and never mutates committed files — full SOP in [`docs/ops/upgrade_preview.md`](docs/ops/upgrade_preview.md).

The "suspected breaking" classifier in [`scripts/upgrade_preview.py`](scripts/upgrade_preview.py) flags major bumps, 0.x minor bumps (pre-1.0 SemVer convention), and any change to a hand-curated watchlist of strategic packages (`langchain*`, `pydantic`, `next`, `react`, `@radix-ui/*`, `@ai-sdk/*`, `playwright`, `vitest`, …). The renderer is stdlib-only so it survives the very dep break it is trying to forecast.

## Theme

The UI is deliberately **dark-only** — the "FUI" (fictional user interface)
language (neural-grid, holo-glass, deep-space gradients, scan-lines) is
designed around a dark canvas. There is no light-mode toggle. A
`color-scheme: dark` declaration is set at the root so browsers render
native controls, scrollbars and autofill in the dark palette even when
the host OS is configured for light mode. Users who prefer a light UI
should use a different tool — this is a mission-control dashboard, not a
documentation site.

Motion preferences are honoured: `prefers-reduced-motion: reduce`
disables the neural-flow animation, toast urgency pulses, and all
tween transitions.

## License

Proprietary. All rights reserved.
