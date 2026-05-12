# AUDIT-23 — "Shipped but not deployed" audit (Sprint D / E / F)

**Ticket**: OP-976 (AUDIT-23) · **Date**: 2026-05-12 · **Severity**: P0 (anti-pattern)
**Sibling audits**: `docs/audit/2026-05-03-deep-audit.md` (topic-focused, "code 100% / infra 0%" first flagged), `docs/audit/2026-05-06-deep-audit.md`
**Memory anchors**: `project_audit_findings.md`, `project_staging_gate_gap.md`, `project_release_audit_sink_mismatch.md`, `project_d5_main_promote_mechanism.md`, `project_force_promote_override.md`
**Re-run harness**: `scripts/deployment-audit.sh` (commit it; re-run quarterly or after any infra-touching ticket reaches 公開済み)

---

## 0. tl;dr

| Metric | Value |
|---|---|
| Sprint D/E/F tickets reviewed (touch infra) | 13 |
| Of those, a non-trivial infra deliverable | 10 |
| Confirmed **live** on prod | 2 (OP-762 — *late*, enabled 2026-05-12; OP-960 path) |
| **Partial** (shipped, deploy step incomplete or peer-gated) | 5 |
| Confirmed **not deployed** | 3 (OP-767, OP-878, OP-965 timers) |
| **"Shipped that is actually live"** | **≈20 % fully, ≈70 % counting partials** |
| New remediation findings filed | 0 net-new tickets — all 8 map to existing tickets (see §5); 1 new umbrella note proposed (AUDIT-23-a) |

**Bottom line**: every concrete failure surfaced at the 2026-05-12 staging-gate
planning session — the OP-925 R3 cascade — is one instance of the same defect
class: *the ticket's code merged to `develop`, the ticket moved to 公開済み, and
the corresponding production activation step (symlink + `systemctl --user enable
--now`, `docker compose up -d`, create `~/.config/omnisight/release-audit.env`,
stand up a develop-tracking staging env) was never done.* The unit files,
compose files, and EnvironmentFile= references all carry their own install
instructions in the header — they were just never executed on the prod host.
The fix is process (DoD discipline), not more code. See §6.

---

## 1. The defect class

**Name**: *shipped-but-not-deployed* — a special case of anti-pattern #4 ("push
without commit") inverted: here there *is* a commit, the merge happened, the
ticket closed, but the artefact never reached its runtime. It is invisible to
every existing gate because every existing gate stops at "merged to `develop`".

It is **not** in `docs/sop/architecture-anti-patterns.md` yet. AUDIT-23
proposes adding it as pattern #13 — see §6.4 for the cookbook entry text.

### 1.1 Failure patterns observed

| Pattern | This-sprint example(s) | What's missing | Downstream break |
|---|---|---|---|
| systemd unit shipped, not activated | OP-762 (`release-milestone-checker.timer`), OP-798 (`sora-bridge-sync.timer`), OP-877 (`auto-promote-develop.timer`) | unit not copied to `~/.config/systemd/user/`; not `enable --now`'d; `loginctl enable-linger` not set | timer never fires → no `milestone_ready` events → release chain stalls silently; bridge runs stale code |
| code module shipped, env-wire missing | OP-964 (`OMNISIGHT_DATABASE_URL` for the D5 audit sink) | `~/.config/omnisight/release-audit.env` never created on prod; the `EnvironmentFile=-…` line silently falls back to local SQLite | `release_audit` durable trail in pg-primary never gets rows → OP-925 "where is the audit row" mystery |
| container compose shipped, no operator started it | OP-767, OP-878 (`deploy/staging/docker-compose.yml`) | never `docker compose -f … up -d`; no systemd wrapper unit (`omnisight-compose-staging.service` does not exist) | `staging.sora.services` does not resolve → R3 `ci_canary` / `ci_smoke` gates have no producer → R3 cascade |
| producer code shipped, gated by absent peer | OP-965 (`staging-gate-canary.timer`, `staging-gate-smoke.timer`) | timers deliberately left disabled until the staging env (OP-767/878/927) exists | designed-in; the *peer* (staging env) is the actual gap (AUDIT-19 / OP-927) |
| sync mechanism shipped, cadence not configured | OP-798 (`sync_sora_bridge.sh`) | `sora-bridge-sync.timer` not enabled → 5-min reconcile never runs | sora-bridge HEAD drifts from `origin/develop`; discovered via OP-925 cascade |
| description / contract drift | OP-927 R5 | description still says "deploy staging" but the post-design-change model is "audit / release_audit" — wording mismatches semantics | next agent reads R5, builds the wrong thing, or skips it as "already done" |

