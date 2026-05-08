# OmniSight Docs

This site is the public render of the OmniSight repository's living documentation.

The framework decision is captured in [ADR-0012 — Docs-site framework](https://github.com/anthropics/OmniSight/blob/develop/docs/adr/ADR-0012-docs-site-framework.md) (META OP-785). Subsequent migration tickets will route the existing operator docs, ADRs, and tool reference into this site.

## Sections

- [Lessons](lessons/index.md) — engineering lessons learned, one file per lesson, indexed by date and ticket.

## How to run locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/mkdocs serve --dirty
```

The dev server reloads on every save with sub-second incremental rebuilds. To produce the publishable artifact, run:

```sh
.venv/bin/mkdocs build
```

Output lands in `docs-site-dist/` (configured via `site_dir` in `mkdocs.yml`), which the OP-792 GitHub Pages workflow already publishes on every `develop` merge.
