---
id: L-OP-777
ticket: OP-777
title: Release notes should be operator-editable Gerrit patchsets
date: 2026-05-08
tags: [release, jira, gerrit, docs]
---

# Release notes should be operator-editable Gerrit patchsets

**Situation**: Sprint D needed release notes from JIRA fixVersion data,
but publishing directly from automation would turn ticket summaries into
external copy without the operator's editorial pass.

**Fix**: `backend.agents.release_notes_generator` writes
`docs/releases/vX.Y.Z.md` and pushes it to Gerrit as a patchset. The
operator can edit the generated draft before any public announcement.

**Verification**: `backend/tests/test_release_notes_generator.py` covers
the synthetic five-ticket milestone, section grouping, per-file lesson
links, event-log cursor consumption, and the systemd polling contract.

**Generalisation**: Generated human-facing release artefacts should be
drafted as reviewable changes. Automation can assemble the source data,
but the publication boundary should stay with an operator-reviewed patch.
