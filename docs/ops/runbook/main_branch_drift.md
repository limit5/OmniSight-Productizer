# `main_branch_drift`

| field | value |
|-------|-------|
| Severity | `DEGRADED` |
| Source | T5 drift scanner — `scripts/drift_scanner.py` |
| Tier owner | release-eng |
| Parent META | OP-721 |

## What triggers it

The drift scanner compares the local checkout's `main` (or the
configured "trunk" branch) against `origin/main` on the canonical
remote. If the local trunk has commits that origin doesn't, **or**
vice versa, it emits `code="main_branch_drift"`.

This catches three distinct misuses:

1. **Local work pushed to the wrong remote** — the local `main`
   has commits that never reached origin. Eventually a fetch will
   surface this, but the scanner makes it visible same-day.
2. **Local trunk stale** — origin has merged work the deploy host
   hasn't pulled. Long-running deploy hosts diverge silently.
3. **History rewrite** — the SHAs differ even after fast-forward
   merge. Someone force-pushed origin/main (which is forbidden by
   CLAUDE.md L1 § Safety Rules) **or** the local checkout has
   force-rebased its trunk. Either is a process violation.

## Severity rationale

`DEGRADED` because the drift itself doesn't break anything — the
production code path isn't using `git pull` to make decisions. But
the drift is *the* signal that the release-eng workflow is being
bypassed, and the longer it sits, the harder it gets to untangle.

The case that should escalate to `CRITICAL` is the third one
(history rewrite). The scanner does **not** auto-promote — read
the alert context's `divergence_kind` field:

* `local_ahead` / `remote_ahead`: real `DEGRADED`.
* `divergent` (different SHAs at the same commit count): treat as
  `CRITICAL`; force-push to main is forbidden.

## Immediate action

1. **Read the divergence kind** from the alert context. From the
   deploy host:

       cd /path/to/working/copy
       git fetch origin
       git rev-list --left-right --count main...origin/main

   Output `A B` means: A local-only, B remote-only commits. Both
   non-zero → `divergent`.

2. **`local_ahead` (A>0, B=0):**

       git log origin/main..main --oneline

   If those commits look intentional and unpushed: push them
   through the normal review path (Gerrit), then resolve drift.
   If unintentional: `git reset --hard origin/main` after backing
   the local commits up
   (**confirm with the user before destructive ops** — see
   CLAUDE.md "Executing actions with care").

3. **`remote_ahead` (A=0, B>0):**

       git pull --ff-only origin main

   This is the simple case: the deploy host is stale.

4. **`divergent` (A>0, B>0):**

   **Stop.** Do not force-push. Treat as `CRITICAL`:

   * Capture both side's SHAs in a JIRA ticket against
     release-eng.
   * If origin/main was rewritten: this violates CLAUDE.md's
     "NEVER force-push to main" rule. Open a postmortem
     immediately.
   * If local main was rewritten: the deploy host needs to be
     rebuilt from the canonical remote.

## Root-cause investigation

* Recent activity in `git reflog` on the deploy host.
* `docs/ops/upgrade_rollback_ledger.md` — was a rollback run
  locally that was never propagated upstream?
* `docs/sop/jira-ticket-conventions.md` §11 — workflow violations
  that lead here are tracked.
* `docs/ops/multi-wsl-deployment.md` — multi-host deploys
  occasionally drift if one host is offline during a push.

## Escalation

* `local_ahead` / `remote_ahead` resolved by simple pull/push:
  close after the next clean scanner run.
* Recurring (≥2 times/month): the deploy host is stale; consider
  adding a periodic auto-fetch under release-eng's ownership.
* `divergent` (any history rewrite): always escalate. Postmortem
  required even if scope is "harmless."
