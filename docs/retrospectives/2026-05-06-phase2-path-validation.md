# Retrospective: Phase 2 Path Validation Session

**Date**: 2026-05-06
**Type**: Milestone retrospective (not tier-drift — manual)
**Linked tickets**: OP-247 (Gerrit wire-up META, prereq-clearing)
**Author**: Claude (interactive session) + sora (operator)
**Status**: Captured + filed

---

## Situation

Per `docs/sop/migration-plan-2026-05.md`, governance Phase 2 entry gate had 6 BLOCKER items, set up across 2026-05-04 to 05-06 prep work:
1. GitLab `external_url` fix (HTTP-only → working)
2. HTTPS on GitLab + Gerrit (Sectigo wildcard cert via Synology DSM Reverse Proxy)
3. GitLab production project `omnisight/OmniSight-Productizer` creation
4. Gerrit production project + ACL configuration (per ADR 0003 group structure)
5. Gerrit replication.config wired to push develop+tags to GitLab
6. GitLab → GitHub mirror webhook for one-way OSS visibility

Items 1+2 were already done on 2026-05-05. Items 3-6 were the active work in this 2026-05-06 session.

The original assumption (per my pre-session estimate, before reading context properly) was that this would require ~1-2 weeks of operator-and-Claude collaboration with multiple infrastructure unknowns to debug — "Layer 1 path validation" was scoped as a multi-day META `meta:tooling` ticket (OP-248-equivalent).

## Divergence

Actually completed all 6 items + bonus end-to-end smoke test in a single ~3-hour collaborative session.

Two full review cycles ran successfully:
- Change #22: smoke commit on lessons-learned.md → cross-bot codex +1 → sora +2 → Submit → propagated through Gerrit → GitLab → GitHub in <30s
- Change #23: actual docs commit (L11-L13 lessons + Phase 2 status update) → same flow, all 3 endpoints aligned at `b646345910d6f8`

ADR 0003 dual-sign gate empirically validated:
- `ai-reviewer` group: `-1..+1` voting range enforced for AI bots
- `non-ai-reviewer` group: `-2..+2` for human reviewer (sora)
- `submittable=True` only when both contributions present
- DefaultSubmitRule (Phase 3 O7 Prolog rule deferred) handled the dual-sign gate correctly

3 fresh lessons captured: L11 (origin topology drift), L12 (replication.config templating gotchas), L13 (credentials in URL antipattern).

## Root cause (why divergence was positive)

Three reasons the work went much faster than estimated:

1. **Track A/B/C 6-verb baselines were already proven**. The 2026-05-04 governance Phase 0 work had already validated CRUD-style mechanics on each system. This session was just "wire them together for one specific repo" — no fundamental unknowns left.

2. **HTTPS upgrade had already eaten the hardest infrastructure work**. Synology DSM Reverse Proxy + Sectigo cert handled both GitLab and Gerrit HTTPS. By 2026-05-06 the heavy lifting was done; remaining items were CRUD-shaped (project creation) or config-edit-shaped (replication.config).

3. **Real-time collaborative debug was effective**. When my replication.config draft had 3 bugs (env var literal, case mismatch, duplicate prefix), `replication list --detail` exposed them immediately and I could iterate the fix in the same session. Same for the JQL `issuetype = "ストーリー"` gotcha — caught in 5 minutes via direct testing rather than reading docs.

## Contributing factors

- Operator (sora) had already prepared `/tmp/gerrit-replication-config/all-projects-config/` from earlier Phase 2 prep (2026-05-06) — the muscle memory shortened admin-side execution time
- Both bots' SSH keys + JIRA tokens already worked in the file paths I expected (per `reference_*.md` memories)
- Two AI agents (claude-bot + codex-bot) made cross-bot review trivial to test; without the second bot we'd have to manually fake the 2-account scenario
- Operator's Synology + Sectigo cert setup is admin-only; if this had needed fresh infrastructure decisions, the session would have stalled

## Concrete fixes (already applied, not deferrable)

Captured during the session, all already merged via Gerrit cycle #23 (commit `b646345910d6`):

- `docs/sop/lessons-learned.md` L11: repo topology drift documented
- `docs/sop/lessons-learned.md` L12: replication.config templating gotchas
- `docs/sop/lessons-learned.md` L13: credentials in secure.config not URL
- `docs/sop/migration-plan-2026-05.md`: Phase 2 entry gate marked 5/6 satisfied + Step 2.1/2.3 marked partially done + new "Phase 2 status (2026-05-06)" section
- OP-247 description (in JIRA): refreshed to reflect that infra prerequisites are done; only runner-side wiring remains

Implicit fixes the session produced:

- `omnisight/OmniSight-Productizer` project on Gerrit + GitLab + ACL + replication is now production-ready
- `develop` + `main` branches seeded across all 3 endpoints
- Cross-bot review pattern empirically proven for ADR 0003 dual-sign

## Verification

3-endpoint state at session close (2026-05-06 13:31:59 UTC, Change #23 submit):

| Endpoint | develop | main |
|---|---|---|
| Gerrit | `b646345910d6f8` | (intentionally absent — ADR 0001 main is FF target, not Gerrit-reviewed) |
| GitLab | `b646345910d6f8` | `20ef0b79248b` (predates Gerrit cycle, intentional pre-flow legacy state) |
| GitHub | `b646345910d6f8` | `20ef0b79248b` (mirrored from GitLab) |

`develop` byte-equal across all three ✓ — Phase 2 path validated.

`main` ≠ `develop` on GitLab/GitHub by SHA (same tree, different commit metadata): expected and documented per ADR 0001 5-branch flow — `main` only advances via FF from `release/*` / `hotfix/*`, so legacy SHA on `main` is fine until first release cut.

## Next-Wave lessons

What I'd carry into the OP-247 (runner Gerrit wire-up) work:

1. **Don't assume infrastructure is the bottleneck**. When tooling primitives are validated separately (Track A/B/C), wiring them into a real pipeline is usually fast. Estimate based on remaining unknowns, not perceived total complexity.

2. **Collaborative debug works when both sides have shell access**. For OP-247, the runner-side Python work is independent of operator infrastructure; the model can iterate alone. But for the JIRA bridge (`gerrit_jira_bridge.py` listener), operator action will be needed (events-stream daemon setup) — pace estimate accordingly.

3. **Lesson-capture is a force multiplier**. L11-L13 will save the next person from the same 3 bugs if they re-run the wire-up. The 5 minutes spent writing each lesson is worth ~1-2 hour debug savings on recurrence.

4. **Cycle 2 was faster than Cycle 1**. Repeating a process accumulates implicit muscle memory (commit-msg hook installation, Change-Id awareness, push-then-amend retry pattern). For OP-247, expect ~30% faster iteration after the first end-to-end ticket-via-runner ships.

5. **ADR 0003 doesn't require Phase 3 Prolog rule to enforce dual-sign**. DefaultSubmitRule + group voting ranges (`ai-reviewer -1..+1`, `non-ai-reviewer -2..+2`) gives equivalent gates for the common case. Phase 3 Prolog rule is needed for the merger-agent-bot exception path, not the baseline.

## What this retrospective is NOT

- Not a tier-drift retro per §14 (this work was milestone-shaped, not estimate-shaped)
- Not a Wave retro per §10a (no specific Wave/Epic completed; Phase 2 is governance migration, not an MP/RPG/FX2 Wave)
- Not blocking the next ticket — this is purely capture-and-move-on

## Filed-as

- `docs/retrospectives/2026-05-06-phase2-path-validation.md` (this file)
- META JIRA ticket (`meta:retrospective` + `meta:milestone`) opened for visibility + tracking
