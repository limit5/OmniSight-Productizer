# ADR 0004 — Per-agent JIRA identity (rejected shared bot pattern)

**Status**: Accepted (2026-05-04)

**Context**

OmniSight runs multiple AI agents in parallel:
- Claude (Anthropic subscription, via auto-runner-sdk.py + auto-runner.py)
- Codex (OpenAI subscription, via auto-runner-codex.py)
- Future: domain specialists, merger-agent, lint-bot, security-bot

Each agent will need to interact with the issue tracker (per [ADR 0001](0001-five-branch-gitflow.md) `feature/<JIRA-key>-<owner>-*` branch convention; per [ADR 0003](0003-gerrit-code-review.md) Gerrit + JIRA cross-link). Two identity models were evaluated:

| Model | How it works |
|---|---|
| **Shared bot** | One JIRA service account (e.g. `omnisight-bot`) used by all AI agents; attribution lives in the comment / commit metadata (e.g. `[claude]: ...`, `[codex]: ...`) |
| **Per-agent identity** | Each agent has own JIRA Cloud account (`claude-bot`, `codex-bot`, ...); attribution is the JIRA `assignee` / `author` / `commenter` field |

The trade-off:

| Dimension | Shared bot | Per-agent |
|---|---|---|
| Cost | 1× per-seat license | N× per-seat license (~$8/user/month each) |
| Attribution | embedded in payload (parseable but lossy) | first-class JIRA field |
| Revoke granularity | all-or-nothing (revoke the bot kills all agents) | per-agent (revoke claude doesn't kill codex) |
| Audit clarity | "the bot did it" → must parse payload | JIRA history shows agent name directly |
| Permission scoping | uniform across agents | per-agent role / project access |
| Token rotation | one rotation point (high blast radius) | per-agent rotation (lower blast radius) |
| Compatibility with Gerrit per-reviewer +1/+2 | broken (all reviews from same identity) | works (each agent's +1 distinct) |

The Gerrit dimension is decisive: [ADR 0003](0003-gerrit-code-review.md)'s submit-rule depends on identifying `merger-agent-bot` distinctly from other AI reviewers, and counting +1 votes per-agent. Shared bot model collapses all AI signal into one row.

**Decision**

Per-agent JIRA identity. Each AI agent has its own Atlassian account:

| Agent | Email pattern | accountId (Track A baseline) |
|---|---|---|
| Claude | `rt3628+claude-bot@gmail.com` | `712020:e98ecc0a-4c88-48f3-ab49-9c3067ca9a34` |
| Codex | `rt3628+codex-bot@gmail.com` | `712020:bc59c45a-f83a-4a4d-904d-53fb15e6063b` |
| Future: merger-agent-bot | `rt3628+merger-bot@gmail.com` | TBD when Phase 3 stands it up |
| Future: lint-bot / security-bot | `rt3628+lint-bot@gmail.com` etc. | TBD |

**Email convention**: `+plus-addressing` against operator's primary mailbox (`rt3628@gmail.com`). All bot mails route to one inbox without needing N forwarding rules.

**Token storage**: file-based, chmod 600, per-agent (see `reference_jira_atlassian.md` memory):
- `~/.config/omnisight/jira-claude-token`
- `~/.config/omnisight/jira-codex-token`
- `~/.config/omnisight/jira-<agent>.env` for site URL + email + project key

**MCP integration**: each agent's runner sources only its own env file (`OMNISIGHT_JIRA_<AGENT>_EMAIL` distinct env var names prevent cross-bot collision). Phase 3 mcp-atlassian server runs per-agent, not shared.

**Cost acceptance**: Atlassian Cloud Standard ~$8.15/user/month. With 2 agents (Track A baseline) = ~$16/month. With 5 agents (Phase 3 full) = ~$40/month. User accepted as the cost of clear attribution + revoke granularity.

**Consequences**

Positive:
- First-class attribution in JIRA history (no payload-parsing required for "who did this")
- Per-agent revoke (token leak contained to one agent)
- Per-agent permission scoping (e.g. lint-bot can comment but not transition; merger-agent can edit but not delete)
- Compatible with Gerrit per-reviewer scoring (same identity model crosses both systems)
- Audit log clarity for compliance / post-mortem

Negative:
- ~$8/agent/month per-seat cost (linear scaling)
- More Atlassian admin overhead (account creation, license assignment, group membership)
- Plus-addressing requires gmail (or equivalent) — if operator switches to a provider without it, must reorganise
- Token rotation is N× the work (one-by-one, not all-at-once)

Neutral:
- Each agent must store + load its own auth file; runner scripts already structured for this
- displayName collisions possible (`claude-bot` duplicate if same name set on multiple Atlassian sites) — not a concern for single-site

**Track A verification (2026-05-04)**

Both bots verified via 4-verb smoke test (CREATE / TRANSITION / COMMENT / DELETE) against `OP` project on `https://soraapp.atlassian.net`. See `reference_jira_atlassian.md` memory for OP-1 / OP-2 baseline runs.

**Related**

- [ADR 0003 — Gerrit code review](0003-gerrit-code-review.md) — same identity model used for Gerrit per-reviewer +1/+2
- `reference_jira_atlassian.md` (memory) — Track A baseline + auth file paths
- `project_governance_migration_plan.md` (memory) — Phase 0-3 rollout
