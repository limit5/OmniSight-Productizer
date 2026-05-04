# Post-Deploy Recovery Runbook (FX.9.11)

**Audience:** the operator running `scripts/deploy-prod.sh` against the
production WSL host, and the on-call engineer who gets paged when the
script aborts mid-deploy.

**Why this exists:** the 2026-05-04 first-time prod deploy of the
FX.1–8 backlog (commit `9676d17e`) tripped **five distinct
production-readiness gates**, each of which aborted the deploy at a
different stage. None of them were code bugs in the feature branches
— they were last-mile gaps between "code merged + CI green" and
"production stack actually runs this code path" (the same
`dev-green ≠ prod-ready` class the SOP §"Production Readiness Gate"
warns about).

This runbook captures, **per gate**:

1. **Symptom** — the exact failure the operator sees.
2. **Root cause** — what was actually wrong, in one paragraph.
3. **Recovery (right-now fix)** — minimal sequence to unblock the
   current deploy. Assumes the permanent fix has *not* yet landed
   (so the runbook is useful in a re-occurrence even before the gate
   has been re-armed).
4. **Permanent fix landed in this repo** — the FX.9.x row that closed
   each gate, so future operators can confirm the guard exists rather
   than re-derive it.
5. **Prevention / preflight check** — the one or two commands that
   would have caught the gap **before** the operator pressed deploy.
   The aggregated preflight checklist lives in §7.

The five gates are presented in **deploy-flow order** (the order the
operator hits them when re-running `scripts/deploy-prod.sh`), not
TODO row order:

| # | Gate | Aborts deploy at | Permanent fix |
|---|------|------------------|---------------|
| 1 | **GPG signer** missing  | Step 1 (`check_deploy_ref.sh` Layer 2)              | FX.9.8 |
| 2 | **Backup passphrase** unset in non-interactive shell | Step 1b (`backup_prod_db.sh`)         | FX.9.7 |
| 3 | **DLP scanner** crashes on FTS5 / WITHOUT ROWID + false positives | Step 1b (DLP sweep inside `backup_prod_db.sh`) | (e9908bfc) + FX.7.10 preflight |
| 4 | **Alembic migration** fails (4 heads / SA 2.x params / `platform.py` shadow) | Step 2.5 (`alembic upgrade heads`) | FX.9.1 + FX.9.2 + FX.9.3 + FX.9.4 + FX.9.5 |
| 5 | **TypeScript build** fails (React 19 `JSX` namespace + templates/ tsc target drift) | Step 2 (`docker compose build` of `Dockerfile.frontend`) | 803d403d + 9676d17e |

> Note: Gate 5 fires **earlier** than Gates 3 and 4 in the flow
> (Step 2 < Step 2.5), but is presented last because it lives entirely
> in the build phase and is conceptually independent — Gates 1-4 are
> all "operator host / runtime artefact" issues, Gate 5 is "frontend
> source compiles".

The runbook ends with §6 a **drift-guard inventory** (which automated
tests prevent each gate from re-emerging silently) and §7 a
**pre-deploy preflight checklist** (one-page operator copy/paste)
that runs all five gate sanity checks in <30 seconds.

---

## 1. Gate 1 — GPG signer missing → `check_deploy_ref.sh` Layer 2 abort

### 1.1 Symptom

Operator runs `scripts/deploy-prod.sh` (no flags). Step 1 prints:

```
✅ Layer 1: ref 'branch:master' matched allowlist
❌ Layer 2: 'branch:origin/master' is signed by <FPR-X>, but no
   trusted signer fingerprint matches.
   Trusted signers from deploy/prod-deploy-signers.txt:
     (none — file contains only comments)
   Use --insecure-skip-verify only as an audit-trailed emergency.
```

Deploy aborts at Step 1 — no images built, no containers touched, no
data risk.

### 1.2 Root cause

FX.7.9 (2026-05-04 morning) shipped the two-layer
`check_deploy_ref.sh` gate (allowlist + GPG signer) but landed
`deploy/prod-deploy-signers.txt` with **only the comment block** — no
real fingerprints. Layer 1 passed (`master` was on the allowlist),
but Layer 2 had nothing to compare against, so every signed commit
failed the check. The escape hatch `--insecure-skip-verify` worked
but is supposed to be loud-and-rare, not the default.

### 1.3 Recovery (right-now)

Two paths, depending on whether you have an operator GPG key already:

**Path A — operator already has a release-signing key:**

```bash
# 1. Capture the fingerprint (40 hex chars, no spaces)
FPR=$(gpg --list-secret-keys --with-colons you@example.com \
       | awk -F: '/^fpr:/ {print $10; exit}')

# 2. Append to signers list (commit + push to master so the gate
#    sees it)
echo "$FPR" >> deploy/prod-deploy-signers.txt
gpg --armor --export "$FPR" >> deploy/release-signers.asc
git add deploy/prod-deploy-signers.txt deploy/release-signers.asc
git commit -S -m "chore(deploy): add operator GPG fingerprint $FPR"
git push origin master

# 3. Re-run the deploy
./scripts/deploy-prod.sh
```

