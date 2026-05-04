# ADR 0003 — Gerrit Code Review (primary review tool, not GitLab MR / GitHub PR)

**Status**: Accepted (2026-05-04)

**Context**

CLAUDE.md L1 (immutable agent rules) defines the review semantics:

> NEVER bypass Gerrit Code Review. AI reviewer max score is +1. Human +2 required for merge.
> **Exception (O6 Merger Agent, #269):** The `merger-agent-bot` account MAY cast Code-Review: +2 on patchsets it produced to resolve merge conflicts, scoped strictly to the correctness of the conflict-resolution block. The merger's +2 never substitutes for a human +2 — the O7 submit-rule enforces a dual-sign gate.

This implies fine-grained scoring (`+1` separate from `+2`, `merger-agent-bot` distinct from `non-ai-reviewer` group) that GitLab MR approvals + GitHub PR reviews don't natively express:

| Capability | GitHub PR | GitLab MR | Gerrit |
|---|---|---|---|
| Per-reviewer score (+1 / +2 / -1 / -2) | ❌ binary | ❌ binary | ✓ |
| Patchset-level review (rebase doesn't lose review) | ❌ | partial (re-review on push) | ✓ via Change-Id |
| Submit-rule scriptable (e.g. "1 +2 from merger-bot AND 1 +2 from non-ai") | ❌ | partial (CODEOWNERS) | ✓ Prolog rules |
| `Depends-On` chain (multi-patchset dependency declaration) | ❌ | ❌ | ✓ |
| Trivial rebase / reorder during review | partial | partial | ✓ first-class |

GitLab MR exists in our infra (per [ADR 0002](0002-gitlab-primary-github-mirror.md)) but is treated as a **post-review artefact**, not the review surface.

**Decision**

Gerrit is the canonical code review tool. Architecture:

```
contributor's worktree
  ↓ git push origin HEAD:refs/for/develop
Gerrit review queue
  ↓ +1 from AI reviewers (lint-bot / security-bot / merger-bot / claude / codex)
  ↓ +2 from non-ai-reviewer group member (human)
  ↓ O7 submit-rule gate (Prolog): require ≥1 +2 from non-ai-reviewer
  ↓ Gerrit submit (auto-creates merge commit on GitLab develop branch)
GitLab develop branch
```

**Reviewer roles (group membership)**

| Group | Members | Max score | Purpose |
|---|---|---|---|
| `non-ai-reviewer` | operator, future human contributors | +2 | merge gate (only humans) |
| `ai-reviewer` | claude, codex, future agents | +1 | parallel AI review (lint, sec, correctness) |
| `merger-agent` | `merger-agent-bot` | +2 (special) | O6 conflict-resolution patchsets only |
| `lint-bot` | `lint-bot` | +1 | automated style + format |
| `security-bot` | `security-bot` | +1 | automated security scan |

**O7 submit-rule (Prolog)**

```prolog
% require at least one +2 from non-ai-reviewer group
submit_rule(submit(R)) :-
  gerrit:max_with_block(-2, 2, 'Code-Review', CR_max),
  (CR_max = ok(_) ; CR_max = need(_)),
  approver_in_group(non_ai_reviewer, _),
  R = label('Code-Review-Human-Approved', ok).
```

Plus O6 path: a `merger-agent-bot` +2 is valid *only* if the patchset's diff is bounded to conflict-resolution markers (enforced by hook checking diff shape). Non-conflict diffs from merger-agent get its +2 demoted to +1.

**Tier-based bypass (per [ADR 0005](0005-tier-authority-levels.md))**

For Tier S items (path-based whitelist, low blast radius), AI can self-+2 with 24h revert window. Submit-rule queries the path map → if all touched paths are Tier S → skip non-ai-reviewer requirement. Tier M / L / X all require human +2.

**Consequences**

Positive:
- Per-reviewer score lets AI agents contribute partial signal (`+1` "looks good to me, lint clean") without holding up merge.
- Patchset-level Change-Id survives rebase — review history doesn't reset on every push.
- Prolog submit-rule is auditable + testable (separate from any UI / GitLab logic).
- O6 merger-agent path solves the "AI must resolve trivial merge conflicts" without giving full +2 authority.
- `Depends-On` chain enables atomic multi-patchset features (e.g. backend + frontend in two commits but submit together).

Negative:
- Gerrit operational complexity (Java service + dedicated DB; OpenID auth bridge; SSH key management for git push).
- Steep contributor learning curve (Change-Id hook, `refs/for/branch` push semantics, no GitHub-style "Files Changed" tab).
- mcp-gerrit MCP server not available off-the-shelf; must self-author with FastMCP (Phase 3 task).
- Gerrit UI is dated (vs GitLab/GitHub).

Neutral:
- Existing GitLab MR continue to work as the merged-artefact view; reviews show as "approved by Gerrit".

**Phase rollout**

- **Phase 0**: Track C deferred. JIRA + GitLab baseline first.
- **Phase 1-2**: No Gerrit yet. Reviews fall back to GitLab MR + manual `+2` convention in MR description.
- **Phase 3**: Gerrit infra stood up. `refs/for/develop` push becomes mandatory entry. mcp-gerrit FastMCP server authored.
- **Phase 3+ retrospective**: if Track C fails (>30 days unresolved), fallback is GitLab MR + CODEOWNERS + manual review tracking, retry Gerrit in 1-2 months (per governance plan memory).

**Related**

- CLAUDE.md L1 — review rules (immutable, takes precedence over this ADR if conflict)
- [ADR 0001 — Git Flow](0001-five-branch-gitflow.md) — defines `develop` as Gerrit's canonical target
- [ADR 0005 — Tier authority](0005-tier-authority-levels.md) — defines when AI can self-+2 (Tier S only)
- `project_governance_migration_plan.md` (memory) — Phase 3 timeline + Track C fallback
