# EPIC design — multi-project runner routing (v3, post-3-way-audit)

**Date:** 2026-06-14 · supersedes the v1 (scoped-class) and v2 (generalize-camviewpro) design docs, both of which the 3-way audit found BROKEN. · **Touches the runner's own execution path → every child = mandatory human +2 (L1).**

## Premise correction (from the 3-way audit, VERDICT NEEDS-WORK)
v2 claimed "just generalize the camviewpro lane + push via `resolve_gerrit_push_identity`." The audit (me + sub-agent + codex, file:line-cited; `docs/audit/codex-reviews/case5-runner-routing-{codex,subagent}-2026-06-14.txt`) proved that is wrong:
- The self-tenant path **hardcodes BOTH the develop-baseline FETCH and the refs/for PUSH to `omnisight/OmniSight-Productizer`** (`jira_dispatch.py:502,660-665,842-851,1194-1199`). Pushing "via the normal identity path" literally targets productizer.
- Routing by `project_key` is useless — all conf tickets live in JIRA project **OP**, which resolves to the productizer default (`delivery_target.py:132-134`).
- The camviewpro lane is **GitHub-PR-specific** (`open_contribution_pr`, `camviewpro-ro`, GPT-5.5 co-author) — not a Gerrit router.
- `repo:` routing isn't at the dispatch seam; a typo'd label **silently falls through to productizer** (the very mis-pickup we meant to kill). Fail-closed needs an explicit pre-gate.
- Shared Gerrit gates (backpressure / file-mutex / staleness) are **not project-scoped** → conf changes can pause productizer runners + cause false same-path collisions.
- conference-appliance Gerrit ACL `inheritFrom=All-Projects` ≠ productizer governance (no AI+1/human+2 submit-rule). claude-bot CAN push refs/for (verified, change 1584) but the review gate is absent.

**Conclusion: this is a real, multi-ticket runner-core feature, not a small change.** This doc re-architects it properly.

## Target architecture
A **routed-repo descriptor** resolved per-ticket from a `repo:<name>` LABEL (not tenant, not project_key), threaded through the runner's fetch → CLI-cwd → push, with the existing self-tenant productizer path untouched as the default (the safe floor). Concretely:

```
RoutedRepo = { name, gerrit_url, ref="refs/for/develop", git_account_ref, push_identity }
```

1. **Routing config + resolver.** A settings map `routed_repos` keyed by `repo:<name>` → RoutedRepo. `resolve_routed_repo(labels)`:
   - no `repo:` label → `None` (normal productizer path, unchanged).
   - `repo:` present AND resolves → the RoutedRepo.
   - `repo:` present AND unresolved/malformed → **raise** (fail-closed). REQUIRED config (not the fail-open `delivery_target` default-fallback).
