# ADR 0001 — Five-branch Git Flow

**Status**: Accepted (2026-05-04)

**Context**

Until 2026-05-04 OmniSight ran a single-branch model: `master` was the only persistent branch, all commits went directly to it, deploys ran from `master` tip. This worked while:

- One human contributor (operator)
- One AI runner at a time
- No external collaborators
- No formal stabilisation window between development and prod

Three forces broke that model:

1. **Multi-AI parallelism**. By 2026-05-04 we run Claude (subscription) + Codex (subscription) in parallel, with a Tier-B worktree convention so they don't clobber master directly. The KS.2/KS.3 + FX.11 collision (codex-work was 21 commits behind master, surfaced multi-head alembic + dependency drift, see governance lesson #1) showed that direct-to-master parallelism doesn't scale.
2. **Multi-human collaboration approaching**. Future reviewers / contributors can't review on a fast-moving `master` tip — need a stable target.
3. **Production-grade governance**. AI-driven code requires explicit review gates (Gerrit +1/+2 + O7 dual-sign per CLAUDE.md L1). Single-branch + direct push has no review chokepoint.

The 2026-05-04 strategic session evaluated three branch models:

| Model | Pros | Cons |
|---|---|---|
| Trunk-based + short-lived feature branches | Simple, fast | No stabilisation window; emergency hotfix conflicts with in-progress dev |
| GitHub Flow (main + feature/*) | Industry default | No `develop` integration trunk; release timing tied to merge timing |
| Full Git Flow (5 branches) | Stabilisation + emergency path + integration trunk | More ceremony |

**Decision**

Adopt full Git Flow with 5 branch types:

```
main         (prod, FF-only from release/* / hotfix/*)
release/v*   (stabilisation window per release)
develop      (integration trunk, Gerrit-merged)
feature/*    (per-contributor WIP, one branch per agent / per JIRA ticket)
hotfix/*     (emergency main fix, cherry-pick back to develop)
```

**Naming + flow rules**

- `main` only advances via fast-forward from `release/v<N>` (release cut) or `hotfix/<id>` (emergency).
- `develop` is the canonical integration target; all `feature/*` merges land here via Gerrit submit.
- `release/v<N>` cuts from `develop` tip when the operator decides "freeze". Bug fixes during stabilisation land directly on `release/v<N>` and cherry-pick back to `develop`.
- `feature/<JIRA-key>-<owner>-<short-desc>` is the canonical feature naming (e.g. `feature/OP-42-claude-cmek-wizard`). One per agent at a time per ticket; agent's own worktree.
- `hotfix/<id>` cuts from `main` tip; lands on `main` (FF), then cherry-pick to `develop` (and any active `release/*`).
- **`master` is renamed to `main`** as part of Phase 1 migration (BLM-era industry default, see [`migration-plan-2026-05.md`](../sop/migration-plan-2026-05.md)).

**Branch protection (post-Phase-1)**

| Branch | Direct push | Force push | Delete | FF-only merge |
|---|---|---|---|---|
| `main` | ❌ (operator only) | ❌ | ❌ | ✓ from release/* / hotfix/* |
| `develop` | ❌ (Gerrit submit only) | ❌ | ❌ | n/a (merge commits OK) |
| `release/v*` | only release manager | ❌ | ❌ (until release shipped + 30 days) | n/a |
| `feature/*` | owner only | ✓ for that owner | ✓ after merge | n/a |
| `hotfix/*` | only release manager | ❌ | ✓ after merge | n/a |

**Consequences**

Positive:
- Stabilisation window between feature complete and prod deploy.
- Emergency hotfix path doesn't block in-progress dev.
- Clear audit trail (which feature shipped in which release).
- Compatible with Gerrit (`refs/for/develop` review semantics).
- Multi-contributor parallelism without collision (each owns `feature/*`).

Negative:
- More ceremony per change (4 commit + 2 merge sequence vs 1 push).
- Branch GC discipline required (stale `feature/*` accumulate).
- Tooling references `master` → must be swept (deploy scripts, CI, runners, see Phase 1 SOP).
- Initial cognitive load — operators / contributors learn the flow.

Neutral:
- Compatible with both fast-forward-only deploy gates (FX.7.x scripts already use allowlist) and merge-commit-history visualisation.

**Related**

- [ADR 0002 — GitLab primary + GitHub mirror](0002-gitlab-primary-github-mirror.md) — defines where these branches live
- [ADR 0003 — Gerrit code review](0003-gerrit-code-review.md) — defines how `feature/*` → `develop` merges happen
- [ADR 0005 — Tier S/M/L/X authority](0005-tier-authority-levels.md) — defines who can promote each branch direction
- [Phase 1 migration SOP](../sop/migration-plan-2026-05.md) — operational steps for the rename + branch cut
