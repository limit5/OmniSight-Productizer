---
id: L-OP-740
ticket: OP-740
title: Parallel gates should ship default-off before enforcement
date: 2026-05-08
tags: [ci, gerrit, git, runner]
legacy_lesson: 28
---

# Parallel gates should ship default-off before enforcement

**Situation**: OP-740 needed to introduce a Gerrit `Verified` label and
ci-bot voting permission before real CI existed. Enforcing
`label:Verified=+1` immediately would have made merge availability
depend on a brand-new bot that could only vote unconditionally until C2.

**Fix**: Land the label, ACL, account-provisioning script, sticky
copyCondition, and submit-requirement block first, but set the new
requirement `applicableIf = is:false`. C2 can flip that single
project.config flag when real CI is ready; C3 provides the recovery
path before enforcement.

**Verification**: `backend/tests/test_gerrit_verified_gate.py` pins
the default-off requirement, ci-bot Verified-only ACL, sticky trivial
rebase behavior, and runbook migration notes.

**Generalisation**: For any new parallel gate, separate "surface exists"
from "surface blocks submit." Ship the label/status/permission surface
first, prove operators can read and recover it, then flip enforcement
in a later change with live signal and rollback mechanics in place.
