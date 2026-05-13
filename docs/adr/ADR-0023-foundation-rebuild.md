---
id: ADR-0023
title: Foundation Rebuild — implementing ADR-0001 + ADR-0002 + 13 other cross-cutting gaps discovered as aspirational
status: Draft
date: 2026-05-13
---

# ADR-0023 — Foundation Rebuild

- **Status**: Draft v2 (2026-05-13, amended after codex independent review). Awaits operator review + `+2` before locking; once locked, Sprint S12: Bedrock META + ~250 children file against this ADR.

**Amendment log**:
- v1 (2026-05-13): initial draft
- v2 (2026-05-13): codex-cli independent review surfaced 3 contradictions + 5 missing pieces; amended §2 (restart boundary), §2.1 (shadow mode), §3.2.1 (GitLab Runner spec), §3.5 + Appendix D (worktree vs ephemeral clones), §4.31.B (B-0 single-runner mode), §6.1 (parity model); new §11–§15 (phase gate evidence / operator-only taxonomy / inventory baseline / cosign legacy policy / registry namespace table).

- **Deciders**: operator (`nanakusa sora`) + claude main-session
- **Tickets**: AUDIT-31 META (to be filed after `+2`) · OP-1045 (per-pickup ephemeral clones, absorbed into 31.B) · 7 existing AUDIT-29 followups that this ADR touches
- **Amends**: ADR-0001 (Five-Branch Git Flow) · ADR-0002 (GitLab self-hosted primary, GitHub one-way mirror, Gerrit review layer)
- **Relates**: ADR-0019 (release:force-promote override) · ADR-0020 (release-cut as single merge change) · ADR-0021 (Release Pipeline Coordinator) · L-OP-247 (origin = GitHub vs ADR-0002 plan) · L-OP-247 (Gerrit replication.config templating)
- **Supersedes**: nothing (this ADR is an *implementation contract* for ADRs that were Accepted but never executed)
- **Blocks**: rc2 cut (rc2 cannot proceed until AUDIT-31 fully complete, per operator decision 2026-05-13)

---

## §1. Context

### 1.1 What happened

Between 2026-05-04 and 2026-05-13, two strategic ADRs were Accepted:

- **ADR-0001 (2026-05-04)** declared a five-branch Git Flow (`master` retired → `develop` integration / `feature/*` dev / `agent/*` multi-agent / `release/*` stabilisation) to handle multi-AI parallelism.
- **ADR-0002 (2026-05-04)** declared a three-remote architecture: GitLab self-hosted as primary, GitHub one-way mirror for visibility, Gerrit as code-review layer.

**Both ADRs are nominally Accepted in the doc canon. Neither has been actually implemented.** Discovery (2026-05-13) catalogued:

- **15 cross-cutting failures** — every layer (governance, source-of-truth, CI, image dist, signing, env separation, monitoring, multi-agent, runner pattern, container topology, deployment, registry reference, branch hygiene, replication, alerting) operates on aspirational config that was never deployed (see Appendix A for the full inventory).
- **Production runs on miracles**: `omnisight-productizer-backend-a/b` containers `unhealthy` for 5 days. `omnisight-productizer-caddy-1` `unhealthy` for 9 days. `ai_engine` exited 9 days ago. **No alert fired.** Users still see a working site because the unhealthy-but-running backends + healthy frontend + Cloudflare tunnel mask the degradation.
- **L-OP-247 (2026-05-06) explicitly warned** "ADR records intent; reality may lag silently. Drift-scan periodically." The drift continued 7 days; lesson was filed but never enforced.
- **Multi-agent collisions are routine**: at the time of this ADR draft, `codex-1` and `codex-2` worktrees are both checked-out on `feature/OP-995-runner-fresh` (a state `git worktree` is supposed to forbid). Codex-2 has 5 modified files; codex-1 has only a sentinel. AUDIT-24 fencing-token mutex addressed JIRA pickup race; it does not address worktree branch race.
- The "miracle" that the system has held together is the **operator manually rescuing** every collision, every silent break, every missing sync, every stale label. That operator labour is itself a unsustainable resource: it scales with downtime not with feature delivery.

### 1.2 Why this needs an explicit ADR (not just more tickets)

The previous ADR pattern was: "decide → write → call Accepted → assume done." The pattern broke at the *call Accepted* step — the doc state diverged from runtime state, and no contract closed the loop.

ADR-0023 is **deliberately different**: it does NOT make a new design decision. It locks down the *implementation contract* for two existing Accepted ADRs that were never executed. The decision is *implement what we already decided*. The ADR locks scope, ordering, success criteria, and rollback triggers, so the work is auditable instead of vibes-driven.

### 1.3 Why AUDIT-31 (not "continue AUDIT-29")

AUDIT-29 is pre-rc2 stabilisation (Cognee + 3D memory + Coordinator). It assumed underlying infra was sound. Discovery proved the underlying infra is not sound: AUDIT-29 was built on sand. AUDIT-31 is a different layer of work: it rebuilds the sand into a foundation, so AUDIT-29 (Coordinator + 3D memory) actually delivers value once it sits on the new floor.

Concrete: rc2 cut depends on real staging-gate signals. Staging-gate signals depend on real CI publishing per-develop-commit images. CI publishing depends on Gerrit→GitLab replication. Replication depends on the GitLab project actually existing. Each layer was assumed but absent. AUDIT-31 builds the chain.

---

## §2. Decision

**Execute the Foundation Rebuild as Sprint S12: Bedrock** — an 11-sub-phase META + ~250 children, taking approximately 18-26 wall-clock weeks (per operator-adjusted 2× budget), structured so that each phase has a hard exit criterion before the next can begin, with the explicit constraint that **production-serving containers must not be restarted into a new image during the rebuild** (definition below).

### 2.0 Definition: "production containers MUST NOT restart"

This constraint is widely-cited in this ADR and was ambiguous in v1. v2 makes the boundary explicit:

**FORBIDDEN during rebuild (counts as "restart")**:
- Stop + start of any container in the `omnisight-productizer` compose project (backend-a, backend-b, caddy, cloudflared, frontend, installer, docker-socket-proxy)
- `docker compose down` then `docker compose up` on prod compose project
- Pull-new-image-then-recreate-container on prod compose project
- Forcing a new image tag onto a running prod backend
- `systemctl restart` on any unit that has an `ExecStart=` that supervises prod compose project containers

**PERMITTED during rebuild (does NOT count as "restart")**:
- Adding sibling compose projects on the same WSL (e.g., Prometheus / Grafana / Alertmanager containers in their own compose project)
- Installing new systemd units that do NOT touch prod compose containers
- `caddy reload` (config reload without process restart, atomic upgrade)
- Reading `/metrics` from prod backends (Prometheus scrape)
- Modifying / installing files in `/home/user/sora-bridge/deploy/systemd/*.service` source dirs (does not affect running unit until enable)
- Editing source files in working tree (containers run from BUILT image, not source)
- Adding monitoring sidecars whose lifecycle is independent of prod compose project
- Verifier insertion in `deploy-prod.sh` (only affects FUTURE deploys, not current containers)

**OPERATOR-WINDOW REQUIRED (allowed but only in scheduled maintenance)**:
- Phase 31.I prod health hardening: graceful drain + restart of one backend replica at a time (rolling, never both at once)
- Phase 31.J cutover: final container-image swap to new namespaced + signed images (one replica at a time)
- Phase 31.K cold-start drill: full stop + start of prod compose (scheduled off-peak)

The hard rule: **non-operator-window changes never touch prod compose container lifecycle**. Phase 31.G observability and 31.H systemd discipline must be designed to satisfy "permitted" semantics. Phase 31.I and 31.J explicitly require operator-window because they cross this boundary.

The five load-bearing architectural decisions, all of which override defaults preserved by inertia from 2026-05-04 ADRs:

### 2.1 GitLab CI is *the only* CI

GitHub Actions are **decommissioned as gating CI** entirely. They will not be migrated. They will not be kept "in parallel for safety." Per operator decision 2026-05-13: the project considers only GitLab CI from this point forward. GitHub remains a read-only mirror for OSS visibility (per ADR-0002). The 30 existing GitHub workflows are deprecated by Phase 31.E; their replacement is `.gitlab-ci.yml`. Any future re-evaluation of GitHub Actions is out of scope for AUDIT-31 and gets its own ADR.

**Parity model clarification (v2)**: GitLab CI parity for Wave 1 (ci.yml migration) is validated by:
1. Running the migrated GitLab CI pipeline against the *same git SHA* that previously ran on GitHub Actions
2. Comparing **historical run outputs from GitHub Actions** (pre-decommission) against fresh GitLab CI runs
3. **No concurrent dual-CI execution** — GitHub Actions stays disabled (workflows renamed `.disabled` from day 1 of Wave 1)
4. Parity criteria: same pass/fail per job AND same required-check semantics AND artifact-name compatibility for downstream consumers (≥95% match required to advance from Wave 1 → Wave 2)

