# Architecture Decision Records (ADR)

Records of significant architectural / governance decisions made for OmniSight. Each ADR captures: status, context, decision, consequences. Older ADRs are not deleted when superseded — they're marked `Superseded by ADR-XXXX` so the historical reasoning stays auditable.

Format: Michael Nygard style (Status / Context / Decision / Consequences) with light adaptations.

## Index

| # | Title | Status | Date |
|---|---|---|---|
| [0001](0001-five-branch-gitflow.md) | Five-branch Git Flow | Accepted | 2026-05-04 |
| [0002](0002-gitlab-primary-github-mirror.md) | GitLab self-hosted primary, GitHub one-way mirror, Gerrit review layer | Accepted | 2026-05-04 |
| [0003](0003-gerrit-code-review.md) | Gerrit code review (primary review tool) | Accepted | 2026-05-04 |
| [0004](0004-per-agent-jira-identity.md) | Per-agent JIRA identity (rejected shared bot) | Accepted | 2026-05-04 |
| [0005](0005-tier-authority-levels.md) | Tier S/M/L/X AI authority with 4-layer protection | Accepted | 2026-05-04 |
| [0006](0006-tls-termination-synology-reverse-proxy.md) | TLS termination at Synology DSM Reverse Proxy + Sectigo wildcard cert | Accepted | 2026-05-05 |
| [0007](0007-multi-provider-subscription-orchestrator.md) | Multi-provider subscription orchestrator + cost calculator (Anthropic / OpenAI MVP, Gemini / xAI structural slot, v3 onboarding) | Accepted | 2026-05-06 |
| [0008](0008-agent-rpg-class-skill-leveling.md) | Agent RPG class & skill leveling system (Guild × instance × XP × 3-layer memory × talent tree × party synergy) | Accepted | 2026-05-06 |

## How to add a new ADR

1. Pick the next free number (`ls docs/adr/ | grep '^[0-9]'`).
2. Copy the template from `0001-five-branch-gitflow.md` (Status / Context / Decision / Consequences sections).
3. Fill in ≥ 3 alternatives evaluated in Context.
4. Be explicit about Consequences (positive / negative / neutral).
5. Cross-link related ADRs at the bottom.
6. Update this README index.

## How to supersede an ADR

When a decision is overturned, do **not** delete the old ADR.

1. Add `Status: Superseded by ADR-XXXX` to the old ADR header.
2. Write the new ADR with `Status: Accepted` and `Supersedes: ADR-YYYY` cross-reference.
3. Update this index to show the old as `Superseded`.

## Operational SOPs (downstream of ADR)

ADRs record decisions; SOPs implement them. Implementation steps for the 2026-05 governance migration:

- [Phase 0-5 migration plan](../sop/migration-plan-2026-05.md) — operational steps for the rename + branch cut + GitLab + Gerrit rollout
- [Phase implementation SOP](../sop/implement_phase_step.md) — generic per-Phase implementation rules
- [Bug fix SOP](../sop/fix_bug_step.md) — generic bug fix rules
