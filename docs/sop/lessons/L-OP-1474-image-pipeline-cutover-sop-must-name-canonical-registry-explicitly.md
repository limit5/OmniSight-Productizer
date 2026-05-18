---
id: L-OP-1474
ticket: OP-1474
title: SOPs must name the canonical registry explicitly, not "the registry"
date: 2026-05-18
tags: [runner, sop, migration, registry]
---

# SOPs must name the canonical registry explicitly, not "the registry"

**Situation**: Phase 5 cut the canonical container registry from
`ghcr.io/omnisight` to `registry.gitlab.com/omnisight`. Phase 6 (this
ticket, OP-1474) discovered that the operator-facing runbooks used a
mix of fully-qualified `ghcr.io/...` URLs and bare `the registry`
language. Stale fully-qualified URLs are easy to grep and update;
bare-noun references are invisible to a `ghcr.io` grep but still steer
the operator toward the wrong host via shell history and aliases. The
Phase-5 cutover would have left a documentation half-life of one
operator generation if Phase 6 had only chased fully-qualified URLs.

**Fix**: Two-pronged refresh. (a) Replace every fully-qualified
`ghcr.io/...` occurrence in
`docs/operations/{prod-deploy,release-cut,release}-runbook.md` with
the new `registry.gitlab.com/...` path. (b) Add an explicit
`OMNISIGHT_IMAGE_REGISTRY` environment-variable check at the top of the
prod-deploy runbook so any host with stale `ghcr.io` config fails loudly
before the orchestrator runs, and record the decision in
[[ADR-0038-image-pipeline-on-gitlab]] so future operators have an
anchor explaining *why* the registry moved.

**Verification**:
* `grep -R 'ghcr\.io' docs/operations/{prod-deploy,release-cut,release}-runbook.md`
  returns no matches.
* `docs/operations/prod-deploy-runbook.md` §1a now lists
  `OMNISIGHT_IMAGE_REGISTRY` with the explicit
  `: "${OMNISIGHT_IMAGE_REGISTRY:?}"` guard.
* `docs/operations/release-cut-runbook.md` §1–§2 cite
  `git@gitlab.com:omnisight/...` and the sora SSH identity rather than
  `github.com`.
* `docs/adr/ADR-0038-image-pipeline-on-gitlab.md` records the cutover
  decision, tradeoffs, and rollback path.

**Generalisation**: When a canonical host / registry / endpoint moves,
the SOP refresh must enumerate the env keys that pin the new host and
add a fail-loud guard at the top of the runbook. A grep over the old
host name is necessary but not sufficient — the SOP must also stop
using bare nouns ("the registry", "the remote") that allow stale
operator muscle-memory to point at the wrong target. Cross-link the
new ADR from every refreshed runbook so the *why* is one click away.