The "shadow mode" language used in v1 §6.1 was misleading; corrected in v2.

### 2.2 ci.yml first

Within Phase 31.E (GitLab CI migration), the migration order is **`ci.yml` (719 LOC, ~15 jobs, PR-gating critical) first**, then image-build, then live-tests, then release, then everything else. Rationale: gating CI is the highest-trust layer; without it the migration cannot be validated. Image-build comes second because rc2 depends on it.

### 2.3 cosign key-based, not keyless

cosign signing uses real ed25519 keypair (cosign generate-key-pair). Public key committed at `deploy/cosign/cosign.pub`. Private key + password stored as GitLab CI variables (`SORA_COSIGN_KEY` + `SORA_COSIGN_PASSWORD` — already named in existing workflows). **No GitHub OIDC dependency, no Sigstore Fulcio dependency, no GitLab OIDC dependency.** This is the simplest path that's fully self-sovereign and works the same on GitLab CI as on any other future runner.

### 2.4 ai-core integration preserved, not migrated

ai-core (`/home/user/work/sora/omnisight-ai-core/`, 192GB ollama_storage) does NOT migrate. Productizer continues to consume ai-core via the existing `omnisight-ai-core_omnisight_net` external bridge network and the `ai_tunnel` Cloudflare URL. The interface contract is pinned in `backend/tests/test_phase1_ai_core_network.py`. Phase 31.F verifies the interface survives the cosign and image-namespace changes; nothing else touches ai-core.

### 2.5 Three runner-capacity scenarios baked into design

Every phase plan must be evaluable under three operator-capacity scenarios:

- **4-runner** (claude × 2 + codex × 2) — normal parallel mode
- **2-runner** (claude × 1 + codex × 1) — degraded mode (e.g. one runner instance fails, or operator wants to throttle for safety)
- **1-runner** (any single runner) — last-resort mode (e.g. mid-migration window with only one runner online)

This is not a planning afterthought. The phase ordering, ticket dependency DAG, and parallelism analysis explicitly produce **three different wall-clock estimates** so the operator can choose the capacity that matches reality without re-planning.

---

## §3. Architecture (post-AUDIT-31)

### 3.1 Source-of-truth + replication

```
                    ┌──────────────────────────────┐
                    │  Gerrit (sora.services:29418)│ ← source-of-truth
                    │  governance: submit-rules     │   (review + ACL + merge gate)
                    └────┬──────────────────────────┘
                         │  (replication plugin, push, real-time)
                         ↓
                ┌──────────────────────────────┐
                │  GitLab self-hosted          │ ← primary CI/CD + container registry
                │  sora.services:49156         │   omnisight/omnisight-productizer
                │  GitLab CI = the only CI     │
                └────┬─────────────────────────┘
                     │  (one-way push mirror)
                     ↓
                ┌──────────────────────────────┐
                │  GitHub                       │ ← read-only OSS visibility
                │  github.com/limit5/...       │   no CI gating; no PR review
                └──────────────────────────────┘
```

Replication is monitored. The drift detector wakes when SHA divergence > 1 commit OR replication has not run in > 15 min. Drift = P1 alert (Discord webhook).

### 3.2 CI/CD pipeline (GitLab CI)

```
push to refs/for/develop (review)
  │
  ▼
Gerrit review (humans + AI per submit-rule)
  │ +2 + submit
  ▼
Gerrit develop tip moves
  │
  ▼ (replication, ~30 s)
GitLab develop tip moves
  │
  ▼ (push event)
.gitlab-ci.yml pipeline runs:
  │
  ├── stage: validate
  │     ├── ci-tests (was: ci.yml)           ← 15 parallel jobs
  │     ├── migration-check
  │     └── migration-compat
  │
  ├── stage: build
  │     ├── image: backend (multi-arch: amd64 + arm64)
  │     ├── image: frontend
  │     ├── image: bridge
  │     ├── image: proxy (when v* tag)
  │     └── image: installer (when v* tag)
  │     ↓ (push to)
  │     GitLab Container Registry: registry.sora.services:49157/omnisight/...
  │     + mirror to GHCR: ghcr.io/limit5/omnisight-*
  │
  ├── stage: sign
  │     ├── cosign sign --key $SORA_COSIGN_KEY (key-based)
  │     └── attest digests + git_sha to release_audit table
  │
  ├── stage: live-tests (gated)
  │     ├── aws-kms-live (parallel)
  │     ├── gcp-kms-live
  │     ├── vault-transit-live
  │     ├── llm-live
  │     └── local-fernet-kms-live
  │
  └── stage: release (v* tag only)
        └── push tag + create release artefact + cosign sign

staging-sync.service (every 5 min) consumes Container Registry tag = sha-<develop-tip-short-sha>
  → docker pull → docker compose up -d --wait → /readyz green → JSONL emit
  → release_milestone_checker R3 gate = real signal
```

### 3.2.1 GitLab Runner architecture (v2 added)

The GitLab CI pipeline above presumes GitLab Runners exist and have specific characteristics:

| Aspect | Decision | Rationale |
|---|---|---|
| **Host** | Same physical host (X870E-NOVA-WIFI, Ryzen 9 9950X3D 32-core, 66 GB) running prod WSL Ubuntu-24.04, but in a **separate WSL distro** dedicated to CI runners (proposed: new "Ubuntu-22.04-ci" or repurpose existing idle Ubuntu-22.04) | Avoid prod-runner CPU contention; 32 cores is enough for both with cgroup quotas; multi-WSL is already the topology |
| **Executor type** | `docker` executor (Docker-in-Docker for image builds) + `shell` executor for fast jobs that don't need container isolation | Match existing GitHub Actions semantics; DinD needed for multi-arch image build |
| **Concurrency** | 6 concurrent jobs (3 reserved for image build, 2 for tests, 1 for live/scheduled) | Leaves headroom on 32-core host; matches ~20 peak from §0.2 with safety margin |
| **Tags** | `arch:amd64`, `arch:arm64` (via QEMU), `priv` (privileged DinD), `gpu` (RTX 4080 for future LLM jobs) | Allows job-level pinning |
| **Privileged Docker** | Required for image build jobs (Buildx + multi-arch); restricted via runner config to only image-build jobs by tag | Trade-off acknowledged: privileged = attack surface; mitigated by namespace + caps |
| **Cache** | GitLab Runner cache backed by local disk (S3-compatible MinIO container later if storage grows) | Local-first; remote when scale demands |
| **Artifact storage** | GitLab project's built-in artifact store (initial); MinIO sidecar if quota exceeded | Match GitLab defaults |
| **Network** | Same WSL bridge as prod; egress allowed to GHCR (mirror push) + sigstore.dev (cosign keyless legacy) + anthropic.com + openai.com (for LLM-live-tests) | Document allowed egress in runner config |
| **Secrets** | GitLab CI variables (encrypted at rest); operator provisions one-shot at runner bring-up | Mirror existing 37-secret GitHub model, but on GitLab |

**Operator-deploy steps** (one-shot, in Phase 31.E pre-flight, NOT runner-doable):
1. Provision the CI WSL distro
2. `apt install gitlab-runner` + `gitlab-runner register` with the project's runner token
3. Verify `gitlab-runner verify` returns success
4. Provision the 37 secrets via GitLab CI variables UI (see Phase 31.E spec)

### 3.3 Image distribution + cosign

- **GitLab Container Registry** is primary (where CI pushes).
- **GHCR** is one-way mirror (for prod consumers that pull from GHCR; legacy + Helm chart references).
- All `your-org` placeholder occurrences (20+ across repo) are replaced with the real namespace via a templated abstraction: env-var `OMNISIGHT_GHCR_NAMESPACE=limit5` becomes mandatory in `~/.config/omnisight/staging-compose.env`, `release-audit.env`, `prod-deploy.env` (all 3 staged).
- cosign signs every published tag.  Verifier expects real `cosign.pub` (PEM). Verifier failure on any `deploy-prod.sh` call → operator escape only via explicit `--insecure-skip-verify`.

### 3.4 5-env separation (mapping to existing multi-wsl-deployment.md)

The "5 environments" are conceptual; the physical topology is **3 WSL distros + 1 deploy strategy + 1 in-process testing**:

