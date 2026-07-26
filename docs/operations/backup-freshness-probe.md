# Off-site backup freshness probe

**Ticket:** OP-2731 (P4) · **Scope:** `scope:failed-units-2026-07-25`

## Why a probe, when the lanes already have `OnFailure=`

`OnFailure=` covers a lane that fails *loudly*. It does not cover the case that
nearly went unnoticed: **a lane that keeps succeeding locally while nothing
reaches S3.**

It also cannot express duration. The alert channel is idempotent per unit and
throttled hourly *by design* (OP-2728), so a persistent condition produces one
issue and then near-silence — "day 9 of no off-site copy" looks identical to
"day 1". During 2026-07-22..26 the encrypted lane was blocked four consecutive
nights; a per-run failure signal never says *how long*.

So this probe asserts the property actually worth having — **a recent, complete,
restorable set exists off-host** — rather than the verdict of any single run.

## Cohort, not object

An object being present proves nothing. A payload whose digest never uploaded is
not restorable; a digest alone is not a backup. Freshness is therefore evaluated
over a **cohort**: every member required by the current stage, sharing one
timestamp. The newest *complete* cohort is what must be recent. Incomplete
cohorts are reported and explicitly **not** counted as fresh.

## Stage is a validated enum, never free-form configuration

The first draft made the cohort requirements configurable, to fix a different
problem — and configurability immediately reintroduced the way to make the gate
vacuous, since a digest-only member set would let an incomplete backup pass.

Configuration selects a **stage** and nothing else. Each stage's minimum member
set is hardcoded and cannot be shrunk:

| stage | required members | payload |
|---|---|---|
| `s1_payload_digest` | payload, digest | `*.dump.gz` |
| `s2_encrypted` | payload, digest | `*.dump.gz` **or** `*.dump.gz.gpg` |
| `s3_attested` | payload, digest, **attestation** | as s2 |

Stages exist because the capabilities land in sequence (P4 → F1 → P1). **Each
stage is tightened in the same commit as the capability it depends on**, so the
probe never demands an artefact nothing yet produces — which is how a check ends
up written to swallow its own condition. `s2` accepts both payload names so the
probe does not go red on the F1 changeover night.

Validation runs **on every execution**, not only at deploy, so a unit file edited
afterwards cannot silently weaken a live check. A probe that cannot validate its
own configuration **exits non-zero and alerts — it never reports "fresh"**.

## Every failure mode was induced, not assumed

Verified 2026-07-27, exit codes captured directly (not through a pipeline — an
earlier run of these same checks reported `rc=0` for everything because `$?` was
reading `tail`, which is exactly the sort of verification that proves nothing):

| induced condition | result |
|---|---|
| stage unset | `rc=1`, refuses to guess |
| stage `whatever` / `s1` / `S1_PAYLOAD_DIGEST` / `""` | `rc=1` |
| impossible max-age (0.0001d) | `rc=1`, names the staleness |
| prefix with no objects | `rc=1` — an empty prefix is a failure, not a fresh backup |
| stage `s3_attested` before P1 exists | `rc=1` — "71 cohorts found but NONE complete" |
| real run at `s1` | `rc=0`, newest complete cohort 0.2d old |

The stage-enum contract is pinned by
`backend/tests/test_backup_freshness_stage_enum.py` (15 tests), and the
belt-and-braces runtime check was **mutation-tested**: removing it turns exactly
one test red, and restoring it returns all 15 to green.

## Schedule

06:10 local, `RandomizedDelaySec=300`. Deliberately clear of every lane — A at
02:17, the DR drill at 04:30, B and C at 11:00 — so a red probe never merely
means "a lane is mid-run".

## The lanes' own alerting

`omnisight-pgdump-local-daily` and `omnisight-pgdump-s3-daily` had **no
`OnFailure=` at all** until this change: during the four-night outage they were
the only reason any backup existed, while being the only two that could not
report their own failure. Both now carry the same drop-in as
`omnisight-prod-backup`.

That wiring was **proven by a deliberately induced failure**, not by reading the
config: a temporary drop-in replaced `ExecStart` with `/bin/false`, the unit was
started, the channel raised OP-2761, and the drop-in was removed with `ExecStart`
and `OnFailure` both verified afterwards. OP-2761 was closed as the test artefact
it is — leaving it open would make a future *real* failure comment there instead
of raising a fresh alert.

## Still to come in OP-2731

`s2_encrypted` activates with F1 (encrypt lane C before upload); `s3_attested`
with P1 (the DLP gate on upload eligibility). Neither stage may be selected
before its capability exists — the probe will correctly refuse.
