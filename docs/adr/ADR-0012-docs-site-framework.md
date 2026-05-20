---
id: ADR-0012
title: Docs-site framework — MkDocs (Material)
status: Accepted
date: 2026-05-08
---

# ADR 0012 — Docs-site framework: MkDocs (Material)

**Status**: Accepted (2026-05-08, Sprint E — META OP-784)

**Decider**: sora (operator) + AI fleet

**Related**:
- [ADR-0001 — Five-branch Git Flow](ADR-0001-five-branch-gitflow.md)
- OP-787 — dynamic lessons index (consumer)
- OP-789 — generated tool reference doc (consumer)
- OP-792 — develop-merge publish pipeline (consumer; supersedes its hand-rolled markdown renderer)

---

## Context

The repo carries three growing markdown corpora that need a public, indexed,
searchable site:

| Corpus | Source | Rate of change |
|---|---|---|
| ADRs | `docs/adr/ADR-*.md` | a few per sprint |
| Lessons | `docs/sop/lessons/L-*.md` (per-file YAML frontmatter) | several per week (per OP-737 conventions) |
| Operator docs | `docs/operator/<locale>/**/*.md` | continuous |

OP-792 shipped a 240-line custom Python markdown renderer
(`backend/docs_static_site.py`) that emits `docs-site-dist/` and is published
to GitHub Pages on every `develop` merge. That renderer was a stop-gap to
unblock the publish pipeline. It re-implements a partial subset of CommonMark
(headings, tables, lists, fenced code, inline links/code/strong) and ships
no search, no nav, no theme, no syntax highlighting, no permalinks beyond
`<h*>` anchors. Continuing on that path means we own a markdown engine.

This ADR picks the framework the docs site will live on going forward and
formalises the answer that OP-792 deferred. It does **not** rip out
`docs_static_site.py` today — that migration is a follow-up ticket.

### Decision criteria (from OP-785)

1. Incremental rebuild **< 30s** on operator laptop (`mkdocs serve` dev loop).
2. **Frontmatter / YAML** support — required by `docs/sop/lessons/L-*.md`
   (per OP-787) which carry `id / ticket / title / date / tags / legacy_lesson`.
3. **JIRA cross-link plugin** feasibility — `OP-785` style references in
   prose should auto-link to `https://soraapp.atlassian.net/browse/OP-785`.
   Plugin must be authorable in-tree, by us, in the same language as the
   rest of the toolchain.
4. **Theme customisation** — at minimum: dark theme matching the existing
   operator UI palette, ToC, search, code highlighting, locale switcher
   for the four operator-doc locales (`en / zh-TW / zh-CN / ja`).
5. **Deploy target compatibility** — output is plain `.html` + assets so
   the same artifact can publish to GitHub Pages (current), Caddy on
   `sora.services` (likely), or Netlify (fallback) without rewrites.

## Decision

Adopt **MkDocs 1.6 with the Material for MkDocs theme**. Plugins authored
in-repo as Python packages under `docs-site/plugins/`. Source tree at
`docs-site/`; build output continues to land in `docs-site-dist/`. (The
`actions/upload-pages-artifact` step from OP-792 referenced here is the
GitHub Pages publish path, dormant since 2026-05-07 — the live serving path
is now Cloudflare Tunnel + Caddy per ADR-0022. `docs-site-dist/` remains the
build output regardless of which serving path consumes it.)

Rationale:

- **Same language as the rest of devops/backend.** `backend/docs_site_lessons.py`,
  `backend/docs_site_adr.py`, `backend/docs_site_tool_reference.py` already
  parse our markdown corpora in Python. An MkDocs plugin that pulls those
  modules in is a `import` away — no IPC, no cross-runtime build chain.
- **Plugin authoring is trivial.** Subclass `mkdocs.plugins.BasePlugin`,
  override `on_page_markdown`, return text. The JIRA cross-linker is ~30
  lines (verified by the spike — see §Spike below).
