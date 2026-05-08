# `audit_write_failed`

| field | value |
|-------|-------|
| Severity | `CRITICAL` (per-code override in `configs/error_pager.yaml`) |
| Source | T3 forwarder — `scripts/journal_error_forwarder.py` (record originates in `gerrit-jira-bridge` audit logger) |
| Tier owner | bridge-maintainer + compliance |
| Parent META | OP-721 |

## What triggers it

The bridge's audit logger emits `event="audit_write_failed"` at log
level `ERROR` when an audit-row INSERT fails for any reason **other
than** the boot-time pool race (which is
[`audit_pool_not_initialised`](audit_pool_not_initialised.md)).

The T3 forwarder applies a per-code severity override
(`severity_map.audit_write_failed: CRITICAL`), promoting the
default `DEGRADED` from priority-3 logs to `CRITICAL`. This routes
the alert to the full channel matrix (JIRA + email + Slack + LINE)
and starts the `OMNISIGHT_NOTIFIER_CRITICAL_REPAGE_SECONDS` re-page
loop.

## Severity rationale

Loss of an audit-trail row is **not** "just a bug" — it's a
compliance issue. The audit log is referenced by:

* `docs/ops/soc2_type2_readiness_checklist.md` (control AC-2.7)
* `docs/ops/gdpr_dsar_alignment_sop.md` (DSAR audit trail)
* `docs/sop/jira-ticket-conventions.md` §17 (review-trail
  reconstruction)

Any gap in the trail can invalidate downstream evidence chains, so
this gets `CRITICAL` regardless of how transient the underlying
write failure looks.

The promotion is **explicit** in T3 config — if you remove the
override, the alert silently downgrades to `DEGRADED` and stops
paging Slack/LINE. Don't.

## Immediate action

1. **Capture which audit row failed.** The alert payload's `context`
   carries the `audit_event` field and (when available) the
   `audit_row_pk`. Note both before doing anything else — the row
   may need manual reconstruction.

2. **Check the database health** before assuming a bug:

       psql -h $PGHOST -U $PGUSER -d $PGDB \
            -c "SELECT count(*) FROM audit_log WHERE ts > now() - interval '5 min';"

   * Empty / very low? Audit pipeline is broken end-to-end.
   * Normal volume? The failure is per-row (e.g. constraint
     violation or schema drift).

3. **Cross-check schema drift.** A `relation "audit_log" does not
   exist` or column mismatch is most often a missed alembic
   migration:

       alembic -c backend/alembic.ini current
       alembic -c backend/alembic.ini heads

   If they differ, you're looking at the same underlying cause as
   [`schema_drift`](schema_drift.md) — follow that page first.

4. **Stop write traffic** if the audit table itself is corrupt:
   put the bridge into read-only mode (set
   `OMNISIGHT_BRIDGE_READONLY=1`, restart unit). Open a JIRA ticket
   tagged `compliance` and notify on-call lead.

## Root-cause investigation

| Fail pattern in the original log | Likely cause | Fix |
|-----------------------------------|--------------|-----|
| `UniqueViolationError` on `audit_log_pkey` | Duplicate-key bug in caller; the row is partially written | Reconstruct from the application log; rewrite the audit row with a fresh PK |
| `UndefinedColumnError` | Schema drift | Run pending alembic migrations |
| `OperationalError: connection refused` | Postgres down | `docs/ops/db_failover.md` |
| `asyncpg.exceptions.InterfaceError: pool is closed` | Bridge re-init bug | Bounce the bridge unit |

## Escalation

* Single occurrence: file under bridge-maintainer; capture the
  failed payload and reconstruct the row within 24h.
* Recurring (≥2 in 1h) or any pattern that loses ≥10 rows: escalate
  to the **compliance lead** immediately. Trigger the
  audit-reconstruction SOP at
  `docs/ops/gdpr_dsar_alignment_sop.md` §audit-gap.
* Postmortem mandatory; link from a META ticket labelled
  `meta:op-721` AND `compliance:audit-gap`.
