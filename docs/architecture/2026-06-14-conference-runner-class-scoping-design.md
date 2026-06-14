# Design — multi-project runner routing (per-ticket repo target, fixed runner pool)

**Date:** 2026-06-14 (v2 — supersedes the scoped-class approach below) · **Status:** design (for review before any code) · **Touches the runner's own execution path → mandatory human +2 per L1.**

## The better architecture (operator insight 2026-06-14)
JIRA, the Gerrit server, and the bot accounts (claude-bot/codex-bot) are all **shared**. The ONLY per-ticket difference between "work on the big project" and "work on conference-appliance" is **which repo to clone and which Gerrit project to push to**. Therefore:

> Do NOT spin up a dedicated runner instance per project (that explodes the runner count as projects grow). Instead: keep a **fixed runner pool**; at pickup, the runner **resolves the target repo from the ticket**, clones it into a **per-project per-runner workspace**, runs the agent there, and pushes `refs/for/develop` to that project's Gerrit. N projects → 0 new runners. This also fixes the mis-pickup at the ROOT (any runner that picks a conf ticket clones the RIGHT repo — there is no "wrong repo" anymore), rather than the scoped-class approach which only prevented certain runners from picking.

## This mechanism ALREADY EXISTS — generalize, don't build
The **camviewpro contribution lane** is exactly this pattern, just hardwired to one label + GitHub delivery:
- **Dispatch seam** (`auto-runner-jira.py:3231`): `if _is_camviewpro_contribution(snapshot.labels): _run_camviewpro_contribution(...)` — a per-ticket branch that runs a DIFFERENT repo's work instead of the big-project clone.
- **Per-ticket worktree** (`contribution_runner.py:129`): `worktree = workspace_root / _repo_dir_name(source.repo_url)`; clones the target repo, fetches base, creates the branch, runs `implement()` THERE. This is "runner works in the sub-project's own working dir."
- **Gerrit delivery already modeled** (`delivery_target.py`): `repo_url` doc = "full git push URL (e.g. an SSH **Gerrit** or GitHub URL)", `ref_spec=f"refs/for/{target}"` "for a **Gerrit review queue**". `backend/config.py:94` docstring even ships a Gerrit example: `{"ACME": {"repo_url": "ssh://...gerrit...:29418/acme/widgets", "ref_spec": "refs/for/main", "git_account_ref": "acme-gerrit"}}`.

The ONLY camviewpro-specific piece is the GitHub delivery (`open_contribution_pr`). For Gerrit projects we use the Gerrit push the delivery model already describes.

## Change set (generalize the existing lane to Gerrit targets)
1. **Generalize the dispatch predicate.** `_is_camviewpro_contribution(labels)` → a general `resolve_routed_repo(labels, project_key)` that reads a `repo:<name>` label (or a project/scope→repo map in settings) and returns a routed-repo descriptor (repo_url + ref_spec + git_account_ref + clone identity). camviewpro becomes ONE entry; conference-appliance is another (`repo:conference-appliance` → `ssh://<bot>@sora.services:29418/omnisight/conference-appliance`, `refs/for/develop`). No label → the normal big-project path (unchanged — the safe floor).
2. **Add a Gerrit contribution delivery** alongside the GitHub PR one. The contribution_runner clone/branch/implement flow is reused as-is; only the final delivery differs: `git push origin HEAD:refs/for/develop` to the routed Gerrit project (identity via `jira_dispatch.resolve_gerrit_push_identity`, the same path the normal lane uses). This is the only genuinely new code.
3. **Per-project workspace path.** Clone into `<workspace_root>/<project>/<repo>-<instance>/...` so concurrent projects never share a working tree (operator's path structure). Trivial change to `_repo_dir_name`/workspace_root composition.
4. **Routing config.** A settings map (like `product_sources`/`delivery_targets`) keyed by `repo:<name>` (or scope) → {repo_url, ref_spec, git_account_ref}. conference-appliance + camviewpro both live here. Adding a future project = one config entry + a `repo:` label on its tickets. ZERO runner-count growth.

## Why this is better than the scoped-class approach (now rejected)
| | scoped-class (rejected) | per-ticket routing (this) |
|---|---|---|
| runner count as projects grow | +1 instance pair per project | **fixed** |
| mis-pickup risk | treated as symptom (prevent pickup) | **eliminated at root** (clone right repo) |
| new project onboarding | new class in 5 maps + new systemd units + re-class tickets | **one config entry + a `repo:` label** |
| code duplication | none | none |
| reuses proven mechanism | no (new maps) | **yes (camviewpro lane)** |

## Shared-by-design (accepted trade-off)
The runner CODE stays single (big-project-managed) — it is shared platform infrastructure that every project benefits from. A bad runner-core commit affects all projects equally (including the big project itself); that is already gated by mandatory human +2 on runner-core changes and recovers for everyone on revert. The isolation the operator wants is **workspace/clone/push isolation** (projects never stomp each other's trees) — this design delivers exactly that, without exploding runner count or duplicating ~181 backend/agents files.

## Roll-out (each step reversible)
1. Land the generalized routing + Gerrit delivery via Gerrit review (`refs/for/develop`, human +2). Pure addition; no `repo:` label → unchanged normal path (safe floor). Regression gate: existing OP tickets still run in the big-project clone exactly as before.
2. Add the conference-appliance routing config entry. Re-label ONE leaf (OP-2170) with `repo:conference-appliance` + `agent:auto`. Verify: a runner picks it, clones conference-appliance into its per-project workspace, implements, pushes `refs/for/develop` to **conference-appliance** (NOT productizer); GitLab mirror (stopgap timer) reflects after submit.
3. On green: label the rest of the Case 5 leaves + enable agent:auto as blockers clear.

## Verification gates
- After step 1: a normal OP ticket (no `repo:` label) still runs against productizer (no regression); a unit test for `resolve_routed_repo` (camviewpro + conference + no-label cases).
- After step 2: the conf ticket's Gerrit change is under `omnisight/conference-appliance`; the runner's workspace shows the per-project path; no normal-lane behavior change.

## Risks / notes
- Edits the runner's own pickup/dispatch path → **mandatory human +2** (L1). Additive; no-`repo:`-label path bit-for-bit unchanged.
- The `repo:` label value must resolve in the routing config or the runner must fail-closed (abstain + flag), never silently fall back to the big-project repo (that was the original mis-pickup). Fail-closed is the key safety property.
- Credentials/quotas/bots remain shared (claude-bot/codex-bot, provider quota, Postgres) — by design.

---
## (superseded) v1 — scoped agent-class approach
The original v1 proposed a new `subscription-{claude,codex}-conf` class registered across ~5 maps + dedicated systemd instances. Rejected because it grows the runner count per project and treats mis-pickup as a symptom. Kept here for history; the per-ticket routing above is the chosen design.
