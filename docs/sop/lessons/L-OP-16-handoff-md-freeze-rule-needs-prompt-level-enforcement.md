---
id: L-OP-16
ticket: OP-16
title: `HANDOFF.md` freeze rule needs prompt-level enforcement
date: 2026-05-06
tags: [ci, git, jira, runner]
legacy_lesson: 7
---

# `HANDOFF.md` freeze rule needs prompt-level enforcement

**Situation**: After CLAUDE.md L1 was amended 2026-05-06 to mark `HANDOFF.md` FROZEN (per `docs/sop/jira-ticket-conventions.md` §7), OP-16 codex run still appended a 12-line resolution entry to `HANDOFF.md`. Codex reads CLAUDE.md but the legacy "always generate HANDOFF.md" SOP from `auto-runner-codex.py` training history overrode the new convention.

**Fix**: `auto-runner-jira.py::_build_prompt` adds explicit "DO NOT append to HANDOFF.md — that file is FROZEN" directive. Prompt-level instruction is deterministic; CLAUDE.md alone is too easy to skip when the model has prior training to the contrary. Commit `a6e6cd9f`.

**Verification**: OP-246 + OP-15 (after fix) committed without touching HANDOFF.md. `git log main..HEAD -- HANDOFF.md` returns empty for both runs.

**Generalisation**: Any rule change in CLAUDE.md / convention docs that overrides existing AI training MUST be mirrored as an explicit prompt directive when AI is invoked autonomously. The CLAUDE.md amendment is the *intent*; the prompt directive is the *enforcement*.
