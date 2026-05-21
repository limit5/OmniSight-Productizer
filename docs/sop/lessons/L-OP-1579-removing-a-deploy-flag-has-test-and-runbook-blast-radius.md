---
id: L-OP-1579
ticket: OP-1579
title: Removing a prod-deploy flag has drift-guard-test + operator-runbook blast radius
date: 2026-05-22
tags: [deploy, release-train, drift-guard, runbooks, scope, blast-radius]
---

# Removing a prod-deploy flag has drift-guard-test + operator-runbook blast radius

**Situation**: RT-07a tightened `scripts/deploy-prod.sh` +
`scripts/check_deploy_ref.sh` to the single-trunk release-train contract
— a prod deploy identity is now a FINAL tag (`vX.Y.Z`) or an image
digest only. That meant *removing* three long-standing affordances: the
`BRANCH=${OMNISIGHT_DEPLOY_BRANCH:-main}` default, the `--branch` deploy
path, and the `--insecure-skip-verify` escape hatch (plus its
`OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY` env equivalent). The script edit
was the small part; the blast radius was the lesson.

**Fix**: two predictable downstream surfaces break on a flag/escape-hatch
removal and must be handled in the same change (or explicitly handed
off):

1. **Drift-guard tests pin the OLD behavior.** The FX.7.9 guard
   (`backend/tests/test_deploy_prod_ref_verifier_drift_guard.py`)
   asserted the verifier *accepted* allowlisted branches and that the
   deploy script *exposed* `--insecure-skip-verify`. Those assertions
   are correct-for-the-old-world and fail loudly — the guard doing its
   job. Migrate the guard to the new contract (it is a deploy-script
   test = `tests`/`devops` area, even though it physically lives under
   `backend/tests/`); do not weaken it.
2. **Operator runbooks reference the removed flag.** Several runbooks
   still tell an operator to run `deploy-prod.sh --insecure-skip-verify`
   / `--branch` / "deploy with no flags → branch:main". After the change
   those commands hard-error. When the runbooks are wedded to a wider
   not-yet-landed migration (here: `main` retirement / hotfix-as-tag =
   RT-17/RT-18), do NOT rewrite them piecemeal in the narrow ticket —
   that pre-empts the sibling tickets and risks docs that contradict
   un-merged code. Flag them as explicit follow-ups in the ticket
   comment so the coupling is recorded rather than silently rotting or
   prematurely half-fixed.

**Verification**:
`python3 -m pytest backend/tests/test_deploy_prod_ref_verifier_drift_guard.py`
green after migrating the guard (42 cases: branch/rc/partial/unset
rejected, final tag + well-formed digest accepted, removed flags now
error). Manual: `deploy-prod.sh --branch=main` and
`deploy-prod.sh --insecure-skip-verify --tag=v1.2.3` both exit non-zero
with `Unknown argument`.

**Generalisation**: before deleting a deploy CLI flag/env, `grep` it
across `backend/tests`, `tests/`, `docs/**runbook**`, and sibling
`scripts/*` (e.g. `scripts/hotfix_cut.sh` defaulted its deploy command
to `deploy-prod.sh --branch …`). Migrate the tests in-ticket; record
doc/sibling-script couplings as named follow-ups when they belong to
other migration tickets. A removed escape hatch left referenced in a
runbook is an operator footgun at 3am.
