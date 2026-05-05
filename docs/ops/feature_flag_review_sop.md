# WP.7.6 - Quarterly Feature Flag Review SOP

> Status: active SOP
> Scope: quarterly `feature_flags` registry review, N10 ledger evidence,
> and long-untouched flag alert handling.
> Ledger: [`upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md)

This SOP keeps feature flags from becoming permanent hidden product
state. The registry table is the source of truth for flag name, tier,
state, owner, creation time, and `expires_at`. Runtime mutation history
is read from `audit_log` rows written by WP.7.1's audit contract:
`entity_kind="feature_flag"` and `entity_id=<flag_name>`.

The SOP is operational only. It does not deploy runtime code, add a
toggle path, or change feature flag resolution.

## 1. Quarterly cadence

Run this cadence during the first working week of every quarter:

1. **Snapshot** - export the current `feature_flags` rows, sorted by
   `flag_name`, and compute a SHA-256 fingerprint for the snapshot.
2. **Mutations** - query `audit_log` for the latest
   `entity_kind="feature_flag"` mutation per flag. If no mutation row
   exists, use `feature_flags.created_at` as the last mutation time.
3. **Owner review** - send every flag owner their active flags, tier,
   state, `expires_at`, and last mutation age.
4. **Disposition** - record one review disposition per flag:
   `keep`, `graduate`, `disable`, `delete`, `extend-expiry`,
   `owner-missing`, or `risk-accepted`.
5. **Alerts** - create long-untouched flag alert rows for flags that
   meet the thresholds in Section 3.
6. **Ledger** - append one N10 row under
   `## Feature Flag Quarterly Reviews` with the snapshot and review
   fingerprints.

The flag owner is accountable for the review disposition. The platform
owner is accountable for the N10 ledger row and alert routing.

## 2. Review inputs

Minimum SQL shape for the review export:

```sql
SELECT
  flag_name,
  tier,
  state,
  expires_at,
  owner,
  created_at
FROM feature_flags
ORDER BY flag_name;
```

Minimum mutation lookup:

```sql
SELECT
  entity_id AS flag_name,
  max(created_at) AS last_mutation_at
FROM audit_log
WHERE entity_kind = 'feature_flag'
GROUP BY entity_id;
```

If a production schema names the audit timestamp differently, use the
canonical audit event timestamp column for that deployment. Do not infer
activity from application logs; the review must be reproducible from
the registry snapshot plus the audit ledger.

## 3. Long-untouched flag alert

A long-untouched flag alert fires when a flag has no audit_log mutation
after its creation fallback for the configured age threshold.

| Threshold | Required action |
|---|---|
| 90 calendar days | Owner acknowledgement required; append a N10 `Stale Feature Flag Alerts` row if the flag remains active |
| 180 calendar days | Platform owner escalation required; flag must be deleted, disabled, graduated to release behavior, or explicitly risk-accepted |

Alert payloads must include flag name, tier, state, owner, `expires_at`,
last mutation timestamp, age in days, and requested disposition. Alert
payloads must not include customer data, secrets, raw audit payloads, or
runtime user preference values.

The first implementation may route this as a scheduled operator check.
When WP.7.8 adds the operator UI, the same thresholds should surface as
read-only warning state for non-admin roles and as an admin action cue.

## 4. N10 ledger rows

Append one row to `## Feature Flag Quarterly Reviews` after each
quarterly review:

```markdown
| 2026-Q2 | 2026-07-01T09:00:00Z | <registry-snapshot-sha256> | <review-sha256> | 14 | 2 | open | BP owners acknowledged; HD cleanup due 2026-07-15 |
```

Append one row to `## Stale Feature Flag Alerts` for each stale flag
that remains active at review close:

```markdown
| 2026-07-01T09:10:00Z | wp.preview.checkout | preview | payments | 2026-03-20T12:00:00Z | 103 | <alert-sha256> | owner-ack-pending | expires_at 2026-08-01 |
```

`review-sha256` fingerprints the stored review packet: registry export,
owner acknowledgements, disposition list, and cleanup tracker. The full
packet stays in the private operational evidence vault. `alert-sha256`
fingerprints the alert packet for one flag. Git stores only summary
metadata.

Do not edit previous rows. If a row is wrong, add a correction row with
`correction -> <quarter/review-sha256>` or
`correction -> <flag-name/alert-sha256>` in Notes.

## 5. Disposition vocabulary

Use these strings in review packets and N10 notes:

| Disposition | Meaning |
|---|---|
| `keep` | Flag remains active and has current owner acknowledgement |
| `graduate` | Flag behavior should become default release behavior |
| `disable` | Flag should be switched off but retained for rollback window |
| `delete` | Flag row and dead code path should be removed |
| `extend-expiry` | `expires_at` needs a new bounded date and rationale |
| `owner-missing` | No accountable owner responded by review close |
| `risk-accepted` | Platform owner accepted continued staleness with written rationale |

`risk-accepted` requires a dated rationale in the private evidence vault
and a follow-up due date. It is not a permanent state.

## 6. Evidence checklist

- [ ] Registry snapshot exported and fingerprinted
- [ ] Latest feature flag mutation age computed from `audit_log`
- [ ] Every flag owner received the review packet
- [ ] Every active flag has a review disposition
- [ ] 90 calendar days and 180 calendar days thresholds evaluated
- [ ] Long-untouched flag alert rows appended for active stale flags
- [ ] N10 `Feature Flag Quarterly Reviews` row appended
- [ ] Cleanup tracker linked in the N10 Notes column

## 7. Production status

This SOP does not deploy runtime code. Production readiness is
operational: the first quarter is complete only when the registry
snapshot, owner dispositions, stale flag alerts, and N10 ledger rows
exist.