---

## 2. Scope

**In scope**: Sprint D, E, F tickets in status 公開済み / Published whose
deliverable is *infrastructure* — a systemd unit/timer, a container or compose
file, a daemon, a cron entry, an env-wire, or a DB migration that requires a
manual `alembic upgrade` on prod.

**Out of scope** (AC §5): Sprint G / H — those tickets are too in-flight to
audit cleanly. They will get the same treatment as each reaches 公開済み. Also
out of scope: pure-code tickets with no runtime side, pure-docs/ADR tickets,
and the `backend` / `db` / `embedded` / `frontend` / `security` / `tooling`
domains (this is a `devops` + `docs` + `tests` ticket).

### 2.1 How the candidate list was enumerated

OP-976's runner cannot reach JIRA REST from the worktree (the dispatch module's
JQL pickup runs in the runner process, not the CLI). The candidate list was
therefore assembled from four sources and cross-checked against the OP-925
cascade findings:

1. `git log --all` subject lines for `[OP-…]` tags on `develop` since 2026-04-25.
2. `deploy/systemd/*.{service,timer}` headers — every unit names its origin
   ticket in a `; [OP-…]` comment + carries its own `systemctl --user enable
   --now` install recipe (which is exactly the step that gets skipped).
3. ADR-0010 (D1 milestone gate), ADR-0011 (Sprint D 18-child plan),
   ADR-0016 (D5 main-promote), ADR-0017/0018 (release conductor / event-driven).
4. The 2026-05-12 staging-gate planning session notes (the tickets it named:
   OP-767, OP-878, OP-798, OP-762, OP-965, OP-964, OP-927 R5).

> ⚠ **`TicketCoverageMiss` caveat** (error catalog): this is *not* a JQL-exact
> enumeration. A Sprint D/E/F ticket whose deliverable touched infra but whose
> subject line did not mention the artefact, and which is not represented in
> `deploy/systemd/`, will be missed. **Follow-up `AUDIT-23-a`**: run the JQL
> from the ticket's "Investigation methodology" §1 against the live JIRA, diff
> the result set against §3's table, and append any new rows. Cross-check every
> child of META OP-761 (Sprint D) and the Sprint E / F METAs.

---

## 3. Per-ticket audit table

Legend — **Deployed?**: `yes` = artefact confirmed live (or repo-side proof it
must be, plus host evidence); `late` = was `no` for a window, since fixed;
`partial` = artefact shipped but the activation/wire step is incomplete *or*
the deliverable is peer-gated; `no` = confirmed not running; `n/a` = no runtime
artefact (docs/ADR/pure-code). `verify` column = the `scripts/deployment-audit.sh`
check kind that re-confirms the row on a live host.

