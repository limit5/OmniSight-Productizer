# CI Worker Runbook (OP-742, META OP-739)

The CI worker is a long-running daemon that polls Gerrit for open
patchsets, runs an impact-selected pytest subset, and posts a
`Verified ±1` vote with a structured failure comment.

It replaces the implicit "operator runs the full suite by hand" gate
with a fast, repeatable check: typical backend PSes finish in 5-15 min
instead of 60-180 min, and `area:docs` PSes auto-verify in under 30 s.

The daemon does **not** substitute for the human +2 — Verified is the
machine-side gate, Code-Review +2 is the human-side gate. See
`reference_gerrit_submit_requirements.md` for the submit-rule split.

## Files
- `scripts/ci_worker.py` — daemon entry point (`python ci_worker.py`)
- `scripts/ci_test_impact.py` — test impact analyser (importable +
  CLI: `python ci_test_impact.py file1 file2 …` prints JSON)
- `deploy/systemd/ci-worker.service` — systemd unit
- `backend/tests/test_ci_worker.py`,
  `backend/tests/test_ci_test_impact.py` — unit coverage

## Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/ci-worker.service ~/.config/systemd/user/
# Edit Environment= lines for local paths / bot identity.
systemctl --user daemon-reload
systemctl --user enable --now ci-worker
loginctl enable-linger $USER
```

First-boot smoke:
```bash
journalctl --user -u ci-worker -f -n 100
# expect: "ci-worker starting workers=2 poll=30s project=omnisight/..."
```

## Operating modes

| Env var                          | Default | Notes |
|----------------------------------|---------|-------|
| `OMNISIGHT_CI_WORKER_PARALLEL`   | 2       | 2-4 supported. Higher only after pytest fixture isolation is checked. |
| `OMNISIGHT_CI_WORKER_POLL_S`     | 30      | Spec default. Lower polling races the Gerrit indexer; don't go below 10. |
| `OMNISIGHT_CI_WORKER_TIMEOUT`    | 900     | Per-PS pytest ceiling (15 min). Full-suite runs may need raising. |
| `OMNISIGHT_CI_WORKER_DRY_RUN`    | 0       | Set to `1` to log votes without posting. Use during the first 24-48 h of soak. |
| `OMNISIGHT_CI_FORCE_RERUN`       | 0       | Re-run even if PS already has a Verified vote (emergency only). |

## How a PS is processed

1. `gerrit query` returns open changes lacking a Verified vote on the
   current patchset.
2. The worker tries to claim
   `/tmp/ci-worker-<change_id>-<short-rev>.lock`. A live PID owner
   wins; a stale (dead PID) lock is reclaimed.
3. Policy decision (in order):
   1. If JIRA `area:` labels include `security` → `full`.
   2. If file paths are 100 % docs → `skip` → auto +1.
   3. If file paths are frontend / docs only → `frontend-only` → auto +1.
   4. If file paths are devops / tooling only → `lint-only` → auto +1.
   5. If any change touches a high-fan-out path
      (`backend/db.py`, `backend/agents/scheduler.py`, etc.) →
      `full`.
   6. Otherwise → `affected`: direct test map ∪ reverse-import
      transitive closure of changed Python modules.
4. The selection is run via `python -m pytest --no-cov -q [-x] …`.
5. The worker votes:
   - `Verified +1` with a one-line policy + duration comment on
     success.
   - `Verified -1` with the six-field comment from OP-742(e) on
     failure (PS, policy, runtime, failed-test list, log snippet,
     category hint for C3).

## Failure-category hints (for OP-741 / C3)

| `category_hint`  | Meaning                                       | C3 recovery |
|------------------|-----------------------------------------------|-------------|
| `real-bug`       | Genuine assertion failure                     | Push fix as PS{n+1} |
| `flaky`          | Failure mentions `flaky` / `TimeoutError` etc | Retry once, escalate on 2nd flake |
| `stale`          | pytest collected nothing (mis-classified)    | Force `full` policy via `OMNISIGHT_CI_FORCE_RERUN=1` |
| `infra`          | Collection / fixture error (rc 2/3/4)         | Operator investigation, no auto-retry |
| `passed`         | (only on +1 votes)                           | n/a |

## Common operations

**Force a re-run of one PS without restarting the daemon:**
```bash
rm -f /tmp/ci-worker-<change_id>-<short-rev>.lock
OMNISIGHT_CI_FORCE_RERUN=1 \
  python scripts/ci_worker.py --once
```

**Inspect the impact analyser for a hypothetical change set:**
```bash
python scripts/ci_test_impact.py \
  --area backend \
  backend/agents/foo.py backend/routers/bar.py
# {"policy": "affected", "test_files": [...], "reason": "..."}
```

**Stop voting for one shift (planned outage / refactor):**
```bash
systemctl --user stop ci-worker
# patches will accumulate; on resume the worker drains them in
# poll order, parallelism = OMNISIGHT_CI_WORKER_PARALLEL.
```

## Caveats / known limitations

- The impact analyser parses static `import`/`from` statements only.
  Dynamic imports (`importlib.import_module(name)`) are invisible to
  the graph; if you change a module behind a string-keyed registry
  the conservative fallback (high-fan-out list) is your only safety
  net. Add the registry's module to `HIGH_FAN_OUT_PATHS` if you find
  a class of misses.
- The reverse-import graph is built on daemon startup. A sweeping
  refactor on `develop` won't shift selections until the worker
  restarts (cheap — `systemctl --user restart ci-worker`).
- `gerrit query` does not surface JIRA `area:` labels directly. The
  current implementation parses the `OP-XXX` keys from the change
  subject and (TODO follow-up ticket) will resolve labels via the
  JIRA REST API. Until then, area-only short-circuits land via the
  file classifier — slightly more conservative, never less.
- The merger-bot's conflict-resolution +2 (per CLAUDE.md exception)
  is orthogonal: this daemon never casts Code-Review, only Verified.

## Cross-references

- Parent META: OP-739 (CI parallel-gate program)
- Sibling C1: Verified label setup (Gerrit `project.config`)
- Sibling C3: OP-741 — Verified -1 recovery state machine
- Sibling R5: AI Reviewer auto-+1 (`backend/agents/ai_reviewer.py`)
- Lesson L26 — test-impact analysis rationale (`docs/sop/lessons/L-OP-736-runner-git-operations-need-layered-cleanup-for-stuck-rebase.md`)