| Env | WSL distro | Topology | Status pre-AUDIT-31 | Status post-AUDIT-31 |
|---|---|---|---|---|
| **Dev** | Ubuntu-26.04 | own backend + frontend + tests | ✗ WSL clean but not provisioned | ✓ provisioned via `setup-dev-env-full.sh` |
| **Testing** | Ubuntu-26.04 (shared with Dev) | in-process pytest | ✗ implicit | ✓ explicit `os-test` alias |
| **Staging** | Ubuntu-22.04 | own docker-compose.staging.yml | ✗ WSL idle, containers on prod WSL | ✓ containers on Ubuntu-22.04 |
| **Canary** | Ubuntu-24.04 (within Prod) | Caddy reverse-proxy weight shift 5%→25%→100% | ✗ template exists, never run | ✓ deployed at release time via canary-runbook |
| **Prod** | Ubuntu-24.04 | docker-compose.prod.yml + Cloudflare Tunnel | ✓ running but quietly unhealthy | ✓ running + actively monitored |

ai-core (192GB) stays on prod WSL (Ubuntu-24.04) and is reached via existing `omnisight-ai-core_omnisight_net` bridge from prod-WSL Productizer containers, OR via Cloudflare URL `ai_tunnel` from dev-WSL Productizer.

### 3.5 Multi-agent runner pattern (post-fix)

```
Pickup time:
  1. Runner acquires JIRA claim (AUDIT-24 fencing-token, already works)
  2. Runner constructs unique branch name:
       feature/{TICKET}-{INSTANCE}-pid{PID}-{EPOCH}-runner
     - {TICKET}    = OP-1234
     - {INSTANCE}  = claude-1 | claude-2 | codex-1 | codex-2
     - {PID}       = OS process ID
     - {EPOCH}     = unix seconds
     - example:    feature/OP-1234-claude-1-pid12345-1715583600-runner
     → guaranteed collision-free across runners
  3. Runner creates ephemeral clone (OP-1045):
       /tmp/runner-pickup/{INSTANCE}/{EPOCH}-{TICKET}/
       git clone --depth N --branch develop gerrit:omnisight/OmniSight-Productizer.git
     - separate `.git/objects/` per pickup (no tree-reuse pollution)
     - auto-deleted on push success
     - preserved on push failure (operator review)
  4. Runner creates branch from develop tip in ephemeral clone
  5. Runner does work, commits, pushes
  6. On push success: clone deleted, branch deleted locally, only Gerrit Change-Id remains
  7. On push failure: clone preserved at well-known path; operator sees in /tmp/runner-pickup/{INSTANCE}/

Worktree pattern (5-worktree, shared object store) is RETIRED for runner pickups.
```

**v2 clarification of "runner worktree" terminology** (codex review surfaced contradiction with Appendix D):

After Phase 31.B completes:
- **Runner pickup directories** = ephemeral clones at `/tmp/runner-pickup/{INSTANCE}/{EPOCH}-{TICKET}/`. Created per pickup, destroyed on push success. **NOT git worktrees.** **NOT persistent.**
- **Runner launcher root** = persistent directory per runner instance (e.g., `~/runner-claude-1/`) that holds:
  - The tmux session
  - The runner Python script (auto-runner-jira.py)
  - The runner's `~/.cache/omnisight/` state
  - JSONL emit destinations
  - NO checked-out source code
  - On pickup, the runner script `git clone`s into a fresh ephemeral path under `/tmp/runner-pickup/`
- **Long-running operational checkouts** = `sora-bridge` (gerrit-jira-bridge daemon), `omnisight-main-sync` target. These are distinct from runner pickups; they use the "isolated long-lived checkout" pattern (OP-798).

Appendix D Phase 4 language ("provision 4 runner worktrees in dev WSL") is corrected in v2: it should read "provision 4 runner **launcher roots** in dev WSL (no checked-out source; ephemeral clones happen per pickup per §3.5)".

### 3.6 Observability + alerting

```
                ┌────────────────────────────────────┐
                │ omnisight-journal-error-forwarder  │
                │   tails systemd journal            │
                │   classifies via configs/error_pager.yaml  │
                └────┬───────────────────────────────┘
                     │
                     ▼
                ┌────────────────────────────────────┐
                │ operator_notifier.py (3-tier)       │
                │   classify by severity              │
                └────┬─┬─────────────────────────────┘
                     │ │
        ┌────────────┘ └──────────────┐
        ▼ P0 / P1                       ▼ P2 / P3
   Discord webhook                  Email digest (6h batches)
   (immediate, mobile push)         (Gmail SMTP, app password)

   Prometheus + Grafana + Alertmanager (containers, prod WSL):
     - scrape /metrics on all backend instances
     - alert rules: omnisight_ha_07 group
     - Alertmanager → operator_notifier → Discord/Email
     - Grafana dashboard uid omnisight-ha-07
```

### 3.7 Runner capacity scenarios

Every phase plan in §4 is annotated with three wall-clock estimates:

- **C4** (4 runners): claude × 2 + codex × 2 — normal
- **C2** (2 runners): claude × 1 + codex × 1 — degraded
- **C1** (1 runner): any single runner — last-resort

The critical-path bottleneck phases (31.A, 31.B, 31.E ci.yml, 31.J cutover) are operator-supervised regardless of runner count: the wall-clock under each capacity is dominated by operator review + verification windows, not by parallel-task throughput.

Sub-phases that ARE runner-throughput bound (31.D replication, 31.F image dist, 31.G observability, 31.H systemd install) scale ~linearly with runners up to C2; beyond C2 the parallelism return is small because the sub-tickets are individually <0.5d.

| Phase | C4 estimate | C2 estimate | C1 estimate |
|---|---|---|---|
| 31.A Dev WSL one-button | 3-4 wk | 4-5 wk | 5-6 wk |
| 31.B Multi-agent fix + OP-1045 | 2-3 wk | 3-4 wk | 4-5 wk |
| 31.C Notifier | 1-2 wk | 1-2 wk | 2-3 wk |
| 31.D Replication | 2-3 wk | 2-3 wk | 3-4 wk |
| 31.E GitLab CI (waves) | 8-12 wk | 10-14 wk | 14-18 wk |
| 31.F Image + cosign | 2-3 wk | 3-4 wk | 4-5 wk |
| 31.G Observability | 2-3 wk | 2-3 wk | 3-4 wk |
| 31.H Systemd discipline | 1-2 wk | 2-3 wk | 3-4 wk |
| 31.I Prod health hardening | 1-2 wk | 1-2 wk | 2 wk |
| 31.J 5-env cutover | 3-4 wk | 3-4 wk | 4-5 wk |
| 31.K Doc + ADR amendment | 1-2 wk | 1-2 wk | 2 wk |
| **Total (with parallel work)** | **18-26 wk** | **22-32 wk** | **30-42 wk** |

### 3.8 ai-core interface preservation

ai-core compose stack stays on Ubuntu-24.04 (prod WSL) with:
- `ai_gateway` (nginx :8080) — public via Cloudflare Tunnel
- `ai_engine` (ollama) — needs separate ticket to restart (exited 9d ago)
- `ai_cache` (redis) — joined to Productizer via external `omnisight-ai-core_omnisight_net`

AUDIT-31 only:
- Verifies the external network contract survives image-namespace + WSL changes (`test_phase1_ai_core_network.py` runs in CI)
- Documents ai-core restart procedure in operator runbook (NOT migrating)
- Does NOT touch ollama_storage, model loading, or ai_engine restart logic

Separate ticket (OP-XXX, out of AUDIT-31) covers ai_engine 9d-exited recovery.

---

## §4. Phase plan (11 sub-phases)

Decomposition principle: each sub-phase = one sub-META. Each sub-META has 4-section AC discipline (Code / Deploy / Integration / Exercised) + Go-Live target. Per operator-adjusted estimate, **~250 children total**. The ticket decomposition strategy is *deliberately deferred* until after operator review of this ADR (per operator decision 2026-05-13).

### 31.A — Dev WSL Ubuntu-26.04 one-button provisioning

**Goal**: From a clean Ubuntu-26.04 WSL, run `scripts/setup-dev-env-full.sh` once → working dev environment in <30 min with no manual prompts beyond cred secrets.

**Scope** (16 gap items extending the existing 213-line `setup-dev-env.sh`):
- apt packages (extended set; postgresql-client; build-essential)
- nvm + Node LTS + pnpm
- Python venv + requirements
- `~/.config/omnisight/*` cred provisioning (semi-automatic with operator prompt for secret values)
- SSH keys for Gerrit bots (claude-bot + codex-bot + merger-bot)
- Gerrit clone (NOT GitHub) per L-OP-247 fix
- 4 runner worktrees (claude-1/2 + codex-1/2) provisioning
- PostgreSQL container + alembic upgrade head verification
- Cognee + Neo4j + Graphiti compose stack (dev mode, separate from prod WSL)
- GHCR Docker login
- 25 of 84 systemd user units (dev subset)
- tmux session bootstrap (4 runners, paused initially)
- Cross-WSL network probe (verify can reach prod-WSL services for diagnostics)
- Validation suite (post-install smoke)
- `--dry-run` mode + `--resume` mode + `--validate` mode

