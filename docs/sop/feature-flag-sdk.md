# Feature flag SDK (OP-773)

**Status**: shipped 2026-05-08, OP-773 (META, Sprint D / D12)
**Owner**: platform / devops launch operator
**Module**: `backend/feature_flag_sdk.py`
**Config**: `config/feature_flag_rollouts.yaml`

## Purpose

Replace scattered `OMNISIGHT_*_ENABLED` env vars with a unified
DB-backed feature flag service that supports:

- Global enabled / disabled toggle (DB row, persisted via the WP.7.1
  `feature_flags` table; alembic 0194).
- Percent rollout per flag, deterministic by `(flag_name, user_id)`.
- Optional segment filter (tenant_class, role, etc).
- 30 s TTL cache so an operator flip in `/admin/feature-flags`
  propagates to every backend worker within the SLA window.

## SDK contract

```python
from backend.feature_flag_sdk import is_flag_enabled

if is_flag_enabled("ks.byog.enabled", user_id=current_user.id,
                   segment={"tenant_class": current_tenant.class_,
                            "role": current_user.role}):
    ...
```

Resolution priority (each layer can short-circuit to `False`):

1. **DB row** — `feature_flags.state == 'enabled'` (authoritative).
   `disabled` short-circuits to `False`.
2. **Env alias fallback** — when the DB row is missing, read
   `OMNISIGHT_<flag>_ENABLED` and treat `0|false|no|off` as `False`,
   `1|true|yes|on` as `True`.
3. **`default_enabled` from rollout config** — final fallback when
   neither DB nor env is present.
4. **Segment filter** — every key in `segment_filter` must match an
   equal value in the caller's `segment`.  Missing key → fail closed.
5. **Percent rollout** — `bucket_for(name, user_id) < rollout_pct`.
   Anonymous traffic (`user_id=None`) returns `True` only at
   `rollout_pct == 100`.

## Cache contract

- 30 s TTL per `(flag_name, user_id, segment)` tuple.
- TTL is deterministic — every worker reaches the new value within
  30 s of an operator flip without requiring Redis pub/sub fan-out.
  Workers wired to WP.7.4 invalidation will see it sooner.
- Tests pass an explicit clock to the SDK to avoid sleep-based
  flake; production uses `time.monotonic`.

## Operator flow

1. Operator opens `/admin/feature-flags` (existing UI).
2. Toggles a flag → `PATCH /feature-flags/{name}` updates `state`
   in the DB and publishes the WP.7.4 invalidate signal.
3. Backend workers reflect the new value within 30 s (SDK TTL
   ceiling) — most reflect within milliseconds via Redis fan-out.

## Migrating a new env var

1. Add an entry to `config/feature_flag_rollouts.yaml` with
   `name`, `env_alias: OMNISIGHT_<NAME>_ENABLED`, `default_enabled`,
   `owner`, and (optionally) `rollout_pct` / `segment_filter`.
2. Replace `os.environ.get("OMNISIGHT_<NAME>_ENABLED", ...)` call
   sites with `is_flag_enabled("<dotted.name>", user_id=..., segment=...)`.
3. Once the env var has zero callers, delete it from `.env.example`
   in a follow-up cleanup ticket.

## Boundary follow-ups (OP-773 §11)

This row is the `area:backend / area:devops / area:tests / area:docs`
slice of OP-773.  Two pieces remain out-of-area for this ticket and
are deferred:

- **`area:db`** — adding `rollout_pct`, `segment_filter`, and
  `updated_at` columns to the `feature_flags` table.  Until that
  lands, rollout / segment metadata lives in the YAML config file
  rather than per-row in PG.
- **`area:frontend`** — extending `app/admin/feature-flags/page.tsx`
  to render the `rollout_pct` slider, segment filter editor, and
  per-flag usage stats.  The existing toggle UI continues to work
  unchanged.

Both follow-ups will be filed as separate `area:db` and
`area:frontend` tickets so the runner can pick them up under the
matching boundary.

## Tests

`tests/test_feature_flag_sdk.py` — 17 tests covering the AC list
and the rollout / segmentation contract.  Run locally:

```bash
PYTHONPATH=. pytest tests/test_feature_flag_sdk.py -q
```
