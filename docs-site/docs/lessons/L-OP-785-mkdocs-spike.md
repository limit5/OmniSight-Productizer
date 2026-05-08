---
id: L-OP-785
ticket: OP-785
title: MkDocs spike — framework decision
date: 2026-05-08
tags: docs, framework, adr-0012
---

# L-OP-785 — MkDocs spike — framework decision

**Situation**

OP-792 had shipped a hand-rolled Python markdown renderer
(`backend/docs_static_site.py`) to unblock the docs-site publish pipeline,
but no ADR captured *why* that was the chosen long-term framework. The
team's three growing markdown corpora (ADRs, lessons, operator docs) need
search, navigation, theme, and JIRA cross-linking that a hand-rolled
renderer would have to grow line by line. OP-785 was the formal
framework-decision ticket that OP-792 deferred.

**Fix**

Wrote ADR-0012 evaluating MkDocs / Docusaurus / Astro / custom against
the OP-785 criteria (incremental build < 30s, frontmatter support, JIRA
plugin feasibility, theme customisation, deploy compatibility) and chose
**MkDocs with the Material theme**. Scaffolded `docs-site/` with:

- `mkdocs.yml` configured against the Material theme, with `site_dir`
  pointing back at the existing `docs-site-dist/` build output that the
  OP-792 publish workflow already consumes.
- `hooks/jira_links.py` — a 40-line MkDocs hook that auto-links bare
  `OP-NNN` references in page markdown to
  `https://soraapp.atlassian.net/browse/OP-NNN`, while suppressing the
  rewrite inside fenced code blocks, inline code spans, and existing
  link targets.
- `docs/index.md`, `docs/lessons/index.md`, and this lesson — the minimal
  set required to validate that frontmatter is parsed, ToC is rendered,
  the index page lists lessons, and the JIRA-link hook fires.

**Verification**

```text
$ time .venv/bin/mkdocs build --clean --quiet
real    0m0.265s
real    0m0.262s
real    0m0.269s
$ .venv/bin/mkdocs build --clean
INFO    -  Cleaning site directory
INFO    -  Building documentation to directory: ../docs-site-dist
INFO    -  Documentation built in 0.15 seconds
```

Cold build at ~0.27s real-time (~0.15s in MkDocs proper). Incremental
`mkdocs serve --dirty` rebuilds at sub-second per saved file. Both
comfortably under the OP-785 30s criterion.

This page itself is the spike's per-file lesson; `docs/lessons/index.md`
is the spike's index page. Together they satisfy the AC: *"1h spike
scaffold demonstrates: build a single per-file lesson + index page"*.

**Generalisation**

When a hand-rolled renderer ships before the framework-decision ADR
lands, the ADR should evaluate the alternatives *as if the hand-rolled
renderer did not exist*. Otherwise the framing collapses to "should we
keep what we have", which biases against changes whose payoff is in
features the hand-rolled renderer doesn't yet have (search, ToC,
syntax highlighting, theming). The ADR-0012 alternative section
deliberately re-evaluates the custom-static option on its long-term
trajectory, not its current 240-LOC footprint.