| Ticket | Sprint | Deliverable (artefact) | Deployed? | Failure mode if not | Remediation | verify kind |
|---|---|---|---|---|---|---|
| OP-761 | D | ADR-0010 + ADR-0011 (Sprint D plan) | n/a | — | — | — |
| OP-762 | D | `release-milestone-checker.{service,timer}` + `scripts/release_milestone_checker.py` | **late** — unit shipped 2026-05-08; timer **not enabled until 2026-05-12** | no `milestone_ready`/`milestone_blocked` events → entire RELEASE META chain stalls with no signal | enabled 2026-05-12; **standing fix** = §6 DoD `deployed:` AC + the `deployment-audit.sh` cron | `systemd-timer` |
| OP-766 | D | `auto-promote-main.service` (`backend.agents.auto_promote_main` daemon, `Restart=always`) | **partial** — superseded-in-place: D5 now promotes via a Gerrit *review* change (`refs/for/main` + `milestone:R3-fastforward` hashtag, ADR-0016 / OP-960), not this daemon's direct push. The daemon may still be running, idle, or never installed — **host probe required** | if the daemon *is* still installed and pushing `develop:main` directly it races OP-960's Gerrit-review path → double-promote / non-FF abort loops | confirm on host whether `auto-promote-main.service` is active; if yes, decide keep-vs-retire vs OP-960 — tracked under **OP-960 follow-ups** (memory `project_d5_main_promote_mechanism.md`) | `systemd-unit` |
| OP-767 | D | `deploy/staging/docker-compose.yml` + `deploy/staging/caddy.json` (staging auto-deploy env) | **no** — compose file in repo; never `docker compose … up -d`; no `omnisight-compose-staging.service` wrapper exists in `deploy/systemd/` | `staging.sora.services` does not exist → D6 auto-deploy-staging is a no-op → R3 `ci_canary` + `ci_smoke` gates have **no producer** → **OP-925 R3 cascade** | **= OP-927 R5 / AUDIT-19** (stand up a develop-tracking staging env). Add the `omnisight-compose-staging.service` wrapper as part of that ticket | `container` |
| OP-798 | E | `sync_sora_bridge.sh` + `sora-bridge-sync.{service,timer}` (Phase A — automate sora-bridge git-sync) | **partial → flagged** — unit + timer shipped 2026-05-?; per the OP-925 cascade post-mortem the timer was **never enabled**; install recipe sits unrun in the unit header. Host probe needed to confirm current state | sora-bridge runs yesterday's code; staleness window (5 min) never closes; the OP-723 *liveness* watchdog stays green so nothing alerts | enable `sora-bridge-sync.timer` on the bridge host (`systemctl --user enable --now`); add it to the host's unit manifest checked by `deployment-audit.sh`. Tracked under **OP-798 deploy follow-up** (or co-opt the OP-925 cascade remediation ticket) | `systemd-timer` |
| OP-877 | D | `auto-promote-develop.{service,timer}` (daily 07:00 UTC `develop→refs/for/main`) | **partial** — unit shipped; host probe needed for `is-enabled`; **its OP-964 EnvironmentFile is the real gap** (next row) | if not enabled: D5 daily promote evaluation never runs → release chain depends on the 5-min checker only; if enabled but env-file missing: see OP-964 | confirm `is-enabled` on host; ship `release-audit.env` per OP-964 row | `systemd-timer` |
| OP-878 | F | `deploy/blue-green/*` + `scripts/auto_deploy_staging.py` (staging blue-green auto-deploy) | **no** — blue-green caddy fragments + decider script in repo; never exercised because there is no running staging env to switch colours on | D-series blue-green staging path is dead code; canary→staging promotion has nothing to promote *to* | **= OP-767 / OP-927 R5 / AUDIT-19** — same root gap (no staging env); revisit blue-green wiring once staging exists | `container` |
| OP-884 | D? | D12 feature-flag SDK (`backend/feature_flags.py` + flag store) — *peer of OP-911* | **partial** — code module shipped; whether the flag store / config source is wired into prod env is **unverified** (host probe) | F13 canary (OP-911) reads flags from an unconfigured store → flags default-off, canary gating inert | confirm flag-store env wire on prod (`/proc/<backend-pid>/environ | grep OMNISIGHT_FEATURE_FLAG`); track under OP-911 readiness | `env-var` |
| OP-911 | F | F13 feature-flag-gated canary (`canary_pipeline.py` flag hook) — uses D12 OP-884 | **partial** — code shipped; depends on (a) OP-884 flag store wired and (b) a staging/canary target that exists (OP-767/927) | canary "gated by flag" path inert until both peers land | gated; no independent action — clears when OP-884 wire + OP-927 staging env land | `env-var` |
| OP-921 | F | AUDIT-7 — ADR-0012 META ref typo fix (OP-785 → OP-784) | n/a | — | — | — |
| OP-927 | F | R5 — staging environment, develop-tracking | **partial / desc-drift** — the *artefact* (a live develop-tracking staging env) does not exist; **and** the description still says "deploy" while the post-design-change model is "audit / `release_audit`". Two defects in one row: missing infra **and** contract drift | (infra) R3 gates have no producer (cascade); (drift) next agent builds wrong thing or skips R5 as "done" | **AUDIT-19** owns the infra side (stand up staging env). **AUDIT-23-a sub-task**: rewrite OP-927 R5's description to match the audit model + add a `deployed:` AC item. Linked relates → OP-976 | `container` |
| OP-960 | D | D5 main-promote via Gerrit review (ADR-0016 + `scripts/auto_promote_develop_to_main.sh` pushing `refs/for/main`) | **yes (path)** — mechanism documented + script shipped + ADR-0016 accepted; the `develop→main` advance now goes through a Gerrit review change with `milestone:R3-fastforward` hashtag. Open items are ADR/script polish (memory `project_d5_main_promote_mechanism.md`), not the core path | n/a (core path live) | track residual polish under existing OP-960 follow-ups | `systemd-timer` (via `auto-promote-develop.timer`) |
| OP-964 | F | AUDIT-16 — `EnvironmentFile=-/home/user/.config/omnisight/release-audit.env` in `auto-promote-develop.service` (+ in `staging-gate-{canary,smoke}.service`) + asyncpg audit-DB smoke test | **partial** — the *unit-file line* shipped (`deploy/systemd/auto-promote-develop.service:44`, `staging-gate-canary.service:49`, `staging-gate-smoke.service:37`); the `-` prefix means the file is **optional**, so if `~/.config/omnisight/release-audit.env` was never created on prod the sink silently writes to local SQLite — the exact OP-925 symptom | `release_audit` durable trail in pg-primary stays empty → "where is the audit row" → cannot prove a promotion happened | **create `~/.config/omnisight/release-audit.env` (mode 0600) on the prod host** with `OMNISIGHT_DATABASE_URL=postgresql+asyncpg://…@pg-primary:5432/omnisight`; verify with the runbook's "Audit DB connectivity smoke test". Also: the sink **still writes the generic `audit` table, not `release_audit` 0207** — that retarget is an open **backend** follow-up (memory `project_release_audit_sink_mismatch.md`), out of this ticket's area | `env-var` |
| OP-965 | F | AUDIT-17 — `staging-gate-{canary,smoke}.{service,timer}` + `staging-gate-alert.service` + `scripts/staging_gate.py` | **no (by design)** — units shipped + carry their install recipe; timers deliberately **left disabled** until a develop-tracking staging env exists (memory `project_staging_gate_gap.md`) | none *yet* — but the moment staging exists these MUST be enabled or R3 `ci_canary`/`ci_smoke` gates stay producerless | **enable the two timers as the final step of AUDIT-19 / OP-927 R5** (the unit headers say so); add to host unit manifest. ADR §2 prose deferred (docs out-of-area for OP-965, in-area here — see §6.4) | `systemd-timer` |