**Critical safety property**: This phase does NOT touch prod WSL. It only sets up dev WSL. The cutover happens in Phase 31.J.

### 31.B — Multi-agent runner permanent fix

**Goal**: Two runners on the same JIRA ticket can never produce a worktree-branch race; one runner's commits can never be polluted by another runner's stale objects.

**v2 added — Critical chicken-and-egg protocol (B-0 prerequisite)**:

The work IN 31.B changes how runners run, while runners are running. Without protocol the deployment race can reproduce the exact bug being fixed. Mandated sequence:

1. **Freeze**: All but ONE runner instance paused (graceful Ctrl-C tmux). The remaining runner is the "deploy runner" and is operator-attended.
2. **Sole-runner B-1+B-2 ticket execution**: deploy runner picks up B-1 and B-2 sub-tickets in single-pickup mode, with explicit operator review at each commit.
3. **Self-verification**: deploy runner's own NEXT pickup uses the new code path (branch namespacing + ephemeral clone). Verify mutex selects correctly + no branch collision + stale-state cleanup.
4. **Phased re-activation**: bring up codex-1, then claude-1, then codex-2, then claude-2 in sequence, each with 1-hour observation window between activations.
5. **Rollback flag honored**: any runner can fall back to legacy worktree via `OMNISIGHT_RUNNER_LEGACY_WORKTREE=1` env if deploy runner self-verification fails.

This protocol is itself a sub-phase: **31.B-0 (chicken-and-egg freeze)** must execute before any of B-1 through B-5.

**Scope**:
- **B-0 (chicken-and-egg freeze, v2 added)**: Operator-supervised freeze protocol above. 1 day operator window.
- **B-1 (immediate, branch namespacing)**: `auto-runner-jira.py` branch creation uses `feature/{TICKET}-{INSTANCE}-pid{PID}-{EPOCH}-runner` format. Unit tests validate uniqueness under concurrent pickup.
- **B-2 (ephemeral clones, OP-1045)**: Runner pickup uses per-pickup `git clone` in `/tmp/runner-pickup/{INSTANCE}/{EPOCH}-{TICKET}/`. Shared object store retired for runner pickups. On push success: clone deleted. On push failure: preserved for operator review at a well-known path.
- **B-3 (cleanup)**: 56+ stale `feature/OP-*-runner-fresh` and `agent/*` branches in main repo cleaned up after migration. Automated cleanup runs nightly with safety floor.
- **B-4 (tests + chaos)**: Integration test simulates 4-runner concurrent pickup on same ticket; verifies AUDIT-24 mutex selects one + others abort cleanly; verifies no branch collision; verifies stale-state cleanup.
- **B-5 (rollback flag)**: env var `OMNISIGHT_RUNNER_LEGACY_WORKTREE=1` falls back to old shared-worktree pattern (for emergency).

### 31.C — Operator notifier (Discord + Email)

**Goal**: Any production-affecting event surfaces to operator within 60 sec for P0, 5 min for P1, or in next 6-hour digest for P2/P3.

**Scope**:
- `operator_notifier.py` with 3-tier routing
- Discord webhook P0 + P1 (2 webhook URLs as secrets)
- Email digest (SMTP via Gmail app password)
- `configs/error_pager.yaml` classifier (per-unit severity + per-event-type rules)
- `omnisight-journal-error-forwarder.service` install + boot enable (Restart=always, RestartSec=5)
- Wire 6 existing `OnFailure=` chains (staging-gate-canary, staging-gate-smoke, staging-sync, staging-pg-snapshot, omnisight-canary, omnisight-credential-expiry-check) to notifier
- End-to-end test: simulated journal ERROR → notifier → Discord/Email roundtrip in <60 s

### 31.D — Source-of-truth replication

**Goal**: Gerrit develop, GitLab develop, GitHub develop all align at same SHA within 60 sec of any Gerrit submit. Drift > 1 commit OR replication idle > 15 min → P1 alert.

**Scope**:
- Gerrit replication plugin install (operator-supervised; one-shot)
- `replication.config` for `[remote "gitlab-mirror"]` and `[remote "github-mirror"]` (per L-OP-247 fix; hardcoded URL, no env-var substitution)
- SSH keys / HTTP creds for both mirror destinations
- Replication health monitor script (every 5 min cron, JSONL emit + drift-alert wire)
- ADR-0002 lesson: drift surfaced within 60 sec instead of silently after a week

### 31.E — GitLab CI migration (largest sub-phase)

**Goal**: All gating CI runs on GitLab CI. GitHub Actions deprecated.

**Scope** (waves, **ci.yml first per operator decision**):

- **Wave 1: ci.yml (15 jobs, 719 LOC)** — PR-gating critical-path. Migrate to `.gitlab-ci.yml` stages. Each job validated against parity baseline.
  - Sub-tickets per job: backend-tests, lint, backend-migrate, frontend-unit, openapi-contract, conflict-marker-drift, lessons-index-git-gate, lockfile-drift, env-secrets-plaintext, auth-coverage, llm-adapter-firewall, runner-bubblewrap-escape-regressions, runner-past-failure-regressions, secret-pre-commit, catalog-schema, renovate-config, proxy-tests, backend-compliance, backend-critical, backend-coverage-combine, backend-loadout
- **Wave 2: image build** — build-images.yml + docker-publish.yml + image-build.yml → `.gitlab-ci.yml build` stage. Multi-arch (amd64 + arm64) via GitLab Runner with QEMU. Push to GitLab CR + mirror to GHCR.
- **Wave 3: cosign signing** — `.gitlab-ci.yml sign` stage. Key-based (per 2.3). `SORA_COSIGN_KEY` + `SORA_COSIGN_PASSWORD` GitLab CI variables.
- **Wave 4: live tests** — aws-kms / gcp-kms / vault / llm-live / local-fernet-kms → GitLab CI scheduled pipelines. Externally-credentialed.
- **Wave 5: release pipeline** — release.yml + scheduled (dr-drill-daily, postgres-backup-dr, cve-scan, lint-audit-nightly, e2e-multi-device-nightly) → GitLab CI schedules.
- **Wave 6: other** — frontend-stale-detector / merge-arbiter / migration-check / migration-compat / blue-green-gate / k8s-helm-smoke / major-upgrade-gate / db-engine-matrix / api-compat / fallback-branches / docs-site-publish / eol-check / milestone-check / ab-anthropic-tests / upgrade-preview / multi-version-matrix → GitLab CI.
- **Wave 7: decommission** — Rename `.github/workflows/*.yml` → `*.yml.deprecated`. Keep GitHub repo accessible read-only. Tag last commit before rename with `last-github-actions-run`.

### 31.F — Image distribution + cosign real-ification

**Goal**: Every image push is signed with a real cosign key. Every consumer verifies (or explicitly bypasses with `--insecure-skip-verify` operator flag).

**Scope**:
- `cosign generate-key-pair` (operator-supervised, one-shot)
- Update `deploy/cosign/cosign.pub` from placeholder to real PEM
- Provision `SORA_COSIGN_KEY` + `SORA_COSIGN_PASSWORD` GitLab CI variables
- Replace `your-org` placeholder in 20+ files via templated abstraction (env-driven, not hard-coded substitution)
- `scripts/verify_image_signature.sh` updated to recognize new key + new identity regex (or fall back to keyless if image is from before AUDIT-31 cutover)
- All consumers (deploy-prod.sh, staging-sync.service, staging-pg-snapshot.service) call verifier on each pull
- Audit log: cosign sign + verify events written to `release_audit` table (so audit-walk can cross-reference)

### 31.G — Observability stack deployment

**Goal**: Prometheus + Grafana + Alertmanager actually running, scraping prod metrics, raising alerts that hit the operator notifier from 31.C.

**Scope**:
- 3 containers (prometheus / grafana / alertmanager) in own compose project on prod WSL
- Prometheus scrape config for backend, frontend, postgres, neo4j, cognee, ai-core gateway
- Grafana dashboard import (uid `omnisight-ha-07`)
- Alertmanager routing to operator_notifier
- alert rules deploy (`omnisight_ha_07` group: ReplicaLagHigh / RollingDeploy5xxRateHigh / BackendInstanceDown + extensions per Phase 31.I findings)
- omnisight-canary.service health-check integration

### 31.H — Systemd discipline + missing 59 units

**Goal**: 84/84 sora-bridge systemd units installed (or explicitly retired with rationale). Critical daemons have `WatchdogSec=`. `omnisight-compose-prod.service` installed so prod auto-recovers on host reboot.