2. **Fail-closed dispatch pre-gate.** BEFORE worktree prep / `_invoke_cli`: if `resolve_routed_repo` raises → abstain + `runner-blocked:repo-unresolved`, never continue to the productizer workspace.
3. **Generalized fetch+clone.** A routed ticket clones its OWN repo (the `contribution_runner` self-clone pattern — NOT the wrapper's productizer `$workspace`), into a per-instance path `<workspace_root>/<repo>/<instance>-<ticket>/` (no collision; verify orphan-reaper + workspace-safety ignore/handle it). `sync_to_gerrit_develop` must learn a `repo_url`/`ref` param OR a parallel `sync_routed_repo`.
4. **Gerrit review delivery.** A `GerritReviewDelivery` (parallel to `github_pr_target`): commit-msg with **Change-Id** (hook or manual trailer) + the L1 **dual co-author** trailers, then `git push <gerrit_url> HEAD:refs/for/develop` with the routed push identity. Reuses contribution_runner's clone/branch/implement; only delivery differs.
5. **Project-scope the shared Gerrit gates.** Add `project:<gerrit-project>` to backpressure, file-mutex, staleness, duplicate-detection, and already-merged queries (`jira_dispatch.py:560-568,2567-2588,2735-2749`). Key by `(gerrit_project, ticket)`.
6. **Tenant/context decision.** A routed (internal, non-customer) repo stays a sibling of `omnisight-self` but selects a repo-appropriate prompt context (NOT the productizer-internal corpus). Encode in RoutedRepo.
7. **conference-appliance governance ACL.** Give it productizer's submit-requirements (AI+1 / human+2, ai-reviewer-bots, non-ai-reviewer, Verified) via its `refs/meta/config`, OR explicitly decide a lighter gate. Operator/admin step.

## Child-story breakdown (each = independent, testable, single-CLI-session, area-coherent, revertable; safe-floor first)
- **R.0 (safe floor, schema/config only, ZERO behavior change):** add `routed_repos` settings + `RoutedRepo` type + `resolve_routed_repo(labels)` with unit tests (no-label→None, resolves, unresolved→raise). No call sites wired. *area: backend, tests · tier: M*
- **R.1 fail-closed dispatch pre-gate:** wire `resolve_routed_repo` BEFORE worktree prep; `repo:`-unresolved → abstain + `runner-blocked:repo-unresolved`. Normal (no-label) path bit-for-bit unchanged (regression test). *area: tooling, tests · tier: M*
- **R.2 generalized fetch+clone:** routed ticket self-clones its repo into the per-instance path; `sync` learns repo_url/ref; orphan-reaper/workspace-safety verified. *area: tooling, backend, tests · tier: L*
- **R.3 GerritReviewDelivery:** Change-Id + dual co-author + push refs/for to the routed gerrit_url with routed identity. *area: backend, tests · tier: L*
- **R.4 project-scope shared Gerrit gates:** add gerrit-project to backpressure/file-mutex/staleness/dup/merged queries. *area: backend, tests · tier: M*
- **R.5 tenant/context selection for routed repos.** *area: backend, tests · tier: M*
- **R.6 conference-appliance governance ACL** (operator/admin: copy productizer submit-requirements + bot groups into refs/meta/config). *tier: X, requires:operator-approval*
- **R.7 INTEGRATION GATE + end-to-end:** add `routed_repos[conference-appliance]`, re-label ONE Case 5 leaf (OP-2170) `repo:conference-appliance`+agent:auto, verify it clones conference-appliance (NOT productizer), pushes refs/for there, normal runners unaffected, change appears under conference-appliance, GitLab mirror reflects. *tier: M · the assemble-into-live gate the SOP demands.*

Dependencies: R.0 blocks all; R.1→R.2→R.3 sequential (dispatch→clone→deliver); R.4/R.5 parallel after R.0; R.6 independent (operator); R.7 blocked by R.1..R.6.

## Roll-out order (de-risk: safe floor → disabled feature → operator-gated activation)
R.0 (inert) → R.1..R.5 (feature, no `routed_repos` entry yet = still inert) → R.6 (ACL) → R.7 (add the one config entry + one ticket = activation). Each via Gerrit review + human +2. No Case 5 leaf gets agent:auto until R.7 proves the routing.

## Stage-4 re-audit fold-in (codex VERDICT: SOUND-WITH-CHANGES — every prior blocker now maps to a child)
1. **R.4 must name BOTH already-merged hardcodes.** `auto-runner-jira.py:424` `already_merged_in_gerrit` queries `message:<ticket> status:merged` with NO `project:` filter and runs pre-pickup (`:2881`); `jira_dispatch.already_merged_in_gerrit:1898` uses `status:merged project:{GERRIT_PROJECT_PATH}` (productizer-pinned). R.4 AC names both.
2. **R.1 is NOT inert if any pickable ticket already carries a `repo:` label** (fail-closed would change its behavior before R.7). R.1 AC: a preflight/test proving no pickable ticket carries `repo:`, OR gate R.1 behind an enable flag until R.7. (Our 21 Case 5 tickets are HOLD + carry no `repo:` label yet — verify before R.1 lands.)
3. **Split R.2 → R.2a (routed clone + `sync_routed_repo`) and R.2b (routed CLI cwd + sentinel + workspace-safety + orphan-reaper)** unless one CLI session can test all of clone-path + sync + wrapper-interaction + cleanup. Default to the split.
- Answered open questions (codex): use a NEW `sync_routed_repo` (don't parameterize the productizer-shaped `sync_to_gerrit_develop:1103`); per-instance workspace (not `TemporaryDirectory`, which hides crash artifacts); self-tenant + context-override (not a customer tenant); project-scoping is an ADDITIVE optional `project:` filter (backpressure is owner-only today at `:560` — don't change the productizer default).

## Open questions for the re-audit (Stage 4) — RESOLVED above
- Should `sync_to_gerrit_develop` be parameterized or forked into `sync_routed_repo`? (audit: don't reuse the hardcoded one.)
- Per-instance workspace path vs contribution_runner's TemporaryDirectory — which, given orphan-reaper?
- Is the conf context a new tenant or self+context-override? (audit F7.)
- Does project-scoping the file-mutex/backpressure risk regressing the productizer queries? (must be additive filter.)