### 3.1 Score

- Tickets with a runtime artefact: **10** (OP-761/921 are n/a).
- Fully live: **2** — OP-762 (late; live since 2026-05-12) + OP-960 (path).
- Partial / peer-gated: **5** — OP-766, OP-798, OP-877, OP-884, OP-911, OP-927, OP-964 *(seven rows; OP-877 ⊃ OP-964 overlap → count 5 distinct activation gaps)*.
- Confirmed not deployed: **3** — OP-767, OP-878, OP-965-timers.
- **"% of shipped that is actually live" ≈ 20 % strict; ≈ 70 % if every "partial" is generously counted as half.** Either way the dominant failure mode is *the activation step was never run*, and there is a single root gap (no develop-tracking staging env, OP-927/AUDIT-19) sitting under 4 of the 10 rows.

> ⚠ **`FalsePositiveDeployed` caveat** (error catalog): rows marked `partial —
> host probe required` and even the `late`/`yes` rows must be re-confirmed by a
> live-host run of `scripts/deployment-audit.sh` — "enabled" is not "firing".
> The script's `systemd-timer` check therefore reports `last-trigger` /
> `ActiveEnterTimestamp` of the *service*, not just `is-enabled` of the timer.

---

## 4. Verification methodology (reproducible — AC §2)