**Scope**:
- Audit each of 84 units: install (dev or prod or both) | retire | defer
- WatchdogSec= for: gerrit-jira-bridge.service, omnisight-backend.service, omnisight-frontend.service, omnisight-graphiti-mcp.service, ci-worker.service
- `omnisight-compose-prod.service` install on prod WSL
- `omnisight-journal-error-forwarder.service` system-scope install (already in 31.C)
- Each unit has clear ownership + restart policy + alert hookup

### 31.I — Production health hardening

**Goal**: 0 production containers `unhealthy` for >1h without alert. Cold-start drill: deliberately stop+start prod, recover in <5 min, no data loss.

**Scope**:
- Fix `omnisight-productizer-backend-a/b` 5d unhealthy (root cause: same migration mismatch as staging; resolves after Phase 31.A's alembic upgrade + Phase 31.E's fresh image build)
- Fix `omnisight-productizer-caddy-1` 9d unhealthy (config audit; likely TLS cert renewal or upstream backend healthcheck dependency)
- Define recovery procedure for each prod container (runbook entry per container)
- Cold-start drill: scheduled window (off-peak), graceful stop, validate restart sequence
- Health check alerting via Phase 31.G chain

### 31.J — 5-env carve-out cutover

**Goal**: Dev work runs on Ubuntu-26.04 (dev WSL). Prod containers untouched throughout. Staging containers move to Ubuntu-22.04. Canary procedure documented + tested.

**Scope** (6-phase migration sequence; explicit non-destructive design):
- Phase 0: Inventory + emergency backup (codex-2 in-flight 5-file work preserved)
- Phase 1: Drain runners on prod WSL (graceful Ctrl-C, JIRA claim cleanup)
- Phase 2: Provision dev WSL via Phase 31.A's `setup-dev-env-full.sh`
- Phase 3: Restore in-flight work via `/mnt/wsl` shared mount or git apply
- Phase 4: Provision 4 runner worktrees in dev WSL
- Phase 5: Verify ONE ticket cycle dev WSL pickup → Gerrit push
- Phase 6: Cutover decision (A: dev WSL active / prod runners stopped, B: dual-run for 1 week, C: rollback)
- Reactivate Ubuntu-22.04 staging WSL
- Define canary as Caddy weight-shift deployment strategy (no separate WSL); test 5%→25%→100% via dry-run

### 31.K — Documentation + ADR amendment + lesson

**Goal**: ADR canon reflects implemented reality. Per-phase runbook exists. Lesson captured for future drift prevention.

**Scope**:
- Each sub-phase produces a per-phase runbook (under `docs/operations/`)
- ADR amendment commit: update ADR-0001 and ADR-0002 "Status" field from "Accepted" to "Accepted + Implemented (via AUDIT-31)" with the implementation date
- L-OP-NNN lesson: "Aspirational ADR pattern — how to detect + remediate" with the AUDIT-31 narrative as case study
- Operator retrospective document under `docs/retrospectives/`
- Lesson surfaced via Cognee for future agent runs (per AUDIT-29 31.A wire-up)

---

## §5. Critical path + parallel work

```
31.A (3-4 wk @ C4) ────→ 31.B (2-3 wk) ────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
                                          │                                                                                                          │
                                          ↓                                                                                                          │
                                       31.D replication (2-3 wk)                                                                                     │
                                       31.E ci.yml first wave (4-6 wk in C4)                                                                         │
                                       31.F cosign + image namespace (2-3 wk; depends on 31.E wave 2)                                                │
                                          │                                                                                                          │
                                          ↓                                                                                                          │
                                       31.G observability (2-3 wk; can start with 31.D)                                                              │
                                       31.H systemd 59-install (1-2 wk; can start anytime)                                                           │
                                       31.I prod health (1-2 wk; runs after 31.A + 31.E wave 2)                                                      │
                                          │                                                                                                          │
                                          ↓                                                                                                          │
                                       31.E remaining waves (4-6 wk in C4)                                                                           │
                                          │                                                                                                          │
                                          └──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
                                                                                                                                                     │
                                                                                                                                                     ↓
                                                                                                                                                  31.J cutover (3-4 wk)
                                                                                                                                                     │
                                                                                                                                                     ↓
                                                                                                                                                  31.K doc + ADR (1-2 wk)
                                                                                                                                                     │
                                                                                                                                                     ↓
                                                                                                                                                  ★ rc2 cut (gated on AUDIT-31 done)
```

### 5.1 What can run in parallel under C4

- **Always parallel-safe**: 31.G observability, 31.H systemd 59-install, 31.K doc drafting
- **Parallel with 31.A**: 31.C notifier (separate codepath), 31.D replication design (operator-supervised plugin install can wait)
- **Parallel with 31.B**: 31.E wave 1 ci.yml migration (uses GitLab CI runners, not Productizer code)
- **Parallel with 31.E**: 31.F image dist (depends on wave 2 image build done), 31.I prod health
- **Strictly sequential**: 31.A → 31.B (multi-agent fix needs dev WSL stable to test) → 31.J (cutover needs B fix verified)

### 5.2 Operator-attention windows (irreducible)

Even with C4 parallelism, **operator-supervised work cannot parallelize**:

- 31.A cred provisioning prompts
- 31.B ephemeral-clone deploy on 4 runners (one at a time)
- 31.D Gerrit plugin install (Gerrit admin SSH; one-shot, no rerun)
- 31.E Wave 1 ci.yml parity validation (4-eyes review per job)
- 31.F cosign keypair generation (one-shot)
- 31.J Phase 5 verify one-ticket cycle (operator watches)
- 31.J Phase 6 cutover decision (operator commits to path)

Each operator-attention window is 1-2 days plus 0.5-1 day verification. Total operator-attention floor: ~3-4 weeks. C4 can compress everything else around this floor but cannot eliminate it.

---

## §6. Risk registry + rollback triggers

### 6.1 Risk-by-phase

| Phase | Top risk | Mitigation | Rollback trigger |
|---|---|---|---|
| 31.A | dev WSL provisioning fails mid-way | `--resume` flag, idempotent steps, `--dry-run` first | abort if dry-run shows error; prod WSL unaffected |
| 31.B | branch namespacing breaks existing claimed tickets | feature flag (legacy worktree fallback); migration script renames in-flight branches | OMNISIGHT_RUNNER_LEGACY_WORKTREE=1 reverts |
| 31.C | Discord webhook rate-limited (30 req/min) | classifier merges P0 storms; circuit-breaker test before deploy | revert to log-only mode |
| 31.D | Gerrit replication plugin breaks Gerrit submit-rule eval | install on Gerrit non-prod first; rehearse one project before all | Gerrit `plugin disable replication` reverts to no-replication state |
| 31.E ci.yml | GitLab CI parity gap not caught | Wave 1 validated via historical-comparison parity (see §2.1; no concurrent dual-CI) | ≥95% parity gate required; Wave 1 may iterate up to 3 times before declared failure |
| 31.E image build | GitLab CR storage cost or quota | establish retention policy first; cap concurrent build jobs | Wave 2 build can be paused; existing GHCR images stay |
| 31.F cosign | private key leak | key stored only as GitLab CI variable + operator local; not in repo | re-issue key + rotate |
| 31.G observability | Prometheus consumes too much disk | retention 7d; cardinality budget per metric family | drop high-cardinality labels first |
| 31.H | restart unit changes break running service | each unit restart tested in dev first | systemd revert to last good unit |
| 31.I | cold-start drill loses data | run during off-peak; backup before; staged retry | restore from backup_hourly.timer artefact |
| 31.J | cutover decision wrong | 6-phase explicit; Phase 5 verify gates Phase 6 | rollback = stop dev WSL runners, prod WSL resumes |
| 31.K | doc canon drift | each phase commits its runbook with the impl PR | doc audit at sub-meta close |

### 6.2 Cross-phase rollback triggers

A rollback at any of these triggers cascades AUDIT-31 to a safer checkpoint:

- **Trigger A**: 31.A's `setup-dev-env-full.sh` produces a working dev WSL but operator can't actually do dev work on it (test failure, missing tool, surprise dep) → halt 31.J, keep working on 31.A.
- **Trigger B**: 31.B's branch namespacing causes any data loss in any pickup → revert to legacy worktree pattern; debug; reset.
- **Trigger C**: 31.E Wave 1 (ci.yml) GitLab parity gap > 5% (jobs whose GitLab outcome differs from historical GitHub outcome on the same SHA) → halt Wave 2 advance; iterate Wave 1 (up to 3 iterations); after 3 failed iterations, ADR amendment required to revise scope.
- **Trigger D**: 31.G observability stack consumes > 20% of prod WSL memory → reduce retention, drop labels, scale back.
- **Trigger E**: 31.J Phase 5 verify ANY ticket cycle fails → cutover decision is C (rollback); don't touch prod WSL runners.

---

## §7. Success criteria

