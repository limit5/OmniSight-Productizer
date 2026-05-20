# What is OmniSight? — Strategic Snapshot 2026-05-20

**Status**: This snapshot supersedes [`2026-05-14-what-is-omnisight.md`](2026-05-14-what-is-omnisight.md) for the §8 "Where We Are Today" status table and adds a new §8.6 "7-dimension honest audit". Sections §1–§7 and §9–§12 of the 2026-05-14 snapshot remain authoritative without change (frame, anti-patterns, 3-layer architecture, survival vs growth, integration excellence, bootstrap self-reference, common Q&A, glossary). When in doubt about non-status content, defer to the 2026-05-14 snapshot.

**Audience**: future Claude conversations, new collaborators, future-self, investors.

## Why this snapshot exists

Six days (2026-05-14 → 2026-05-20) of significant Layer 1 infrastructure work materially changed the "Where We Are Today" picture, and an operator question on 2026-05-20 demanded an explicit "do the 7 dimensions actually work yet?" answer that the 2026-05-14 snapshot didn't surface clearly enough.

Per the supersession discipline (§12 of 2026-05-14 doc), new dated snapshot rather than in-place edit.

---

## 8. Where We Are Today (2026-05-20)

### 8.1 Working ✅ (newly added or matured since 2026-05-14)

**Infrastructure / platform** (Layer 1):
- 5 runner instances (claude × 2 + codex × 3) on Family F systemd, Linger=yes, OP-1137 ephemeral per-cycle clone — verified cold-start safe ([[reference_cold_start_safety_inventory]])
- Boreas-C runner architecture (OP-1135 META + 4 children all done) — per-runner .git isolation + ephemeral clone delivers ~2-3s clone time + zero cross-worktree races
- **GitLab CR end-to-end live** (OP-1478 META activation 2026-05-19 22:51 CST) — Gerrit tag push → GitLab CI pipeline (15 jobs: build + cosign sign + SBOM + in-toto attest + audit-emit) → GitLab Container Registry on sora.services:49160 → prod compose pull. Bundle.json populated with real metadata. See [[project_op_1478_gitlab_cr_activation_2026_05_19]]
- Code flow Gerrit → GitLab → CI → CR fully verified ([[reference_code_flow_gerrit_to_github]]); GitHub mirror broken since 2026-05-07 (visibility-only, operator-deferred)
- Bridge heartbeat tmpfs trap fixed via env override drop-in ([[project_bridge_heartbeat_tmpfs_trap]]); reboot-safe
- Runner mutex revert-cleanup self-repaired by runner itself (OP-1524, claude-2 commit) — meta-loop "runner fixes runner bugs" empirically proven

**Releases shipped**:
- v0.5.0-rc5 (2026-05-19 cut) + hotfix1/2/3/4 (CI activation chain)
- WP META OP-4 complete — all 13 WP children shipped (including WP.4 onboarding picker + WP.10 BP fleet UI lanes, the only Layer 2b touches in the period)
- Sprint Boreas-C runner architecture (OP-1135) closed 2026-05-20 01:05

### 8.2 Fragile ⚠️ (current state)

- **Operator-intervention rate on runner tickets ~67%** — empirical baseline from 12 recent tickets. Root cause: file_jira_ticket.py doesn't auto-add `capability:enable=gerrit_push` (OP-1526 filed 2026-05-20 to fix at source); 10+ filing rules undocumented (OP-1527 + OP-1528 filed to consolidate)
- **GitLab CR per-instance Gerrit HTTP password missing** for runner accounts → cosmetic `[runner-pre-review-self-fix-warning]` on 58% of pushes (only matters during conflict resolution which is rare)
- **JIRA auto-transition gap** for operator-pushed (sora identity) Gerrit Changes — bridge daemon only walks In-Progress/Under-Review/Approved → Published; operator-pushed tickets stay in To Do until manually walked through workflow
- prod + dev co-existing on one host (gitlab-runner privileged docker executor exposes attack surface; accepted short-term)

