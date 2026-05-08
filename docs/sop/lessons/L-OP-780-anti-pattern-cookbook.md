---
id: L-OP-780
ticket: OP-780
title: Architecture incidents need symptom-first cookbook entries
date: 2026-05-08
tags: [architecture, sop, meta]
---

# Architecture incidents need symptom-first cookbook entries

**Situation**: The 2026-05-08 runner-fleet hardening sprint surfaced the
same architecture traps across multiple tickets: flat-file registries,
shared mutable worktree config, retry-unsafe external mutations, missing
event cursors, strict terminal-state handlers, and migration tickets
colliding with in-flight work. Future tickets risk re-discovering the same
root causes if the lessons remain scattered across ticket comments.

**Fix**: Consolidate repeated traps into `docs/sop/architecture-anti-patterns.md`
using a mandatory Symptom / Root Cause / Cure / Examples / Reference Tickets
format, then cross-link it from the ticket DoD path so authors can scan by
symptom before filing or completing architecture/process work.

**Verification**: OP-780 verifies the cookbook file exists, covers the ten
requested sprint patterns in the required format, links from
`docs/sop/jira-ticket-conventions.md` near Definition of Done, and regenerates
`docs/sop/lessons-learned.md` with this per-file lesson.

**Generalisation**: When a process or architecture failure recurs in 2+
tickets, document it as a symptom-first cookbook entry with a mechanical
cure. Ticket authors should cite the existing Cure instead of redesigning
the response from scratch.