**Path B — first-ever setup (no operator key yet):**

Use the helper `./scripts/setup_release_signer.sh` and follow the
4-stage runbook in `docs/runbook/gpg-release-signer-setup.md`
(first-time setup / rotation / revocation / cold-spare). That doc is
the canonical setup procedure; this runbook only links to it because
the operator hits this gate **as part of a deploy attempt**, not as
planned key-management work.

**Emergency escape hatch** (only if both paths above are blocked —
e.g. lost GPG key, host rebuild without keyring restore):

```bash
./scripts/deploy-prod.sh --insecure-skip-verify
# Bypasses Layer 2 only; Layer 1 (allowlist) still enforced.
# The flag is audit-trailed in deploy logs — every use must be
# explained in the post-deploy review.
```

### 1.4 Permanent fix (landed)

- **FX.9.8** (`4828ffa7`) — provisioned the operator GPG release-signer:
  fingerprint `50245609D5BF1E14CA7AD5AF18BC2AB5FDDD932A` added to
  `deploy/prod-deploy-signers.txt`, public key bundled into
  `deploy/release-signers.asc`, helper `scripts/setup_release_signer.sh`,
  full runbook `docs/runbook/gpg-release-signer-setup.md`, drift guard
  `backend/tests/test_release_signer_setup_drift_guard.py`.
- The very commit that lands `prod-deploy-signers.txt` non-empty must
  itself be GPG-signed by the same key (so master tip becomes signed
  and Layer 2 starts passing immediately).

### 1.5 Prevention

```bash
# 30-second preflight: confirms signers list is non-empty AND
# master tip is signed by a trusted signer.
./scripts/check_deploy_ref.sh --kind branch --ref master
# expect both ✅ Layer 1 and ✅ Layer 2 lines.
```

If you're touching the operator GPG setup itself (rotation /
revocation), run the drift guard:

```bash
python3 -m pytest backend/tests/test_release_signer_setup_drift_guard.py -q
```

---

## 2. Gate 2 — `OMNISIGHT_BACKUP_PASSPHRASE` unset in non-interactive shell

### 2.1 Symptom

