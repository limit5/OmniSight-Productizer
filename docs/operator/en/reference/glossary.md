# Glossary

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

Domain-specific terms the UI and logs use. Alphabetical.

**Agent** — a specialised LLM worker. Eight types ship by default
(firmware, software, validator, reporter, reviewer, general, custom,
devops), each with a `sub_type` that selects a role file from
`configs/roles/*.yaml`. Each agent gets an isolated git workspace.

**Artifact** — any file the pipeline produces that is worth keeping:
a compiled firmware image, a simulation report, a release bundle.
Lives under `.artifacts/` and is surfaced in the Vitals & Artifacts
panel.

**Budget Strategy** — a named bundle of five tuning knobs (model
tier, max retries, downgrade threshold, freeze threshold, prefer
parallel) that shapes how expensive each agent invocation is
allowed to be. Four strategies ship: `quality`, `balanced`,
`cost_saver`, `sprint`.

**Decision** — any point where the AI stops and either acts on its
own, asks you, or times out to a safe default. Carries a severity
(`info` / `routine` / `risky` / `destructive`) and a list of options.

**Decision Queue** — the panel (and the in-memory list) of pending
decisions. Newest first.

**Decision Rule** — an operator-authored override that matches a
`kind` glob (e.g. `deploy/staging/*`) and pins the severity,
default option, or auto-execute modes. Rules persist to SQLite
(Phase 50-Fix A1).

**Emergency Stop** — halts every running agent and pending
invocation. Releases concurrency slots, emits `pipeline_halted`. Use
Resume to come back.

**Invoke** — a "global sync" action that asks the orchestrator to
examine current state and do whatever needs doing next. Can also
take a free-form command (`/invoke fix the build`).

**LangGraph** — the agent graph framework underneath. You usually
don't see it, but "graph state" and "reducer" in logs come from
LangGraph semantics.

**L1 / L2 / L3 memory** — tiered agent memory. L1 = immutable core
rules from `CLAUDE.md`. L2 = per-agent role + recent conversation.
L3 = episodic (searchable past incidents, via FTS5).

**MODE** — the global autonomy level. See
[operation-modes.md](operation-modes.md).

**NPI** — New Product Introduction. The hardware-shipping lifecycle
spanning Concept → Sample → Pilot → Mass Production. Each phase
has its own pipeline.

**Operation Mode** — formal name for MODE. Four values: manual,
supervised, full_auto, turbo.

**Pipeline** — an ordered sequence of steps that moves a task from
"idea" to "shipped". Steps are grouped into phases. The Pipeline
Timeline panel visualises the current run.

**REPORTER VORTEX** — the left-side scrolling log of everything the
system does. Every `emit_*()` event writes here.

**SSE** (Server-Sent Events) — the one-way push channel the backend
uses to feed real-time updates to every connected browser. Endpoint
`/api/v1/events`. Schema at `/api/v1/system/sse-schema`.

**Singularity Sync** — the marketing name for Invoke. Same thing.

**Slash command** — any command starting with `/` typed into the
Orchestrator AI panel. The built-in ones are `/invoke`, `/halt`,
`/resume`, `/commit`, `/review-pr`, plus anything the skills system
defines.

**Stuck detector** — a watchdog that proposes remediation decisions
(switch model, spawn alternate, escalate) when an agent has been
flailing on the same error for a while. Runs every 60 s.

**Sweep** — the periodic (default 10 s) pass that times out pending
decisions whose deadline has elapsed. Can be triggered manually from
the Decision Queue header.

**Task** — a unit of work. Has an owner agent, priority, status,
parent / child tree, and optional external issue link (GitHub,
GitLab, Gerrit).

**Token warning** — an SSE event fired at 80 % / 90 % / 100 % of the
daily LLM token budget. Triggers auto-downgrade to a cheaper model
at 90 %.

**Workspace** — the per-agent isolated git clone where work
happens. Lives under `OMNISIGHT_WORKSPACE` (defaults to a temp dir).
Status: `none | active | finalized | cleaned`.

## Related

- [Operation Modes](operation-modes.md)
- [Panels Overview](panels-overview.md)
- `backend/models.py` — canonical enum definitions