### 8.3 Not yet built ⛔ (UNCHANGED from 2026-05-14)

The actual product 7-dimensions remain ~95% unbuilt — see §8.6 honest audit below.

### 8.4 Current sprint focus

**Layer 1 hardening continuing**: cost ledger extraction (highest-leverage per strategic reframe Phase 2) is the next major milestone. Coordination substrate migration (JIRA labels → Postgres) is queued.

**No Layer 2b work** scheduled in next 4-6 weeks. Per [[project_runner_pivot_strategic_reframe]] §7 phasing, Layer 2b green-field bootstrap is Phase 7 — ≥6 months out.

### 8.5 2026-05-19/20 key events

- 02:30 → 22:51 CST: v0.5.0-rc5 cut + OP-1478 activation (5 hotfix cycles, 4 GitLab admin operator gates, 17/17 final pipeline green)
- ~00:26 CST: OP-1524 runner self-repair — claude-2 commit fixes OP-977 mutex revert-cleanup gap (the very bug that made the activation cycle painful)
- ~01:05 CST: Sprint Boreas-C META OP-1135 closed (parallel-run criteria satisfied: zero cross-worktree races over cycle #2000+)
- ~01:50 CST: Runner UX hardening META OP-1525 filed (3 children to fix the ~67% operator-intervention rate)

### 8.6 7-dimension honest audit (NEW SECTION — answers "do main features work yet?")

The 2026-05-14 §2 lays out what a complete OmniSight must deliver. **Two-axis honest read** 2026-05-20: separate "bricks built" from "orchestration glue built" — because a system can have 60% of the pieces and still 0% of the end-to-end user flow.

#### Layer interpretation per dimension

| Dimension | Bricks (libraries / components / templates / configs) | Orchestration glue (user-input → output flow) | End-to-end user demo runs? |
|---|---|---|---|
| **2.1 Platform primitives** (tenant, identity, observability, governance, daemon supervision, cost ledger) | **~70%** built — `tenant_id` in 10+ backend files, 5 daemons + 19 timers, ADR-0001 governance, cosign keyed sign, OP-1478 image pipeline. **Cost ledger NOT extracted** (Phase 2 of strategic reframe) | ~70% (no user flow needed — these are platform-level) | ✅ for dev-runner internal use; ⛔ for user-product use |
| **2.2 Domain skill packs** (Mobile first; per-vertical specialized agent + knowledge + workflow) | **~50-60%** built — `backend/{nextjs,nuxt,astro,fastapi,go_service,rust_cli,ios,android,flutter,rn}_scaffolder.py` ship `render_project()`; 10+ `configs/skills/` packs (enterprise_web, ipcam, mcp-builder, imaging, depth_sensing, etc.); `backend/figma_to_mobile.py` + `app_store_connect.py` + MP.W0/W1 mobile pipeline shipped; BP.A 4-template factory (Spec/Task/Impl/Review + Cognitive Load Scanner, 526 tests) | **~0-5%** — `git grep render_project` returns ONLY the scaffolders themselves; no third-party caller; no orchestrator wires user input to scaffolder dispatch | ⛔ no user can drive any scaffolder |
| **2.3 UI / UX** (idea input, progress tracking, decision support, exception handling) | **~40-50%** built — `app/onboarding/page.tsx` 4-step wizard, `new-project-wizard.tsx` 4 starting modes (GitHub / Upload Docs / Describe in Prose / Blank DAG), `intention-picker.tsx` 4 intentions (incl. `web_app_generation`), tenant/project switcher, /admin/tenants page, WP.4 + WP.10 (BP Fleet UI Lanes), Y6 dashboard tour | **~5-10%** — intention picker sets a localStorage + JSON preference but no backend listener dispatches; new-project-wizard 4 modes route to UI panels but spec→artifact pipeline missing | ⛔ user can configure intent but cannot trigger generation |
| **2.4 Compliance** (FCC/CE/KCC/VCCI/SRRC/NCC + RoHS/REACH/GDPR/export-control) | **~25%** built — `backend/mobile_compliance/` (ASC + Play + Privacy Label gates, 99 tests), `backend/web_compliance/` (W5), 184 cross-suite compliance tests | ~0% — runs only when invoked by dev pipeline, no user-facing compliance workflow | ⛔ |
| **2.5 Supply chain** (BOM, JLCPCB/LCSC/Mouser/Digi-Key) | **0%** | 0% | ⛔ ([[project_hw_expansion_plan]] hard-deferred) |
| **2.6 Manufacturing** | **0%** | 0% | ⛔ |
| **2.7 Simulation / Verification** | **0%** | 0% | ⛔ |

#### Aggregate honest read

- **Bricks aggregate**: ~3.0-3.5 / 7 dimensions have substantial library-level implementation
- **Orchestration aggregate**: ~1.0-1.5 / 7 dimensions have end-user-driven flow (mostly only 2.1 Platform primitives where dev-runner IS the user)
- **End-to-end product demo**: 0/7 dimensions support "user inputs idea → user receives product artifact"

This re-read corrects the 2026-05-14 snapshot §8.3 "Not yet built" + first-cut numbers in this doc's earlier draft, which used a binary "demo works? if not → 0%" cut that obscured the substantial brick-level investment.

#### Distance to Phase 1 (operator-stated goal: "user generates one complete pure-software website OR mobile app")

Two paths matter:

| Goal | Estimated effort | Why this estimate |
|---|---|---|
| **Minimum viable POC** (one skill pack end-to-end, e.g. Next.js): wire `intention-picker` → spec extractor → `nextjs_scaffolder.render_project()` → output a downloadable zip | **1-2 weeks of focused work** | Bricks are ALL there; just needs an orchestrator (`backend/factory/render_orchestrator.py` ~300-500 LOC) + spec extractor + UI wiring. Could ship as `[OP-15xx] Phase 1 POC: idea→Next.js project zip` |
| **Web + Mobile coverage** (5 skill packs: nextjs/nuxt/ios/android/flutter): same POC pattern across all five | +1-2 weeks | Per-skill orchestrator code is ~80% reusable |
| **Deploy target** (user clicks → gets hosted URL on Vercel / self-hosted / etc.) | +2-4 weeks | Depends on choice: Vercel API integration / Cloudflare Pages / self-hosted Caddy; multi-tenant deployment isolation; user-facing rollback |
| **Full Layer 2b user-agent** ([[project_runner_pivot_strategic_reframe]] §7 Phase 7) | **6+ months** | Per the strategic phasing; bootstrap the user-driven agent layer that uses all 7 dimensions |

**Key insight**: the gap is NOT "do we have building blocks". The gap is **"is anyone wiring them into a user-driven flow"**. Per recent work pattern, ~95% of new tickets target Layer 1 + Layer 2a (infrastructure + dev-runner). Layer 2b user-agent has had **near-zero direct investment** in the last 4 weeks (WP.4 intention picker + WP.10 BP Fleet UI Lanes were the only Layer 2b touches in May 2026, and even those are display-only, not generative).

#### What the system CAN do today

- Run an autonomous **dev runner** that ships its own infrastructure improvements via Gerrit (proven via TODO.md 6,851-line completion + 2026-05-20 OP-1524 runner self-repair)
- Maintain its own image pipeline end-to-end (Gerrit → GitLab CR → prod deploy with cosign signing + SBOM + attestation, per [[project_op_1478_gitlab_cr_activation_2026_05_19]])
- Survive host cold-start without operator intervention ([[reference_cold_start_safety_inventory]])
- **Library-level**: programmatically invoke any of 10 scaffolders to generate per-framework skeleton projects (developer-shell access required, not user-driven)
- **Library-level**: mobile App Store / Play Store compliance audits via `backend.mobile_compliance` CLI
- Support 5 concurrent agents via shared Layer 1 primitives

#### What the system CANNOT do today

- Take an idea ("I want to make a baby monitor" OR "I want a todo app") and produce a manufactured product OR a deployable web/mobile app **end-to-end through a user-facing flow**
- Generate a real BOM
- Run any user-driven compliance workflow
- Talk to a real supplier or contract manufacturer
- Simulate any physical property
- **Critically**: even though `nextjs_scaffolder` works at library level, there is NO user path from "I open OmniSight UI" → "I describe my todo app" → "I download a working Next.js project" — that path is the missing orchestrator + spec extractor + UI wiring

#### Strategic tension worth holding

The 2026-05-14 §7 "Bootstrap Self-Reference" claims "product builds itself" as strategic advantage. Empirical realization 2026-05-20: this currently means **"runner strengthens runner"** (OP-1524 self-repair / OP-1525 filing UX), NOT **"runner produces real product artifacts"**. The 6-month phasing assumption is that strengthened Layer 1 + Layer 2a eventually bootstrap Layer 2b. **This assumption has not yet been validated** at the user-flow level (only at library level).

The first real test will be when an explicit "Phase 1 POC" attempt happens (estimated 1-2 weeks focused work using existing bricks) — then we'll know empirically if the brick→orchestration→user-flow path actually flows OR if there are hidden integration gaps.

---

## Reference sections (unchanged from 2026-05-14 snapshot)

- §0 TL;DR — see [2026-05-14 §0](2026-05-14-what-is-omnisight.md#0-tldr)
- §1 The Vision — see [2026-05-14 §1](2026-05-14-what-is-omnisight.md#1-the-vision)
- §2 七個維度 (definitions) — see [2026-05-14 §2](2026-05-14-what-is-omnisight.md#2-七個維度) (the per-dimension audit above uses these definitions verbatim)
- §3 What This Is NOT — see [2026-05-14 §3](2026-05-14-what-is-omnisight.md#3-what-this-is-not)
- §4 Three-Layer Architecture — see [2026-05-14 §4](2026-05-14-what-is-omnisight.md#4-the-three-layer-architecture)
- §5 Survival Floor vs Growth Investment — see [2026-05-14 §5](2026-05-14-what-is-omnisight.md#5-survival-floor-vs-growth-investment)
- §6 Integration Excellence Over Invention — see [2026-05-14 §6](2026-05-14-what-is-omnisight.md#6-integration-excellence-over-invention)
- §7 Bootstrap Self-Reference — see [2026-05-14 §7](2026-05-14-what-is-omnisight.md#7-the-bootstrap-self-reference) **(re-read together with §8.6 above for full strategic context)**
- §9 Common Questions — see [2026-05-14 §9](2026-05-14-what-is-omnisight.md#9-common-questions)
- §10 Glossary — see [2026-05-14 §10](2026-05-14-what-is-omnisight.md#10-glossary)
- §11 Related Documents — see [2026-05-14 §11](2026-05-14-what-is-omnisight.md#11-related-documents), plus newly relevant:
  - [[reference_cold_start_safety_inventory]] — 2026-05-20 verified
  - [[reference_code_flow_gerrit_to_github]] — 2026-05-20 verified
  - [[project_op_1478_gitlab_cr_activation_2026_05_19]] — image pipeline activation
- §12 How This Document Evolves — see [2026-05-14 §12](2026-05-14-what-is-omnisight.md#12-how-this-document-evolves)

## Trigger for next snapshot

Per §12 of 2026-05-14 doc, next snapshot triggered when:
- Cost ledger extraction completes (Phase 2 of strategic sequencing) → 2.1 Platform primitives moves toward ~85%
- First Layer 2b user-agent prototype lands → 2.2 / 2.3 dimensions move off 0%
- Strategic reframe Q1 or Q2 closes (Layer 2b origin OR cross-pollination decision)
- Any market positioning shift OR 3-layer architecture revision

Until then, this 2026-05-20 snapshot is current.