Operator runs `scripts/deploy-prod.sh` either via cron, systemd, or
inside a `bash -c '...'` non-interactive shell (e.g. tmux send-keys,
ssh-with-explicit-command, or a parent process that didn't source the
operator's full env). Step 1b prints:

```
Step 1b: Pre-deploy backup
ERROR: OMNISIGHT_BACKUP_PASSPHRASE is required for encrypted backups
       (set in ~/.profile or pass --skip-encrypt to opt out — fail-close)
```

Deploy aborts at Step 1b — code already pulled (Step 1 passed) but
no images built and no migrations run. **Re-running interactively
will appear to "fix" the problem** because the operator's interactive
bash sources `~/.bashrc` and the variable is set there.

### 2.2 Root cause

The `OMNISIGHT_BACKUP_PASSPHRASE` export sat in `~/.bashrc` since the
FX.4.3 H2 backup gate landed. On Debian / Ubuntu, `~/.bashrc`:

- Is **only** sourced by interactive non-login bash shells.
- Short-circuits early when run non-interactively (the canonical
  `[ -z "$PS1" ] && return` guard near the top).
- Is **not** sourced by login shells directly (login bash sources
  `~/.profile`, which **may** then chain to `~/.bashrc`, but only
  inside the `if [ -n "$BASH_VERSION" ]` block — and crucially that
  chain is interactive-only by `~/.bashrc`'s own internal guard).

Cron, systemd timers, ssh-with-command, and `bash -c` all run
non-interactive shells. None of them see anything in `~/.bashrc`.
The deploy script worked when the operator double-clicked terminal +
typed `./scripts/deploy-prod.sh` (interactive); it failed every other
invocation pattern. This is a "works on my machine" trap that hides
behind shell-flavour subtleties.

### 2.3 Recovery (right-now)

Option A — minimum-touch (just unblock this deploy):

```bash
# In the same shell that will run deploy-prod.sh:
export OMNISIGHT_BACKUP_PASSPHRASE="$(cat ~/.config/omnisight/backup-passphrase)"
./scripts/deploy-prod.sh
```

Option B — fix the root cause now (preferred — survives next reboot):

```bash
# Move the export to ~/.profile (POSIX login-shell entry point;
# sourced by SSH login, WSL initial launch, `bash -l`, `su -`)
cat >> ~/.profile <<'EOF'

# OMNISIGHT prod backup passphrase — moved here from .bashrc (FX.9.7)
# so cron / systemd / non-interactive shells also see it.
if [ -r "$HOME/.config/omnisight/backup-passphrase" ]; then
    export OMNISIGHT_BACKUP_PASSPHRASE="$(cat "$HOME/.config/omnisight/backup-passphrase")"
fi
EOF

# Remove the stale export from ~/.bashrc to avoid drift between the
# two files (replace with a 3-line pointer comment so the next
# operator knows where it went).
sed -i '/^export OMNISIGHT_BACKUP_PASSPHRASE/d' ~/.bashrc

# Re-source for the current shell
. ~/.profile

# Verify
echo "len=${#OMNISIGHT_BACKUP_PASSPHRASE}"   # expect: len=44
./scripts/deploy-prod.sh
```

Cron jobs that need the variable still need `. ~/.profile` explicitly
(or `BASH_ENV=~/.profile`) — `cron` ignores `~/.profile` by default.
That's a known cron quirk, **not** something `~/.profile` solves on
its own. The `~/.profile` move only fixes login-shell paths; cron
needs the explicit source.

Option C — opt out of encrypted backups (NOT recommended for prod):

```bash
./scripts/deploy-prod.sh --skip-encrypt   # if the script supports it
# or set OMNISIGHT_BACKUP_SKIP_ENCRYPT=1; check the helper for the
# current opt-out knob name. Defeats the H2 backup-encryption gate —
# explain in the post-deploy review.
```

### 2.4 Permanent fix (landed)

- **FX.9.7** (`9f685581`) — moved the export from `~/.bashrc:124` to
  `~/.profile`, hardened with `[ -r ... ]` guard so a missing secret
  file produces a clean fail-close in `backup_prod_db.sh:123` rather
  than exporting an empty string. `$HOME` replaces hardcoded
  `/home/user/` for portability.

This is an **operator dotfile change outside the repo** — there's no
PR-level test that can detect a regression. The drift surfaces only
on the next non-interactive deploy, which is exactly the scenario
this runbook handles.

### 2.5 Prevention

```bash
# 5-second preflight: simulate the cron / systemd shell environment
env -i HOME="$HOME" PATH="$PATH" bash -lc \
    'echo passphrase_present=${OMNISIGHT_BACKUP_PASSPHRASE:+yes} len=${#OMNISIGHT_BACKUP_PASSPHRASE}'
# expect: passphrase_present=yes len=44

# Also test the explicit source path (what cron should do):
env -i HOME="$HOME" PATH="$PATH" bash -c \
    '. ~/.profile; echo passphrase_present=${OMNISIGHT_BACKUP_PASSPHRASE:+yes} len=${#OMNISIGHT_BACKUP_PASSPHRASE}'
# expect: passphrase_present=yes len=44
```

If either prints `passphrase_present=` (empty) or `len=0`, Gate 2
will fire on the next non-interactive deploy.

---

## 3. Gate 3 — DLP scanner crashes on FTS5 / WITHOUT ROWID + false positives

### 3.1 Symptom

Two distinct crash modes, both inside `Step 1b: Pre-deploy backup`
(after Gate 2 passed):

**3.1.1 — FTS5 / WITHOUT ROWID `no such column: rowid`:**

```
ERROR: backup DLP sweep failed at table episodic_memory_fts:
       no such column: rowid
       (the scanner reads sqlite_master to enumerate tables but does
       not exclude virtual / WITHOUT ROWID tables — those have no
       implicit rowid column, the SELECT crashes, the deploy aborts)
```

**3.1.2 — False-positive findings on by-design opaque columns:**

```
ERROR: DLP scanner reported 10 finding(s) — refusing to ship backup
       containing potential PII / secret material:
         audit_log.session_id     (SHA256 hex, 32-char)
         prompt_versions.body_sha256  (SHA256 hex)
         tenant_secrets.salt      (random base64)
         ... 7 more findings ...
       Re-run after redacting OR add to scanner allowlist.
```

Both modes abort the deploy at Step 1b — no encrypted backup created,
no images built. Re-running won't help unless the scanner config or
the source DDL changes.

### 3.2 Root cause

`scripts/backup_dlp_scan.py` shipped pre-FX with two production-blocking
defects, both of which only surface on a *real* prod schema (not the
slimmed-down test fixtures CI uses):

1. **Table enumeration too greedy.** `_iter_user_tables` filtered
   `sqlite_master.type = 'table' AND name NOT LIKE 'sqlite_%'`,
   which doesn't exclude SQLite **virtual tables**
   (`CREATE VIRTUAL ... USING fts5`) or **WITHOUT ROWID** tables.
   The 3 FTS5 shadow tables in our schema
   (`episodic_memory_fts`, `episodic_memory_fts_config`,
   `episodic_memory_fts_idx`) lack the implicit `rowid` column the
   scanner uses to anchor findings → `SELECT rowid` errors out, the
   scanner aborts with non-zero exit, deploy aborts.
2. **No allowlist for by-design high-entropy columns.** Once defect
   #1 is past, the scanner flags 10 findings on columns that **must
   be opaque high-entropy by design** (session IDs, content hashes,
   tenant secret salts). These are not data leaks — the scanner just
   can't distinguish "intentionally random" from "accidentally
   leaked secret".

A separate issue, fixed earlier in **FX.7.10**, was a much more
dangerous variant: a **missing scanner script entirely** would
produce the same generic die-message as a legitimate secret-found
abort, silently demoting "deploy artefact incomplete" into a
phantom-secret false positive while encrypted backups shipped with
no DLP gate ever having run. The FX.7.10 preflight guards against
that regression.

### 3.3 Recovery (right-now)

**Mode 3.1.1 (FTS5 / WITHOUT ROWID crash):**

The fix is already merged (`e9908bfc`). If you're hitting this on
`master` head, the most likely causes are (a) you're on an old
checkout that pre-dates `e9908bfc`, or (b) a *new* virtual / WITHOUT
ROWID table was added without updating the scanner allow-skip logic.

```bash
# Check the version
git log --oneline -1 scripts/backup_dlp_scan.py

# If older than e9908bfc, just pull master:
git fetch origin master && git merge origin/master --ff-only

# If on master head and a NEW virtual table caused the regression,
# the scanner already auto-skips by reading sqlite_master.sql for
# `CREATE VIRTUAL` / `WITHOUT ROWID` substrings — verify the new
# table's DDL contains one of those tokens. If it doesn't (e.g.
# unusual vendor extension), add an explicit name to the skip list
# in scripts/backup_dlp_scan.py and ship a follow-up commit.
```

**Mode 3.1.2 (false positives on opaque columns):**

Two options:

```bash
# Option A — short-term unblock only: skip the DLP scan for this
# deploy. Encrypted backup still created; only the secret-leak gate
# is bypassed. Audit-trail in the post-deploy review.
OMNISIGHT_BACKUP_SKIP_DLP=1 ./scripts/deploy-prod.sh   # check the
# helper for current opt-out env knob; the older version of the
# script may not support this and you'd have to comment out the
# DLP block in scripts/backup_prod_db.sh as a one-shot.

# Option B — proper fix: add the offending column(s) to the scanner
# allowlist and ship.
# Edit scripts/backup_dlp_scan.py — there's a KNOWN_SAFE_COLUMNS
# allowlist; add the table.column tuple with a one-line justification
# in a comment ("salt is base64-random by design, not a credential").
git add scripts/backup_dlp_scan.py
git commit -S -m "fix(dlp): allowlist <table>.<column> as by-design opaque"
git push origin master
./scripts/deploy-prod.sh
```

### 3.4 Permanent fix (landed)

- **`e9908bfc`** (`fix(backup-dlp): skip virtual / WITHOUT ROWID tables
  + allowlist known-safe columns`) — both modes resolved in one
  commit. The scanner now reads `sqlite_master.sql` and skips rows
  whose DDL contains `CREATE VIRTUAL` or `WITHOUT ROWID`, and ships
  an explicit allowlist for the 10 by-design high-entropy columns.
- **FX.7.10** (`11d2e154`) — `scripts/backup_prod_db.sh` preflight
  that fails distinctly when the DLP scanner script itself is missing
  / unreadable / not executable, so a deploy-artefact gap can never
  again be conflated with a legitimate secret-found abort. Drift
  guard: `backend/tests/test_backup_dlp_existence_check_drift_guard.py`
  (10 tests).

### 3.5 Prevention

```bash
# Run the backup helper in dry-run mode to exercise the DLP path
# WITHOUT writing a backup file:
scripts/backup_prod_db.sh --label preflight --dry-run
# expect a "DLP scan: 0 findings" line. Anything else => investigate
# before pressing real deploy.
```

After adding any new SQL table (Alembic migration), run the DLP
scanner against a freshly-migrated DB before merging:

```bash
# From a dev shell, after `alembic upgrade head`:
python3 scripts/backup_dlp_scan.py /path/to/dev.db
```

---

## 4. Gate 4 — Alembic migration fails (4 heads + SA 2.x params + `platform.py` shadow)

### 4.1 Symptom

Step 2.5 (`docker compose run --rm --no-deps -e PYTHONSAFEPATH=1 -w /app/backend backend-a python -m alembic upgrade heads`)
fails with one of three distinct error patterns, depending on which
of the underlying defects you hit first:

**4.1.1 — `MultipleHeads`:**

```
sqlalchemy.exc.CommandError: Multiple head revisions are present
for given argument 'head'; please specify a specific target
revision, '<branchname>@head' to narrow to a specific head, or
'heads' for all heads
```

**4.1.2 — `AttributeError: module 'platform' has no attribute 'python_implementation'`:**

```
File "/usr/local/lib/python3.11/site-packages/sqlalchemy/util/...",
  line ..., in <module>
    if platform.python_implementation() == "CPython":
AttributeError: module 'platform' has no attribute 'python_implementation'
```

**4.1.3 — `TypeError: argument 1 must be str, dict or tuple, not immutabledict`:**

```
File ".../alembic/versions/0052_catalog_seed.py", line 47, in upgrade
    conn.exec_driver_sql(sql)
  ...
TypeError: ... immutabledict ...
```

All three abort Step 2.5; the rolling restart never runs; both
replicas stay on the **old** image so the live request path is
unaffected. The DB is left in whatever partial state alembic reached
(each individual migration is wrapped in a tx, so a single migration
is atomic; a multi-migration batch may stop partway).

### 4.2 Root cause

Three independent defects, each one a hard blocker on its own. The
FX.9 sequence had to land them in dependency order:

1. **FX.9.4 — Four converged Alembic heads.** The version graph fan-out
   from `0058` had left four tips: `0059` (web_sandbox), `0106`
   (ks_envelope), `0183` (ab_cost_guard), `0187` (firewall_events).
   `alembic upgrade head` (singular) bails with `MultipleHeads`. Fix:
   no-op merge migration `0188` with a 4-tuple `down_revision`.
2. **FX.9.3 — `backend/platform.py` shadows stdlib `platform`.** Python
   auto-prepends CWD to `sys.path`. The alembic CLI's import order
   meant any process starting with `cwd=/app/backend` won the import
   race with the project module over the stdlib, and SQLAlchemy's
   top-level `import platform` crashed. The defence-in-depth flag
   `-e PYTHONSAFEPATH=1` (Python 3.11+) helps but isn't load-bearing
   anymore — the permanent fix is the rename `platform.py` →
   `platform_profile.py` (79-file blast radius), which **eliminates
   the collision entirely**.
3. **FX.9.1 — SQLAlchemy 2.x `exec_driver_sql` + `immutabledict`
   incompatibility.** `0052_catalog_seed.upgrade()` called
   `conn.exec_driver_sql(sql)`. SA 2.x normalises the no-params call
   to `(sql, immutabledict())` and forwards it to CPython's
   C-accelerated sqlite3 cursor, whose `_pysqlite_query_execute`
   rejects `immutabledict` (only real `dict` / `tuple` / `list` pass
   its `isinstance` check). Fix: route through `op.execute(sql)`,
   which wraps in a `text()` clause and uses the
   `Connection.execute(TextClause)` path (params adapter never
   poisons the cython binding).
4. **FX.9.2 — `backend/alembic/env.py` relative import.** The
   `from alembic_pg_compat import …` form depended on `/app/backend`
   being on `sys.path`, which interacted with the FX.9.3 stdlib shadow
   bug. Fix: absolute `from backend.alembic_pg_compat import …`
   (combined with injecting `Path(__file__).parents[2]` = `/app` to
   `sys.path[0]` before that import). FX.9.2 alone doesn't fix the
   shadow, but it removes one of the two scaffolds the shadow stood
   on.
5. **FX.9.5 — Missing migration step in deploy script.** Even with
   defects #1–4 fixed, `deploy-prod.sh` used to go directly from
   `Step 2: Build` → `Step 3: Rolling restart`, so the new image's
   `/readyz` would hit an unmigrated schema and fail. The new
   `Step 2.5` runs `alembic upgrade heads` (plural) in an ephemeral
   container against the live DB **before** any replica restarts.

The five fixes are cumulative — pre-FX.9, no single fix would have
let the deploy through. Post-FX.9, the deploy passes Step 2.5 cleanly.

### 4.3 Recovery (right-now)

The recovery depends on which of the three error patterns you hit:

**4.3.1 — `MultipleHeads`:**

```bash
# Confirm the head count
docker compose -f docker-compose.prod.yml run --rm --no-deps \
    -e PYTHONSAFEPATH=1 -w /app/backend \
    backend-a python -m alembic heads
# If you see >1 head, two paths:
#   (a) Use `upgrade heads` (plural) — already done by FX.9.5 in
#       deploy-prod.sh Step 2.5 since 68fc49ad. If you're on an old
#       script version, edit the helper to use the plural form.
#   (b) Land a merge migration that consolidates the heads (the
#       FX.9.4 pattern — see backend/alembic/versions/0188_merge_heads.py
#       as the template; copy + edit down_revision to your N tips).
```

**4.3.2 — `AttributeError: module 'platform' …`:**

```bash
# Confirm the FX.9.3 rename has landed
ls backend/platform_profile.py 2>/dev/null \
    && echo "FX.9.3 in place" \
    || echo "FX.9.3 NOT in place — pull master"

# If pre-FX.9.3, the temp-fix is to ensure PYTHONSAFEPATH=1 is set
# (already wired into Step 2.5 by FX.9.5 since 68fc49ad). If your
# script doesn't pass it:
docker compose -f docker-compose.prod.yml run --rm --no-deps \
    -e PYTHONSAFEPATH=1 -w /app/backend \
    backend-a python -m alembic upgrade heads
```

**4.3.3 — `TypeError ... immutabledict`:**

```bash
# This error means the FX.9.1 fix isn't on master yet (or a new
# migration introduced a fresh exec_driver_sql call).
# Quick triage: grep for the bad pattern across all migrations.
grep -rn "exec_driver_sql" backend/alembic/versions/

# Replace conn.exec_driver_sql(sql) with op.execute(sql) and ship.
# The before_cursor_execute hook from alembic_pg_compat.install_pg_compat
# still fires through the op.execute() path, so PG-side
# `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING` rewrites and ::jsonb
# casts continue to work unchanged.
```

**General rollback if migration left the DB in a partial state:**

```bash
# 1. Identify what alembic actually applied
docker compose -f docker-compose.prod.yml run --rm --no-deps \
    -e PYTHONSAFEPATH=1 -w /app/backend \
    backend-a python -m alembic current

# 2. The pre-deploy backup from Step 1b is at:
ls -lh ~/.local/share/omnisight/backups/ | head -5
# pick the latest pre-deploy-* file (.sqlite.gz.gpg.age — confirm
# the timestamp matches the aborted deploy attempt).

# 3. Restore — see docs/ops/db_failover.md for the full procedure;
# very short version is: stop both replicas, decrypt + decompress
# the backup over the live volume's DB file, re-start replicas
# (still on the OLD image), confirm /readyz, then re-attempt the
# deploy with the migration fix in place.
```

### 4.4 Permanent fix (landed)

- **FX.9.1** (`61c44a9e`) — `0052_catalog_seed.py` routed through
  `op.execute()` (drops the immutabledict trap).
- **FX.9.2** (`c83f9712`) — `backend/alembic/env.py` uses absolute
  `from backend.alembic_pg_compat import …` (removes one scaffold of
  the platform.py shadow).
- **FX.9.3** (`a9040bd0`) — `backend/platform.py` renamed to
  `backend/platform_profile.py` across 79 files (eliminates the stdlib
  shadow trap permanently).
- **FX.9.4** (`f1fdda6f`) — `backend/alembic/versions/0188_merge_heads.py`
  collapses 4 heads (`0059 / 0106 / 0183 / 0187`) into single head
  `0188`. `alembic heads` now prints `0188 (head)`.
- **FX.9.5** (`68fc49ad`) — `scripts/deploy-prod.sh` Step 2.5
  ephemeral-container `alembic upgrade heads` (plural form) between
  build and the first rolling restart. Failure aborts before any
  replica is touched.

### 4.5 Prevention

Before pressing deploy:

```bash
# 1. Confirm single-head invariant on master
docker compose -f docker-compose.prod.yml run --rm --no-deps \
    -e PYTHONSAFEPATH=1 -w /app/backend \
    backend-a python -m alembic heads
# expect exactly one line ending with "(head)".

# 2. Dry-run the migration without committing (uses --sql to render)
docker compose -f docker-compose.prod.yml run --rm --no-deps \
    -e PYTHONSAFEPATH=1 -w /app/backend \
    backend-a python -m alembic upgrade heads --sql > /tmp/migration.sql
# Inspect /tmp/migration.sql for any obviously-broken statements.

# 3. Confirm the platform.py shadow is gone
test -f backend/platform.py && echo "REGRESSION — FX.9.3 reverted" || echo "OK"
test -f backend/platform_profile.py && echo "OK" || echo "MISSING"

# 4. Confirm exec_driver_sql is not used in any new migration
grep -rn "exec_driver_sql" backend/alembic/versions/ \
    && echo "REGRESSION — FX.9.1 pattern violated" \
    || echo "OK"
```

CI side:

- The Alembic single-head invariant has a pre-commit hook
  (`FX.7.6 alembic enforcement`, `649746f0`) that rejects silent
  multi-head landings.
- The platform.py shadow has no automated guard — the rename is
  one-way and reversible only by an explicit `git mv` regression. If
  you're concerned, add a `test/no_stdlib_shadow.py` that imports
  `platform` and asserts `platform.python_implementation()` works.

---

## 5. Gate 5 — TypeScript build fails (`Cannot find namespace 'JSX'` + `templates/_shared/` target drift)

### 5.1 Symptom

Step 2 (`docker compose -f docker-compose.prod.yml build`) fails
inside the `frontend` build target with one or both of:

**5.1.1 — React 19 `JSX` namespace removed:**

```
> pnpm run build
> next build

Type error: Cannot find namespace 'JSX'.

  app/admin/ab-preview/page.tsx:42:35
  components/omnisight/ab/batch-eligibility-panel.tsx:118:14
  components/omnisight/ab/batch-progress-panel.tsx:204:14
  components/omnisight/ab/cost-dashboard-panel.tsx:67:14
  components/omnisight/ab/provider-mode-wizard.tsx:88:14
```

**5.1.2 — `templates/_shared/` target drift:**

```
templates/_shared/auth-dashboard/index.ts(82,8)
  error TS5097: An import path can only end with a '.ts' extension
  when 'allowImportingTsExtensions' is enabled.

templates/_shared/bot-challenge/index.ts(565..661)  [22 hits]
  error TS2737: BigInt literals are not available when targeting
  lower than ES2020.
```

The build aborts; no images get pushed; deploy never advances past
Step 2. (Step 1 / 1b passed, so backups exist and code is checked
out — re-running after the fix is safe.)

### 5.2 Root cause

Two independent issues that surfaced on the same deploy:

1. **React 19 removed the global `JSX` namespace.** FX.6.6 regenerated
   `pnpm-lock.yaml` and pulled `@types/react` from 18.x → 19.x within
   the existing caret range. React 19's `@types/react` no longer
   exposes `JSX` as a global — it lives under `React.JSX`, or must be
   explicitly imported (`import type { JSX } from "react"`). Five
   `.tsx` files used the legacy global form (e.g. `function X():
   JSX.Element { ... }`) and broke at type-check.
2. **`templates/_shared/` is emit-only TS that targets generated apps,
   not the productizer's own Next.js build.** `templates/_shared/`
   holds TS source that gets emitted *into* generated app workspaces
   (the productizer is a code-generator; the templates are the
   product's outputs, not its consumers). They use newer TS features
   (BigInt literals → ES2020, `.ts` extension imports → TS 5.x flag)
   that the productizer's own `tsconfig.json` doesn't enable
   (target=ES6 to match Next.js 15's runtime). Pre-fix, `tsc` walked
   into `templates/_shared/` and gate-failed on every BigInt /
   `.ts`-extension hit — none of which the productizer build itself
   actually consumes.

### 5.3 Recovery (right-now)

**Mode 5.1.1 (`Cannot find namespace 'JSX'`):**

```bash
# For each file in the error list, extend the existing react import:
# Before:
#     import { useState } from "react"
# After:
#     import { type JSX, useState } from "react"
#
# Or, if there's no existing react import, add a fresh:
#     import type { JSX } from "react"
#
# This is what 803d403d did across 5 files. Same fix applies to any
# new occurrence:

grep -rln "JSX\.Element\|: JSX\b" app/ components/ hooks/ lib/ \
    --include="*.ts" --include="*.tsx" \
    | while read f; do
        echo "$f — needs JSX import"
      done
```

**Mode 5.1.2 (`templates/_shared/` errors):**

```bash
# Confirm tsconfig.json has the templates exclude (FX 9676d17e fix):
grep -n '"templates"' tsconfig.json
# expect a line like:
#     "exclude": [..., "templates/**/*", ...]

# If missing, add it:
# In tsconfig.json, extend the existing "exclude" array (which already
# lists "configs/skills/*/scaffolds" by precedent — the templates
# convention mirrors that pattern).
```

After either fix:

```bash
# Validate locally before re-deploying
pnpm run build
# expect: "Compiled successfully" + no TS errors.
# If green, push + redeploy.
git add app/ components/ tsconfig.json   # whichever files changed
git commit -S -m "fix(frontend): <describe the fix>"
git push origin master
./scripts/deploy-prod.sh
```

### 5.4 Permanent fix (landed)

- **`803d403d`** — `import { type JSX } from 'react'` added to 5 files
  for React 19 compatibility (unblocks `pnpm run build` in
  `Dockerfile.frontend`). Likely re-surfaces if React types bump
  again and the new version makes a similar global removal.
- **`9676d17e`** — `tsconfig.json` excludes `templates/**/*` so the
  productizer `tsc` doesn't gate-fail on emit-only generated-app
  source.

### 5.5 Prevention

```bash
# 30-second preflight: run the frontend build locally before deploy.
pnpm run build
# expect: "Compiled successfully" + 0 type errors.
```

If you're upgrading React / @types/react:

```bash
# Audit for the JSX-global pattern BEFORE the lockfile bump
grep -rln "JSX\.Element\|: JSX\b" app/ components/ hooks/ lib/ \
    --include="*.ts" --include="*.tsx" \
    | wc -l
# expect: 0 (post-803d403d). Any non-zero means audit before bumping.
```

If you add a new `templates/<flavor>/` directory:

```bash
# Confirm the productizer tsconfig already excludes it (the glob
# "templates/**/*" should cover it, but verify if you used a
# non-standard layout):
pnpm run build
# expect: no template-related errors.
```

---

## 6. Drift-guard inventory

These tests / hooks fail the build (or `--check` mode) when the
permanent fix for a gate regresses. Run any of them ad-hoc with
`pytest -q <path>`.

| Gate | Drift guard | Path |
|------|-------------|------|
| 1 (GPG) | `test_release_signer_setup_drift_guard.py` (7 tests) | `backend/tests/test_release_signer_setup_drift_guard.py` |
| 2 (passphrase) | *(none — operator dotfile, off-repo)* | run §2.5 preflight |
| 3 (DLP existence) | `test_backup_dlp_existence_check_drift_guard.py` (10 tests) | `backend/tests/test_backup_dlp_existence_check_drift_guard.py` |
| 3 (DLP scan correctness) | `test_backup_dlp_scan.py` | `backend/tests/test_backup_dlp_scan.py` |
| 4 (alembic single head) | pre-commit hook | `FX.7.6` alembic-enforcement hook (commit `649746f0`) |
| 4 (alembic 0052) | `test_alembic_0052_catalog_seed.py` (24/24) | `backend/tests/test_alembic_0052_catalog_seed.py` |
| 5 (frontend type-check) | `pnpm run build` | local + Dockerfile.frontend build stage |

For Gates 2 and 5, see the preflight commands in §7.

---

## 7. Pre-deploy preflight checklist (operator copy/paste)

Run this **before** invoking `scripts/deploy-prod.sh`. Total runtime
on the prod WSL host: ~30 seconds when all gates pass.

```bash
#!/usr/bin/env bash
# OmniSight prod deploy preflight — covers FX.9.11 5 gates.
# Run from the repo root on the prod host.
set -e

echo "=== Gate 1: GPG signer ==="
./scripts/check_deploy_ref.sh --kind branch --ref master
# expect both ✅ Layer 1 and ✅ Layer 2

echo "=== Gate 2: Backup passphrase in non-interactive shell ==="
env -i HOME="$HOME" PATH="$PATH" bash -lc \
    'echo passphrase_present=${OMNISIGHT_BACKUP_PASSPHRASE:+yes} len=${#OMNISIGHT_BACKUP_PASSPHRASE}'
# expect: passphrase_present=yes len=44

echo "=== Gate 3: DLP scanner sanity ==="
test -x scripts/backup_dlp_scan.py || {
    echo "FAIL: scripts/backup_dlp_scan.py missing or not executable"
    exit 1
}
# Optional deeper check (~5s): dry-run the backup helper's DLP path.
# scripts/backup_prod_db.sh --label preflight --dry-run

echo "=== Gate 4: Alembic single-head invariant ==="
HEADS=$(docker compose -f docker-compose.prod.yml run --rm --no-deps \
    -e PYTHONSAFEPATH=1 -w /app/backend \
    backend-a python -m alembic heads 2>/dev/null | grep -c "(head)")
test "$HEADS" -eq 1 || {
    echo "FAIL: $HEADS alembic heads (expected 1)"
    exit 1
}

# Defence in depth: stdlib platform shadow gone
test ! -f backend/platform.py || {
    echo "FAIL: backend/platform.py exists (FX.9.3 reverted?)"
    exit 1
}
test -f backend/platform_profile.py || {
    echo "FAIL: backend/platform_profile.py missing (FX.9.3 incomplete?)"
    exit 1
}

# Defence in depth: no exec_driver_sql in migrations
grep -rln "exec_driver_sql" backend/alembic/versions/ && {
    echo "FAIL: exec_driver_sql found in a migration (FX.9.1 pattern violated)"
    exit 1
}

echo "=== Gate 5: Frontend builds ==="
pnpm run build
# expect: "Compiled successfully"

echo ""
echo "✅ All 5 gates green — safe to run scripts/deploy-prod.sh"
```

Save this as `scripts/deploy_preflight.sh` (an opt-in helper — not
auto-invoked by `deploy-prod.sh` itself, because some checks need
docker container build to be current and we don't want preflight to
shadow the canonical script). It's a debugging aid, not a gate.

---

## 8. References

- **SOP §"Production Readiness Gate"** — `docs/sop/implement_phase_step.md`
  L136-263. The original framing of "dev-green ≠ prod-ready"; this
  runbook is the executable counterpart.
- **GPG release-signer setup** — `docs/runbook/gpg-release-signer-setup.md`
  (FX.9.8). First-time / rotation / revocation / cold-spare for the
  Gate 1 fix.
- **Deploy-prod script** — `scripts/deploy-prod.sh`. Step 1 / 1b /
  2 / 2.5 / 3 / 4 / 5 are the abort points referenced throughout
  this runbook.
- **Backup helper** — `scripts/backup_prod_db.sh` + `scripts/backup_dlp_scan.py`.
  The Gate 2 / Gate 3 surface area.
- **Alembic env** — `backend/alembic/env.py`,
  `backend/alembic_pg_compat.py`, `backend/alembic/versions/0188_merge_heads.py`.
  The Gate 4 surface area.
- **Frontend tsconfig** — `tsconfig.json` (templates exclude),
  `package.json` (React 19 pin). The Gate 5 surface area.
- **Earlier post-mortem** — `docs/ops/deploy_postmortem_2026-04-19.md`
  (the *first* prod bootstrap; covers a different set of gates —
  Caddy / SQLite WAL / Next.js standalone / busybox wget — and is
  worth a re-read before any future first-time bootstrap on a fresh
  host). FX.9.11 covers the *second* deploy's gates; together the
  two docs span the operator-relevant production readiness surface.
- **Master TODO** — `TODO.md` Priority FX.9 row (post-deploy
  follow-ups; this runbook is row FX.9.11 itself).
