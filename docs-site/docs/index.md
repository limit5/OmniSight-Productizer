---
title: OmniSight Docs
---

# OmniSight Docs

Internal documentation site for the OmniSight platform. The five top-level
sections mirror the document classes in the repo:

- [SOPs](sop/index.md) — standard operating procedures that govern how work
  is planned, reviewed, and shipped.
- [ADRs](adr/index.md) — architecture decision records.
- [Lessons](lessons/index.md) — per-ticket lessons captured after a non-trivial
  fix or unexpected outcome.
- [Runbooks](runbook/index.md) — step-by-step operator playbooks for live
  systems (deploys, recovery, rotations).
- [Operations](operations/index.md) — operational reference (pipelines,
  dashboards, integrations).

This site is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/).
The scaffold landed in OP-786; subsequent tickets migrate the full corpus from
`docs/` and wire it into the OP-792 develop-merge publish pipeline.

## Local preview

```bash
make docs-serve
```

Then open <http://127.0.0.1:8765>. To use a different port, pass
`DOCS_PORT=NNNN`.
