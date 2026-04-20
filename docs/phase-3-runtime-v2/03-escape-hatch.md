# Phase-3-Runtime-v2 — Escape Hatch Procedure

> **Purpose**: Safety net for rolling back the entire v2 migration back to
> pre-migration state (single-user-safe compat wrapper era) if the migration
> hits an unsolvable blocker mid-way.
>
> **When to use**: single sub-phase stuck for >1 session with no clear path,
> OR critical production-only bug surfaces after partial deploy. **Not**
> for "I don't like how this looks" — rollback is expensive; use judgment.

---

## Tag placement (done in SP-1.0, before any other v2 work)

```bash
# At current HEAD (commit 1006e67f "fix(phase-3 P8)" or newer)
git tag -a phase-3-runtime-v2-start -m "Escape-hatch anchor for P3-RTv2 migration"
git push origin phase-3-runtime-v2-start   # if remote exists
```

This tag is **immutable** for the duration of v2. Never force-update it.

---

## Rollback procedure (emergency)

### Scenario A: Rollback *before* any v2 commits have been deployed to prod

State: v2 commits exist in repo on master, but `docker compose up`'d production
is still running the pre-v2 image (backend image digest from before
`phase-3-runtime-v2-start`).

Steps:
1. `git reset --hard phase-3-runtime-v2-start` — local branch back to tag
2. `git push --force-with-lease origin master` — remote back to tag
3. No prod action needed (prod image unchanged)

Impact: ~0 seconds of prod disruption. All v2 work-in-progress on master
is wiped. Any stashed/uncommitted WIP on machines other than the one doing
the rollback is stranded — so coordinate first.

### Scenario B: Rollback *after* v2 has been deployed (partial or full)

State: prod is running a v2-era backend image that uses the pool. Rollback
target is the last pre-v2 image.

Steps:
1. `git checkout phase-3-runtime-v2-start`
2. `docker compose -f docker-compose.prod.yml build backend-a frontend`
   (rebuilds backend image from tag)
3. `docker compose -f docker-compose.prod.yml up -d --force-recreate
   backend-a backend-b frontend`
4. Verify `/readyz` green + `/api/v1/auth/whoami` 401 (unauth) + dashboard
   loads (operator in fresh browser window)
5. If any part of the v2 Phase involved schema changes that can't be
   reverted cleanly (alembic 0017 tsvector column), the rolled-back
   code path **must still tolerate the column being present** — the
   SQLite compat wrapper ignores unknown columns, so this is fine.
   But alembic 0017 should NOT be `alembic downgrade`'d in production
   unless absolutely necessary (dropping a GENERATED STORED column
   requires a full table rewrite on large tables).
6. `git reset --hard phase-3-runtime-v2-start` + `git push --force-with-lease`

Impact: ~5-15 minutes of prod downtime during rebuild + recreate. All v2
progress is wiped from main branch; if we want to retry later, create a
new branch from the tag before resetting.

### Scenario C: Partial rollback (keep foundation, drop domain ports)

If the foundation (pool, lifespan, get_conn dependency) works but a specific
domain slice (e.g., FTS5 port) is stuck:

1. `git log --oneline phase-3-runtime-v2-start..HEAD` — list all v2 commits
2. Identify the first problematic commit
3. `git revert <commit>..HEAD` — revert the stuck slice and everything after
   it, keeping the foundation
4. Push + rebuild + deploy

This is the preferred "soft rollback" — salvages the good work.

---

## Known rollback limitations

### Alembic migrations cannot be auto-reverted

- Alembic 0017 adds a `tsvector STORED` column to `episodic_memory`
- `alembic downgrade 0016` drops the column, which is a full table rewrite
  on any table with rows
- **Policy**: do NOT run `alembic downgrade` in production without operator
  approval + backup
- The compat wrapper **ignores** unknown columns — having the extra column
  present while the old code runs is harmless

### `db_pg_compat.py` deletion (Epic 11) is a one-way door

Once Epic 11 commits land, the compat wrapper is gone. Rolling back past
Epic 11 requires either:
- Reverting Epic 11's delete commit (trivial if done promptly)
- OR rolling back to `phase-3-runtime-v2-start` tag (nuclear option)

