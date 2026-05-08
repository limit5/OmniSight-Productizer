---
title: Docs-site operator workflow
ticket: OP-794
---

# Docs-site operator workflow (OP-794)

This is the operator-facing runbook for the OmniSight docs site
(<https://docs.sora.services/>). It covers the day-to-day loop an editor
runs to add or fix content: preview locally, search across the corpus,
look up history, commit, and verify the live site picks the change up
within the 5-minute CI envelope.

It pairs with the *pipeline* runbook at
[`docs/operations/docs-site-pipeline.md`](../operations/index.md), which
covers the GitHub Pages publish workflow itself (OP-792). When an
operator-side edit looks fine but the live site does not refresh, that
pipeline runbook is the next page to read.

## 1. Audience and scope

Use this runbook when you are:

- Adding or fixing a per-ticket lesson under `docs/sop/lessons/L-*.md`.
- Editing an ADR under `docs/adr/`.
- Editing operator-facing tutorial / reference content under
  `docs/operator/{en,zh-TW,zh-CN,ja}/`.
- Reviewing how a published page got to its current state.

It is **not** the right runbook for:

- Touching the publish pipeline workflow itself
  (`.github/workflows/docs-site-publish.yml`) — see the pipeline runbook.
- Touching the Next.js application's in-app docs routes — those are a
  separate runtime corpus and out of scope for this site.

## 2. Two builders, one output

The repo intentionally ships two markdown-to-HTML builders that both
target `docs-site-dist/`:

| Builder | Trigger | Used for |
|---|---|---|
| `make docs-serve` / `make docs-build` (MkDocs Material) | `make` from a developer checkout | Local preview with live reload, full search, navigation, ToC. |
| `python -m backend.docs_static_site` | CI develop-merge publish job | Production artifact uploaded to GitHub Pages. |

Both read the same markdown sources (lessons, ADRs, operator docs).
The CI builder is the one that actually publishes, so a clean MkDocs
preview is necessary but not sufficient — see §6 for the dry-run that
covers both paths.

## 3. Local preview

```bash
make docs-serve
```

Defaults to <http://127.0.0.1:8765>. The Makefile creates
`docs-site/.venv` on first invocation, installs the pinned
`docs-site/requirements.txt` (mkdocs 1.6.1, mkdocs-material 9.7.6),
and runs `mkdocs serve` with live reload.

To use a different port (the default avoids 8000, which the backend
holds):

```bash
make docs-serve DOCS_PORT=9000
```

Live reload is automatic — saving any markdown file under `docs/` or
`docs-site/docs/` rebuilds in well under a second and refreshes the
browser tab.

## 4. Search across the lessons corpus

```bash
grep -rn 'pattern' docs/sop/lessons/
```

Use this when you need to find every lesson that mentions a concept,
ticket ID, or symptom. Ripgrep is faster if installed:

```bash
rg 'pattern' docs/sop/lessons/
```

Both commands replace the old habit of `Ctrl-F` inside a single
`docs/sop/lessons-learned.md` aggregate (deleted in OP-790 — see §7).
Each lesson is now a self-contained file named
`L-<TICKET>-<slug>.md` with frontmatter for `id`, `ticket`, `title`,
`date`, and `tags`. Greppable in plain text, no aggregation step.

The site's published lessons index (rendered by
`backend/docs_site_lessons.py`) is a *view* of these source files, not
the source of truth — never edit the rendered output.

## 5. Git history per lesson

```bash
git log --follow docs/sop/lessons/L-OP-XXX-*.md
```

`--follow` keeps the history attached across renames (a lesson slug
can change when the title is sharpened). To see the diff for a single
revision:

```bash
git log -p docs/sop/lessons/L-OP-780-anti-pattern-cookbook.md
```

Old revisions of the *aggregate* index are **not** preserved as a
single moving file — `docs/sop/lessons-learned.md` was deleted in
OP-790, so its long history is reachable via:

```bash
git log -- docs/sop/lessons-learned.md            # commits that touched it
git show 72de5c77:docs/sop/lessons-learned.md     # final tracked snapshot
```

For most "what did this lesson say last quarter" questions, the
per-file history above is the right tool.

## 6. End-to-end dry-run procedure

The dry-run answers the question *"if I commit a docs change now, when
will operators see it on the live site?"* The answer is bounded by the
publish pipeline's 5-minute build job timeout (OP-792).

Step by step:

1. **Edit a lesson source.** Open
   `docs/sop/lessons/L-OP-XXX-*.md` in your editor and make the change.
   For a low-risk dry-run, append a single sentinel line (e.g.
   `OP-794-DRYRUN-CANARY-DO-NOT-MERGE`) to a real lesson — the change
   is trivially revertible and easy to grep for in the rendered
   output.

2. **Preview locally with MkDocs.** In one terminal:

    ```bash
    make docs-serve
    ```

    Open the served URL, navigate to the lesson page, and confirm the
    sentinel renders. Live reload picks the change up in <1s.

3. **Cross-check the production builder.** In another terminal, run
    the same builder CI uses:

    ```bash
    python -m backend.docs_static_site --out /tmp/op794-dryrun
    grep -c 'OP-794-DRYRUN-CANARY' \
      /tmp/op794-dryrun/docs/sop/lessons/index.html
    ```

    A non-zero count means the production renderer also surfaced the
    edit. If MkDocs shows the sentinel but `docs_static_site` does
    not, the change touches a file the CI builder does not consume —
    investigate before commit.

