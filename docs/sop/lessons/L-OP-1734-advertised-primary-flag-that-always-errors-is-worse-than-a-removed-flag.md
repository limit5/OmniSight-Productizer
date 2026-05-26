---
id: L-OP-1734
ticket: OP-1734
title: An advertised-primary deploy flag whose gate always rejects it is worse than a removed flag
date: 2026-05-26
tags: [deploy, release-train, rt20, docs-tool-drift, footgun, scope]
---

# An advertised-primary deploy flag whose gate always rejects it is worse than a removed flag

**Situation**: RT-20 (ADR-0040 §"Decisions LOCKED") narrowed the
production deploy identity to **image-tag-only** — a cosign-verified
image digest. `scripts/check_deploy_ref.sh` was updated to reject
`--kind tag` unconditionally. But `scripts/deploy-prod.sh` was *not*
reconciled: its usage header, every example, the end-of-run rollback
hint, and the deploy-prod runbook still presented `--tag=vX.Y.Z` as the
**primary** path. So `deploy-prod.sh --tag=…` *always* errored — and
worse, the tag path ran `_detect_gerrit_source` + `git fetch --tags`
*before* reaching the always-failing gate, so an operator (live during
v0.6.2) paid a stale-mirror fetch and then an opaque rejection while
following the documented happy path. A live release tool whose
advertised primary flag always errors is a textbook "breaks at the
slightest touch" trap.

**Fix**: reconcile tool + docs + tests on the *surviving* form (digest)
in one change:

1. **Reject the dead flag FAST, with an actionable pointer.** Keep the
   RT-20 gate as the single rejection authority (do NOT weaken it — that
   was the explicit MUST-NOT), but route the rejection up-front (before
   any side effect) and make the gate's message *name the replacement
   command* (`--backend-digest=sha256:… --frontend-digest=sha256:…`),
   not just "deploy by image digest".
2. **Delete the dead path's infrastructure, not just the flag.** The
   gerrit-source detection (`_detect_gerrit_source` / `--gerrit-source`),
   `git fetch --tags`, and `git checkout` existed *only* to feed the
   now-rejected tag deploy; a digest deploy pulls a pre-built image and
   never touches the git tree. Leaving them is unreachable code plus a
   re-fetch footgun. (Their only doc consumer was the deploy-prod
   runbook — reconcile it in the same change.)

**Verification**:
`python3 -m pytest backend/tests/test_deploy_prod_ref_verifier_drift_guard.py
backend/tests/test_prod_deploy_reject_harness_rt07c.py` green (74),
including new guards: `--tag` errors with both digest flags named, the
usage/`--help`/rollback hint show the digest form, and `git fetch` /
`git checkout` / `_detect_gerrit_source` are gone from the executable
body. Manual: `deploy-prod.sh --tag=v0.6.2` exits non-zero immediately
(no fetch) with the digest-pointer message; a `--backend-digest …
--frontend-digest … --dry-run` walks every step (rc 0).

**Generalisation**: when a release migration narrows the accepted deploy
identities, the OLD primary flag tends to linger in the usage header,
examples, rollback hint, and runbook while the gate already rejects it.
That is *worse* than a removed flag (cf. [[L-OP-1579-removing-a-deploy-flag-has-test-and-runbook-blast-radius]]):
the operator follows the docs straight into a guaranteed error,
sometimes after irreversible-ish side effects (a fetch from a possibly
stale mirror). Before shipping such a gate change, grep the flag across
usage headers / examples / rollback hints / runbooks **and** any
pre-gate side effects, and reconcile them all to the surviving form in
the same change. A gate that rejects a path the tool still advertises is
a documentation lie, not a safety feature.