**Rule**: Epic 11 commits land ONLY after Epic 10 (coverage gate) green
AND operator has done a multi-user live verification on staging. No
blind Epic 11.

### Redis / shared state (I10) is unaffected by rollback

- Tag rollback only touches app code + alembic
- Redis keys (rate-limit buckets, shared token usage, SSE subscriber registry)
  persist across rollback
- No cleanup action needed

---

## Rollback drill (SP-1.6)

**Before any domain-slice port commits** (i.e., at end of Epic 1), run the
rollback drill:

1. Make one trivial commit (e.g., add a comment to `db_pool.py`)
2. Push / deploy normally
3. Run Scenario A rollback: `git reset --hard phase-3-runtime-v2-start`
4. Rebuild + redeploy
5. Verify: prod is back to pre-v2 state, `db_pool.py` gone, compat wrapper
   active
6. Document elapsed time + any unexpected friction in `03-escape-hatch.md`
7. Re-apply the v2 commits (cherry-pick from reflog) to continue

This proves the rip-cord works before we're dependent on it.

---

## Decision log

Any time during v2 migration someone **considers** pulling the rip-cord,
log it here:

| Date | Considered by | Trigger | Decision | Outcome |
|---|---|---|---|---|
| (none yet) | | | | |

Rule: even considering the rip-cord counts as a datum. If this table
accumulates 3+ entries within the migration, that's a signal the plan
needs revision regardless of whether we actually reverted.

---

## WIP branch / stash preservation during rollback

`git reset --hard phase-3-runtime-v2-start` **only affects the current
branch's HEAD**. It does NOT touch:

- Other local branches (create a branch before reset to preserve WIP)
- The stash (explicit `git stash pop` still works post-reset)
- Other machines' working copies (coordinate before pushing force)
- The reflog (90-day TTL by default — recover any "lost" commit via
  `git reflog` → `git cherry-pick <sha>`)

**Before pulling the rip-cord, do**:

```bash
# Preserve current WIP work as a branch (non-destructive)
git branch phase-3-runtime-v2-wip-$(date +%Y%m%d-%H%M%S)
# Now the current commits are on TWO refs: master AND that WIP branch
# Reset master without losing the work
git reset --hard phase-3-runtime-v2-start
```

If post-reset someone realises commit X was actually good, recover via:
```bash
git reflog | grep <commit-message-keyword>
git cherry-pick <sha>
```

---

## Docker image snapshot before rollback (extra safety)

Before any `docker compose build --force-recreate` during a rollback,
take a snapshot of the currently-running backend image:

```bash
docker commit omnisight-productizer-backend-a-1 \
    omnisight-backend:pre-rollback-$(date +%Y%m%d-%H%M%S)
```

This gives a committed image we can instantly re-run if the rollback
rebuild itself fails (shouldn't happen, but the point of a safety net
is to cover "shouldn't"s).

---

## Rollback does NOT reset Redis

Redis shared state (I10 rate-limit buckets, shared token usage, SSE
subscriber registry) is independent of code version. After rollback:

- Old rate-limit buckets may be depleted from v2's testing; they'll
  refill at their configured rate
- No manual `FLUSHDB` required; but it's also safe to run if a clean
  slate is desired

---

## What if rollback ITSELF fails

Worst-case scenario: `git reset --hard phase-3-runtime-v2-start`
succeeds but the pre-v2 image won't rebuild / run. Options in order
of destructiveness:

1. **Re-pull from GHCR if configured** — `docker compose pull
   backend-a` may fetch a previously-built image
2. **Rebuild from `phase-3-runtime-v2-start` tag with fresh Docker
   cache** — `docker builder prune` then build
3. **Restore from SQLite snapshot** in `backups/pre-pg-cutover-*.tar.gz`
   — restore to `omnisight-data` volume, comment out
   `OMNISIGHT_DATABASE_URL` in `.env`, recreate backend → runs on
   SQLite path (pre-G4 state). True nuclear option.
4. **Cold-restore from PG standby** — if primary is corrupted,
   promote `pg-standby` via `docs/ops/db_failover.md` §4 procedure

Each level requires operator confirmation. Don't cascade automatically.
