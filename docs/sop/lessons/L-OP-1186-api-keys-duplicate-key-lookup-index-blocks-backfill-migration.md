---
id: L-OP-1186
ticket: OP-1186
title: api_keys legacy/canonical duplicate rows make 0203a_kse backfill fail mid-deploy — backfill must dedupe before unique-index promotion
date: 2026-05-17
tags: [alembic, migration, api_keys, backfill, data-fixup, deploy, pre-flight]
---

# api_keys legacy/canonical duplicate rows make 0203a_kse backfill fail mid-deploy — backfill must dedupe before unique-index promotion

**Situation**: The 2026-05-16 develop-direct prod deploy attempt was driven
to its second failure mode after the alembic graph hot-patch (see
[[L-OP-1176]]) cleared. Migration `0203a_kse_envelope_credential_backfill`
runs `_backfill_api_keys()`, which sets `api_keys.key_lookup_index =
sha256(legacy_secret)[:12]` on rows lacking a lookup index, and then the
follow-up migration promotes `idx_api_keys_lookup` to UNIQUE. The
prod table contained TWO rows that hashed to the same lookup index —
the canonical `ak-legacy-31be6f6c8763` form (Task-#106 era) AND an
older UUID-derived legacy stub still carrying the same `legacy_secret`
plaintext. The backfill UPDATE on the second row raised:

```
psycopg2.errors.UniqueViolation:
  duplicate key value violates unique constraint "idx_api_keys_lookup"
  Key (key_lookup_index)=(31be6f6c8763e7d02a2428015fdb00b6c7f559146f5645492829a667a319e26c)
    already exists.
```

Alembic aborted the entire migration chain at that point. Prod was
left with the alembic graph hot-patch applied (so the `0203` KeyError
from L-OP-1176 was resolved) but stuck at the same `alembic_version`
because the backfill rolled back. The deploy attempt was unblocked
only by manual data fixup of the duplicate rows.

This was the **second** failure mode the deploy hit in the same
~3-hour window — and the second failure mode would have surfaced even
if the L-OP-1176 alembic graph KeyError had never existed, because
the prod data condition predated the merge-node bug. The two issues
just happened to chain in the same deploy attempt; they share no
mechanism.

**Fix** (data-side, applied 2026-05-16 / 2026-05-17):

1. Audit the duplicate cluster (OP-1177 in flight): identify rows that
   hash to the same `sha256(legacy_secret)[:12]` and classify them as
   (a) genuine duplicates of the same logical key (delete the older
   stub, keep the canonical `ak-legacy-<sha256>[:12]>` row), (b) two
   actually-different keys that collided by coincidence (impossible in
   practice for a 32-hex-char hash, but the audit confirms), or (c)
   one already migrated and another mid-process (resume the
   half-finished migration manually).

2. Patch `0203a_kse_envelope_credential_backfill` to be safe against
   pre-existing duplicates (OP-1178): before each UPDATE, query
   whether any *other* `api_keys` row already carries the would-be
   `key_lookup_index`; if yes, either DELETE the older stub row
   in-migration (legacy-stub case) or RAISE with a clear operator
   message that this is a logical-key-conflict case requiring manual
   triage. The migration must not crash with a raw `UniqueViolation`
   that leaks PG-level constraint names; the operator should see
   "row <id> would collide with existing row <other_id> on
   key_lookup_index; legacy_secret already migrated under <other_id>;
   review and delete the duplicate before re-running."

3. Re-run alembic upgrade `0202 → heads` after the data fix
   (OP-1179). Verified prod alembic_version advanced to
   `m_2026_05_16_3head`.

**Verification**:

- Backup `prod-omnisight-2026-05-16-pre-develop-deploy.sql` (~2 MB) taken
  *before* the deploy attempt; this was the safety net that let the
  manual data fixup proceed without fear of unrecoverable loss.
- Post-fix verification: `SELECT COUNT(*) FROM api_keys GROUP BY
  key_lookup_index HAVING COUNT(*) > 1` returns 0 rows; index
  `idx_api_keys_lookup` is UNIQUE; `/readyz` reports
  `migrations.ok = true`.
- Both prod backend replicas now green on
  `image=ghcr.io/your-org/omnisight-backend:main-3967b980` with
  `alembic_head_in_image = m_2026_05_16_3head` (see OP-1184 deploy
  comment).
- OP-1188 is the follow-up to make `deploy-prod.sh --sql` dry-run
  mode actually exercise the backfill (the current `--sql` mode
  breaks on `bind.exec_driver_sql` usage, so this collision was
  invisible in the dry-run output the operator inspected pre-deploy).

**Generalisation**:

- **Data-touching alembic migrations must dedupe defensively, not
  optimistically**. Any backfill that ends with "promote index to
  UNIQUE" must run the dedupe and the index promotion in the same
  migration *and* must check for would-be collisions *before* each
  UPDATE — not after. Assuming the source table is already
  well-formed makes the migration a probe that surfaces the data
  condition only at deploy time, when the cost of fixing it is
  highest (PG transaction rolled back, operator on a deploy clock,
  half-applied migration chain to reconcile).

- **`alembic upgrade --sql` is not a pre-flight for data-side
  migrations**. The `--sql` mode emits the DDL/DML alembic *would*
  run, but it does not execute against real data, so duplicate-row
  collisions never surface. The closest real pre-flight is "restore
  the prod backup into a throwaway PG, run `alembic upgrade heads`
  against it, observe what fails." Make this a documented step in
  `docs/sop/deploy-prod-runbook.md` for any deploy that crosses a
  migration boundary touching data backfills (not just schema).
  (OP-1188 tracks the runbook + script ergonomics.)

- **Legacy-stub rows from in-flight key-format migrations are
  forever**. The Task-#106-era canonicalisation of `api_keys.id` to
  `ak-legacy-<sha256(legacy_secret)[:12]>` left UUID-derived stubs
  in place for backwards-compat; nine months later, the
  `0203a_kse` backfill discovered them by tripping over them.
  When any future migration writes a deterministic-hash column,
  audit the source columns for pre-existing duplicates *during*
  the migration's authoring, not at apply time.

- **Two unrelated failure modes can chain inside one deploy window**.
  L-OP-1176 (alembic graph KeyError) and this lesson are independent
  in mechanism but were observed in the same ~3-hour incident
  because the operator attempted a single develop-direct deploy
  across many months of accumulated graph + data drift. The
  lesson for retrospective scope: when a deploy attempt fails,
  treat each failure mode as its own root cause and write its own
  lesson; do not collapse them into "the 2026-05-16 deploy was
  bad" because the mitigations are different (graph hygiene vs.
  backfill defensiveness).

- **Manual deploy windows are also retrospective windows**. Today's
  session filed OP-1176/1180/1183/1186 + 9 children DURING the
  recovery. Filing the META + AC blocks while the failure was
  fresh meant this lesson and [[L-OP-1176]] could both be written
  with the actual error strings and the exact SHA values
  in-hand, not reconstructed from logs a week later. Feedback
  memory [[feedback_4_ac_discipline]] continues to apply: even
  under deploy-clock time pressure, write the AC blocks tersely
  but DO write them.
