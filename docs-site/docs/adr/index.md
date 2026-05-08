---
title: ADRs
---

# Architecture Decision Records

ADRs capture the *why* behind major architecture choices. Each ADR has a
status (Proposed / Accepted / Superseded) and a date. Once Accepted, an ADR
is binding until explicitly superseded by a follow-up ADR.

Currently published in this scaffold:

- [ADR-0001 — Five-branch Git Flow](0001-five-branch-gitflow.md) — adopting
  develop / feature/* / release/* / hotfix/* / main as the persistent branch
  topology, replacing the single-master model.

The remaining ADRs in `docs/adr/` (GitLab/GitHub mirroring, Gerrit review,
per-agent JIRA identity, tier authority, TLS termination, subscription
orchestrator, agent RPG class, deployment automation, sprint plan) will be
migrated in follow-up tickets.
