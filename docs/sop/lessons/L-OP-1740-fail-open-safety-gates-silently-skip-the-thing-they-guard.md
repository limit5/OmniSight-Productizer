---
id: L-OP-1740
ticket: OP-1740
title: A warn-and-proceed safety gate is fail-OPEN — it silently skips the thing it guards
date: 2026-05-26
tags: [deploy, backup, fail-closed, safety-gate, devops, runbooks]
---

# A warn-and-proceed safety gate is fail-OPEN — it silently skips the thing it guards

**Situation**: `scripts/deploy-prod.sh` Step 1b is the "pg_dump first"
pre-deploy backup that protects a rolling deploy from rolling data
integrity *backwards* (a bad migration / panicking code change). The
backup call itself was correctly fail-closed when the helper was
present (`scripts/backup_prod_db.sh … || err`). But the *guard around
it* was fail-OPEN: when the helper was missing/non-executable the branch
did `warn "proceeding WITHOUT backup"` and continued the deploy. So the
single most consequential precondition — "we have a restore point" —
could be skipped by nothing more than a stripped deploy bundle or a lost
+x bit, and the operator would only see a yellow warning scroll past.
Separately, the backup's mandatory passphrase
(`OMNISIGHT_BACKUP_PASSPHRASE`) lived in `/etc/omnisight/backup-dr.env`
(where the backup timers source it), not `.env`, and the deploy script
never sourced it — so an interactive operator deploy hit "passphrase
required" and had to hunt (live during v0.6.2).

**Fix**: flip the missing/non-exec branch from `warn`+proceed to
`err`+abort (fail-closed), and add a single explicit, loudly-logged
`--skip-backup` override for the rare provably-redundant case. Source
`/etc/omnisight/backup-dr.env` (path overridable via
`OMNISIGHT_BACKUP_DR_ENV` for tests) before the backup step so the
passphrase is present without hunting — `set -a; . "$file"; set +a` so
it exports to the child helper — and never print the value.

**Verification**:
`python3 -m pytest backend/tests/test_deploy_prod_predeploy_backup_op1740.py`
(7 cases). The fail-closed branch is exercised by running the real
deploy script from a sandbox repo with no/`644` backup helper and
asserting it aborts with "missing or non-executable" + "fail-closed";
the env-source path uses a fake helper that records (never prints)
whether the passphrase arrived, with a negative-control test proving it
is the *sourced file* — not ambient env — that delivers it.

**Generalisation**: audit every safety precondition for its *absent-tool*
behavior, not just its *failing-tool* behavior. "Tool ran and failed →
abort" is the easy half; "tool isn't there → ???" is where gates quietly
rot to fail-OPEN. A `warn`+continue on a missing guard is a no-op gate.
Make the default fail-closed and give the bypass exactly one explicit,
audited flag. And when a script depends on a secret/config that lives in
a sidecar env file the *rest* of the system already sources (systemd
timers, here), source it in the script too — don't make the human
rediscover the path at deploy time.
