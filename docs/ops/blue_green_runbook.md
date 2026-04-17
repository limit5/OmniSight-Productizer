# Blue-Green Deploy & Rollback Runbook (G3 / HA-03)

> Operator-facing runbook for the OmniSight blue-green production
> ceremony delivered by TODO rows 1353–1357. Pairs with
> [`docs/ops/dependency_upgrade_runbook.md`](dependency_upgrade_runbook.md)
> (N6 — when blue-green is *required* by the upgrade tier) and
> [`docs/ops/upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md)
> (N10 — how the gate refuses prod deploys without a recorded ceremony).

This runbook is the **canonical recipe** an oncall reads at 3am when:

1. A planned blue-green cutover is about to start (§3 / §4).
2. A cutover just degraded inside its 5-minute observation window (§5).
3. A cutover landed clean but a regression surfaced inside the 24-hour
   warm-standby window (§5).
4. Something looks wrong with the state directory and they need to read
   the breadcrumbs without breaking anything (§6 / §8).

Everything below is *script-backed* — every command shown is an exact
copy from `scripts/deploy.sh` / `scripts/bluegreen_switch.sh` /
`scripts/prod_smoke_test.py`. If a step here disagrees with a script,
the script wins; the contract tests in `backend/tests/test_blue_green_runbook.py`
catch drift.

---

## 1. Why blue-green (and when it is *not* in scope)

OmniSight runs two production replicas (`backend-a:8000`,
`backend-b:8001`) behind Caddy on `:443`. The dual-replica topology
already supports **rolling restarts** (G2 / HA-02) for routine same-version
redeploys. Blue-green sits *on top* of that and is reserved for:

| Trigger | Why blue-green not rolling |
|---|---|
| Major dependency bump (Python / Node / FastAPI / SQLAlchemy major) | Need full DAG smoke on the new code before any user request hits it |
| Schema migration that is not backward-compatible across replicas | Rolling would have a window where one replica is on the new schema and one on the old |
| New container base image (Debian / Alpine major) | OS-level regressions are easier to spot on a single warm-standby than mid-rolling |
| Any PR labelled `deploy/blue-green-required` (N10 gate) | The gate refuses the prod deploy without a recorded ceremony |

For everything else (patch dependency bumps, app-only code change, doc
fixes, hotfixes that don't touch deps) prefer **rolling**:

```bash
scripts/deploy.sh --strategy rolling prod <git-ref>
```

Rolling is faster (≈90 s end-to-end vs. ≈8 min for blue-green) and has
its own runbook coverage in `scripts/deploy.sh` headers + the
G2 `test_deploy_sh_rolling.py` contract.

---

## 2. The five files that *are* blue-green state

`deploy/blue-green/` is the entire load-bearing state. Read-only inspection
is always safe:

```bash
ls -la deploy/blue-green/
scripts/bluegreen_switch.sh status
```

| File | Type | Owner | Meaning |
|---|---|---|---|
| `active_color` | plain text | written atomically by `bluegreen_switch.sh` | source-of-record for which color (`blue`/`green`) is currently serving |
| `active_upstream.caddy` | symlink → `upstream-<color>.caddy` | flipped atomically by `bluegreen_switch.sh` | THE cutover artifact — Caddy reads this; `rename(2)` of this symlink IS the traffic flip |
| `upstream-blue.caddy`, `upstream-green.caddy` | plain text snippets | committed | Caddy upstream blocks, named symmetrically so the symlink can swing without Caddyfile edits |
| `previous_color` | plain text | written by `bluegreen_switch.sh` *before* every flip | breadcrumb that lets `--rollback` know which color to flip back to |
| `cutover_timestamp` | Unix seconds | written by `deploy.sh --strategy blue-green` | when the most recent cutover happened (for the runbook's "was this the suspect deploy?" question) |
| `previous_retention_until` | Unix seconds | written by `deploy.sh --strategy blue-green` | `cutover_timestamp + 24h` — past this point the old color may be pruned, and `--rollback` will refuse with `exit 8` |
| `rollback_timestamp` | Unix seconds | written by `deploy.sh --rollback` | when the most recent rollback happened (mtime + content for timeline reconstruction) |

**Never edit any of these by hand.** `bluegreen_switch.sh` and
`deploy.sh` are the only writers; both use the `tmp.$$` + `mv -f` pattern
so a concurrent `cat`/`readlink` always sees a consistent file.

---

## 3. Pre-flight (before you run any cutover command)

Run these in order. Stop at the first failure and fix root cause —
do **not** push through with `OMNISIGHT_*_FORCE` flags unless the
trade-off is documented in the deploy ticket.

### 3.1 N10 gate ack

If the PR was labelled `deploy/blue-green-required`, confirm the
[blue-green ledger](upgrade_rollback_ledger.md) has an entry for
the *previous* ceremony. The deploy script will refuse on `exit 2`
otherwise (gate logic in `scripts/check_bluegreen_gate.py`).

### 3.2 State directory shape

```bash
scripts/bluegreen_switch.sh status
```

Expected output (the exact shape is contract-locked by
`test_bluegreen_atomic_switch.py::TestStatusCommand`):

```
active=blue
standby=green
symlink_target=upstream-blue.caddy
symlink_color=blue
previous=(none)        # OR a color, if a prior cutover happened
```

| Symptom | Cause | Fix |
|---|---|---|
| `state/symlink mismatch` warning to stderr | A prior ceremony crashed between step 2 (symlink flip) and step 3 (state file write) | Re-run `scripts/bluegreen_switch.sh set-active <color-the-symlink-points-at>` to reconcile — both files end up consistent and idempotent |
| `no active_color state` (exit 2) | State dir uninitialised | `cd deploy/blue-green && echo blue > active_color && ln -s upstream-blue.caddy active_upstream.caddy` then re-status |
| `target snippet 'upstream-XXX.caddy' missing` (exit 3) | Someone deleted the snippet | Restore from `git show HEAD:deploy/blue-green/upstream-<color>.caddy > deploy/blue-green/upstream-<color>.caddy` |

### 3.3 Both replicas alive

Blue-green keeps the *outgoing* color warm for 24 h, but it cannot
flip onto a dead one. Verify both Caddy upstreams answer `/readyz`:

```bash
curl -sf http://localhost:8000/readyz && echo "blue (backend-a) OK"
curl -sf http://localhost:8001/readyz && echo "green (backend-b) OK"
```

If one is already down, do **not** start a cutover — investigate the
dead replica first (`docker compose -f docker-compose.prod.yml logs --tail=200 backend-{a,b}`)
and bring it back to a baseline-symmetric state before continuing.

### 3.4 Image pulled / built

```bash
docker compose -f docker-compose.prod.yml pull
```

`scripts/deploy.sh --strategy blue-green` will `docker compose up -d
--no-deps --force-recreate backend-<standby>` itself, but doing the
`pull` separately means a registry outage surfaces *before* you start
the ceremony (rather than mid-cutover).

### 3.5 Operator hot-keys

Open a second terminal and pre-load the rollback command — at 3am you
do not want to type it from memory:

```bash
# tab 2 — rollback hotkey, ready to fire
echo "scripts/deploy.sh --rollback"
```

---

## 4. Cutover ceremony (planned forward deploy)

```bash
scripts/deploy.sh --strategy blue-green prod <git-ref>
```

What the script does, step by step (anchored in `scripts/deploy.sh`
lines 477–705):

1. **Resolve colors.** Parses `bluegreen_switch.sh status` → `active`
   (currently serving), `standby` (about to be re-imaged). The
   color → service → port map is fixed:
   * `blue` ↔ `backend-a` ↔ host port `8000`
   * `green` ↔ `backend-b` ↔ host port `8001`
2. **Re-create standby container** with the new image
   (`docker compose up -d --no-deps --force-recreate backend-<standby>`).
   `--no-deps` means the active replica + frontend are untouched; users
   are still on the old color.
3. **Wait for standby `/readyz`** on its host port. Default timeout
   `OMNISIGHT_BLUEGREEN_STANDBY_READY_TIMEOUT=120s` (inherits from
   `OMNISIGHT_ROLL_READY_TIMEOUT`). If it never returns 200, the script
   exits **3** *before* any traffic flip — the active color is unaffected.
4. **Pre-cut smoke** on standby:
   `timeout 300 python3 scripts/prod_smoke_test.py http://localhost:<standby_port>`.
   This is the full DAG smoke aimed *directly* at the standby container
   (bypassing Caddy) so a regression in the new code is caught while
   100% of traffic is still on the old color. If smoke fails, the script
   exits **6** and the standby container is left running for triage.
   * Bypass (DEV ONLY): `OMNISIGHT_BLUEGREEN_SKIP_SMOKE=1` — DANGEROUS,
     prints a warning. Production cutovers must always run smoke.
5. **Atomic cutover**:
   `scripts/bluegreen_switch.sh set-active <standby>`. This is the
   `rename(2)` flip on `active_upstream.caddy` — instant, atomic, no
   half-state.
6. **Caddy reload** if `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD` is set
   (e.g. `docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile`).
   Empty default → operator must reload manually; the script prints
   the hint.
7. **Retention breadcrumbs** — atomically writes `cutover_timestamp` and
   `previous_retention_until` (= cutover + 24 h, configurable via
   `OMNISIGHT_BLUEGREEN_RETENTION_HOURS`).
8. **5-minute observation window**: polls `http://localhost:<new-active-port>/readyz`
   every `OMNISIGHT_BLUEGREEN_OBSERVE_INTERVAL=15s` for
   `OMNISIGHT_BLUEGREEN_OBSERVE_SECONDS=300s`. ≥ `OMNISIGHT_BLUEGREEN_OBSERVE_MAX_FAILURES=3`
   consecutive failures → exit **7** with a "run `scripts/deploy.sh --rollback`"
   hint.
9. **24 h warm standby**: the OLD color's container is intentionally
   *never stopped*. It keeps passing `/readyz` so a `--rollback` is a
   single `rename(2)` away.

### 4.1 Exit codes (`--strategy blue-green`)

| Exit | Meaning | Cutover happened? | Operator action |
|---|---|---|---|
| **0** | ceremony passed | yes | celebrate; new active is `<standby>` |
| **3** | standby `/readyz` never came up | no | check `docker compose logs backend-<standby>`; fix image/env; re-run |
| **4** | `docker-compose.prod.yml` missing | no | `git checkout` the file; should never happen on a healthy host |
| **5** | blue-green primitive missing (state dir / `bluegreen_switch.sh`) | no | initialise state dir per §3.2 |
| **6** | pre-cut smoke FAILED | no | check `scripts/prod_smoke_test.py` output; standby container left running for triage |
| **7** | 5-min observation window detected degradation | **yes** — symlink already flipped | run `scripts/deploy.sh --rollback` immediately (§5) |

### 4.2 Dry run (planning a ceremony without touching anything)

```bash
OMNISIGHT_BLUEGREEN_DRY_RUN=1 scripts/deploy.sh --strategy blue-green prod <git-ref>
```

Prints the resolved active/standby colors + plan, exits 0 before any
docker/symlink mutation. Safe to run anytime — useful for change-review
sign-off ("the ceremony will recreate `backend-b`, then flip blue→green").

---

## 5. Rollback ceremony (秒級 fail-back)

Two scenarios trigger this:

* **Inside 5-min observation window** (`exit 7` from §4): the script
  itself tells you to run rollback.
* **Inside 24 h retention window**: a regression surfaces in metrics /
  Sentry / user reports after the observation window passed cleanly.

Either way the command is identical:

```bash
scripts/deploy.sh --rollback
```

No env arg required (3am-friendly). What it does (anchored in
`scripts/deploy.sh` lines 159–299):

1. **Gate (a)** — primitive present? (state dir + `bluegreen_switch.sh`).
   Otherwise exit **5**.
2. **Gate (b)** — `previous_color` breadcrumb exists? Otherwise exit
   **2** (no prior cutover ⇒ nothing to roll back to).
3. **No-op guard** — `active_color == previous_color` ⇒ exit 0
   (defends against double-rollback ping-pong).
4. **Gate (c)** — retention window still open
   (`now ≤ previous_retention_until`)? Otherwise exit **8**.
   Bypass: `OMNISIGHT_ROLLBACK_FORCE=1` (DANGEROUS — only after you
   manually verified the previous color's container is still alive).
5. **Dry-run exit point** — `OMNISIGHT_BLUEGREEN_DRY_RUN=1` exits 0
   here, before any symlink touch.
6. **Gate (d)** — previous color's `/readyz` returns 200?
   Otherwise exit **3**. Bypass: `OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1`
   (DANGEROUS — only if you are about to `docker compose up backend-<prev>`
   manually right after).
7. **Atomic flip** via `bluegreen_switch.sh rollback` — `rename(2)` on
   the symlink, identical primitive as cutover.
8. **Caddy reload** if `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD` is set.
9. **Audit breadcrumb** `rollback_timestamp` (Unix seconds, atomic write).

### 5.1 Exit codes (`--rollback`)

| Exit | Meaning | Symlink flipped? | Operator action |
|---|---|---|---|
| **0** | rollback complete (or no-op / dry-run) | yes (or N/A for no-op/dry-run) | reload Caddy if you didn't set `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD`; investigate the failed forward deploy from logs |
| **2** | no `previous_color` recorded | no | nothing to roll back; this host has never had a cutover |
| **3** | previous color's `/readyz` is dead | no | `docker compose logs backend-<prev>`; either fix + retry, or `OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1` + recreate manually |
| **5** | blue-green primitive missing | no | initialise state dir per §3.2 |
| **8** | 24 h retention window expired | no | confirm previous color is still alive (`curl http://localhost:<prev_port>/readyz`); if yes, `OMNISIGHT_ROLLBACK_FORCE=1`; if no, you must do a *forward* deploy of the older tag instead |

### 5.2 What rollback explicitly does NOT do

* **No `git fetch` / `git checkout` / `pip install` / `pnpm build` /
  `docker compose up` / `docker compose stop` / `systemctl restart`**.
  The whole point of the 24 h warm-standby is that rollback is a single
  `rename(2)` — measured in seconds. Contract pinned by
  `test_deploy_sh_rollback.py::TestNoBuildContract`.
* **No N10 blue-green gate**. The gate is for cut-FORWARD; rollback
  bypasses it on purpose.
* **No additional observation window**. Rollback restores a version
  that was already serving traffic immediately before the cutover —
  it's known-good. The `/readyz` preflight (§5.1 gate (d)) is the only
  health check needed.
* **No second-level rollback**. There is exactly *one* `previous_color`
  slot. Two rollbacks in a row = no-op (exit 0). To go further back,
  do a forward deploy (`scripts/deploy.sh --strategy blue-green prod <older-tag>`).

---

## 6. Post-cutover hygiene (next 24 h)

| When | Owner | Action |
|---|---|---|
| Immediately after cutover | oncall | Verify `scripts/bluegreen_switch.sh status` shows the expected new active; tail Caddy logs for 5xx; check Sentry for new error fingerprints |
| T+1h | oncall | Confirm metrics still nominal (`/api/v1/health` p99 < 1 s, no `database is locked` spikes) |
| T+24h | oncall | Read `previous_retention_until`; if `now > that`, the old color is now eligible for prune. Either let cron prune (future), or `docker compose -f docker-compose.prod.yml stop backend-<old>` manually |
| Anytime within 24h | author | If user reports a regression that maps to the new code, run `scripts/deploy.sh --rollback` (no need to wait for an oncall page) |

To inspect the retention window from the shell:

```bash
PREV_RET="$(cat deploy/blue-green/previous_retention_until)"
date -u -d "@$PREV_RET" '+%Y-%m-%dT%H:%M:%SZ'
echo "rollback budget: $((PREV_RET - $(date +%s))) seconds remaining"
```

---

## 7. Manual primitive reference

`scripts/bluegreen_switch.sh` is the lowest-level primitive — `deploy.sh`
just orchestrates around it. For ad-hoc operations (e.g. testing in
staging, or recovering a corrupted state dir) the four subcommands are:

```bash
scripts/bluegreen_switch.sh status                  # read-only state dump
scripts/bluegreen_switch.sh switch                  # toggle (blue↔green)
scripts/bluegreen_switch.sh set-active blue         # explicit, idempotent
scripts/bluegreen_switch.sh set-active green        # explicit, idempotent
scripts/bluegreen_switch.sh rollback                # flip back to previous_color
```

Exit codes (per `scripts/bluegreen_switch.sh` header):

| Exit | Meaning |
|---|---|
| 0 | success |
| 1 | usage / validation error (operator fixable) |
| 2 | state inconsistency (missing state file) |
| 3 | I/O failure (e.g. symlink creation failed) |

Sandboxing for testing — both contract tests and operators can point
the script at an alternative state dir without touching the committed
`deploy/blue-green/`:

```bash
OMNISIGHT_BLUEGREEN_DIR=/tmp/bg-sandbox scripts/bluegreen_switch.sh status
```

---

## 8. Troubleshooting decision tree

```
deploy.sh --strategy blue-green prod <ref>  →  exit ?
│
├── 0      done. monitor for 24h. retention auto-expires.
├── 3      standby never came up. NO cutover. logs: backend-<standby>.
│           → fix image/env, retry the deploy.
├── 4      compose file missing. recover from git.
├── 5      state dir / primitive missing. initialise per §3.2.
├── 6      pre-cut smoke failed. NO cutover. logs: backend-<standby>.
│           → fix code/data, retry; do NOT bypass smoke in prod.
└── 7      observation window degraded. CUTOVER HAPPENED.
            → IMMEDIATELY: scripts/deploy.sh --rollback
            → analyse logs/metrics from new active (now to be reverted).

scripts/deploy.sh --rollback  →  exit ?
│
├── 0      done. previous color is now active. old color (the failed one)
│           is still running as the new standby — investigate before
│           any future cutover.
├── 2      no previous_color. nothing to roll back. (first-ever deploy?)
├── 3      previous color's /readyz dead. CHOOSE:
│           a) docker compose up backend-<prev>; retry --rollback.
│           b) OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1 + manual recreate.
├── 5      state dir / primitive missing. initialise per §3.2.
└── 8      retention window expired (>24h). CHOOSE:
            a) confirm previous color is alive → OMNISIGHT_ROLLBACK_FORCE=1.
            b) it's pruned → forward deploy older tag instead:
               scripts/deploy.sh --strategy blue-green prod <older-tag>.
```

---

## 9. Tunables (env vars) cheat-sheet

All defaults are production-safe. Override only with a documented reason
(in the deploy ticket or PR).

### Cutover (`--strategy blue-green`)

| Variable | Default | Effect |
|---|---|---|
| `OMNISIGHT_BLUEGREEN_SMOKE_TIMEOUT` | `300` | seconds for `prod_smoke_test.py` to finish |
| `OMNISIGHT_BLUEGREEN_OBSERVE_SECONDS` | `300` | length of post-cutover observation window |
| `OMNISIGHT_BLUEGREEN_OBSERVE_INTERVAL` | `15` | seconds between `/readyz` polls in the window |
| `OMNISIGHT_BLUEGREEN_OBSERVE_MAX_FAILURES` | `3` | consecutive `/readyz` failures → exit 7 |
| `OMNISIGHT_BLUEGREEN_RETENTION_HOURS` | `24` | how long the old color is kept warm for `--rollback` |
| `OMNISIGHT_BLUEGREEN_STANDBY_READY_TIMEOUT` | `120` (inherits `OMNISIGHT_ROLL_READY_TIMEOUT`) | seconds to wait for standby `/readyz` |
| `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD` | `""` | shell command to reload Caddy after symlink flip |
| `OMNISIGHT_BLUEGREEN_DRY_RUN` | `0` | `1` → print plan, exit 0 before any mutation |
| `OMNISIGHT_BLUEGREEN_SKIP_SMOKE` | `0` | `1` → DANGEROUS, skip pre-cut smoke (dev only) |

### Rollback (`--rollback`)

| Variable | Default | Effect |
|---|---|---|
| `OMNISIGHT_BLUEGREEN_DRY_RUN` | `0` | `1` → print plan, exit 0 before symlink flip |
| `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD` | `""` | reload Caddy after rollback |
| `OMNISIGHT_ROLLBACK_FORCE` | `0` | `1` → DANGEROUS, bypass retention window check |
| `OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT` | `0` | `1` → DANGEROUS, bypass `/readyz` check |
| `OMNISIGHT_BLUEGREEN_DIR` | `deploy/blue-green` | redirect state dir (used by contract tests) |

### Shared

| Variable | Default | Effect |
|---|---|---|
| `OMNISIGHT_COMPOSE_FILE` | `docker-compose.prod.yml` | compose file passed to `docker compose -f` |
| `OMNISIGHT_CHECK_BLUEGREEN` | `1` | `0` → bypass N10 gate (documented escape hatch) |

---

## 10. Script & contract index

Every script the runbook references, with the contract test that
guards it. Ship a fix to any of these → re-run the matching test:

| Layer | Script | Contract test | Approx test count |
|---|---|---|---|
| Atomic primitive | `scripts/bluegreen_switch.sh` | `backend/tests/test_bluegreen_atomic_switch.py` | 32 |
| Flag parser | `scripts/deploy.sh` (flags) | `backend/tests/test_deploy_sh_blue_green_flag.py` | 24 |
| Cutover ceremony | `scripts/deploy.sh` (`--strategy blue-green`) | `backend/tests/test_bluegreen_precut_ceremony.py` | 29 |
| Rollback fast-path | `scripts/deploy.sh` (`--rollback`) | `backend/tests/test_deploy_sh_rollback.py` | 40 |
| This runbook | `docs/ops/blue_green_runbook.md` | `backend/tests/test_blue_green_runbook.py` | this row |
| N10 gate | `scripts/check_bluegreen_gate.py` | `backend/tests/test_check_bluegreen_gate.py` | (N10) |
| Pre-cut smoke | `scripts/prod_smoke_test.py` | `backend/tests/test_prod_smoke_test.py` | (G1) |
| Reverse proxy | `deploy/reverse-proxy/Caddyfile` | `backend/tests/test_reverse_proxy_caddyfile.py` | 24 |

---

## 11. Anti-patterns — things this runbook will not tell you to do

* **Editing `deploy/blue-green/active_upstream.caddy` by hand.** The
  symlink + `active_color` mirror are kept in sync only by the
  `bluegreen_switch.sh` primitive's tmp-then-rename ordering. A manual
  `ln -sfn` introduces a 2-syscall window where readers see neither
  target.
* **Stopping the old color's container before 24 h elapse.** That throws
  away the rollback safety net and turns `--rollback` into "exit 3,
  upstream dead, please recreate by hand at 3am".
* **Using `OMNISIGHT_BLUEGREEN_SKIP_SMOKE=1` in production.** The flag
  exists for local dev against fixtures that can't run the full DAG
  smoke; bypassing in prod defeats the entire reason blue-green is
  preferred over rolling for risky changes.
* **Running both `OMNISIGHT_ROLLBACK_FORCE=1` *and*
  `OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1` at the same time without
  manually `curl`-ing the previous color first.** Each flag prints a
  DANGEROUS warning individually; together they give you no preflight
  *and* no retention check, so the symlink can flip to a dead upstream
  with zero validation.
* **Reading `cutover_timestamp` / `rollback_timestamp` while a write is
  in flight.** It's safe — both writers use `tmp.$$ + mv`, so a
  concurrent reader sees either the old value or the new value, never
  a half-written file. (Pattern pinned by the rollback test
  `test_rollback_timestamp_written_atomically`.)

---

## 12. Change-management checklist (paste into the deploy ticket)

```
[ ] Pre-flight: scripts/bluegreen_switch.sh status — both colors healthy
[ ] Pre-flight: curl /readyz on :8000 and :8001 both return 200
[ ] Pre-flight: docker compose pull (image present)
[ ] Pre-flight: tab 2 has `scripts/deploy.sh --rollback` ready
[ ] Cutover:    scripts/deploy.sh --strategy blue-green prod <ref>
[ ] Cutover:    confirm exit 0 OR follow §8 troubleshooting tree
[ ] Post:       Caddy logs / Sentry clean for 5 min after window pass
[ ] Post:       T+24h decision: prune old color OR extend retention
[ ] Post:       N10 ledger entry recorded (docs/ops/upgrade_rollback_ledger.md)
```
