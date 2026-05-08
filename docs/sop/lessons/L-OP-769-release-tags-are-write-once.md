---
id: L-OP-769
ticket: OP-769
title: Release tags are write-once automation outputs
date: 2026-05-08
tags: [release, git, gerrit, gitlab]
---

# Release tags are write-once automation outputs

**Situation**: Sprint D needed automatic tagging after staging gates pass,
but release tags become downstream deployment evidence. Reusing a tag name
for a different commit would make D9 approval and later rollback evidence
ambiguous.

**Fix**: `backend.agents.auto_tag_release` creates annotated SemVer tags
only when absent. If `vX.Y.Z` already exists at another commit, the run
fails instead of moving it. The daemon then pushes the tag and matching
`release/vX.Y` branch to Gerrit and GitLab before emitting
`release_tagged`.

**Verification**: `backend/tests/test_auto_tag_release.py` covers the full
synthetic `staging_passed` flow, immutable tag rejection, ignored unrelated
events, service wiring, and docs references.

**Generalisation**: Release evidence should be append-only. Automation may
create a missing release ref, but must never reinterpret an existing one.