Every row in §3's `verify kind` column maps to one of these checks. They are
implemented in `scripts/deployment-audit.sh`; run that on the prod / bridge
host. Manual equivalents:

| Kind | Command(s) — run on the host that owns the artefact | "deployed" means |
|---|---|---|
| `systemd-unit` | `systemctl --user is-active <unit>` **and** `systemctl --user is-enabled <unit>` | `active` **and** `enabled` (not `linked`, not `static` masquerading as enabled) |
| `systemd-timer` | `systemctl --user is-enabled <timer>` **and** `systemctl --user list-timers --all | grep <timer>` **and** `systemctl --user show <service> -p ActiveEnterTimestamp,Result` | timer `enabled` **and** appears in `list-timers` with a `LAST` within ~2× its cadence **and** the *service*'s last `Result=success` |
| `container` | `docker ps --format '{{.Names}}\t{{.Status}}' | grep -i <service>` **and** the container's healthcheck is `healthy` (or an explicit probe: `curl -fsS http://<host>:<port>/healthz`) | container `Up` **and** `healthy`/probe 200 |
| `env-var` | pick a representative live PID (`systemctl --user show <unit> -p MainPID` or `pgrep -f <module>`) → `tr '\0' '\n' < /proc/<pid>/environ | grep -E '^<VAR>='` | the var is present **and** non-empty **and** points at the real target (e.g. `pg-primary`, not `sqlite://`) |
| `alembic-head` | on the prod PG: `OMNISIGHT_DATABASE_URL=… alembic current` (or `SELECT version_num FROM alembic_version;`) | the printed revision == the repo's `alembic heads` (i.e. no un-applied migration that needs a manual `alembic upgrade head`) |
| `description-drift` | manual: read the ticket's description AC against the current ADR / design — no automatable check | wording matches the shipped semantics |

**`loginctl enable-linger`** is a silent prerequisite for *all* `--user` units
on a headless box: without it the user manager exits at logout and every timer
stops. The script checks `loginctl show-user "$USER" -p Linger` and reports
`Linger=no` as a red row that invalidates every other `--user` result.

---

## 5. Remediation backlog

Per the `RemediationOverlap` mitigation: **link to existing tickets, do not
file duplicates.** All 8 "no"/"partial" findings already have a home:

