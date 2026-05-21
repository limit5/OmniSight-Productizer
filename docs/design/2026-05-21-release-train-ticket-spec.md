# Release-train migration — ticket spec **v2** (post codex completeness audit; NOT yet filed)

**Status**: v2 — incorporates codex ticket-spec audit 2026-05-21 (`docs/audit/codex-reviews/release-train-ticket-spec-codex-audit-2026-05-21.txt`, verdict GO-WITH-FIXES). Implements work plan v2 / revised Model A / ADR-0040. **STILL NOT filed to JIRA** — file only after the operator confirms + the filing-format expansion below is done.

## What v1-spec got wrong (fixed in v2)
- 7 coverage GAPS → added **RT-18..RT-23** + folded filing-tool drift into RT-03.
- Labels were shorthand → now **literal**: `capability:enable=gerrit_push`, `runner:no-commits-expected`.
- Missing `class:*` (drives runner JQL pickup) → added per row.
- `type:meta-doc`/`ops` not CLI-supported (`file_jira_ticket.py --type` = bug|feature|docs|meta) → normalized.
- `area:ci(or devops)`/`gerrit(or devops)` placeholders not fileable → **RT-03 must land first**; until then ci/gerrit tickets are `[HOLD]`.
- `[BOT+OP]` invalid prefix → RT-13 split into RT-13a (BOT) + RT-13b (OP).
- Parent META rows had `area:—` (not fileable) → given `area:docs` + filed as Epic/META parent.
- `blockedBy` shorthand (`RT-04a,b,c,d`) → expand to **explicit edge pairs** at filing (`blocked_key`/`blocker_key`, SOP §19).

## ⚠ Filing-format requirement (codex E1 — MUST do before POST)
This table is the BLUEPRINT. At filing time **each row becomes a full Story** with: `## Prerequisites` YAML (blocks_on/soft_prereqs/mutex/schema_locks/live_state), all **4 AC sections + Go-Live**, and a **machine-executable `verify:` block per runtime-expectation AC** (or explicit operator-waiver). Scope-anchor (Goal + Files + Spec-ref to this doc) is present so runner-authored 4-AC is acceptable, but the Exercised verify block is mandatory.

## ⚠ Filing ORDER (hard)
1. **RT-00** (accept ADR-0040) → 2. **RT-03 + filing-tool drift fix** (so `area:ci`/`area:gerrit`/`area:db` are all fileable+recognised consistently) → 3. **RT-01/RT-23 freeze** → 4. everything else. Do NOT batch-file ci/gerrit/db tickets before step 2 (unknown-area / db-rejected loops).

Format legend: `class` = `sub-claude`/`sub-codex`/`operator`. Labels shown literal. `🤖`=runner code child · `👤`=operator/ops (tier:X + `runner:no-commits-expected`).