### 7.1 Per-phase exit criteria (each has 4-AC discipline + Go-Live)

Each sub-phase requires:
- **Code AC** (modules / scripts / unit files exist + tests pass)
- **Deploy AC** (artefacts installed on the target host)
- **Integration AC** (cross-feature contract verified)
- **Exercised AC** (production observation: N invocations / M events / K days uptime)

### 7.2 META-level exit criteria

AUDIT-31 META closes when ALL of:

- ✓ Dev WSL Ubuntu-26.04 alive with 5 runners (or fewer per chosen scenario) producing real Gerrit changes
- ✓ Prod WSL containers all `healthy` for ≥48 h consecutive
- ✓ 0 `unhealthy` containers for >1h ever during last 14 d without alert
- ✓ Gerrit + GitLab + GitHub SHAs align at same SHA over 7 d (excluding obvious sub-second replication windows)
- ✓ GitLab CI runs all 30 (formerly GitHub) workflows green at parity
- ✓ Real cosign signatures on ≥10 published images
- ✓ Operator notifier delivered ≥5 real P0/P1 events (or zero days without an event + simulated test)
- ✓ Cold-start drill completed (<5 min recovery) with operator runbook
- ✓ ADR-0001 + ADR-0002 status amended to "Implemented (via AUDIT-31)"
- ✓ Lesson L-OP-NNN about aspirational ADR pattern surfaced via Cognee

### 7.3 rc2 cut gates on AUDIT-31 META close

Per operator decision 2026-05-13: **rc2 RELEASE-v0.5.0-rc2 META does not begin until AUDIT-31 META is fully closed**. Existing rc1 force-promote pattern stays as escape hatch for emergencies, but the regular path waits.

---

## §8. Time budget (operator-adjusted)

Per operator decision 2026-05-13: original 9-13 wk estimate is doubled to **18-26 wk** (C4) wall-clock to account for unforeseen scope expansion + manual rescue + operator review windows. Under degraded capacity scenarios:

- C4 (4 runners): **18-26 wk** (~4.5-6.5 months)
- C2 (2 runners): **22-32 wk** (~5.5-8 months)
- C1 (1 runner): **30-42 wk** (~7.5-10.5 months)

Ticket count estimate: **~250 children** across 11 sub-METAs. Ticket decomposition strategy and decomposition rules deferred to operator-claude review session (per operator 2026-05-13).

---

## §9. Relations to ADR-0001 + ADR-0002

ADR-0023 does not supersede ADR-0001 or ADR-0002. It amends them in the sense that:

- ADR-0001 (Five-Branch Git Flow) — design **stays**. AUDIT-31 phase 31.B adds the multi-agent runner pattern that ADR-0001 omitted. Phase 31.K updates ADR-0001 Status to "Accepted + Implemented (via AUDIT-31)".
- ADR-0002 (GitLab self-hosted primary, GitHub one-way mirror, Gerrit review layer) — design **stays**. AUDIT-31 phase 31.D + 31.E executes the implementation. Phase 31.K updates ADR-0002 Status to "Accepted + Implemented (via AUDIT-31)".

A pattern is captured for future drift prevention: **a new lesson** (L-OP-NNN, Phase 31.K) documents the "aspirational ADR" anti-pattern — when an ADR is Accepted but no implementation contract closes the loop, the doc state diverges from runtime state silently. The remediation pattern: every ADR with operational claims must spawn a META ticket with 4-AC discipline + Go-Live target before being signed off as "Implemented."

---

## §10. Alternatives considered

### Alt-1 — Forget the past ADRs; design fresh

Rejected: the 2026-05-04 ADRs were good design. The problem was execution, not design. A fresh ADR would risk re-litigating decided architecture and would lose lessons (L-OP-247 etc.).

### Alt-2 — Partial implementation (only what blocks rc2)

Rejected by operator (2026-05-13): "目前被挖出來，表示之前就沒做好，不該再往後延" — the recurring pattern of discovering missing foundation work proves partial fixes accumulate debt faster than they shed it.

### Alt-3 — Keep GitHub Actions as backup CI

Rejected by operator (2026-05-13): "目前 CI 只考慮 GitLab CI" — dual-CI maintenance is its own cost; commitment to one CI is cleaner.

### Alt-4 — Skip cosign; use no signing

Rejected: production deploy paths reference signature verification (deploy-prod.sh, runbooks). Removing signing without removing the verification calls leaves a worse "we sign but never check" or "we check but never sign" gap. Real signing closes the loop.

### Alt-5 — Migrate ai-core too

Rejected by operator (2026-05-13): ai-core's 192GB ollama_storage is not worth replicating; interface preservation is sufficient.

### Alt-6 — Run 1 single runner (no parallelism)

Considered: simpler ops, no multi-agent race ever. Rejected because operator's existing system relies on 4-runner throughput for delivery velocity. Phase 31.B targets fixing the race, not abandoning parallelism. The C1 scenario is an emergency-degraded mode, not a steady state.

---

## §11. Phase gate evidence model (v2 added)

Each sub-phase has hard exit criteria (per §7.1 4-AC discipline). v2 adds the **evidence model** — how a downstream phase knows an upstream phase is closed.

### 11.1 Evidence types per AC section

| AC section | Evidence types |
|---|---|
| **Code AC** | Gerrit Change-Id + merged status (link to commit) + test output (pytest/jest log artefact attached to sub-META JIRA ticket) |
| **Deploy AC** | systemctl status output / docker ps output / file existence + ownership check / output of `scripts/deployment-audit.sh` for that domain — emitted as JSONL artefact attached to JIRA ticket |
| **Integration AC** | Cross-feature contract test passing (test file path + commit SHA) OR live observation log (specific journal grep, specific HTTP call output) attached to JIRA |
| **Exercised AC** | Sustained observation evidence: ≥N events / ≥M hours uptime / ≥K successful invocations, with timestamps + source — attached to JIRA as evidence file |

### 11.2 Phase gate closure procedure

A sub-phase META is moved to `公開済み` only when:
1. All children of the sub-META are `公開済み` (auto-walked by Gerrit-jira-bridge)
2. The sub-META description has an **Evidence appendix** with 4 sections (Code/Deploy/Integration/Exercised), each populated
3. The Evidence appendix links to:
   - GitLab CI pipeline run URLs (or GitHub Actions run URLs for legacy)
   - Gerrit change URLs
   - File-evidence JSONL paths under `docs/audit/AUDIT-31-phase-{X}-evidence/`
   - Operator sign-off comment (form: `[operator-signoff phase-X exit] <date> <signature>`)
4. The downstream phase's blocked-by check looks at the META's status + the Evidence appendix presence

### 11.3 Who certifies

- **Code AC**: Gerrit `+2` from human reviewer (per existing submit-rule)
- **Deploy AC**: operator OR runner (if `class:operator` label absent, runner can attach evidence; else operator must)
- **Integration AC**: same as Deploy AC
- **Exercised AC**: operator only (requires judgment call on whether observation period is sufficient)

### 11.4 Evidence storage canonical path

```
docs/audit/AUDIT-31-phase-{phase-letter}-evidence/
  README.md          (per-phase evidence index)
  code-ac.json       (links to commits + Gerrit changes + test outputs)
  deploy-ac.json     (systemctl / docker / file evidence)
  integration-ac.json
  exercised-ac.json  (observation logs)
  operator-signoff.md (signed by operator with date)
```

The `docs/audit/AUDIT-31-baseline-2026-MM-DD.md` artefact (per §13) is the immutable starting baseline; per-phase evidence references it.

---

## §12. Operator-only vs runner-doable ticket taxonomy (v2 added)

Not every ticket in this Sprint can be done by a runner. Some operations are destructive, cross-system, or require credentials a runner doesn't have. v2 mandates explicit classification:

### 12.1 Operator-only tickets (NO `class:subscription-*` label)

These tickets get `class:operator` label OR no `class:*` label at all, so runner JQL skips them.

**Hard rules — always operator-only**:
- Gerrit plugin install / Gerrit config changes (no Gerrit admin SSH from runner)
- GitLab project creation, GitLab CI Runner registration, GitLab CI variable provisioning
- GitHub repo/workflow settings changes
- cosign keypair generation
- Secret rotation (real-value writes to `~/.config/omnisight/*` files)
- Database destructive operations (alembic upgrade head on prod; DROP / TRUNCATE)
- Cross-WSL command execution (running commands on Ubuntu-22.04 / Ubuntu-26.04 / a different WSL distro than where the runner is)
- Prod container restart / stop / pull-new-image
- Cold-start drill
- Cloudflare Tunnel configuration changes

### 12.2 Operator-supervised tickets (`class:subscription-*` + `class:operator-window` label)

