# Hotfix Workflow

**Ticket:** OP-775

Use this path only for production hotfix tickets labelled
`hotfix:vX.Y.Z` and `tier:S`. Planned releases continue through the
normal milestone pipeline.

## Fast Path

1. Cherry-pick one fix commit onto the active release branch:

   ```bash
   scripts/cherry_pick_hotfix.py <commit-sha> --to release/vX.Y
   ```

2. The script creates the next patch tag for that line. Example:
   `v1.0.0` becomes `v1.0.1`.

3. Run the hotfix planner with the ticket labels and gate results:

   ```bash
   scripts/hotfix_pipeline.py \
     --ticket OP-775 \
     --label hotfix:v1.0.1 \
     --label tier:S \
     --smoke-status green \
     --critical-slo-status green \
     --operator-approved
   ```

4. The fast path skips milestone wait, metric-baseline observation, and
   the 5% canary stage.

5. Production canary runs `25% -> 100%`.

6. Operator approval is still required before production deploy. Missing
   approval keeps the planner in `hotfix_fast_track_blocked`.

## Synthetic Budget

The pinned synthetic budget is 29 minutes:

| Stage | Minutes |
|---|---:|
| Cherry-pick | 3 |
| Smoke | 5 |
| Critical SLO | 5 |
| Operator approval | 10 |
| 25% canary | 4 |
| 100% canary | 2 |

This budget assumes approval is granted during the simulated run. If the
operator is unavailable, the hotfix remains blocked rather than deploying
without the D9 gate.
