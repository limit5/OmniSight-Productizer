# Release discipline — updating a live OmniSight

Rules we commit to so a live OmniSight never eats uncommitted work,
and so rollback is always a 3-minute operation. Written for a
single-operator, trunk-based workflow.

## 1. Schema migrations must be forward-compatible

`backend/db.py::_migrate` is additive-only by convention. Every
migration entry adds a column with a safe default:

```python
("tasks", "new_field", "TEXT NOT NULL DEFAULT ''"),
```

**Forbidden:**
- `ALTER DROP COLUMN` — old binaries still in flight would crash.
- `ALTER RENAME COLUMN` — same problem.
- Migrations that require backfill before the app can start.

**To retire a column** (rare):
1. Release N: mark unused in code, stop writing to it.
2. Release N+1: stop reading from it.
3. Release N+2: add a migration that drops it. Only now is it safe
   because no in-flight binary references it.

This is the same rule that keeps the Phase 63-E decay contract
honest ("never delete memory rows, only down-weight"). Same
principle, higher-stakes cousin.

## 2. Feature flags gate risky work

Two flag systems already exist; use them, don't invent a third:

- **`OMNISIGHT_SELF_IMPROVE_LEVEL`** (env, `off / l1 / l3 / l4 / all`)
  gates the intelligence-track features:
  - L1 = skill extraction
  - L3 = IIS, prompt registry canary, memory decay, RAG prefetch
  - L4 = fine-tune nightly
- **Prompt registry canary** (Phase 63-C) routes 5 % of traffic to
  a new prompt version and auto-rolls back on regression.

Default-off in prod, default-on in staging, promote to prod only
after 24 h clean in staging.

## 3. Every prod release is a git tag

```bash
git tag v0.2.0 -m "release: DAG-G canvas + 67-E prefetch hardening"
git push --tags
```

`scripts/deploy.sh prod v0.2.0` consumes the tag — no `master` HEAD
deploys ever reach prod. This is what makes rollback trivial:

```bash
scripts/deploy.sh prod v0.1.9    # same command, older ref
```

## 4. Staging gets 24 hours

After tagging:

1. `scripts/deploy.sh staging vX.Y.Z`.
2. Watch for 24 h. The metrics that move first:
   - `omnisight_process_start_time_seconds` — confirms the
     restart took.
   - `omnisight_persist_failure_total` — any non-zero is a signal.
   - `omnisight_decision_total{severity="destructive"}` — a spike
     means an auto-execution path misfired.
   - `omnisight_provider_failure_total` — LLM provider health.
3. If clean, `scripts/deploy.sh prod vX.Y.Z`.

24 h is the Decision Engine's default timeout — same clock.

## 5. Never rewrite history on master

Tags are immutable contracts. `git push --force` to `master`
invalidates every tag pointing into that history. If a commit is
wrong, ship a new commit that reverts it.

## 6. Audit trail is load-bearing

`audit_log` (Phase 53, hash-chained) records every promote / reject /
auto-execute. Keep it. It is how post-mortems find the moment
something broke without re-reading the repo.

## 7. The four artefacts a release touches

| Artefact | Managed by | What changes |
|---|---|---|
| Code | git tag | whatever the PR / phase introduced |
| Schema | `db.py::_migrate` | additive column only (see §1) |
| Env | `.env` | only when `.env.example` grew a new variable |
| Prompts | `prompt_registry` | new versions land via `bootstrap_from_disk`; canary rolls them |

If a release needs more than these four, it's two releases pretending
to be one — split it.

## 8. Anti-patterns seen in the wild

- Deploying from `master` instead of a tag → no rollback.
- Landing schema migration + code change + env change in one
  release, then discovering the env var was forgotten on prod →
  app won't start, and the DB is already migrated.
- Hand-editing `data/omnisight.db` to "fix" a bad row → violates
  Phase 63-E's never-delete rule, pollutes the audit chain.
- Force-pushing `master` to "clean up" — see §5.

## Related

- `docs/operations/deployment.md` — WSL + Tunnel topology, one-time
  setup.
- `scripts/deploy.sh` — the canonical release driver.
- `.github/workflows/ci.yml` — the gate every tag must clear.
- `.github/workflows/release.yml` — builds release artefacts on tag
  push.
- `HANDOFF.md` — phase-by-phase log; check here before deciding a
  feature is missing.
