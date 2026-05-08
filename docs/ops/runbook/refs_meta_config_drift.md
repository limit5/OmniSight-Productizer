# `refs_meta_config_drift`

| field | value |
|-------|-------|
| Severity | `DEGRADED` |
| Source | T5 drift scanner — `scripts/drift_scanner.py` |
| Tier owner | gerrit-admin |
| Parent META | OP-721 |

## What triggers it

The drift scanner fetches `refs/meta/config` from the deployed
Gerrit project and compares the relevant blobs (project.config,
groups, submit-rule references) against the committed sample under
`docs/sop/` / the Gerrit fixture in this repo. If they diverge,
the scanner emits `code="refs_meta_config_drift"` with a
per-section diff in the alert context.

Sections that matter:

* `submit-requirements.*` — the declarative submit-rule
  configuration. Drift here can silently bypass the dual-2 review
  gate (`reference_gerrit_submit_requirements.md`).
* `access.*` — group permissions on `refs/heads/*` / `refs/for/*`.
  Drift can grant push to wrong groups.
* `label.*` — Code-Review / Verified label scoring. Drift can
  silently change merge eligibility.

## Severity rationale

`DEGRADED` because the drift may not have caused a real bypass yet
— the scanner is a **leading indicator**. But the security model of
the program assumes the live config matches the audited fixture, so
drift is **never** "fine to leave."

The reason it isn't `CRITICAL`:

* gerrit-admin needs time to reconstruct the diff — paging at 3am
  is operationally counterproductive when the violation is a
  policy mismatch, not an active exploitation;
* if the drift is being actively exploited, you'll see a spike in
  `audit_write_failed` or merge-velocity anomalies — those are the
  paging signals.

## Immediate action

1. **Pull the live diff** and read it carefully:

       git fetch ssh://claude-bot@sora.services:29418/All-Projects refs/meta/config:refs/remotes/origin/meta-config
       git diff refs/remotes/origin/meta-config -- project.config groups

   Cross-reference with the committed fixture in
   `docs/ops/gerrit_dual_two_rule.md` and
   `reference_gerrit_submit_requirements.md` (memory).

2. **Determine intent:**

   * **Drift is intentional** (gerrit-admin made a deliberate
     change): commit the new fixture into the repo and re-run the
     scanner. This is the standard workflow.
   * **Drift is accidental** (someone edited via Gerrit UI / bypass
     pushed to refs/meta/config): revert by pushing the committed
     fixture back to refs/meta/config.

   Use:

       git push ssh://claude-bot@sora.services:29418/All-Projects \
           HEAD:refs/meta/config

   to restore. **Always** read the diff before push — refs/meta/config
   is unrecoverable if you push a regression.

3. **Re-run the scanner**:

       python scripts/drift_scanner.py --json | jq -e 'all(.[]; .code != "refs_meta_config_drift")'

4. If drift is in the **submit-requirements** section, also re-run
   the dual-2 gate test
   (`reference_gerrit_submit_requirements.md` lists the smoke test
   path). Don't trust the scanner's clean exit alone for this
   subsection.

## Root-cause investigation

| Diff section | Likely cause | Reference |
|--------------|--------------|-----------|
| `submit-requirements.*` | Manual UI tweak / mistaken push | `docs/ops/gerrit_dual_two_rule.md` |
| `access.*` | Group membership change without ADR | `reference_gerrit_self_hosted.md` |
| `label.*` | Label-config rebalance experiment left in place | gerrit-admin shell history |
| `commentlinks.*` | Cosmetic-only — usually safe to commit forward | trivial |

## Escalation

* Drift in `submit-requirements.*` or `access.*` to a higher-trust
  group: escalate to gerrit-admin **immediately** even if scanner
  says `DEGRADED`. Treat as `CRITICAL` until intent is confirmed.
* Drift in cosmetic sections (`commentlinks`, `description`):
  low-priority follow-up; commit forward and move on.
* Drift recurring more than once a week: the deploy / change-
  management process for refs/meta/config is broken. Open an ADR
  to require all such changes go through Gerrit Code Review.