## Title action-tag convention (2026-05-17, applied here)
Summary format = **`[TAG][RT-NN] Title`** (action-tag FIRST, RT-id scope SECOND). The TAG is auto-classified from labels (first match wins): `[META]` (Epic/`type:meta`) · `[HOLD]` (`runner-blocked:*`/`external-blocker:*`) · `[GATE]` NARROW (`requires:operator-approval` only — "operator signs off, no work") · `[REF]` (`type:doc/audit`) · `[BOT]` (Story + `class:subscription-*` + `agent:auto` → runner auto-picks) · `[OP]` (fallback — operator does the work).
**Correction applied to v1-spec mis-tags:** integration/test tickets (RT-04e/05e/07c/10c/15c) are **runner test Stories → `[BOT]`, NOT `[GATE]`** (they DO work, they don't gate approval). RT-16 dry-run is operator ops → `[OP]`. RT-04a is `[BOT]` (ordering via blockedBy, not a HOLD). **All `[BOT]` rows additionally carry `agent:auto` + `class:subscription-*`** (required for the classifier + runner pickup). `[OP]`/operator rows are tier:X (excluded from runner JQL).

---

## META
**[META][release-train] Single-trunk Release Train migration (revised Model A, ADR-0040)** — Epic/meta · tier:X · class:operator · area:docs · DoD = RT-16 dry-run green + RT-17 cutover.

## Unblockers (file first, in order)
| ID | Title | issuetype·tier·type·class | areas | labels | blockedBy | key Exercised AC |
|---|---|---|---|---|---|---|
| **RT-00** | [OP] Accept ADR-0040 + supersede flips + cross-refs + ADR-0023 reconcile | Story·X·docs·operator | docs | runner:no-commits-expected | — | ADR-0040 status=Accepted; ADR-0001/0020/0039/0016 Superseded |
| **RT-03** | [BOT] Filing-vocab + tool drift fix: add `area:ci`+`area:gerrit`, **fix `db` rejection** | Story·M·feature·sub-codex | tooling,docs,tests | capability:enable=gerrit_push | RT-00 | `ci`/`gerrit`/`db` consistently accepted across `auto-runner-jira.py:143`, `scripts/file_jira_ticket.py:37`, `jira-label-schema.yaml`, `jira-label-conventions.md`, `config/capability_matrix.yaml`; validator+prompt-builder tests pass |
| **RT-01** | [OP] Freeze old release writers (timers + webhook runtime triggers) | Story·X·ops→docs·operator | devops,docs | runner:no-commits-expected | RT-00 | timers disabled; webhook main-push/CI-trigger OFF at runtime; rollback boundary doc'd |
| **RT-23** | [BOT] Webhooks CODE cleanup — remove/retarget main force-push + `gh -r main`/GitLab `ref=main` | Story·M·feature·sub-claude | backend,tests | capability:enable=gerrit_push | RT-01 | `webhooks.py` no longer references main; merge webhook does NOT trigger release CI |
| **RT-18** | [OP] Hotfix stop-the-line / no-fix-isolation policy (revert-unready-or-tag-from-last-release) | Story·X·docs·operator | docs | runner:no-commits-expected | RT-00 | written rule + owner; referenced by RT-12/RT-16 |
| **RT-19** | [OP] Final-tag + promote actor protection (GitLab protected `v*` tags, Gerrit tag ACL, release-manager group) | Story·X·security→ops·operator | security,devops | runner:no-commits-expected | RT-00 | only release-train service / release-manager can create final `v*`; every tag/promote has immutable audit row |
| **RT-20** | [OP] DECISION: final git-tag semantics (excluded-from-CI vs image-tag-only) | Story·X·docs·operator | docs | runner:no-commits-expected | RT-00 | decision recorded in ADR-0040; blocks RT-09/RT-12 |
| **RT-21** | [OP] Bridge runtime inventory (first-class image vs backend-profile vs host-sync) | Story·X·docs·operator | docs,devops | runner:no-commits-expected | RT-00 | digest set in RT-12/13 matches actual deployed components |

## Phase 0.1 — green signal
| ID | Title | issuetype·tier·type·class | areas | labels | blockedBy |
|---|---|---|---|---|---|
| RT-04 | [META] Fast exact-SHA green signal | Epic·X·meta·operator | docs | — | RT-03 |
| RT-04a | [BOT][RT-04a] Fast per-change submit gate CI job | Story·M·feature·sub-codex | ci,tests | capability:enable=gerrit_push | RT-03 |
| RT-04b | [BOT] Green-evidence store+query by full SHA | Story·M·feature·sub-claude | backend,db | capability:enable=gerrit_push | RT-03,RT-04a |
| RT-04c | [OP] Enable Gerrit Verified on develop (repo change split from protected-setting activation) | Story·X·ops·operator | gerrit,devops | runner:no-commits-expected | RT-03,RT-04a |
| RT-04d | [BOT] Bad-SHA negative test | Story·M·tests·sub-codex | tests | capability:enable=gerrit_push | RT-04a,RT-04b |
| RT-04e | [BOT][RT-04e] Green-signal integration verifier | Story·M·tests·sub-claude | tests,devops | capability:enable=gerrit_push | RT-04a,RT-04b,RT-04c,RT-04d |

## Phase 0.2 — shared infra (parallel) — every devops feature row needs an activation child or names its [GATE] as activation
| ID | Title | issuetype·tier·type·class | areas | labels | blockedBy |
|---|---|---|---|---|---|
| RT-05 | [META] Staging fidelity | Epic·X·meta·operator | docs | — | RT-00 |
| RT-05a | [BOT] staging compose→GitLab CR, fail-closed, pull_policy (+activation) | Story·M·feature·sub-codex | devops,tests | capability:enable=gerrit_push | RT-00 |
| RT-05b | [BOT] sync_staging deploy-by-digest + digest equality | Story·M·feature·sub-claude | devops,backend,tests | capability:enable=gerrit_push | RT-05a |
| RT-05c | [BOT] FE/BE compat = hard staging gate | Story·M·feature·sub-claude | backend,frontend,tests | capability:enable=gerrit_push | RT-05a |
| RT-05d | [BOT] staging-gate JSONL digest triplet + /api/version | Story·M·feature·sub-codex | backend,devops,tests | capability:enable=gerrit_push | RT-05a |
| RT-05e | [BOT][RT-05e] Staging-fidelity integration (=activation evidence) | Story·M·tests·sub-claude | tests,devops | capability:enable=gerrit_push | RT-05a,RT-05b,RT-05c,RT-05d |
| RT-06 | [BOT] cosign trust-mode consistency (+negative test +activation) | Story·M·feature·sub-codex | devops,tooling,tests | capability:enable=gerrit_push | RT-00 |
| RT-07 | [META] Prod-deploy lockdown | Epic·X·meta·operator | docs | — | RT-00 |
| RT-07a | [BOT] deploy-prod.sh+check_deploy_ref.sh tag/digest-only (+activation) | Story·M·feature·sub-claude | devops,tests | capability:enable=gerrit_push | RT-00 |
| RT-07b | [BOT] allowlist + prod compose `${IMAGE_TAG:?required}` | Story·M·feature·sub-codex | devops,tests | capability:enable=gerrit_push | RT-07a |
| RT-07c | [BOT][RT-07c] Prod-deploy rejection test harness (=activation) | Story·M·tests·sub-claude | tests,devops | capability:enable=gerrit_push | RT-07a,RT-07b |
| RT-08 | [BOT] /api/version+/readyz overlay (code) | Story·M·feature·sub-claude | backend,tests | capability:enable=gerrit_push | RT-00 |
| RT-08b | [OP] Overlay activation evidence (prod restart exposes triplet) | Story·X·ops·operator | devops | runner:no-commits-expected | RT-08 |

## Phase 1 — Model A core
| ID | Title | issuetype·tier·type·class | areas | labels | blockedBy |
|---|---|---|---|---|---|
| RT-09 | [BOT] CANDIDATE_SHA candidate pipeline | Story·M·feature·sub-codex | ci,backend,tests | capability:enable=gerrit_push | RT-04e,RT-05e,RT-06,RT-20 |
| RT-10 | [META] Version reserve + release_train lock | Epic·X·meta·operator | docs | — | RT-04e |
| RT-10a | [BOT] release_train table + alembic + CAS + audit hard-gate | Story·M·feature·sub-claude | db,backend,tests | capability:enable=gerrit_push | RT-04e |
| RT-10b | [BOT] Early version reservation (fixVersion/META) | Story·M·feature·sub-codex | backend,tests | capability:enable=gerrit_push | RT-10a |
| RT-10c | [BOT][RT-10c] Concurrent-promote race test | Story·M·tests·sub-claude | tests,backend | capability:enable=gerrit_push | RT-10a |
| RT-11 | [BOT] Release-to-release migration rollback gate | Story·M·feature·sub-claude | backend,db,tests | capability:enable=gerrit_push | RT-10a,RT-08 |
| RT-22 | [BOT] Force/break-glass policy impl (rewrite force-promote across release_milestone_checker; no unattended force; audited) | Story·M·feature·sub-codex | backend,tests | capability:enable=gerrit_push | RT-10a |
| RT-12 | [BOT] Promote job (GitLab CR, 3 images, digest-verify, attest, idempotent) + final-tag-no-rebuild test | Story·M·feature·sub-claude | devops,backend,tests | capability:enable=gerrit_push | RT-05e,RT-06,RT-10a,RT-11,RT-19,RT-20,RT-21,RT-22 |
| RT-13a | [BOT] release_train_status.py tool (status+rollback resolve) | Story·M·feature·sub-codex | tooling,backend,tests | capability:enable=gerrit_push | RT-08,RT-10a |
| RT-13b | [OP] Operator status+rollback EXERCISE against staging/prod-like | Story·X·ops·operator | devops | runner:no-commits-expected | RT-13a,RT-21 |
| RT-14 | [BOT] GitLab CR retention + release_train protection | Story·M·feature·sub-codex | devops,tooling,tests | capability:enable=gerrit_push | RT-10a |
| RT-15 | [META] FE runtime feature flags | Epic·X·meta·operator | docs | — | RT-00 |
| RT-15a | [BOT] /api/feature-flags/effective (public, server-eval, fail-closed) + allowlist | Story·M·feature·sub-claude | backend,tests | capability:enable=gerrit_push | RT-00 |
| RT-15b | [BOT] FE provider/hook/SSR bootstrap + tier-enum align | Story·M·feature·sub-codex | frontend,tests | capability:enable=gerrit_push | RT-15a |
| RT-15c | [BOT][RT-15c] FE-flag integration | Story·M·tests·sub-claude | tests | capability:enable=gerrit_push | RT-15a,RT-15b |

## Phase 2 — cutover (after dry-run)
| ID | Title | issuetype·tier·type·class | areas | labels | blockedBy |
|---|---|---|---|---|---|
| RT-16 | [OP][RT-16] End-to-end dry-run | Story·X·ops·operator | devops,tests | runner:no-commits-expected | RT-09,RT-12,RT-13b,RT-15c,RT-07c,RT-08b,RT-18 |
| RT-17 | [OP] Cutover cleanup: DELETE old automation + Gerrit cutover + main retirement | Story·X·ops·operator | devops,gerrit | runner:no-commits-expected | RT-16,RT-03 |

## Dependency graph (acyclic — expand to explicit edges at filing; DELETE stale Blocks before POST)
```
RT-00 → {RT-01,RT-03,RT-18,RT-19,RT-20,RT-21,RT-05*,RT-06,RT-07*,RT-08,RT-15*}
RT-03 → {RT-23?no(RT-01), RT-04*, and gates ci/gerrit/db filing}      RT-01 → RT-23
RT-04a→RT-04b,RT-04d ; RT-04a→RT-04c ; {04a,b,c,d}→RT-04e
RT-05a→{05b,05c,05d}→RT-05e ; RT-07a→RT-07b→RT-07c ; RT-08→RT-08b
RT-04e→RT-10a→{10b,10c,RT-11(+RT-08),RT-22,RT-14}
RT-09 ← {RT-04e,RT-05e,RT-06,RT-20}
RT-12 ← {RT-05e,RT-06,RT-10a,RT-11,RT-19,RT-20,RT-21,RT-22}
RT-13a ← {RT-08,RT-10a} ; RT-13b ← {RT-13a,RT-21}
RT-16 ← {RT-09,RT-12,RT-13b,RT-15c,RT-07c,RT-08b,RT-18} ; RT-17 ← {RT-16,RT-03}
```
RT-12 still correctly blocked behind staging (RT-05e) + migration (RT-11). No P0→P1→P0 cycle.

## Runner-comprehension corrections (codex pass 6, 2026-05-21 — verdict GO-WITH-FIXES)
Codex confirmed the tag corrections (integration→`[BOT]`, RT-16→`[OP]`, RT-04a→`[BOT]`). #1 goal-drift hazard = **missing per-ticket scope anchors** (the runner's file-mutex + prompt-boundary depend on an explicit `## Files / Paths` section; without it the runner falls back to coarse defaults and over/under-edits). So at filing **every Story MUST contain**:
```
## Goal   (one imperative sentence, explicitly excluding sibling work)
## Files / Paths   (exact paths incl tests)
## Spec references   (this spec RT-NN row + workplan phase lines + Model A corrected-rule lines)
## Out of scope   (name the sibling RT-XX that owns the adjacent work)
## Acceptance (4-AC + Go-Live, each runtime AC has a machine `verify:` block)
```

**Anti-drift title rewrites (embed the guardrail IN the title):**
- RT-04a → `Fast develop submit-gate CI job (per-change ONLY; NOT candidate certification)`
- RT-04d → `Bad full-SHA rejection tests for green-evidence/candidate gate (tests only)`
- RT-05a → `Staging compose: GitLab CR + fail-closed ref + pull_policy (NO digest-deploy logic — that's RT-05b)`
- RT-05d → `Staging-gate evidence: JSONL digest triplet + observed /api/version fields (NOT RT-08 prod overlay)`
- RT-06 → `Cosign KEY-BASED trust consistency + unsigned/tampered negative tests (NO OIDC impl)`
- RT-07a → `deploy-prod/check_deploy_ref final-tag-or-digest-only (NO allowlist/compose — that's RT-07b)`
- RT-08 → `/api/version+/readyz deployment overlay using <CHOSEN load semantics>` — ⚠ **see RT-08-pre below**
- RT-09 → `API/manual CANDIDATE_SHA=<40hex> pipeline; build sha-<fullsha>; NO cand/* tag; NO final git tag`
- RT-10b → `Early version reservation for planning ONLY (fixVersion/META); NO image/git tag`
- RT-11 → `Promote-time release-to-release migration compat gate (NO prod-rollback command)`
- RT-22 → `Remove unattended force-promote; audited human break-glass only; NEVER bypass digest equality`
- RT-12 → `Promote by retagging validated GitLab CR digests to reserved version; CONSUME RT-20/RT-21 decisions`
- RT-13a → `release_train_status.py status+rollback digest resolver TOOL ONLY (exercise = RT-13b)`
- RT-15b → `FE feature-flag provider/hook/SSR bootstrap; CONSUME RT-15a API contract`
- RT-17 → `Post-dry-run cutover cleanup: delete old automation + Gerrit cutover + retire main per accepted ADR`

**NEW — RT-08-pre [OP] DECISION: overlay load semantics** (read-before-start-from-env-lock vs per-request-with-cache-invalidation). Hidden decision inside `[BOT]` RT-08 → runner would pick one and drift. Add as `[OP]` decision blocking RT-08. (mirrors RT-20/RT-21 pattern.)

**Mutex / schema-locks (shared files across siblings):**
- `backend/api_versioning.py` — **RT-05d ∩ RT-08** (biggest collision): RT-05d adds only staging-evidence fields; RT-08 owns general overlay. `mutex_with` or strict blockedBy.
- `release_train` table schema — `schema_locks` on RT-10a; RT-10b/10c/11/22/14 take a downstream contract lock.
- staging group files (`deploy/staging/docker-compose.yml`, `scripts/sync_staging_to_develop.sh`, `scripts/staging_gate.py`) — mutex across RT-05a/b/d.
- prod-deploy files (`scripts/deploy-prod.sh`, `scripts/check_deploy_ref.sh`, `deploy/prod-deploy-allowlist.txt`, `docker-compose.prod.yml`) — mutex across RT-07a/b.
- `/api/feature-flags/effective` response + tier-enum — contract lock RT-15a → RT-15b.

**Classifier note:** runner pickup JQL = `Story + class:<runner> + To Do + empty-assignee + NOT tier:X` (it does NOT key on `agent:auto` or the `[OP]` title). So the real gate keeping `[OP]` rows away from runners is **tier:X + no `class:subscription-*`** — enforce at filing. `area:tests` is an AREA not a `--type` (use `--type feature/docs`).

## ✅ 3 blocking decisions LOCKED 2026-05-21 (in ADR-0040 §"Decisions LOCKED")
- **RT-20 = IMAGE-TAG-ONLY**: no `v*` git tag; release identity = promoted GitLab CR image tag+digest + `release_train` row. RT-09/RT-12 now unblocked (consume this).
- **RT-21 = digest PAIR (backend+frontend), NOT triplet**: bridge is a host control-plane daemon (sora-bridge checkout), decoupled from the release-train deploy gate. All "digest triplet" → "digest pair" in RT-08/12/13/14.
- **RT-08-pre = READ-BEFORE-START from env lock**: RT-08 overlay reads a deploy-written lock once at startup; fail-closed if missing. RT-08 now unblocked (embed this semantics).
→ RT-20/RT-21 stay as `[OP]` record tickets (capture the decision + do the small doc/inventory follow-through); RT-08-pre folded into RT-08's AC.

## Remaining before filing (operator)
1. ✅ 3 decisions locked (above). 2. Confirm `class` split. 3. Story expansion → see `docs/design/2026-05-21-release-train-stories.md`. 4. File in hard order: RT-00 → RT-03(+tool drift) → RT-01/RT-23 → rest; don't batch ci/gerrit/db before RT-03.
