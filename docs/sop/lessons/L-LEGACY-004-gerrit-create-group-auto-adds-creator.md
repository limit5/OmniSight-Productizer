---
id: L-LEGACY-004
ticket: LEGACY-004
title: `gerrit create-group` auto-adds creator
date: 2026-05-05
tags: [ci, gerrit, legacy]
legacy_lesson: 4
---

# `gerrit create-group` auto-adds creator

**Situation**: Setting up Gerrit groups (`non-ai-reviewer`, `ai-reviewer`, `merger-agent-bot` per ADR 0003), the `gerrit create-group` SSH command auto-added the creator (sora) to every group it created. ADR 0003 explicitly forbids sora from being in `ai-reviewer` or `merger-agent-bot` (separation of concerns).

**Fix**: After `create-group`, immediately run `gerrit set-members <group> --remove sora` for any group sora shouldn't be in. Codified as Gerrit SOP cleanup step.

**Verification**: Post-cleanup `gerrit ls-members <group>` returns expected member list. ADR 0003 separation of concerns preserved.

**Generalisation**: Admin tools that "helpfully" auto-include the actor often violate intended permission boundaries. Always verify membership / ACL after creation, never trust create-time defaults.