These are runner-doable BUT require an explicit operator window:
- Phase 31.B B-0 (chicken-and-egg freeze + runner deploy)
- Phase 31.I prod backend rolling restart
- Phase 31.J Phase 5-6 cutover steps
- First-ever GitLab CI Wave 1 run (verify parity manually)
- First cosign-signed image push (verify signature roundtrip)

Runner picks up + does the code/config part; operator confirms before any deploy-side action.

### 12.3 Runner-doable tickets (normal `class:subscription-*`)

Everything else. Specifically:
- Code edits in `backend/`, `frontend/`, `scripts/`, `docs/`, `deploy/systemd/*` (writing new unit files for review, not enabling them)
- Tests (unit + integration)
- `.gitlab-ci.yml` edits + Gerrit push for review
- Documentation
- JIRA label hygiene (cleanup)
- Cosign verifier script changes
- Prometheus alert rule edits (file edits; deploy via separate operator window)

### 12.4 Prepare-only tickets

For tickets that ARE operator-only but where preparation can be done by runner:
- Generate the script that does the operator action
- Generate the runbook
- Generate the dry-run output
- Operator picks up the prepared artefact + executes

These get tagged `class:operator-prepare-only`: runner writes the artefact + pushes for review; operator executes after merge.

---

## §13. Inventory baseline artifact (v2 added — answers codex Q10)

**Mandate**: Before the first sub-phase ticket execution, an immutable inventory baseline MUST be filed at `docs/audit/AUDIT-31-baseline-2026-MM-DD.md`. This artefact freezes the "current reality" so all 250 tickets implement against the same assumptions.

### 13.1 Baseline contents (mandatory sections)

| Section | Data | Source command |
|---|---|---|
| Git remotes | All remotes for Productizer + sora-bridge + ai-core checkouts | `git remote -v` per checkout |
| Branches | All branches per remote; identify stale (>30d untouched) + active (referenced by JIRA ticket) | `git branch -a --sort=-committerdate` |
| GitHub workflows | Full inventory: name + size + trigger + secrets used + last successful run | `gh workflow list` + filesystem grep |
| GitLab project state | Project ID, default branch, runners registered, container registry size | GitLab API |
| Container registry references | All places `ghcr.io/` or `registry.sora.services:` appears, with file + line | `grep -rln` |
| cosign material | `deploy/cosign/cosign.pub` content (placeholder yes/no), `SORA_COSIGN_KEY` provisioning state | filesystem inspect |
| systemd units | 84 units' install state (installed yes/no on each WSL distro) | `systemctl --user list-units` per distro |
| Container topology | All `docker ps` output across WSL distros + compose project labels + health status + uptime | `docker ps -a --format` |
| Secrets | 37 GitHub secrets + provisioning state on GitLab (TBD) + canonical `~/.config/omnisight/*` files | inventory + GitLab API |
| WSL distros | All distros + state (running / stopped) + IP + resources | `wsl.exe --list --verbose` + `ip addr` |
| Active runner state | tmux sessions + running auto-runner-jira.py processes + their worktree paths + last claim ticket | `ps aux` + `tmux ls` |
| Timers | All systemd timers + last fire + result | `systemctl --user list-timers --all` |

### 13.2 Baseline production procedure

1. Filed as a single commit by the operator (or runner under explicit Phase 31.A pre-flight ticket) BEFORE any other Sprint S12 ticket
2. Reviewed by operator
3. Tagged with git tag `audit-31-baseline-frozen`
4. Becomes the canonical "what existed when we started"
5. Every sub-phase Evidence appendix (§11.4) references this baseline by tag

### 13.3 Drift detection during execution

A scheduled job (Phase 31.G observability extension) walks the baseline + current state daily during AUDIT-31 execution. Drift = unexpected difference. Drift > N items = P1 alert + sub-phase halts.

---

## §14. Cosign legacy image policy (v2 added — answers codex Q7)

Images published BEFORE Phase 31.F cutover are unsigned (or have placeholder-keyless signatures). After 31.F, all new images are real-key-signed. The transition policy:

### 14.1 Legacy definition

- **Legacy image** = any image whose digest existed in GHCR or local docker before the `cosign sign --key $SORA_COSIGN_KEY` call landed in CI (Phase 31.E Wave 3 + 31.F)
- **New image** = any image built by GitLab CI Wave 3+ after cosign key cutover

### 14.2 Verifier behaviour

- `scripts/verify_image_signature.sh` reads `deploy/cosign/legacy-allowed-digests.json` — a digest allowlist file
- If image digest is in allowlist: pass without signature check
- If image digest is NOT in allowlist + has real signature matching `deploy/cosign/cosign.pub`: pass
- Else: fail (operator must explicitly `--insecure-skip-verify`)

### 14.3 Allowlist provisioning

- One-shot at Phase 31.F cutover: operator runs `scripts/cosign-snapshot-legacy.sh` which enumerates all live image digests in `omnisight-productizer` + `staging` + `ai-core` compose projects + dumps to `deploy/cosign/legacy-allowed-digests.json`
- Commit + Gerrit review + merge
- File frozen; new entries require operator commit + ADR amendment

### 14.4 Expiration

- Legacy allowlist expires **90 days after Phase 31.F cutover**
- After expiration: `verify_image_signature.sh` rejects all allowlisted-but-unsigned images
- 90 days is enough time to rebuild all live containers with signed images
- Phase 31.J cutover should result in all containers being on signed images well before 90d

---

## §15. Registry namespace authoritative-per-consumer table (v2 added — answers codex Q8)

Different consumers pull from different registries. v2 makes this explicit so ticket implementation doesn't drift.

| Consumer | Authoritative source | Fallback | Notes |
|---|---|---|---|
| `staging-sync.service` (Phase 31.E Wave 2 target) | `registry.sora.services:49157/omnisight/omnisight-backend:<sha>` (GitLab CR) | GHCR mirror `ghcr.io/limit5/omnisight-backend:<sha>` if GitLab CR unreachable | Phase 31.D replication guarantees both have same content; staging-sync chooses GitLab CR first because GitLab is canonical |
| `omnisight-staging-compose.service` (the staging compose) | GitLab CR primary | GHCR mirror | Env var `OMNISIGHT_REGISTRY_BASE` defaults to GitLab CR; operator may override |
| `omnisight-productizer-*` prod compose | **GitLab CR** for new prod deploys (Phase 31.J onwards) | GHCR mirror is fallback | Existing local `ghcr.io/your-org/*:latest` images keep running until Phase 31.J explicit cutover |
| Helm chart `deploy/helm/omnisight/values.yaml` | GitLab CR (via templated value) | GHCR mirror (override) | Helm not actively used in current prod, but template must be correct for future K8s use |
| K8s manifests `deploy/k8s/10-deployment-backend.yaml` | GitLab CR (via templated value) | GHCR mirror | Same as Helm |
| Cloudflare-fronted public consumers (3rd-party OSS) | GHCR public mirror | (none) | GitLab CR is on internal LAN; only GHCR has public access |
| Phase 31.K's `last-github-actions-run` artefacts | GitHub (archived) | (none) | Kept for historical traceability only |

The env-var abstraction: every compose / K8s / Helm reference uses `${OMNISIGHT_REGISTRY_BASE}` + `${OMNISIGHT_IMAGE_NAMESPACE}` + `${OMNISIGHT_IMAGE_TAG}`. Defaults documented per consumer in `infra/registry-defaults.yaml`. No hardcoded `your-org` in source after Phase 31.F.

---

## §16. Open questions / future

1. **GitHub Actions long-term**: Decommission completely OR keep as visibility mirror with restricted scope (no gating, no secrets)? Phase 31.E Wave 7 currently says "rename to .deprecated"; the actual disposition is a Phase 31.K decision after observation period.
2. **Multi-project replication**: When OmniSight grows to a second project (sora-bridge upgraded? new product?), replication.config needs per-project blocks. Out of scope here; future ADR.
3. **Sigstore self-hosting**: Phase 31.F uses key-based cosign. If at some point the operator wants keyless OIDC (audit chain + transparency log), that's a follow-up ADR. cosign.pub placeholder text already covers this transition.
4. **Multi-tenant cloud deployment**: Out of AUDIT-31 scope. Pinned for a future migration ADR.
5. **WSL2 mirrored networking**: Phase 31.J Phase 6 evaluates whether mirrored mode is worth enabling for cross-WSL convenience. If yes, separate decision in 31.K retro.

---

## Appendix A — 15 cross-cutting failures cataloged

These were discovered during 2026-05-13 deep sweep (see chat transcript dimensions D1-D14):