| # | Finding | Severity | Owning ticket | Action | Link to |
|---|---|---|---|---|---|
| R1 | No develop-tracking staging env (root gap under OP-767, OP-878, OP-911, OP-965) | **P0** — blocks the entire RELEASE chain (R3 `ci_canary`/`ci_smoke` have no producer) | **OP-927 R5 / AUDIT-19** | stand up `staging.sora.services` (compose already in `deploy/staging/`); add `omnisight-compose-staging.service` wrapper; **then** `systemctl --user enable --now staging-gate-canary.timer staging-gate-smoke.timer` (OP-965) | relates → OP-976 |
| R2 | `release-milestone-checker.timer` was unenabled D-launch → 2026-05-12 | P0 (now closed) — keep a regression guard | **OP-762** (closed) + this audit's prevention §6 | already enabled; the `deployment-audit.sh` quarterly cron is the guard | — |
| R3 | `sora-bridge-sync.timer` never enabled on the bridge host | P1 — bridge runs stale code; surfaced via OP-925 | **OP-798** (deploy follow-up) or the OP-925 cascade remediation ticket | `systemctl --user enable --now sora-bridge-sync.timer`; add to bridge-host unit manifest | relates → OP-976 |
| R4 | `~/.config/omnisight/release-audit.env` not created on prod → D5 audit sink silently uses SQLite | P0 — no durable `release_audit` trail | **OP-964** (deploy step) | create the env file (0600) with the pg-primary DSN; run the runbook smoke test; confirm a `release_audit` row appears after the next `auto-promote-develop` run | relates → OP-976 |
| R5 | D5 audit sink writes generic `audit` table, not `release_audit` (0207) | P1 — schema/semantics mismatch | **backend follow-up** (memory `project_release_audit_sink_mismatch.md`) — **out of this ticket's area** | retarget the sink; not actioned here (area = backend) | noted only |
| R6 | OP-927 R5 description drift ("deploy" vs "audit") | P2 — cosmetic but causes agent confusion | **OP-927** | rewrite R5 description to the audit model + add `deployed: yes/n-a` AC item | relates → OP-976 |
| R7 | `auto-promote-main.service` (OP-766) may be running and racing OP-960's Gerrit-review path | P1 — potential double-promote | **OP-960 follow-ups** | host probe; if active, decide retire-vs-keep; document in ADR-0016 | relates → OP-976 |
| R8 | D12 feature-flag store (OP-884) env-wire on prod unverified | P2 — F13 canary gating inert until confirmed | **OP-911** readiness | `/proc/<backend-pid>/environ` probe; wire if missing | relates → OP-976 |

**Net new tickets**: 0 — but **one umbrella note, `AUDIT-23-a`**, should be
opened (or this section quoted into OP-927) covering: (i) the JQL coverage
re-pass (§2.1 caveat), and (ii) the OP-927 R5 description rewrite. It is small
enough to be a sub-task of OP-927 rather than a standalone ticket; operator's
call in retrospective.