4. **Commit and push to Gerrit.** Standard flow per
    `docs/sop/jira-ticket-conventions.md`. The change-id from
    `git push origin HEAD:refs/for/develop` is what later lands on
    `develop`.

5. **Wait for the merge to land on `develop`.** The publish pipeline
    triggers on push to `develop`, so the clock for the 5-minute
    envelope starts at the merge, not at the Gerrit submit.

6. **Verify within 5 min.** The build job has a 5-minute timeout
    (pinned by `test_build_installs_framework_deps_and_builds_static_site`).
    Reload <https://docs.sora.services/> and search for the sentinel.
    If it does not appear:

    - Open the `Docs Site Publish` workflow run for the merge commit
      on GitHub Actions.
    - If the build job failed, the `notify-operator` job emits an
      annotation with the failed run URL — fix the source and re-push.
    - If the build job succeeded but the sentinel is missing, the
      change touched a path outside the production builder's input —
      see step 3.

7. **Revert the canary.** Push a follow-up patch removing the
    sentinel. Never merge a `DO-NOT-MERGE` line.

### Dry-run sign-off — 2026-05-08

The OP-794 ticket author executed steps 1–3 + 7 (the offline portion)
on this branch (`feature/OP-794-runner-fresh`):

| Step | Command | Observed |
|---|---|---|
| 1. Edit | append sentinel to `L-OP-780-anti-pattern-cookbook.md` | — |
| 2. MkDocs build | `make docs-build` | 0.26s mkdocs build, 1.10s wall |
| 3. Static-site build | `python -m backend.docs_static_site` | 0.06s wall, 39 pages |
| 3. Sentinel grep | `grep -c OP-794-DRYRUN-CANARY` lessons index | `1` (surfaced) |
| 7. Revert | restore from backup | clean `git status` for the source file |

Both builders surfaced the sentinel in <2s on a developer laptop. The
remaining envelope (Gerrit submit → develop push → 5-minute CI build →
GitHub Pages deploy) is bounded by the pipeline contract pinned in
`backend/tests/test_docs_site_publish_pipeline.py`. End-to-end
verification on the public site requires a real merge and was not
performed during this dry-run; the contract pin is the standing proof.

## 7. Migration FAQ

### "I used to read `docs/sop/lessons-learned.md`. Where is it now?"

It was deleted from the working tree in OP-790. The per-file lessons
under `docs/sop/lessons/L-*.md` are the source of truth, and the
*rendered* index lives at <https://docs.sora.services/docs/sop/lessons/>
(produced at build time from the per-file frontmatter).

If you have a bookmark or local reference to the aggregate file:

- **For browsing:** open <https://docs.sora.services/docs/sop/lessons/>.
- **For grepping:** run `grep -rn 'pattern' docs/sop/lessons/`.
- **For history of a specific lesson:** see §5.
- **For the final pre-deletion snapshot:**
  `git show 72de5c77:docs/sop/lessons-learned.md`.

The aggregate is not coming back — it was the canonical example of
the *flat-file registry* anti-pattern documented in
`docs/sop/architecture-anti-patterns.md`.

### "I see two builders. Which one is canonical?"

Both, for now. MkDocs (`make docs-serve`) is the local preview and
will eventually become the canonical production renderer (per the
OP-785 ADR queue); `backend.docs_static_site` is what CI publishes
today. Until the swap lands as a separate ticket, keep both happy:
preview with MkDocs, dry-run with `docs_static_site` before commit
(§6 step 3).

### "Where do operator-facing tutorial pages live?"

Under `docs/operator/{en,zh-TW,zh-CN,ja}/`. The CI builder generates
one site section per locale (`/docs/operator/en/`, …) so a translated
edit lands at the same URL pattern with a different locale segment.

## 8. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `make docs-serve` errors on first run with `pip` problems | `docs-site/.venv` left in a broken state by a prior interrupted install | `make docs-clean && make docs-serve` |
| MkDocs warns `Doc file '…' contains a link '…' but the target is not found` | Lesson or ADR cross-link points outside the scaffolded subset | Acceptable until the corpus migration tickets land; do not add `strict: true` to `mkdocs.yml` |
| Live reload stops responding | Watcher hit a path it cannot stat (e.g., a deleted symlink) | Stop and re-run `make docs-serve` — the venv is reused, restart is <1s |
| Sentinel surfaces in MkDocs but not in `docs_static_site` | Edit is in a file the CI builder does not consume (e.g., `docs-site/docs/runbook/`) | Move the edit to `docs/`, or accept that the change is preview-only |
| CI `Docs Site Publish` build job fails | Source-side parse error or builder regression | Read the GitHub Actions log linked from the `notify-operator` annotation; the previous green deploy stays live until the next green build |

## 9. Related documents

- [`docs/operations/docs-site-pipeline.md`](../operations/index.md) —
  CI publish pipeline contract (OP-792).
- [`docs-site/docs/lessons/index.md`](../lessons/index.md) — lessons
  index landing page.
- [`docs/sop/architecture-anti-patterns.md`](../sop/index.md) — Pattern
  3 (flat-file registry) explains *why* the aggregate
  `lessons-learned.md` was retired.
- [`backend/tests/test_docs_site_publish_pipeline.py`](../operations/index.md) —
  the contract tests this runbook relies on (5-minute timeout, deploy
  needs build, etc.).