1. ADR-0001 5-branch git flow not implemented → multi-agent collision routine; manual rescue baseline
2. ADR-0002 GitLab primary not implemented → CI workflows 30 silent
3. Gerrit→GitLab replication set up 2026-05-06 then silently broke → no monitoring
4. Gerrit→GitHub mirror never set up → GitHub Actions never fire on develop pushes
5. cosign signing chain is placeholder (`cosign.pub` literally says PLACEHOLDER) → "signed image" is fiction
6. ghcr `your-org` placeholder scattered across 20+ files → image distribution doesn't exist
7. 3 envs (per multi-wsl-deployment.md design) not actually separated → all on Ubuntu-24.04
8. Production containers `unhealthy` for 5-9 days without alert → monitoring stack non-existent
9. ai-core integration ai_engine exited 9 days ago → separate ticket out of AUDIT-31
10. sora-bridge 84 systemd units only 25 installed → "shipped but not deployed" pattern
11. 110+ Git branches drift, 56+ runner-fresh + agent + stale feature → governance discipline absent
12. registry.sora.services:5000 referenced 3+ files but unreachable / never deployed → ghost reference
13. Multi-agent worktree race (codex-1 + codex-2 on same branch at time of writing) → mutex doesn't cover branch layer
14. `omnisight-compose-prod.service` not installed but prod containers running → no boot-time guarantee
15. Observability stack (Prometheus/Grafana/Alertmanager) configs exist, no containers running → entire alert chain virtualised

---

## Appendix B — Phase dependency graph (text)

```
Layer 0 (no dependencies, can start day 1):
  31.A (dev WSL provisioning)
  31.C (operator notifier — independent codepath)
  31.D (replication design)
  31.H (systemd 59-install — install scripts only)
  31.K (doc drafting — runs throughout)

Layer 1 (depends on Layer 0):
  31.B (multi-agent fix — needs 31.A's dev WSL to test)
  31.E Wave 1 (ci.yml — needs 31.D's GitLab project)
  31.G (observability — needs 31.D's replication health metric)

Layer 2 (depends on Layer 1):
  31.E Wave 2 (image build — needs Wave 1 stable)
  31.F (cosign + image namespace — needs Wave 2)
  31.I (prod health — needs 31.A's alembic + 31.E Wave 2 fresh image)

Layer 3 (depends on Layer 2):
  31.E Wave 3-7 (cosign / live-tests / release / others / decommission)
  31.J (cutover — needs 31.A + 31.B + 31.E Wave 2)

Layer 4 (final, depends on Layer 3):
  31.K (ADR amendment + lesson — runs at very end)

★ rc2 (gated on all Layer 4 done)
```

## Appendix C — Three-capacity scaling math

For the parallelism-dominated sub-phases (31.D, 31.E, 31.G, 31.H, 31.K), wall-clock scales by:

`wc(N) = sum(operator-attention) + (sum(runner-tickets) / N)`

where:
- `N` = effective runner count (4, 2, or 1)
- `operator-attention` = manual prompts, decisions, verifications (irreducible)
- `runner-tickets` = work that's pure ticket-throughput (≤0.5 d each)

For each sub-phase the operator-attention floor ≈ 30-40% of total. The remaining 60-70% scales with `1/N` up to N=2; beyond N=2 the parallelism return drops sharply because tickets are small enough that scheduling overhead and JIRA-pickup cadence dominate.

---

## Appendix D — 5-worktree migration sequence (for 31.J)

```
Phase 0 (~30 min) — Inventory + emergency backup
  • git worktree list -> snapshot
  • For each runner worktree: git status -> backup dirty files
  • tar -czf /mnt/wsl/migration-backup-YYYY-MM-DD.tar.gz <all-worktrees>
  • Verify backup integrity (tar -t)
  • codex-2 specifically: capture 5 modified files (gerrit_jira_bridge.py, jira_dispatch.py, gerrit-jira-archive-sweep.{service,timer}, gerrit-jira-bridge.md)

Phase 1 (~10 min) — Drain prod-WSL runners
  • tmux send-keys -t claude-runner-loop "C-c" (graceful)
  • Wait for python3 auto-runner-jira.py processes to exit
  • JIRA: clean stale assignee + claim:default:* labels (~15 tickets per earlier sweep)
  • Disable sora-bridge-sync.timer + omnisight-main-sync.timer (no mid-migration pulls)

Phase 2 (~30 min) — Provision dev WSL via setup-dev-env-full.sh
  • Re-confirm Ubuntu-26.04 WSL is the target
  • Run scripts/setup-dev-env-full.sh (interactive cred prompts)
  • Verify all 16 gap items pass validation suite
  • Confirm Gerrit clone (not GitHub) is origin

Phase 3 (~10 min) — Restore in-flight work
  • cp /mnt/wsl/codex-2-dirty-files-*.tar.gz <dev-WSL temp>
  • In dev WSL: apply codex-2 dirty changes (git apply patch OR manual cp)
  • Confirm tests pass under applied changes

Phase 4 (~10 min) — Provision 4 runner LAUNCHER ROOTS in dev WSL (v2: NOT git worktrees)
  • Per §3.5 v2 clarification, runner pickup uses ephemeral clone in /tmp/runner-pickup/ per pickup.
  • Launcher roots are persistent dirs that ONLY hold:
      - tmux session config
      - auto-runner-jira.py (or symlink to /home/user/work/sora/OmniSight-Productizer/auto-runner-jira.py)
      - ~/runner-{INSTANCE}/.cache/omnisight/ state files
  • No checked-out source code in launcher root.
  • Create:
      mkdir -p ~/runner-claude-1 ~/runner-claude-2 ~/runner-codex-1 ~/runner-codex-2
      (each contains state + tmux config; pickup script clones into /tmp/runner-pickup/ per-pickup)
  • tmux launcher scripts set up but PAUSED

Phase 5 (~30 min) — Verify ONE ticket cycle in dev WSL
  • File a test ticket (or pick OP-XXX small backlog)
  • Resume ONE runner in dev WSL
  • Observe: pickup → claim → work → push → Gerrit change
  • Operator confirms full cycle OK

Phase 6 — Cutover decision (Phase J critical commit point)
  (A) Stop prod-WSL runners; activate dev-WSL 4 runners
  (B) Dual-run (both WSLs active) for 1 week comparison
  (C) Rollback — drop dev WSL provisioning, prod WSL resumes

  Operator confirms choice. If (A) or (B):
    • Update omnisight-main-sync.timer target dir
    • sora-bridge-sync.timer stays on prod WSL (bridge daemon is infra-only)
    • prod containers (omnisight-productizer-*) stay on prod WSL untouched

Phase 7 (after cutover stable ≥7 d) — Cleanup
  • Remove or archive prod-WSL worktrees
  • Final inventory + doc update
  • Tag last commit before cutover as `audit-31-phase-j-cutover`
```

---

**End of ADR-0023 draft v2. Awaiting operator review.**

---

## Amendment v2 changes index (for quick re-review)

For operator reviewing v2 vs v1:

| Change | Section | Source |
|---|---|---|
| Amendment log added | top metadata | self-explanatory |
| §2.0 "MUST NOT restart" definition | §2 (NEW) | codex critical ambiguity §2 / Q2 |
| §2.1 parity model clarification | §2.1 (added paragraph) | codex critical ambiguity §3 / Q4 |
| §3.2.1 GitLab Runner architecture | §3.2 (NEW sub-section) | codex missing piece §3 + Q5 |
| §3.5 worktree terminology clarification | §3.5 (added paragraph) | codex critical ambiguity §6 / Q9 |
| §4 31.B B-0 chicken-and-egg protocol | §4 31.B (NEW B-0) | codex implementation risk + Q1 |
| §6.1 parity row corrected | §6.1 (1 row) | codex critical ambiguity §3 |
| §6.2 Trigger C revised | §6.2 (Trigger C) | codex critical ambiguity §3 |
| §11 Phase gate evidence model | §11 (NEW) | codex missing piece §3 + Q3 |
| §12 Operator-only vs runner-doable | §12 (NEW) | codex implementation risk + Q5/Q6 |
| §13 Inventory baseline artifact | §13 (NEW) | codex missing piece §3 + Q10 |
| §14 Cosign legacy image policy | §14 (NEW) | codex critical ambiguity §8 + Q7 |
| §15 Registry namespace per consumer | §15 (NEW) | codex critical ambiguity §5 + Q8 |
| §16 Open questions (was §11) | §16 | renumber due to insertions |
| Appendix D Phase 4 corrected | Appendix D | codex critical ambiguity §6 |

Total: 15 changes addressing 3 contradictions + 5 missing pieces + 10 questions from codex review.

Remaining codex feedback NOT addressed in v2 (deferred to ticket-decomposition level):
- Cross-area ticket design rules (codex implementation risk)
- tier:S/M/L fragmentation discipline (codex implementation risk)
- Per-ticket exact verification commands (codex implementation risk)

These three belong in the ticket-decomposition rule design (next conversation per operator decision 2026-05-13).