- **Frontmatter is native.** MkDocs reads page-level YAML frontmatter into
  `page.meta`; the lessons corpus' existing `id / ticket / title / date /
  tags` keys flow in untouched.
- **Material theme covers the customisation criterion.** Dark palette, ToC,
  client-side search, syntax highlighting (Pygments), `i18n` plugin for the
  four locales, all out of the box and configured declaratively in
  `mkdocs.yml`.
- **Build speed.** Spike scaffold (3 markdown pages + Material theme +
  JIRA hook) builds cold in **~0.27s real-time** (~0.15s in MkDocs
  proper); see §Build benchmark. Incremental `mkdocs serve --dirty` is
  sub-second per edit. Both well under the 30s criterion, with ample
  headroom for the full ~150 file corpus migration.
- **Deploy compatibility.** `mkdocs build` emits `site/` (we override to
  `docs-site-dist/`) as static HTML + assets — drop-in compatible with the
  existing GitHub Pages workflow, Caddy `file_server`, or Netlify.

## Alternatives considered

### Docusaurus 3 — rejected

- React/MDX-heavy. Adds a Node + bundler + JSX runtime to a Python-leaning
  toolchain, which the runner area-policy walls off (`frontend` is out of
  scope for this ticket and most docs work).
- Plugin authoring is React-flavoured: `@docusaurus/plugin-*` packages with
  TypeScript/JSX entering content lifecycle hooks. To reuse the existing
  `backend/docs_site_*.py` indexers we'd shell out, write JSON, and reparse
  on the JS side — exactly the cross-runtime chain we're avoiding.
- Cold builds on a corpus this size are ~10–30s; well within criterion 1
  but not better than MkDocs.
- Strongest selling point (versioned docs UX) is not a current need; ADRs
  and lessons are versioned by git history + frontmatter `date`.

### Astro 5 — rejected

- Modern, fast, MDX-capable. Genuinely good at content-heavy sites.
- Same cross-runtime cost as Docusaurus: plugins are TS, content collections
  read from disk in the JS process. To preserve our Python indexers we'd
  emit JSON from Python and hand off — losing the round-trip simplicity.
- Astro's killer features (component islands, partial hydration, view
  transitions) target interactive sites; an ADR/lesson archive
  doesn't benefit. Paying the cost without getting the feature.
- Locale handling via `astro-i18n` works but is less mature than the
  MkDocs Material `i18n` plugin we already have config for.

### Custom Python static (status quo from OP-792) — rejected as long-term

- Already in-tree (`backend/docs_static_site.py`, 240 LOC). Zero dep cost,
  immediate ship.
- Loses on every other criterion. No search → operators grep the repo. No
  ToC. No syntax highlighting. The hand-rolled markdown matcher already
  has known edge cases (nested lists, blockquotes, footnotes — none
  supported).
- LOC scales linearly with feature count: every theme tweak, every
  permalink style, every code-block extension is hand-written. MkDocs
  Material gives those for free.
- Plugin equivalence (e.g. JIRA cross-link) costs roughly the same in
  custom vs. MkDocs (`re.sub` against page text either way), so MkDocs's
  plugin API is not a tax — it's a structuring win.
- The OP-792 builder remains in-tree as the publish-pipeline implementation
  until a follow-up ticket migrates the workflow to call `mkdocs build`
  directly. Not deleted by this ADR.

## Spike (1h, OP-785)

Scaffold lives at `docs-site/`:

```
docs-site/
├── mkdocs.yml                      # Material theme + JIRA-link plugin wired in
├── requirements.txt                # mkdocs==1.6.1, mkdocs-material==9.5.x
├── plugins/
│   └── jira_links/
│       ├── __init__.py
│       └── plugin.py               # OP-NNN → soraapp.atlassian.net auto-link
└── docs/
    ├── index.md                    # Landing page
    ├── lessons/
    │   ├── index.md                # Index page (table of all lessons)
    │   └── L-OP-785-mkdocs-spike.md  # Demo per-file lesson with frontmatter
    └── stylesheets/
        └── extra.css               # palette tokens for the dark theme
```

What the spike validates:
- ✓ Frontmatter parses: lesson page exposes `page.meta.ticket`, `page.meta.date`.
- ✓ Per-file lesson page renders with ToC + syntax-highlighted code blocks.
- ✓ Index page lists the lesson via Material's `nav` + a hand-built table
  showing the JIRA-link plugin in action (`OP-785` → live atlassian URL).
- ✓ Cold build time benchmarked — see §Build benchmark below.
- ✓ Output written to `docs-site-dist/` so the OP-792 publish workflow does
  not need to change shape to consume the artifact.

## Build benchmark

Cold `mkdocs build --clean` on the spike scaffold (Python 3.12, no
cache, three timed runs):

```
$ time .venv/bin/mkdocs build --clean --quiet
real    0m0.265s
real    0m0.262s
real    0m0.269s
```

MkDocs's own reported phase: `Documentation built in 0.15 seconds`. The
delta to the ~0.27s real-time number is Python interpreter + plugin import
cost.

Incremental rebuild during `mkdocs serve --dirty` (single-file edit):
sub-second observed. **Both well under the 30s criterion.** As the
corpus grows toward the full ~150 file repo target, MkDocs at this scale
benchmarks under 5s cold, which is the relevant ceiling for the full
migration.

## Consequences

**Locked in by this ADR**

- `docs-site/` is the canonical source tree for docs-site framework config.
- Plugins targeting our markdown corpora are authored in Python, in-repo,
  under `docs-site/plugins/`.
- The OP-792 publish workflow continues to publish `docs-site-dist/` —
  no contract change for the deploy step.

**Deferred to follow-up tickets** (out of OP-785 scope)

- Migrating the publish pipeline from `python -m backend.docs_static_site`
  to `mkdocs build` (one-line workflow change + decommission of the
  hand-rolled renderer).
- Wiring the existing `backend/docs_site_{adr,lessons,tool_reference}.py`
  modules in as MkDocs plugins so the dynamic indexers run inside the
  build instead of pre-generating markdown.
- Operator-doc locale switching via `mkdocs-material[i18n]`.
- Caddy serve config on `sora.services` (currently GitHub Pages only).

**Reversibility**

If MkDocs proves unsuitable, the framework is replaceable: the markdown
corpora (`docs/**/*.md`) are framework-agnostic, the JIRA-link plugin is
30 lines, and `docs-site-dist/` is the only contract the publish workflow
depends on. Switching cost is bounded to `docs-site/` plus one workflow
step.
