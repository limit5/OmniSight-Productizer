# Codex Review Prompt — Release-train migration work plan, multi-dimensional audit

**Target output file**: `/tmp/release-train-workplan-codex-audit-2026-05-21.txt`
**Reviewer role**: independent, adversarial reviewer. You previously audited the retire-main migration, Model B (NO-GO), and Model A (GO-WITH-FIXES) — all in `docs/audit/codex-reviews/*2026-05-21*`. THIS pass audits the **fully-expanded WORK PLAN** for executing revised Model A, from several operational dimensions. The operator's explicit worry is the project's **recurring "shipped but not deployed" failure mode**, plus whether the plan is sound on **project-advancement cadence** and **AI-runner execution**.

## Read first
1. The work plan under review: `docs/design/2026-05-21-release-train-workplan.md`.
2. The model + ADR: `docs/design/2026-05-21-model-a-candidate-build-release-train.md`, `docs/adr/ADR-0040-single-trunk-release-train.md`.
3. Your 3 prior audits: `docs/audit/codex-reviews/{retire-main-release-train,model-b-rc-promote,model-a-candidate-build}-codex-audit-2026-05-21.txt`.
4. Project guards/anti-patterns to hold the plan against: `docs/sop/` (the 4-AC discipline + filing conventions), `docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md`, `docs/sop/architecture-anti-patterns.md` (if present), `scripts/deployment-audit.sh`.
5. Runner reality: `backend/agents/pipeline_coordinator*.py`, the capability-matrix / area-label / 4-AC machinery, `scripts/runner-wrapper/`, and any runner SOP under `docs/sop/`.
6. Existing code each work item touches (verify the plan's assumptions; cite `path:line`): `.gitlab-ci.yml`, `scripts/{sync_staging_to_develop,staging_deploy,deploy-prod,check_deploy_ref,promote_image_bundle,build_image_bundle,emit_bundle_json,verify_image_signature,enforce_image_retention,check_migration_compat,release_milestone_checker}.{sh,py}`, `backend/{api_versioning.py,routers/health.py,frontend_compat.py,release_conductor/state_machine.py,agents/auto_tag_release.py,agents/auto_promote_main.py,agents/staging_gate.py}`, `deploy/prod-deploy-allowlist.txt`, `docker-compose.prod.yml`, `deploy/staging/docker-compose.yml`, `.gerrit/project.config`.

## Audit dimensions — be concrete, cite path:line

A. **Shipped-but-not-deployed (the operator's #1 worry).** The plan claims a 4-AC guard. Pressure-test it: which work items are MOST likely to merge code that never actually activates in prod/staging? Is the Deploy/Exercised AC real and checkable, or hand-wavy? Does `scripts/deployment-audit.sh` (or equivalent) actually catch a not-deployed item here? Reference the known incident (`docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md`) and say whether THIS plan would repeat it.

B. **Project-advancement cadence.** Does P0.1 (per-SHA green gate; full suite 60-180min per memory) become a throughput bottleneck that stalls the whole train? During the migration window, does develop keep flowing or freeze? Are there dependency deadlocks (the project has a history of additive-blockedBy cycles)? What is the realistic critical-path duration and where does it stall?

C. **AI-runner execution.** Can the runners actually pick up the 🤖 items? Check against the real capability matrix / area-label whitelist / 4-AC filing rules / boundary enforcement. Which 🤖 items are mis-labeled or actually need a human? Will any item trip the known runner failure modes (capability safe-default leak, area-label whitelist, stuck-revert loops, boundary-forbidden deps)? Does the plan IMPROVE or BURDEN runner throughput?

D. **Sequencing / atomicity / rollback of the migration itself.** Is the Phase 0→1→2 order safe? Are the "atomic groups" truly atomic? Is the "retire old automation only after P2.5 dry-run" actually enforceable, or can a half-migrated state go live? What is the rollback if Phase 1 fails after Phase 0 changed prod-deploy behavior?

E. **Flaws / holes / contradictions** vs the existing code and your 3 prior audits. Anything the work plan asserts that the code contradicts. Any must-fix from the prior audits that the plan silently dropped.

F. **Hidden coupling / blast radius the plan under-counts** — anything that breaks in prod/staging/runner/observability that the plan's item list misses.

## Output
Structure by dimension A–F with `path:line`. For each finding: the problem + why it bites + severity + concrete fix or decision. End with: (1) overall verdict on the WORK PLAN (GO / GO-WITH-FIXES / NO-GO), (2) the **re-sequenced / corrected plan** if needed (esp. to avoid shipped-but-not-deployed and cadence stalls), (3) the top 5 things to change before filing any ticket. Write the FULL review to `/tmp/release-train-workplan-codex-audit-2026-05-21.txt`; do not summarize to stdout.
