# OmniSight Operator Guide — English

Welcome. This guide is for the people who actually use the command
center, not the people who built it. It answers "what does this button
do?" and "why did my task just stop?" in plain language, and links to
the source only when you need to go deeper.

## Who should read which section

| If you are … | Start here |
|---|---|
| A product owner or release captain deciding what ships | [Operation Modes](reference/operation-modes.md) → [Decision Severity](reference/decision-severity.md) |
| An embedded engineer running pipelines day-to-day | [Panels Overview](reference/panels-overview.md) → [Glossary](reference/glossary.md) |
| Anyone stuck on a specific screen | Click the `?` icon in that panel's header |

## Quick map

- **Reference** (`reference/`) — precise, exhaustive, always current
- **Glossary** — what "agent" / "task" / "pipeline" / "NPI" mean here
- **Troubleshooting** (coming) — the app shows a red banner, now what?
- **Tutorials** (coming) — follow-along first-invoke walkthroughs

## Agent identity & leveling (Priority RPG)

Agents are no longer just stateless runners — each has a Character Card
with a Guild, level, XP, talents, and per-skill / per-tool proficiency.
For day-to-day operator how-to (viewing the roster, picking a Guild,
reading XP), see the
[Agent RPG system operator guide](../../operations/agent-rpg-system.md).
For the design rationale and the schema lock-ins behind it, see
[ADR-0008 — Agent RPG Class & Skill Leveling System](../../adr/0008-agent-rpg-class-skill-leveling.md)
(the authoritative spec — when this guide and the ADR diverge, the
ADR wins).

## Language versions

This doc is mirrored in English (`en/`), Traditional Chinese
(`zh-TW/`), Simplified Chinese (`zh-CN/`), and Japanese (`ja/`).
English is the source of truth; others are translated. When a
translation lags, the `source_en:` header at the top of each file
tells you which English revision it tracks.