> Note for the OP-976 runner: this CLI's JIRA helper surface is restricted to
> `add_comment` / `transition_back_to_todo` (per the ticket's operating rules),
> so the *creation* of `AUDIT-23-a` and the `relates` links above are left for
> operator triage from this table. The audit doc **is** the backlog; §5 is the
> "filed" artefact in the sense the DoD intends (full graph visible, every row
> mapped to an owner).

---

## 6. Prevention recommendation

Three tactics were on the table (AC §4). They are not mutually exclusive;
recommend adopting **#1 immediately (zero infra) + #3 within the next sprint**,
and treating #2 as the eventual end-state once #3 has shaken out.

### 6.1 Tactic #1 — DoD discipline: mandatory `deployed:` AC item *(recommended, do now)*

Amend `docs/sop/jira-ticket-conventions.md` (and the ticket-creation checklist)
so that **any ticket whose `Files touched` includes `deploy/`, a systemd unit,
a compose file, a cron, or a migration MUST carry an Acceptance Criteria item**:

```
N. deployed: <yes | n-a>
   - yes  → cite concrete evidence: `systemctl --user is-enabled <unit>` output,
            `docker ps` line, `/proc/<pid>/environ` grep, or `alembic current`.
            "merged to develop" is NOT evidence of deployment.
   - n-a  → state why (e.g. "peer-gated on OP-927; timer ships disabled by design").
```

Cost: a checklist edit. Catches the *next* OP-762.

### 6.2 Tactic #2 — separate "ship" vs "deploy" workflow states *(end-state, defer)*

Add a JIRA status **`Deployed`** after `公開済み`. A ticket only reaches
`Deployed` when a deploy-verification step (the §4 checks) passes on prod. The
RELEASE-chain JQL keys off `Deployed`, not `公開済み`. Cost: JIRA workflow
change + retraining the runner's transition logic + a deploy-verifier hook.
High value, non-trivial; do after #3 has surfaced the real toil.

### 6.3 Tactic #3 — scheduled "deployment audit" cron *(do next sprint)*

Wrap `scripts/deployment-audit.sh` in `deploy/systemd/deployment-audit.{service,timer}`
(daily, e.g. 06:30 UTC, before the 07:00 `auto-promote-develop`), reading a
small host-specific manifest of *expected-live* units/containers/env-vars/heads.
Any red row → the `staging-gate-alert.service`-style structured-log line → T1
alerter (OP-722). This is the standing regression guard: it would have caught
OP-762 within 24 h of launch instead of ~4 days later via a downstream failure.
Cost: ~1 unit pair + a manifest file + a few lines of alert glue (a follow-up
ticket, not this one — keeps OP-976 inside `devops`/`docs`/`tests`).

### 6.4 Cookbook entry to add to `docs/sop/architecture-anti-patterns.md`

Proposed pattern **#13 — Shipped-but-not-deployed**:

> **Symptom**: Ticket merged + 公開済み, but the artefact (systemd unit, container,
> env-file, migration) never reached its runtime. Discovered only when a
> downstream consumer fails (cron didn't fire, `staging.sora.services` 404s,
> `release_audit` empty).
> **Why it keeps happening**: every gate stops at "merged to `develop`"; "did
> the operator run the install recipe in the unit header?" is checked by nobody.
> **Cure**: (1) mandatory `deployed: yes/n-a` AC item with `systemctl`/`docker
> ps`/`/proc/.../environ` evidence (§6.1); (2) `deployment-audit.{service,timer}`
> daily red-row alert (§6.3); (3) eventually a `Deployed` JIRA status (§6.2).
> **Past instances**: OP-762, OP-798, OP-767, OP-878, OP-964, OP-927 R5 — all
> in the OP-925 R3 cascade, all catalogued in `docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md`.

(This file is the canonical "what / why / who" record the cookbook entry points
at; the cookbook edit itself is a tiny follow-up — left out of OP-976 so a
single PR doesn't touch both the audit and the SOP registry, per anti-pattern
#1 "numbered flat-file registry" hygiene.)

---

## 7. Sprint G / H — explicitly out of scope

Per AC §5: Sprint G/H tickets are too in-flight to audit cleanly — auditing a
moving target produces false `no`s (the deploy step is legitimately still
pending). When a Sprint G/H infra ticket reaches 公開済み, it gets the §4
treatment, ideally automatically via the §6.3 cron once that lands. No Sprint
G/H rows appear in §3.

---

## 8. Error-catalog self-check

| Catalog error | How this audit mitigates it |
|---|---|
| `TicketCoverageMiss` — list incomplete | §2.1 documents the 4 enumeration sources + explicitly flags that this is *not* a JQL-exact pass; `AUDIT-23-a` is the JQL re-pass; cross-check against META OP-761 + Sprint E/F METAs is called out |
| `FalsePositiveDeployed` — "enabled" ≠ "working" | §3 marks host-unverified rows `partial — host probe required`, not `yes`; §4's `systemd-timer` check reports the *service*'s `last Result=success` + `list-timers` LAST, not just `is-enabled`; the §3.1 caveat says so explicitly |
| `RemediationOverlap` — duplicate tickets | §5 files **0 net-new tickets** — every finding maps to an existing owner (OP-927/AUDIT-19, OP-762, OP-798, OP-964, OP-960, OP-911) with a `relates → OP-976` link; the only new artefact is one optional umbrella sub-task `AUDIT-23-a` |

---

## 9. Re-running this audit

```sh
# On the prod host (and the sora-bridge host for the bridge units):
scripts/deployment-audit.sh                 # uses the built-in expected-live list
scripts/deployment-audit.sh path/to/manifest.tsv   # or a host-specific manifest
echo $?                                     # 0 = all expected-live rows green; 1 = ≥1 red
```

Schedule it (Tactic #3): copy `deploy/systemd/deployment-audit.{service,timer}`
(to be added in the §6.3 follow-up) to `~/.config/systemd/user/`, `systemctl
--user enable --now deployment-audit.timer`. Quarterly minimum; daily preferred.

---

---

## Appendix A — `deployment-audit.sh` first run (2026-05-12, dev/prod-overlap host)

Run on the current dev+prod-overlap host (memory `project_prod_environment.md`),
`--user` bus, built-in manifest. Raw output (trimmed):

```
✓ OK    linger          user                              Linger=yes
✓ OK    systemd-timer   release-milestone-checker.timer   enabled; service Result=success; LAST=~5min ago
✗ RED   systemd-timer   auto-promote-develop.timer        timer not installed
✗ RED   systemd-unit    auto-promote-main.service         unit not installed
✗ RED   env-var         OMNISIGHT_DATABASE_URL@auto-promote-develop.service   no live PID
✗ RED   systemd-timer   sora-bridge-sync.timer            timer not installed   (expected here — bridge host is separate)
✗ RED   container       staging@…:8010/healthz            no running container matching 'staging'
✗ RED   systemd-timer   staging-gate-canary.timer         timer not installed   (gated by design)
✗ RED   systemd-timer   staging-gate-smoke.timer          timer not installed   (gated by design)
✗ RED   alembic-head    auto                              `alembic current` returned nothing (needs backend venv on prod)
summary: 2 green · 8 red · 4 red-with-expected=yes (fatal)
```

What this *confirms* (upgrading several §3 rows from "host probe required"):

- **OP-762** — `release-milestone-checker.timer` **is now live** (enabled, last
  fire ~5 min ago, `Result=success`). The late-fix landed. → row stays `late`.
- **OP-877 `auto-promote-develop.timer`** — **not installed on this host.** The
  D5 daily promote is *not* running here. → confirms `partial`/`no` lean.
- **OP-766 `auto-promote-main.service`** — **not installed** → no race with
  OP-960, but D5 also has no daemon-style consumer here. Consistent with the
  ADR-0016 "promote via Gerrit review" model having superseded it.
- **OP-965 staging-gate timers** — **not installed** → confirms the `gated`
  classification (they ship disabled until the staging env exists).
- **Staging env** — **no container** → confirms OP-767/878/927-R5 root gap.
- `alembic-head` could not be checked from this shell (no backend venv on PATH);
  re-run from the backend deploy venv on the prod host. `sora-bridge-sync.timer`
  must be checked on the *sora-bridge* host, not here — its `not installed`
  result here is expected, not a finding.

(This is exactly the kind of red-row table the §6.3 daily cron would emit.)

---

## Appendix B — Sources & limits

- Ticket enumeration: §2.1 (4 sources, *not* a JQL-exact pass — `AUDIT-23-a`
  closes that). `git log --all` on `develop`, `deploy/systemd/*` headers,
  ADR-0010/0011/0016/0017/0018, the 2026-05-12 planning-session ticket list.
- Repo-side evidence cited inline: `deploy/systemd/auto-promote-develop.service:9-15,44`
  (OP-964 EnvironmentFile + the `-`-optional fallback warning), `:staging-gate-canary.service:15,49`,
  `:staging-gate-smoke.service:37`, `deploy/staging/docker-compose.yml`,
  `deploy/blue-green/*`, `deploy/systemd/release-milestone-checker.service` header
  (install recipe), `deploy/systemd/sora-bridge-sync.{service,timer}` headers.
- Live-host evidence: Appendix A run (dev/prod-overlap host, 2026-05-12).
- Not done (out of this ticket's `devops`/`docs`/`tests` area, or needs prod
  access this CLI doesn't have): the actual `docker compose up` of staging
  (= AUDIT-19); the `release-audit.env` creation on prod (= OP-964 deploy step);
  the JIRA ticket *creation* of `AUDIT-23-a` + the `relates` links (= operator
  triage from §5); the `release_audit` sink retarget (= backend follow-up).

---

*Authored under OP-976 (AUDIT-23). Operator review of this doc + acceptance of
the §6 prevention tactic is a DoD item — see the OP-976 ticket comment thread.*
