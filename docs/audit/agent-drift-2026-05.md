# Agent Drift Report - 2026-05

Window: rolling 30d ending 2026-05-31 vs prior 30d

| agent_class | ticket_type | current_n | prior_n | time_delta | success_delta | lessons_delta | alerts |
|---|---|---:|---:|---:|---:|---:|---|
| subscription-codex | Task | 2 | 2 | 38.5% | -50.0% | -37.5% | warn: success_rate_drop=-50.0%, warn: lessons_used_drop=-37.5% |

## Thresholds
- Success rate drop >10% MoM: page operator after baseline, warn during first 2 months.
- Time-to-complete increase >50% MoM: warn.
- Lessons-used drop >30% MoM: warn.
