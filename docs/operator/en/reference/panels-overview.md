# Panels Overview — what every tile on the screen is for

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

The dashboard has 12 panels. On desktop they tile; on mobile / tablet
you swipe between them using the bottom nav bar. This is the one-line
job of each, with a pointer to a deeper doc where relevant.

## Top-bar (always visible)

| Element | Job | Deep link |
|---|---|---|
| **MODE** pill | Global autonomy level — how much the AI does without asking | [operation-modes.md](operation-modes.md) |
| **Sync count** | Number of global "Singularity Sync" invocations this session | — |
| **Provider health** | Which LLM providers are reachable right now | — |
| **Emergency Stop** | Halts every running agent and every pending invocation | — |
| **Notifications** bell | Unread L1-L4 notifications (Slack / Jira / PagerDuty / in-app) | — |
| **Settings** gear | Provider keys, integrations, per-agent model overrides | — |
| **Language** globe | Switch UI language (affects doc links too) | — |

## Primary panels

| Panel | URL param | Who cares | One-line job |
|---|---|---|---|
| **Host & Device** | `?panel=host` | Engineer | Which WSL2/Linux host and attached camera / dev-board you are driving |
| **Spec** | `?panel=spec` | PM + Eng | The `hardware_manifest.yaml` spec your agents are building against |
| **Agent Matrix** | `?panel=agents` | Both | 8 agents × their current status / thought chain / progress |
| **Orchestrator AI** | `?panel=orchestrator` | Both | Chat with the supervisor agent; slash commands live here |
| **Task Backlog** | `?panel=tasks` | PM | Sprint-like task list, drag to reassign, sort by priority |
| **Source Control** | `?panel=source` | Engineer | Per-agent isolated workspace, branch, commit count, repo URL |
| **NPI Lifecycle** | `?panel=npi` | PM | Phases from Concept → Sample → Pilot → MP with dates |
| **Vitals & Artifacts** | `?panel=vitals` | Both | Build logs, simulation results, firmware artifacts to download |
| **Decision Queue** | `?panel=decisions` | Both | Pending decisions awaiting approve/reject + history | ⭐ |
| **Budget Strategy** | `?panel=budget` | PM | 4 strategy cards × 5 tuning knobs for token / cost control |
| **Pipeline Timeline** | `?panel=timeline` | Both | Horizontal timeline across phases, current-progress marker, ETA |
| **Decision Rules** | `?panel=rules` | Both | Operator-defined rules that override severity/mode defaults |

## Deep-link cheat sheet

URL params survive page reload and can be shared with a teammate.

```
/?panel=decisions                     ← open Decision Queue
/?decision=dec-abc123                 ← open Queue AND scroll to this decision
/?panel=timeline&decision=dec-abc123  ← timeline visible, decision still queued
```

Invalid `?panel=` values fall back to the Orchestrator panel (not a
crash).

## Mobile / tablet navigation

On screens narrower than the `lg` breakpoint (1024 px):

- The 12 panels collapse into a single-column view
- The **bottom nav bar** shows: ← previous panel, centre pill (tap to
  open full menu), → next panel, plus a dot row mapping each panel
- Dot tap targets are 44 × 44 px even though the visual dot is 8 px
  (WCAG 2.5.5)

## Keyboard shortcuts (inside Decision Queue / Toast)

- **A** — approve the focused / newest decision with its default option
- **R** — reject the focused decision
- **Esc** — dismiss the current toast without acting
- **← / →** or **Home / End** — switch the Decision Queue tab
  between PENDING and HISTORY

## Under the hood

- Panel registry: `app/page.tsx · VALID_PANELS` + the `readPanelFromUrl()`
  helper
- URL sync: `app/page.tsx` `useEffect` ties `activePanel` to
  `?panel=` via `history.replaceState`
- Mobile nav: `components/omnisight/mobile-nav.tsx`

## Related reading

- [Operation Modes](operation-modes.md)
- [Decision Severity](decision-severity.md)
- [Glossary](glossary.md) — unsure what "NPI" or "singularity sync" mean?
