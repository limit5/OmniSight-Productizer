---
id: SPRINT-S12G-V2-FAMILY6-IMAGE-DB-DRIFT-CONTRACT
version: v1 (2026-05-16)
title: G.A-v2 Family ⑥ — Image-vs-DB alembic head drift contract (HIGHEST PRIORITY)
scope: Contract spec for the asymmetric-upgrade ("Option C") image-vs-database alembic-head drift defense — what the backend MUST do at startup when its baked `alembic_head_in_image` disagrees with the live database's `alembic_version`, what gets detected (D1), what gets reported (D2), what gets recovered automatically (D4), and what stays in the operator-only rescue path (D5). Doc-only ticket (v2-⑥-1a); no runtime change. Anchors are stable and consumed by every downstream Family ⑥ child.
status: Draft — OP-1153 (this ticket); locked per operator decision 2026-05-14 Q1 (asymmetric upgrade) + Sprint S12.G v1.4 spec §"Family ⑥ — Image-vs-DB alembic head drift (HIGHEST PRIORITY)"
related:
  - sprint-s12g-A-v2-runtime-defense-contract-spec.md §3 "Family ⑥ — Image-vs-DB alembic head drift" (parent spec)
  - 2026-05-16-v2-family5-image-surfacing-contract.md (OP-1154 — supplies the baked `MANIFEST.json` + `/version` truth-sources this contract reads)
  - 2026-05-16-v2-alertbridge-framework-contract.md (OP-1144 — the AlertRule contract `v2-⑥-AlertRule` consumes)
  - docs/architecture/ADR-0036-forward-only-deploy-invariant.md (companion ADR locked by this ticket)
  - docs/adr/ADR-0033-governance-engine-and-operator-authority.md (L2 fingerprint requirement that v2-⑥-RescueCLI inherits)
  - docs/adr/ADR-0034-override-review-and-separation-of-duties.md (audit-row contract that subcontract 7 inherits)
  - docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md (incident that surfaced this gap; 7-day-stale image masked an `alembic_version=0202` advance against an image baked at `0200`)
  - JIRA OP-1153 (v2-⑥-1a — this spec)
  - JIRA OP-1156 (v2-⑥-2a — ADR-0036 ratification ticket)
  - JIRA OP-1157+ (v2-⑥-1bc / v2-⑥-2bc / v2-⑥-3bc / v2-⑥-AlertRule / v2-⑥-RescueCLI / v2-⑥-DRDrillForward / v2-⑥-DRDrillBackward / v2-⑥-Integration — downstream impl tickets that consume this spec)
---

# G.A-v2 Family ⑥ · Image-vs-DB alembic head drift contract — v1 (2026-05-16)

## §0. Reading order

1. §1 — the gap class this spec eliminates (3 minutes); the 2026-05-14 outage in one paragraph; what "image-vs-DB drift" means and why it is the highest-priority family in G.A-v2.
2. §2 — **drift class definition**: the 3 image/DB state combinations (AHEAD / EQUAL / BEHIND) and why each Option C behavior is correct on its own terms.
3. §3 — **Option C asymmetric upgrade rationale**: why Option A (auto-up always) and Option B (always block) were rejected; cite-by-name from the operator-locked decision and from ADR-0036.
4. §4 — **detection contract (D1)**: the always-on `alembic_drift` Prometheus gauge, what it samples, why it is decoupled from `/readyz`, and what its three values mean.
5. §5 — **remediation contract (D2)**: the `/readyz` 503 response shape with the new `remediation` field; required vocabulary; example responses for forward / backward / unknown.
6. §6 — **recovery contract (D4)**: the forward startup hook — advisory lock acquisition, idempotent `alembic upgrade head`, success / contention / failure outcomes; the only auto-recovery surface in this family.
7. §7 — **rescue contract (D5)**: scope of `v2-⑥-RescueCLI`; the L2-fingerprint + operator-window gate; what stays explicitly out of scope of this ticket and lands in the rescue CLI's own spec.
8. §8 — **§3.0.6 seven deterministic subcontracts**: the named subcontract list (migration lock acquire / image-head resolution / forward hook / backward fail-fast / rescue backup capture / downgrade eligibility matrix / audit event writer) that downstream tickets cite by index.
9. §9 — **build-time multi-head invariant**: `alembic heads` returning >1 head MUST fail the image build; no silent pick-first; structured remediation pointing at `alembic merge`.
10. §10 — **cross-family delegation**: Family ⑧ (shutdown) does NOT detect drift — handoff goes to Family ⑥ AlertRule via the `omnisight_alembic_drift` gauge; Family ⑤'s audit script is a redundant out-of-band detector.
11. §11 — **pre-rc2 vs post-rc2 phasing**: which sub-tickets are pre-rc2 (1bc / 2bc / 3bc / AlertRule) vs post-rc2 (RescueCLI / DRDrills / Integration); the filing order this spec implies.
12. §12 — anchors, out-of-scope, downstream filing order, class-override note.

This spec is the **contract** — not the implementation. Code lands in `v2-⑥-1bc` (the always-on `alembic_drift` Prometheus gauge), `v2-⑥-2bc` (the startup hook + advisory lock + idempotent upgrade), `v2-⑥-3bc` (the `/readyz` 503 `remediation` field), `v2-⑥-AlertRule` (the `OmniSightAlembicDrift` rule consuming AlertBridge), `v2-⑥-RescueCLI` (the operator-window rescue CLI implementing subcontracts 5-7), and `v2-⑥-DRDrillForward` / `v2-⑥-DRDrillBackward` / `v2-⑥-Integration` (synthetic-drift chaos + soak). Anchors in §2 through §11 are stable and will be referenced from every downstream ticket's AC.

ADR-0036 (`docs/architecture/ADR-0036-forward-only-deploy-invariant.md`) is the companion document: it states the **invariant** (forward-only deploy is the canonical path; image-BEHIND refuses to start) in the binding ADR vocabulary. This contract spec is the implementation contract for that invariant.

---

## §1. The gap class this spec eliminates

### §1.1 The class, in one sentence

> *"A running backend image's bundled migration files can be ahead of, equal to, or behind the database's current `alembic_version`, with three structurally different correct behaviors and no single mechanism today able to distinguish them — so every container restart is a coin flip between silent corruption (image behind DB, queries reference dropped columns), silent recovery (image ahead, upgrade should have run but didn't), and accidental success."*

Concrete instance from 2026-05-14 (verbatim from `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md`): the prod backend image was built 2026-05-07 with alembic versions topping out at `0200_provider_usage_event.py`. Between 2026-05-07 and 2026-05-13 the DB was advanced to `0202` by an operator running `alembic upgrade head` against a freshly-merged develop branch (OP-1046 resolved the 7-heads merge that produced `0201` + `0202`). The image was never rebuilt. At 2026-05-14 09:22:04 the host came back from a Windows-forced reboot and the 7-day-old image attempted to start; `/readyz`'s always-on migration probe correctly returned 503 with:

```json
{ "migrations": { "ok": false, "detail": "migration_pending: current=0202 latest_file=0200_provider_usage_event.py" } }
```

— and so systemd marked `omnisight-compose-prod.service` `failed`, and the `depends_on: service_healthy` chain stopped frontend / caddy / cloudflared / installer from starting. The public URL was therefore down from 05:29 (reboot) until manual recovery at 10:50, despite the actual breakage being a 7-day-old image / DB-ahead-of-image misalignment (= image **BEHIND** in the vocabulary of this spec) that had been silently failing readyz 118 consecutive times in the live container without alerting anyone.

The retro identified **five** discrete gaps:

1. There was no in-band detection independent of `/readyz`'s migration probe (which is event-loop-style: probed on each healthcheck poll, lost between polls). A 118-event streak should have woken someone; it didn't.
2. There was no remediation hint in the 503 body. The operator who saw `migration_pending` had to grep the codebase to know whether to redeploy the image or downgrade the DB.
3. There was no rule by which the running backend should **decide** what to do on startup — autonomy was zero, every drift required operator action.
4. There was no rescue path with operator-authority audit. The recovery the operator performed (overrides to `/livez` in 3 docker-compose lines + 3 Caddyfile lines) was correct but un-audited.
5. There was no build-time check that the image being shipped had a self-consistent migration head. A future image built with the merge resolution half-done would have shipped two heads silently.

This spec closes gaps 1-5 in the design dimension; the impl tickets `v2-⑥-1bc / 2bc / 3bc / AlertRule / RescueCLI / Integration` implement the closure.

### §1.2 Why this is the HIGHEST-PRIORITY family in G.A-v2

Three reasons:

1. **Affects every prod restart.** Every container recreation — operator-triggered, host-reboot-triggered, watchtower-triggered, kubelet-triggered — re-runs the image-vs-DB comparison from scratch. A latent drift becomes a real outage at the next restart, which can be minutes from now or weeks from now; the outage is decoupled in time from the change that caused it.
2. **Biggest design blind spot in S12.** Sprint S12's defense contract (the parent spec `sprint-s12g-A-v2-runtime-defense-contract-spec.md`) implicitly assumed that "the image runs the migrations". The 2026-05-14 outage proved that assumption false: in a forward-only-deploy environment the image *is* the canonical migration source, but in any rollback-capable environment the assumption fails and produces silent corruption or silent stall. This family makes the assumption load-bearing.
3. **Cross-cuts every other family.** Family ⑤ (shipped-but-not-deployed) reads the same `alembic_head_in_image` truth-source; Family ⑦ (allowlist) doesn't itself produce drift but `/readyz` is one of the public allowlist entries; Family ⑧ (shutdown) cannot detect last-shutdown-was-forced and therefore relies on Family ⑥ to detect the drift left behind; Family ⑩ (runner) logs image-version per pickup and reads the same source. Family ⑥'s detection / recovery primitives are consumed across the rest of G.A-v2.

### §1.3 Why "operator just always rebuilds" doesn't dissolve the gap

Five reasons; each is the rejection of a tempting "human-discipline" fix:

1. **Operator can't always rebuild.** Forced reboots (2026-05-14 Windows update), kernel updates, hypervisor migrations, hardware failure, kubelet evictions — all force a restart with whatever image happens to be on the host. The image at that moment is the image; rebuild is decoupled from restart.
2. **Rollback is a real recovery path.** When a forward image ships a regression, the on-call rolls back to N-1. If the DB has already advanced to N, this rollback creates an image-BEHIND-DB state. Without a structured response, the rollback succeeds (containers come up green) but every query touching post-N-1 columns 500s.
3. **Cadence is not a contract.** "We try to deploy daily" reduces expected drift age but doesn't produce a number anyone can audit. The detection gauge in §4 is a contract; deploy cadence is a guideline.
4. **CI != deploy.** OP-1035 shipped per-commit image builds to GHCR in May; the prod deploy half was not delivered. The half that *was* delivered created a false sense of "we have CI for images" without the deploy half. Family ⑥'s autonomy hook (§6) closes that latency in the forward direction; ADR-0036's invariant closes it in the backward direction by *refusing* to mask drift.
5. **The DB axis is invisible from outside.** GHCR shows image SHAs and tags; nothing in the operator's normal `docker compose ps` / `gh release view` workflow shows the DB's `alembic_version`. Without §4's gauge + §5's `/readyz` `remediation` field, the operator can only see drift after it has already become a 503 chain.

The structural fix is to make image / DB alembic-head agreement a **first-class invariant** with three named state-handlers (one per state) and four detection / recovery / rescue surfaces. Once those exist, the operator's role compresses from "check everything before every restart" to "respond to a single named alert".

---

## §2. Drift class definition — the three image/DB state combinations

This is the section every downstream Family ⑥ ticket cites by index. It enumerates the three states, the correct behavior in each, and the rationale that ties each state's behavior to the asymmetric-upgrade invariant.

### §2.1 The three states

Let `image_head` = the single alembic revision baked into the image at build time per `MANIFEST.json.alembic_head_in_image` (Family ⑤ §4 contract); let `db_head` = the single row in `alembic_version.version_num` of the live Postgres.

| # | State | Definition | Correct startup behavior | Defense dim | Subcontract |
|---|---|---|---|---|---|
| **S1** | **EQUAL** | `image_head == db_head` | No-op. Backend starts normally. `omnisight_alembic_drift` gauge = 0. | none (steady state) | n/a |
| **S2** | **AHEAD** | `image_head > db_head` per alembic revision graph (image carries migration files the DB has not yet applied) | Inside the migration advisory lock (subcontract 1), run `alembic upgrade head`. Idempotent — already-applied is a no-op. On success the gauge transitions 1 → 0; backend completes startup. | D4 (recovery) | subcontract 3 |
| **S3** | **BEHIND** | `image_head < db_head` per alembic revision graph (DB carries migration revisions the image's `versions/` directory does not contain) | **Refuse to start.** Emit structured log per §5.5; exit code 78 (`EX_CONFIG` per `sysexits.h`). `omnisight_alembic_drift` gauge stays at the BEHIND value (= 2 per §4.2) for the lifetime of the failed container. | D2 (remediation) | subcontract 4 |

A fourth state, **UNKNOWN**, is handled defensively in §5.3 (e.g., `image_head` cannot be parsed because the MANIFEST is missing). UNKNOWN is treated as BEHIND for safety: the container refuses to start. This is documented separately in §5.3 because it shares the BEHIND response but has a different cause.

### §2.2 Why "AHEAD auto-upgrades" is correct on its own terms

Three orthogonal reasons:

1. **Forward image is the operator's intent.** A newer image was deployed because *someone decided* the system should advance. The migration files baked into that image are part of the same artifact that ships the new behavior; refusing to apply them would mean the new code is running against the old schema, which is the same silent-corruption class we are eliminating.
2. **Migration files are reviewed in the same code-review path.** Each alembic revision under `backend/alembic/versions/` lands through Gerrit Code Review (CLAUDE.md §"Safety Rules"); the operator who approved the image build implicitly approved its migrations. Auto-applying does not bypass review — it executes review already given.
3. **Postgres advisory lock makes concurrent-replica safe.** §6 + subcontract 1 guarantee that under multi-replica startup, exactly one replica runs the upgrade and others wait. A naive "every container runs upgrade" would race; the lock makes auto-upgrade a single-actor operation even when the caller is multi-replica.

The asymmetry — AHEAD auto-upgrades, BEHIND refuses — is asymmetric on purpose. It is not a "be conservative when uncertain" rule; it is a **forward-only-deploy invariant** (ADR-0036). The image is the canonical advance vector; the DB follows. Reversing that direction requires explicit operator authority via the rescue CLI (§7), never implicit container behavior.

### §2.3 Why "BEHIND refuses to start" is correct on its own terms

Four reasons; each is a different failure mode the refusal prevents:

1. **A rolled-back image cannot apply migrations it does not have.** If `image_head = 0200` and `db_head = 0202`, the image's `versions/` directory contains files `0001..0200_*.py`. It does not contain `0201_*.py` or `0202_*.py`. There is no source code in the image with which to apply the missing migrations forward; there is no source code in the image with which to downgrade the database backward. The image is structurally unable to do either.
2. **Running the image against the advanced DB is silent corruption.** Any query that references a column introduced by `0201` or `0202` will fail at runtime — but only on the code path that touches it, and only when that path is exercised. The result is intermittent 500s that mimic application bugs, with no signal that the cause is schema drift. The retro's "118 consecutive failing healthchecks" was the lucky case; an arbitrary subset of API endpoints failing silently is the unlucky case.
3. **Refusing-to-start makes the problem loud, not soft-recoverable.** A backend that crashloops surfaces to systemd (`failed` state), to Prometheus (`up == 0`), to the operator (the public URL is down). Soft recovery — "serve traffic for endpoints that don't touch the missing columns" — would hide the problem until the next code path tickled it, by which time the operator has moved on. Loudness on startup is the correct severity for a drift this dangerous.
4. **Exit 78 is semantically right.** `EX_CONFIG` (78) is the BSD `sysexits.h` code for "configuration error". Image-BEHIND-DB is a deploy-time misconfiguration: the operator selected an image incompatible with the DB. Other exit codes (1, 2, 70) would either be too generic or wrong; 78 communicates to systemd / docker-compose / kubelet that this is not transient (so don't restart) and not a program bug (so don't page on `up == 0` per `OmniSightBackendCrashloop`).

### §2.4 Why EQUAL is the steady state (and not the goal)

EQUAL is the asymptotic state of a healthy deployment chain. After every `docker compose up -d` triggered by `scripts/auto-redeploy.sh` (Family ⑤ §9), AHEAD transitions to EQUAL via subcontract 3. After every `omnisight rescue` operator action (Family ⑥ §7), BEHIND transitions to EQUAL via subcontracts 5+7. Within a steady-state EQUAL window, the only thing the runtime does is *maintain* the invariant — no auto-action, no remediation, no alert.

EQUAL is **not** the goal of the family. The goal is the **graceful transition** between the three states. EQUAL is what every transition lands at; the family's contract is on the transitions, not on the destination.

### §2.5 Comparison semantics — `<` and `>` on alembic revisions

Alembic revisions are a DAG, not a totally ordered scalar. A naive lexicographic comparison would treat `0200_provider_usage_event` < `0201_runner_claims` but would also wrongly treat `0099_zz` > `0100_aa`. The contract uses **graph-position** comparison:

- `image_head < db_head` iff the path from `image_head` to `db_head` in the alembic revision graph is non-empty AND traverses only `downgrade -> upgrade` (i.e., `image_head` is an *ancestor* of `db_head`).
- `image_head > db_head` iff `db_head` is an ancestor of `image_head`.
- `image_head == db_head` iff they are the same revision.
- All other configurations (disjoint branches, unmerged) are **UNKNOWN** per §5.3. They cannot occur in practice if subcontract 2 (build-time multi-head invariant, §9) holds and if all DB writes go through `alembic upgrade head` — but the runtime must handle UNKNOWN defensively rather than crash.

The implementing ticket (`v2-⑥-2bc`) uses `alembic history --rev-range` to compute this comparison; the helper output's non-emptiness is the comparison's truth value. The spec does NOT prescribe a Python-side reimplementation of the alembic graph walk — call the existing CLI.

---

## §3. Option C asymmetric upgrade rationale

### §3.1 The operator-locked decision

**Sprint S12.G v1.4 spec, Q1 (2026-05-14):** *"Family ⑥ drift-handling option: **Option C** — auto-upgrade if image AHEAD; fail-fast (exit 78) if BEHIND."*

Operator decision LOCKED. ADR-0036 (`docs/architecture/ADR-0036-forward-only-deploy-invariant.md`) ratifies this lock as a design invariant: forward-only deploy is the canonical path; backward image refuses to start.

### §3.2 Option A (auto-up always) — rejected

**Definition:** every startup, regardless of image vs DB state, runs `alembic upgrade head`.

**Tempting because:** simplest mental model ("the image is the truth; bring DB to image"); no branching; one code path.

**Rejected for three reasons:**

1. **Rolled-back image advances DB further.** If `image_head = 0200` and `db_head = 0202`, Option A's `upgrade head` on the image would: (a) read the image's versions directory, (b) see `0200` is the head, (c) check the DB at `0202`, (d) realize the DB is *ahead* and do nothing — i.e., Option A silently degrades to "do nothing on rolled-back image". That degenerate case is **identical to the 2026-05-14 outage**: image silently runs, DB diverges further. Option A solves the AHEAD case but does not solve BEHIND.
2. **Lossy migrations make `upgrade head` non-symmetric.** Some migrations drop columns or normalize data. After the lossy migration applies, the previous column is irrecoverable from the production data. An "always upgrade" stance would have no problem applying a lossy migration even when the operator intent was to roll back; the rollback intent is destroyed silently.
3. **It bakes "image-is-truth" assumption that is not always desired.** The operator may want to ship a hotfix image that does *not* yet have the latest migration (e.g., an emergency patch built off a release branch). Option A would refuse to start the hotfix or — worse — would apply older migrations and corrupt the schema. Option C lets the hotfix start if and only if its alembic head is `>= db_head`.

### §3.3 Option B (always block) — rejected

**Definition:** every startup, regardless of state, refuses to start if `image_head != db_head`. Operator must manually run `alembic upgrade head` (or downgrade) and only then start the backend.

**Tempting because:** zero auto-action; operator owns every transition; trivially auditable.

**Rejected for two reasons:**

1. **Deploy script bug = hard outage.** The single most common failure mode in deploy automation is the deploy-time-migration step silently skipping. If `scripts/auto-redeploy.sh` (Family ⑤ §9) pulls a new image but the operator forgets the `alembic upgrade` step, Option B refuses to start the new image — but Option B *also* refused to start the old image already (because the new image is what's there now). The result is a deployment-induced outage where neither image runs. Option C lets the new image self-heal in this exact case.
2. **It loads every routine deploy with operator toil.** Every prod deploy must perform a manual migration step. Across hundreds of routine deploys, the per-deploy cost of "remember to migrate" accumulates; eventually someone forgets and we are back to the failure mode of reason 1. Option C makes the routine case (forward image) automatic; only the unusual case (rollback) requires operator action.

### §3.4 Option C — asymmetric upgrade (LOCKED)

| State | Behavior | Authority required |
|---|---|---|
| AHEAD | Auto-run `alembic upgrade head` inside advisory lock; idempotent | none (image's own authority via review-at-build-time) |
| EQUAL | No-op | none |
| BEHIND | Refuse to start; exit 78; structured log + remediation hint | rescue CLI required to recover; L2 fingerprint |

The asymmetry is the point. It encodes the **forward-only-deploy invariant**: forward is auto; backward is operator-explicit. This makes drift impossible to mask — forward drift self-heals; backward drift is loud and operator-actionable.

### §3.5 What Option C does NOT decide

Option C is the in-container response policy. It does **not** decide:

- Whether to redeploy the image. That is Family ⑤'s `v2-⑤-AutoRedeploy` (decision: cron Option (a) ships first; sidecar Option (b) is post-rc2).
- Whether to downgrade the DB. That is Family ⑥'s `v2-⑥-RescueCLI` (operator-window only).
- What the operator does about a BEHIND state. The structured log's `remediation` field (§5.5) names two paths: redeploy newer image OR rescue CLI to downgrade DB. The choice between them is operator-side, not in-container.

Option C says: "the container can decide AHEAD, EQUAL, and 'refuse + report'; everything else is operator-authority territory." That boundary is what ADR-0036 makes binding.

---

## §4. Detection contract (D1) — the `alembic_drift` Prometheus gauge

This is what `v2-⑥-1bc` implements. The contract is on what the gauge surfaces, when it is set, why it is decoupled from `/readyz`, and how it relates to AlertBridge.

### §4.1 Why always-on, not piggyback on `/readyz`?

Today (2026-05-16) `/readyz` includes a migration probe: each `/readyz` GET runs an inline check that `db_head == latest_alembic_head_in_filesystem` and returns 503 if not. This was sufficient for the *symptom* in 2026-05-14 (the systemd healthcheck stopped the dependency chain) but is **insufficient as a detection primitive**:

1. **Polling-coupled.** `/readyz`'s migration probe runs once per healthcheck poll (every 30 s in `docker-compose.prod.yml`). Between polls the drift is undetected. The 118 failing healthchecks the retro identifies were detected — but only at poll cadence; if the operator looked between polls, the same drift was momentarily invisible.
2. **Conflated with other readyz checks.** `/readyz` also probes the DB connection pool, the Redis pool, the disk free space, etc. A 503 from `/readyz` could be any of those; the operator must parse the JSON body. Prometheus rules cannot easily express "ready=false AND the reason is migration drift" without scraping the body.
3. **Hidden when readyz is disabled.** During the 2026-05-14 recovery, the operator swapped healthchecks from `/readyz` to `/livez` (3 docker-compose lines + 3 Caddyfile lines per the retro). Once healthchecks point at `/livez`, the migration probe never runs. **Detection went to zero**. An out-of-band gauge cannot be disabled by an in-band healthcheck swap.
4. **Cannot be queried by callers other than healthcheck infra.** AlertBridge wants a Prometheus expression; Grafana wants a series; the audit script (Family ⑤ §8.1) wants a gauge. None of them want to GET `/readyz` and JSON-parse the body.

The `alembic_drift` gauge runs always-on (every 60 s, per §4.4) and is decoupled from `/readyz`. The `/readyz` check stays — it is still the right thing for the per-poll healthcheck (it ALSO checks the gauge per §5) — but it is no longer the only or canonical detection surface.

### §4.2 The gauge — three values

```text
omnisight_alembic_drift{instance="..."} ∈ {0, 1, 2}
  0 = EQUAL (image_head == db_head)
  1 = AHEAD (image_head > db_head; auto-upgrade will run / has run)
  2 = BEHIND (image_head < db_head; container will refuse / has refused to start)
```

A separate gauge `omnisight_alembic_drift_unknown` is set to 1 when the comparison cannot be computed (MANIFEST missing, alembic CLI not present, DB unreachable from the gauge collector); 0 otherwise. UNKNOWN is its own gauge, not a fourth value of the main gauge, so that Prometheus rule expressions can use `omnisight_alembic_drift == 2` cleanly without an `or` for the unknown case.

### §4.3 Required labels

| Label | Value | Why |
|---|---|---|
| `instance` | The hostname or container ID the gauge was collected from | Multi-replica disambiguation per AlertBridge §1 |
| `image_head` | The image's alembic revision identifier (e.g., `0204_add_runner_claims_table`) | Cardinality cap §6 of AlertBridge says ≤10 values per label; this is bounded because image upgrades change the value at most a few times per week. Annotation, not routing. |
| `db_head` | The DB's alembic revision identifier | Same. Diagnostic, not routing. |

The four routing labels per AlertBridge §1 (`severity`, `area`, `family`, `defense_dimension`) are set on the AlertRule (§4.5 / §7), not on the gauge. The gauge carries only diagnostic dimensions.

### §4.4 Collection cadence

**Period:** 60 s. Each backend container runs an in-process background task (the same task that runs the startup hook of §6, periodicized) that re-reads `db_head` from `alembic_version` and re-computes the comparison against the (immutable) `image_head` baked at start.

**Why 60 s and not faster:** Postgres advisory-lock-free query overhead is ~5-50 ms; faster cadence wastes DB-connection budget for no operational benefit (the gauge is a *condition*, not a *time series* — the only thing that changes the gauge value is a deploy or a rescue CLI invocation, both of which are minute-scale events).

**Why 60 s and not slower:** AlertBridge §6 cardinality + `for:` clauses assume 1-minute scrape resolution. Slower than 60 s would make the `for: 5m` clauses arithmetic-fragile.

**Failure mode:** if the gauge collection task itself throws (e.g., DB-unreachable), the task does not update the gauge; the last-known value remains. A separate gauge `omnisight_alembic_drift_last_collection_ts` records the unix timestamp of the last successful collection; AlertBridge's freshness rule (§9 of AlertBridge contract) fires if `now() - last_collection_ts > 5m`. This decouples "drift state" from "we know the drift state".

### §4.5 AlertRule shape (informs `v2-⑥-AlertRule`)

```yaml
groups:
  - name: omnisight-family-6
    rules:
      - alert: OmniSightAlembicDrift
        expr: omnisight_alembic_drift > 0
        for: 5m
        labels:
          severity: page
          area: deployment
          family: "6"
          defense_dimension: D1
        annotations:
          summary: "Image / DB alembic head drift on {{ $labels.instance }}"
          description: |
            image_head={{ $labels.image_head }} db_head={{ $labels.db_head }}.
            drift_value={{ $value }} (1=AHEAD/auto-upgrade should run; 2=BEHIND/refuse-to-start).
          runbook_url: https://docs.sora.services/runbooks/omnisight-alembic-drift
          remediation_hint: |
            If drift_value=2 (BEHIND): redeploy a newer image OR run
            `omnisight rescue drift --confirm` per docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md §7.
            If drift_value=1 (AHEAD): check that the startup hook ran;
            `journalctl -u omnisight-backend -g alembic_drift_forward_hook`.
        __bridge:
          critical_labels: [family, instance]
          cardinality_caps:
            instance: 10
```

A single rule covers AHEAD and BEHIND with the same `severity: page` because both are operator-actionable (AHEAD is auto-handled but if it persists >5 min it means the hook didn't run; BEHIND is hard-refuse). The `description` carries the value so the operator can disambiguate.

A second rule `OmniSightAlembicDriftStaleCollection` fires on `omnisight_alembic_drift_last_collection_ts < (now() - 300)`; this is the "we don't know" warn-level rule (per §9 of AlertBridge contract for freshness alerts).

### §4.6 What the gauge does NOT do

- **Does not auto-remediate.** Setting the gauge to 1 does not run `alembic upgrade head`; that is the startup hook of §6 / subcontract 3, which runs at startup, not at every gauge tick. The gauge is observation; the hook is action.
- **Does not page on first tick.** The `for: 5m` clause means a transient gauge=1 during a normal forward redeploy (where the hook is mid-run) does not fire. The hook's expected duration is seconds; 5 minutes is generous.
- **Does not gate `/readyz`.** The `/readyz` 503-with-`remediation` of §5 is the per-poll synchronous detector for systemd / docker-compose / kubelet's healthcheck. The gauge is the periodic observational detector for Prometheus / AlertBridge. They are independent.

---

## §5. Remediation contract (D2) — `/readyz` 503 with `remediation` field

This is what `v2-⑥-3bc` implements. The contract is on the 503 response shape, the required `remediation` strings, and the relationship to the existing `migrations` block.

### §5.1 Today's `/readyz` 503 (insufficient)

From the 2026-05-14 retro:

```json
{ "migrations": { "ok": false, "detail": "migration_pending: current=0202 latest_file=0200_provider_usage_event.py" } }
```

`detail` is human-readable but unstructured. The operator must know that `current=0202 latest_file=0200_*` means "DB is ahead of image" (a not-obvious convention). Recovery steps are not in the body; the operator must consult external docs.

### §5.2 The new `remediation` field

The 503 body adds a top-level `remediation` field with a fixed schema:

```json
{
  "migrations": {
    "ok": false,
    "drift_state": "BEHIND",
    "image_head": "0200_provider_usage_event",
    "db_head": "0202_alertbridge_audit_log",
    "detail": "migration_pending: image_head < db_head"
  },
  "remediation": {
    "summary": "Image is BEHIND the database. Container will not start.",
    "options": [
      {
        "id": "redeploy_newer_image",
        "description": "Deploy a newer backend image whose MANIFEST.alembic_head_in_image is >= 0202_alertbridge_audit_log.",
        "command_hint": "docker compose pull && docker compose up -d backend",
        "authority_required": "operator (no L2 fingerprint required for routine deploy)"
      },
      {
        "id": "rescue_downgrade_db",
        "description": "Downgrade the database to a revision the current image can serve.",
        "command_hint": "omnisight rescue drift --confirm --target 0200_provider_usage_event",
        "authority_required": "operator with L2 fingerprint per ADR-0033; backup captured per §3.0.6 subcontract 5"
      }
    ],
    "doc_url": "https://docs.sora.services/runbooks/omnisight-alembic-drift",
    "contract_anchor": "docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md#§5-remediation-contract-d2"
  }
}
```

`drift_state` is a fixed enum: `EQUAL` (only when the response is 200, not 503; included for symmetry), `AHEAD`, `BEHIND`, `UNKNOWN`.

`options` is an ordered list; the first option is the recommended path (forward-only-deploy preferred per ADR-0036). The list is closed — only the two `id` values above appear in v1; future schema versions may add more, but `id` strings are stable.

### §5.3 UNKNOWN response

If `image_head` cannot be determined (MANIFEST missing or malformed) or `db_head` cannot be read (Postgres unreachable), the body is:

```json
{
  "migrations": {
    "ok": false,
    "drift_state": "UNKNOWN",
    "image_head": null,
    "db_head": null,
    "detail": "manifest_unavailable" | "db_unreachable" | "alembic_graph_disjoint"
  },
  "remediation": {
    "summary": "Drift state cannot be determined. Treating as BEHIND for safety.",
    "options": [
      {
        "id": "investigate_manifest",
        "description": "Check that /app/MANIFEST.json exists and parses; rebuild image via scripts/bake-image-manifest.sh.",
        "command_hint": "docker exec backend cat /app/MANIFEST.json | jq .",
        "authority_required": "operator (read-only forensics)"
      },
      {
        "id": "rescue_downgrade_db",
        "description": "If MANIFEST is unrecoverable but DB head is known, downgrade DB to a known-safe revision.",
        "command_hint": "omnisight rescue drift --confirm --target <known-safe-rev>",
        "authority_required": "operator with L2 fingerprint per ADR-0033"
      }
    ]
  }
}
```

UNKNOWN is rare (it implies build-time bake failed or runtime is severely degraded); the rescue option is included only because UNKNOWN-but-recoverable scenarios exist (corrupted MANIFEST but live DB).

### §5.4 AHEAD response

If `image_head > db_head` AND the startup hook (§6) is mid-run, the `/readyz` 503 body is:

```json
{
  "migrations": {
    "ok": false,
    "drift_state": "AHEAD",
    "image_head": "0204_add_runner_claims_table",
    "db_head": "0202_alertbridge_audit_log",
    "detail": "alembic_upgrade_in_progress"
  },
  "remediation": {
    "summary": "Image is AHEAD; auto-upgrade is running. Wait for completion.",
    "options": [
      {
        "id": "wait",
        "description": "Auto-upgrade typically completes in <30s; recheck /readyz.",
        "command_hint": "curl -fsS http://localhost:8000/readyz | jq .migrations",
        "authority_required": "none"
      }
    ]
  }
}
```

If the hook has already completed but the readyz check runs before the cache invalidation, the state should be EQUAL with `migrations.ok=true` and a 200. The AHEAD-with-in-progress response is the rare race window; AHEAD-without-in-progress (hook failed / didn't run) returns the same AHEAD body but `detail: "alembic_upgrade_did_not_run"` and `options` includes `restart_container`.

### §5.5 Structured log on BEHIND startup

When the startup hook (§6 / subcontract 4) refuses to start because of BEHIND, it emits exactly one structured log line before `sys.exit(78)`:

```json
{
  "event": "alembic_drift_backward",
  "timestamp": "2026-05-16T02:00:14Z",
  "image_head": "0200_provider_usage_event",
  "db_head": "0202_alertbridge_audit_log",
  "exit_code": 78,
  "remediation": "deploy image with alembic_head_in_image >= 0202_alertbridge_audit_log OR run 'omnisight rescue drift --confirm' to downgrade DB; see docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md §7"
}
```

This is the log line subcontract 4 emits. It is structured (a JSON object on a single line) so log shippers and journald greps can match `event=alembic_drift_backward` deterministically. The `remediation` string is the same operator hint as the `/readyz` body's `remediation.summary` so the operator sees the same guidance whether they read the log or the HTTP response.

### §5.6 The `remediation` field is NOT user-facing copy

The strings inside `remediation.options[].description` are written for **operators**. They are not localized; they are not i18n-keyed; they are technical English with command hints. If a future UI surfaces `/readyz` to end-users, that UI is responsible for translation; the backend contract is on the structured shape, not the prose. Implementing tickets MUST NOT pull these strings through the `i18n/` middleware.

---

## §6. Recovery contract (D4) — forward startup hook

This is what `v2-⑥-2bc` implements. The contract is on the startup hook's behavior: advisory lock acquisition, idempotent `alembic upgrade head`, and the three outcomes (success, contention, failure).

### §6.1 Hook placement

The hook runs in the backend container's startup path, **before** the FastAPI application begins accepting traffic. Specifically: after `python -m alembic` is importable (i.e., the package is installed), after Postgres is reachable (per the existing `wait-for-db` step), but before `uvicorn` is `exec`'d.

Implementation: a new `backend/agents/migration_startup_hook.py` module called by the container's entrypoint script. The module exposes one function:

```python
def run_startup_hook(*, db_url: str, alembic_ini: str, manifest_path: str) -> None:
    """Run the Family ⑥ Option C startup hook.

    Either returns normally (EQUAL or AHEAD-and-upgraded), or calls
    sys.exit(78) (BEHIND or UNKNOWN-treated-as-BEHIND).
    Never returns successfully without invariants holding.
    """
```

The entrypoint script invokes this function before `exec uvicorn ...`. If the function calls `sys.exit(78)`, uvicorn never starts; the container's `docker inspect` shows `State.ExitCode: 78`.

### §6.2 The three outcomes

| Outcome | Trigger | Effect |
|---|---|---|
| **Success — EQUAL** | `image_head == db_head` | Hook returns; `omnisight_alembic_drift` set to 0; backend starts. |
| **Success — AHEAD-and-upgraded** | `image_head > db_head`; upgrade runs inside advisory lock; succeeds | Hook returns; gauge transitions 1 → 0; backend starts. Structured log: `event=alembic_drift_forward_hook outcome=success migrations_applied=[0201_*, 0202_*]`. |
| **Wait — AHEAD-and-contended** | `image_head > db_head`; advisory lock held by another replica | Hook waits up to 60 s; on lock acquisition, re-checks `db_head` (the other replica may have completed); branches to EQUAL or AHEAD-and-upgraded. On 60 s timeout, exits 78 with `event=alembic_drift_forward_hook outcome=lock_timeout`. |
| **Fail — BEHIND** | `image_head < db_head` | No upgrade attempted; emit structured log per §5.5; `sys.exit(78)`. |
| **Fail — UNKNOWN** | comparison cannot be computed | Treat as BEHIND; emit structured log with `drift_state=UNKNOWN`; `sys.exit(78)`. |
| **Fail — upgrade error** | `image_head > db_head`; upgrade attempted; alembic raises | Lock released (per advisory-lock idempotency); structured log `event=alembic_drift_forward_hook outcome=upgrade_error error=<msg>`; `sys.exit(78)` (do NOT serve traffic against partially-applied schema). |

### §6.3 Advisory lock contract (subcontract 1)

The hook acquires the Postgres advisory lock with key `alembic_lock` (the same key existing alembic uses for env.py-managed locks):

```python
# pseudo-code
lock_key = hash("alembic_lock") & 0xFFFFFFFF  # 32-bit; deterministic
acquired = await db.execute("SELECT pg_try_advisory_lock(:k)", k=lock_key)
if not acquired.scalar():
    # poll-with-backoff up to 60s
    for delay_ms in [100, 200, 500, 1000, 2000, 5000, 10000, ...]:
        await asyncio.sleep(delay_ms / 1000)
        acquired = await db.execute("SELECT pg_try_advisory_lock(:k)", k=lock_key)
        if acquired.scalar():
            break
    else:
        sys.exit(78)  # lock timeout
try:
    # re-read db_head HERE — another replica may have completed the upgrade
    db_head = await read_db_head()
    if db_head == image_head: return  # EQUAL post-wait
    if db_head > image_head: emit_backward_log(); sys.exit(78)
    run_alembic_upgrade_head()
finally:
    await db.execute("SELECT pg_advisory_unlock(:k)", k=lock_key)
```

Contract invariants:

- **At most one replica runs `alembic upgrade head` at a time** (advisory lock is exclusive).
- **The lock is released on every exit path** including `sys.exit(78)` and unhandled exceptions (the `finally` block, plus `pg_advisory_unlock_all()` is called by Postgres on connection close as a safety net).
- **`pg_try_advisory_lock`, not `pg_advisory_lock`** — the latter blocks forever; the former returns immediately and lets the hook implement its own backoff.

### §6.4 Idempotency (subcontract 3)

`alembic upgrade head` is idempotent by construction: alembic checks each revision against `alembic_version` before applying. The hook does NOT need to track "have I run this before" itself — alembic's per-revision check is the source of truth.

Subcontract 3's test asserts:

1. Fresh DB at `db_head=0200` + fresh image at `image_head=0204` → hook runs; applies `0201..0204`; second startup attempt with the same image runs the hook, sees `db_head==image_head`, no-ops.
2. Crash mid-upgrade (kill -9 during application of `0203`) + restart → hook runs; alembic's per-revision check resumes from wherever it crashed; final state is consistent.
3. Concurrent startup of 2 replicas (subcontract 1 test fixture) → one acquires lock, runs upgrade, releases; second acquires lock, sees `db_head==image_head`, no-ops.

### §6.5 What the hook does NOT do

- **Does not downgrade.** Even when `image_head < db_head` and the operator might prefer "just downgrade the DB", the hook refuses. Downgrade is exclusively a rescue-CLI operation (§7 / subcontract 5+6). The hook is a one-direction (forward) recovery primitive.
- **Does not run on every restart unconditionally.** If `image_head == db_head` (EQUAL), the hook reads `db_head`, compares, and exits the comparison branch. It does not call alembic. (Calling alembic in the EQUAL case is a no-op but wastes ~500 ms of startup; the EQUAL branch skips it.)
- **Does not retry on alembic error.** If alembic raises during the upgrade, the hook does NOT retry. The hook exits 78 and the operator investigates. Auto-retry would mask a real defect in a migration file.
- **Does not write to the `runner_audit_events` table.** The audit-row contract (subcontract 7) is for rescue actions, not for routine forward upgrades. A successful auto-upgrade is logged structurally but is not an audited operator action.

---

## §7. Rescue contract (D5) — `v2-⑥-RescueCLI` scope

This section sets the BOUNDARY for the rescue CLI's own spec ticket. The CLI's full design lives in its own spec when `v2-⑥-RescueCLI` is filed (post-rc2 per §11); this section defines the scope so the boundary is clear from the day `v2-⑥-1a` lands.

### §7.1 The CLI — what it is

`omnisight rescue drift {--confirm,--dry-run,--target <rev>}` — an operator-window-only CLI that can downgrade the DB to a revision the running image can serve.

In scope for `v2-⑥-RescueCLI`:

- **Backup capture before downgrade** (subcontract 5): `pg_dump | gpg --encrypt` to `/var/omnisight/rescue-backups/<ts>-<from>-<to>.sql.gpg`; backup file path printed; integrity verified by re-decrypt-then-restore-into-temp-db.
- **Downgrade eligibility check** (subcontract 6): refuse if any spanned migration is tagged `downgrade_safe: irreversible`; warn + require `--force` if `lossy-data`; proceed silently if all spanned migrations are `lossless`.
- **Audit event row** (subcontract 7): write `runner_audit_events(event_type='rescue_drift_downgrade', actor_fingerprint=<L2>, ticket_ref=<JIRA>, before_state={db_head, image_head}, after_state={db_head_new}, evidence_path=<backup>, timestamp)` row before exit. Persists even if the rescue process is killed mid-flight (write happens before the `alembic downgrade` invocation).
- **L2 fingerprint check**: per ADR-0033, the CLI refuses to run without a valid `OMNISIGHT_OPERATOR_FINGERPRINT` env var that matches an entry in `runner_operator_fingerprints`. The check happens BEFORE the backup; a missing fingerprint is a no-side-effect exit.

Out of scope for `v2-⑥-RescueCLI`:

- **Forward upgrade.** The startup hook (§6) is the forward path; the CLI does not duplicate it. If the operator wants to force `alembic upgrade head` outside the startup window, they can do it via the existing alembic CLI inside the container (not via `omnisight rescue`).
- **Image rollback.** Image management is a docker-compose / GHCR concern; the rescue CLI does not pull / tag / push images.
- **Drift detection.** The CLI does not poll, does not watch, does not run as a daemon. It is operator-invoked only.

### §7.2 Why the CLI's own spec, not this one

Two reasons:

1. **Scope.** The rescue CLI's design is substantial — encryption, eligibility matrix, audit-write atomicity, operator UX, L2 fingerprint check. Folding it into this spec would make this doc unreadable.
2. **Phasing.** The CLI is post-rc2 (§11). This contract spec ships pre-rc2 to unblock `v2-⑥-1bc / 2bc / 3bc / AlertRule`. Coupling the CLI's spec to this one would block all four pre-rc2 children on a much heavier post-rc2 design.

What this spec **must** lock down: the **interface** the CLI presents to the runtime — specifically, that subcontracts 5-7 (§8) name three deterministic behaviors the rescue CLI implements and that downstream Family ⑥ tickets can cite §8 by index when writing AC. The CLI's own implementation spec then satisfies §8.

### §7.3 The CLI is the ONLY supported downgrade path

Per ADR-0036: the only authority for moving DB schema backward is the operator via the rescue CLI. No other path is supported:

- The startup hook (§6) does not downgrade.
- `/readyz` does not provide a downgrade endpoint.
- The alembic CLI inside the container CAN technically downgrade (`alembic downgrade <rev>`) but doing so bypasses the backup, audit, and eligibility check; this is **forbidden** for production DBs and the SOP doc (`docs/sop/...`) must call it out as a rule.

If an operator runs raw `alembic downgrade` against prod, that's an incident class — caught after-the-fact by the missing `runner_audit_events` row plus the absence of a `*.sql.gpg` backup. The contract is "always via `omnisight rescue`"; deviations are audit gaps.

---

## §8. §3.0.6 seven deterministic subcontracts

Verbatim-equivalent to parent spec §3.0.6, re-enumerated here with stable indices that downstream tickets (`v2-⑥-2bc`, `v2-⑥-RescueCLI`, `v2-⑥-DRDrillBackward`, etc.) cite by **subcontract number**, not by paraphrase. The names below ARE the names; do not rename in derivative work.

### §8.1 Subcontract 1 — Migration lock acquire

- **What:** Postgres advisory lock acquisition via `pg_try_advisory_lock` (key `alembic_lock`) before any alembic upgrade.
- **Where:** §6.3 (startup hook); the rescue CLI also acquires this lock before its downgrade (the backup itself can be taken without the lock; the downgrade cannot).
- **Contract test:** 2 concurrent backend replicas startup against the same DB with `image_head > db_head`; **exactly one** runs upgrade; the other waits (max 60 s); on lock release the waiter re-reads `db_head`, sees EQUAL, no-ops; both replicas serve traffic afterward.
- **Failure injection:** kill the lock-holder mid-upgrade (`kill -9`); `pg_advisory_unlock_all` releases the lock on connection close; the waiter acquires, re-reads, runs the remaining migrations from the half-applied state.
- **Implementing ticket:** `v2-⑥-2bc`.

### §8.2 Subcontract 2 — Image-head resolution

- **What:** the image's `alembic_head_in_image` is the single value baked into `/app/MANIFEST.json` per Family ⑤ §4. The hook + gauge read from MANIFEST, NOT from a runtime filesystem scan of `backend/alembic/versions/` (filesystem is tamperable post-build; the MANIFEST is immutable per Family ⑤ §4.3).
- **Where:** §6 (hook); §4 (gauge); subcontract 2's invariant guards the `image_head` value used everywhere else.
- **Build-time invariant:** `alembic heads` MUST return exactly one head; multi-head fails the build per §9.
- **Contract test:** image build on a branch with 2 alembic heads (synthetic): build exits non-zero with `multi_head_remediation` line in stderr. Runtime: MANIFEST tampered (`/app/MANIFEST.json` rewritten to a non-existent revision) → hook computes UNKNOWN per §5.3 → exits 78.
- **Implementing tickets:** `v2-⑤-Dockerfile-Manifest` (build-time bake + multi-head fail-build); `v2-⑥-2bc` (runtime read).

### §8.3 Subcontract 3 — Forward startup hook

- **What:** if `image_head > db_head`, run `alembic upgrade head` inside the advisory lock. Idempotent — already-applied migration is a no-op.
- **Where:** §6.2 (success — AHEAD-and-upgraded); §6.4 (idempotency).
- **Contract test:** stale DB (`db_head=0200`) + fresh image (`image_head=0204`) → hook runs once, migrations `0201..0204` applied; second startup with same image: hook sees EQUAL, no-op (no second alembic call); a synthetic kill -9 mid-upgrade test asserts state recoverable on restart.
- **Implementing ticket:** `v2-⑥-2bc`.

### §8.4 Subcontract 4 — Backward fail-fast

- **What:** if `image_head < db_head`, refuse start. Exit 78. Structured log per §5.5.
- **Where:** §2.3 (rationale); §5.5 (log shape); §6.2 (fail — BEHIND).
- **Contract test:** rolled-back image (`image_head=0200`) + advanced DB (`db_head=0202`) → container exits 78; `docker inspect` shows `State.ExitCode: 78`; journald contains `event=alembic_drift_backward image_head=0200 db_head=0202`; the existing replica (if any) is unaffected; an `OmniSightAlembicDrift{drift_value=2}` alert fires within 5 min.
- **Implementing ticket:** `v2-⑥-2bc`. Cross-link: `v2-⑤-AutoRedeploy` §9.2 application of this invariant on the redeploy script side.

### §8.5 Subcontract 5 — Rescue backup capture

- **What:** before any rescue downgrade, `pg_dump` to GPG-encrypted file at `/var/omnisight/rescue-backups/<ts>-<from-rev>-<to-rev>.sql.gpg`. Encryption key per ADR-0034's operator-key custody chain.
- **Where:** §7.1 (rescue CLI scope).
- **Contract test:** rescue downgrade against synthetic DB → backup file written; backup decryptable with operator's private key; `psql` against the decrypted dump restores into a temp DB with row-counts matching pre-downgrade state.
- **Implementing ticket:** `v2-⑥-RescueCLI`.

### §8.6 Subcontract 6 — Downgrade eligibility matrix

- **What:** each alembic migration file's header MUST include `downgrade_safe: lossless | lossy-data | irreversible`. Rescue CLI walks `image_head..db_head` revisions, reads each header, refuses if ANY spanned migration is `irreversible`; warns + requires `--force` if ANY is `lossy-data`.
- **Where:** §7.1 (rescue CLI scope).
- **Migration-author obligation:** every NEW migration filed after `v2-⑥-RescueCLI` ships MUST set the tag; CI lint enforces. Existing migrations `0001..<current>` get the tag in a one-shot backfill ticket (out of scope for `v2-⑥-1a` — to be filed by `v2-⑥-RescueCLI` as a prerequisite).
- **Contract test:** rescue CLI run with an `irreversible` migration in the span → exits with `eligibility_error` non-zero; with `lossy-data` but no `--force` → exits with `force_required`; with `--force` + `lossy-data` → proceeds; with all `lossless` → proceeds silently.
- **Implementing ticket:** `v2-⑥-RescueCLI`.

### §8.7 Subcontract 7 — Audit event writer

- **What:** every rescue action writes a `runner_audit_events` row BEFORE the `alembic downgrade` invocation. Schema: `(event_type, actor_fingerprint, ticket_ref, before_state, after_state, evidence_path, timestamp)`. Row persists across rescue-process restart (i.e., killing the process mid-downgrade does not lose the audit record).
- **Where:** §7.1 (rescue CLI scope); ADR-0034 audit contract.
- **Contract test:** rescue CLI killed mid-flight (e.g., `kill -9` after the audit row is written but before alembic completes) → `runner_audit_events` row still queryable; row's `after_state` recorded as `pending` (or NULL); a follow-up "rescue resume" action would create a new row with the completing transition.
- **Implementing ticket:** `v2-⑥-RescueCLI`.

### §8.8 How downstream tickets cite the subcontracts

Pattern in AC:

```
* [ ] Implements §3.0.6 subcontract 3 (forward startup hook) per
      docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md §8.3
      — contract test passes; structured log emits on success.
```

Numbering is stable across versions of this spec; subcontract names are stable. If a future revision adds a subcontract 8, that addition is a forward-compat change; the existing 1-7 indices do not renumber.

---

## §9. Build-time multi-head invariant

Per Sprint S12.G v1.4 codex caveat: `alembic heads` returning >1 head at build time MUST fail the image build. This is enforced inside `scripts/bake-image-manifest.sh` per Family ⑤ §4.4; the contract here is on the **rationale** and on the **remediation** structure.

### §9.1 Why fail the build (not pick-first, not warn-and-continue)

Three reasons:

1. **Multi-head is a developer error, not a runtime condition.** A repository should have exactly one alembic head; >1 head means a branch merge happened without running `alembic merge`. The window between the bad merge and the image build is the cheapest moment to catch it; runtime can only complain after the broken image is in production.
2. **"Pick first" silently picks wrong.** Lexicographic ordering of revision IDs is not the semantic ordering of the alembic graph. Picking `head[0]` could pick either branch, and the resulting image would silently bake a `MANIFEST.alembic_head_in_image` that disagrees with the operator's mental model of "latest migration".
3. **The build is the latest enforcement point.** CI runs lint and tests; both can be bypassed (`--no-verify`, skipping tests locally). The image build is gated behind the docker-compose / GHCR push step that operators DO trust; failing the build there is the latest non-bypassable signal.

### §9.2 Structured remediation on multi-head

The bake script (per Family ⑤ §4.4) exits 92 with stderr:

```
bake-image-manifest: alembic heads count != 1 (got 2)
heads:
  0204_alpha_branch_change
  0204_beta_branch_change
remediation: run 'alembic -c backend/alembic.ini merge -m "<reason>" 0204_alpha_branch_change 0204_beta_branch_change' to converge branches; commit the resulting merge migration; rebuild image.
```

The remediation text MUST include the exact `alembic merge` invocation with the actual heads. This is the closest thing to a "click-fix" the developer can get; copy-paste-able is the bar.

### §9.3 No silent "pick-first" anywhere

The contract forbids any code path that picks one of multiple heads silently:

- The bake script: fails (§9.2).
- The startup hook: cannot encounter multi-head at runtime if the bake invariant holds; if it somehow does (operator manually rewrote MANIFEST), the hook computes UNKNOWN per §5.3.
- `/readyz`: returns `drift_state: UNKNOWN` per §5.3.
- The gauge: returns to the UNKNOWN gauge per §4.2.

A future tool or script that needs "the latest head" picks via the alembic graph DAG (e.g., `alembic heads --resolve-dependencies` if available), not by string sort.

### §9.4 Why not enforced at PR time (lint-only)?

Tempting because: catch the bad merge earlier, before any image build.

Rejected for two reasons:

1. **Lint is bypassable.** PR-time lint can be skipped (`git commit --no-verify`), the runner can ignore it for emergency rollback, the operator can override. Build-time enforcement is non-bypassable for any image that ships to GHCR.
2. **Lint adds, doesn't subtract.** A future PR-time lint that ALSO catches multi-head is a defense-in-depth improvement; this spec does not preclude it (`v2-⑥-MultiHeadLint` could be filed as a follow-up). The build-time check is the irreducible minimum.

The build-time check is the contract; PR-time lint is an optional reinforcement.

---

## §10. Cross-family delegation

This section sets the boundary against other Families so cross-family-shaped tickets can be filed unambiguously.

### §10.1 Family ⑧ (graceful shutdown) does NOT detect drift

The 2026-05-14 retro touched both: the Windows-host forced shutdown + the image/DB drift. The temptation is to put "post-shutdown drift check" into Family ⑧. **The contract refuses that coupling**:

- Family ⑧'s scope is shutdown ordering, grace periods, and PG WAL safety. It deals with the *shutdown event*, not with what state the system is in *after* the shutdown.
- A forced shutdown does not itself create drift; it only **exposes** pre-existing drift on the next startup. The detection of that drift is Family ⑥'s job via the gauge of §4 (which runs on every startup, not just post-forced-shutdown ones).
- A "Family ⑧ post-shutdown drift check" would either duplicate Family ⑥'s gauge or coordinate with it; either is a coupling cost without benefit.

Specifically, per parent spec §3 Family ⑧'s **D1 N/A rationale** ("a 'last shutdown was forced' signal would require Windows-side hook integration..."), Family ⑧ does NOT emit drift signals. Family ⑥ AlertRule (`OmniSightAlembicDrift`) is what fires after any restart — forced or not — if drift is present. Operator post-reboot runbook (Family ⑧ `v2-⑧-4a-Doc`) instructs the operator to **check the Family ⑥ alert dashboard**, not to run a Family ⑧-specific drift check.

### §10.2 Family ⑤ (image surfacing) is a redundant detector

Per Family ⑤ §8.1: the deployment-audit script's `T1.alembic_head_in_image vs T3.head` comparison is a **second, independent** check on the same invariant. By design.

- Family ⑥'s `alembic_drift` gauge is the in-process always-on detector (60 s cadence, runs inside the backend container).
- Family ⑤'s audit script is the out-of-band detector (daily systemd timer, runs on the host outside the backend container).

The two detectors are **expected to agree**. If they disagree (gauge says EQUAL, audit says drifted, or vice versa), one of them has a bug; the audit script's evidence file (Family ⑤ §6) is the forensic record that lets the diagnosis happen.

The `OmniSightAlembicDrift` rule's `expr` (per §4.5) reads ONLY the in-process gauge. The audit script's findings ride a separate gauge `omnisight_alembic_drift_detected_by_audit`; the `OmniSightAlembicDrift` rule's `expr` may be expanded in a follow-up to `OR` both gauges (defense-in-depth) but the v1 contract is the in-process gauge alone — audit is forensic-only in v1.

### §10.3 Family ⑦ (allowlist) — `/readyz` must stay public

The `/readyz` endpoint (whose 503 body §5 extends) is in `PUBLIC_PATH_ALLOWLIST` per Family ⑦. The `v2-⑥-3bc` implementer MUST verify the entry exists post-Family ⑦ refactor (`v2-⑦-2bc`); if a future change removes `/readyz` from the allowlist, the healthcheck chain breaks (similar to today's `/health` 401 symptom). Cross-link: `v2-⑦-ContractTest` covers this case.

### §10.4 Family ⑩ (runner) — runner logs `image_head` per pickup

The runner (`auto-runner-codex.py` / `auto-runner-jira.py` / `auto-runner-multi.py`) reads `/version` per Family ⑤ §8.2 to log image-version per pickup. The same read includes `alembic_head_in_image`; the runner records it in `runner_claims.external_refs`. This is forensic-only: a post-hoc question "which image was the runner using for OP-1153?" is answerable.

The runner does NOT gate pickup on drift: a backend with drift is still picked up (the runner is a Gerrit-side actor; its pickup decision is upstream of the backend's `/readyz`). If a backend's drift causes its `/readyz` to be 503, that affects backend-served requests but does not affect runner pickup.

### §10.5 Family ⑨ (auxiliary services) — N/A

Family ⑨'s auxiliary-service contract is "available-then-use, unavailable-then-skip". Drift detection is not auxiliary; it is a defense primitive. No coupling.

### §10.6 v2-AlertBridge — the cardinality budget

`OmniSightAlembicDrift` consumes one rule's worth of AlertBridge cardinality budget (`critical_labels: [family, instance]`; cap 10 instances). The freshness companion rule `OmniSightAlembicDriftStaleCollection` (per §4.4) consumes a second rule's worth. Total Family ⑥ contribution to AlertBridge: 2 rules; well within the family's allocated budget per AlertBridge §6.

---

## §11. Pre-rc2 vs post-rc2 phasing

The parent spec's filing-order arrows imply Family ⑥ is the highest-priority family. Within Family ⑥, the 10 tickets are NOT all equally urgent. This section defines the phasing.

### §11.1 Pre-rc2 children (ship before the v2 RC2 cut)

Required for rc2: detection + remediation + recovery + alert. Without these four, the 2026-05-14-class outage repeats.

| # | ID | Tier | Why pre-rc2 |
|---|---|---|---|
| 1 | `v2-⑥-1a` (THIS) | S | Spec doc + ADR; everything else cites by anchor. |
| 2 | `v2-⑥-2a` | S | ADR-0036 ratification — separate ticket so the ADR can land as a binding doc with its own review. |
| 3 | `v2-⑥-1bc` | S | The `alembic_drift` gauge — D1 detection primitive. Without this, the in-process detector doesn't exist. |
| 4 | `v2-⑥-2bc` | S | The startup hook (subcontracts 1, 3, 4) — D4 recovery + the BEHIND fail-fast. Without this, AHEAD doesn't auto-upgrade and BEHIND doesn't refuse-start. |
| 5 | `v2-⑥-3bc` | S | The `/readyz` 503 `remediation` field — D2 remediation surface. Without this, operators see today's bare `migration_pending` detail and must grep docs. |
| 6 | `v2-⑥-AlertRule` | S | The `OmniSightAlembicDrift` Prometheus rule — the alert that fires on the gauge. Without this, the gauge is silent. |

These six MUST land before rc2. The dependency arrows are: 1a → 2a → {1bc, 2bc, 3bc} → AlertRule (which is ALSO blockedBy `v2-AlertBridge-1bc`).

### §11.2 Post-rc2 children (ship after rc2 closes)

These are the durability-cum-soak tickets. They are not urgent for the next prod-restart outage (which the pre-rc2 set covers); they are urgent for the medium-term confidence.

| # | ID | Tier | Why post-rc2 |
|---|---|---|---|
| 7 | `v2-⑥-RescueCLI` | M | Operator rescue CLI; substantial design (subcontracts 5-7); operator-window deployment; not needed in the steady-state forward-only flow. Until it ships, BEHIND state is recoverable by "deploy newer image" (the §5.2 first option), which is sufficient. |
| 8 | `v2-⑥-DRDrillForward` | S | Chaos test for the forward hook. Important for confidence but the contract test (subcontract 3) already verifies the core invariant; the DR drill is "do it again in a chaos-mode harness". |
| 9 | `v2-⑥-DRDrillBackward` | S | Chaos test for the backward fail-fast + rescue CLI exercise. Requires `v2-⑥-RescueCLI` to exist, so it cannot ship before that. |
| 10 | `v2-⑥-Integration` | L | 14-day soak with synthetic drift injection. Requires all of the above; verifies 0 false-positive alerts. |

### §11.3 Phasing diagram

```
            ┌── v2-⑥-1a (THIS, OP-1153) ──┐  pre-rc2
            │                              │
            ▼                              ▼
   v2-⑥-2a (ADR-0036)                 v2-⑥-1bc (alembic_drift gauge)
            │                              │
            ▼                              ▼
   v2-⑥-2bc (startup hook,          v2-⑥-3bc (/readyz remediation)
   subcontracts 1, 3, 4)                    │
            │                              │
            └──────────────┬───────────────┘
                           ▼
                  v2-⑥-AlertRule         (also blockedBy v2-AlertBridge-1bc)
                           │
                           ▼                 ───────────────────────────
                       [rc2 cut]
                           │                 ───────────────────────────
                           ▼
                  v2-⑥-RescueCLI            post-rc2
                  (subcontracts 5, 6, 7)
                           │
            ┌──────────────┴──────────────┐
            ▼                             ▼
   v2-⑥-DRDrillForward          v2-⑥-DRDrillBackward
            │                             │
            └──────────────┬──────────────┘
                           ▼
                  v2-⑥-Integration (14d soak)
```

### §11.4 What happens if rc2 closes BEFORE the pre-rc2 set is done?

This is the failure mode the operator wants to avoid. The contract is:

- The operator (or release captain) does NOT close rc2 if any of the 6 pre-rc2 Family ⑥ tickets is still open.
- "Open" means: not Resolved + Done. A ticket in Code Review counts as open.
- If rc2 timeboxes (e.g., a fixed cut date), the rc2 captain MUST escalate Family ⑥ children to the top of the queue rather than slip rc2 past the open-Family-⑥ guard.

This is enforced socially (release captain's checklist), not by code; a future `v2-⑥-RC2GateCheck` ticket could automate the check but is not in scope.

---

## §12. Spec ticket notes

### §12.1 Runtime impact of this ticket: ZERO

No code in `backend/`, no migration, no env var, no CI yaml, no security policy, no devops change. The only files touched by OP-1153 are:

- `docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md` (this document, new)
- `docs/architecture/ADR-0036-forward-only-deploy-invariant.md` (companion ADR, new)
- `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` (one cross-reference line added to the Family ⑥ section)

All Code AC items are documentation (schemas as JSON / YAML examples, structured-log shapes, subcontract enumerations); all Deploy AC items are markdown-lint + the x-ref line; all Integration AC items are the §-anchors that downstream tickets cite; the Exercised AC item is verified when the first downstream impl ticket (likely `v2-⑥-1bc` or `v2-⑥-2a`) lands without amending this spec.

### §12.2 Class override note

This ticket is filed under **claude** class (specs / ADRs / architecture memos route to claude per coordination.md). The 9 downstream children mix codex (impl: `1bc`, `2bc`, `3bc`, `RescueCLI` partial) and claude (AlertRule, DR drills, integration, RescueCLI scoping). The class split mirrors the parent spec §3 Family ⑥ table.

### §12.3 Out of scope

Per parent spec + this spec:

- **Rescue CLI implementation details.** The subcontracts 5-7 specify behavior; the encryption key custody, the operator UX, the JIRA hook on rescue-exit are all `v2-⑥-RescueCLI`'s spec.
- **Multi-database / multi-tenant DB shape.** This contract assumes one Postgres DB per backend deployment. Multi-DB drift (one tenant ahead, another behind) is a hypothetical future shape and not in scope.
- **Non-Postgres backends.** The advisory lock semantics use Postgres `pg_try_advisory_lock`. A future SQLite or MySQL backend would need a different locking primitive; out of scope.
- **Migration content review.** Whether a specific migration `0203_*.py` is correct is the PR review's job; this contract assumes migrations are correct by construction. It catches whether they are *applied*, not whether they are *right*.
- **Rollback of code without rollback of DB.** If an operator rolls back the backend image but keeps the DB ahead, the contract says: container refuses to start. The operator's correct response is either (a) redeploy a newer image OR (b) rescue-downgrade. The contract does NOT enable "run old code against newer schema" — that would re-create the silent-corruption class.
- **PR-time multi-head lint.** §9.4 explicitly defers this.
- **Hot reload of `image_head`.** The MANIFEST is read at hook startup. A future "re-read MANIFEST on SIGHUP" capability is out of scope.

### §12.4 Anchors stable for downstream AC citation

Downstream tickets MUST cite by §-anchor (not by line number) when AC items reference this doc. Stable anchors (matching the GitHub markdown convention `#section-id`):

- `#§2-drift-class-definition--the-three-imagedb-state-combinations`
- `#§3-option-c-asymmetric-upgrade-rationale`
- `#§4-detection-contract-d1--the-alembic_drift-prometheus-gauge`
- `#§5-remediation-contract-d2--readyz-503-with-remediation-field`
- `#§6-recovery-contract-d4--forward-startup-hook`
- `#§7-rescue-contract-d5--v2-rescuecli-scope`
- `#§8-§306-seven-deterministic-subcontracts`
- `#§9-build-time-multi-head-invariant`
- `#§10-cross-family-delegation`
- `#§11-pre-rc2-vs-post-rc2-phasing`

For subcontract-by-index citations (e.g., from `v2-⑥-2bc` and `v2-⑥-RescueCLI`), the sub-anchors `#§81-subcontract-1--migration-lock-acquire` through `#§87-subcontract-7--audit-event-writer` are stable. If any section is renamed in a future revision, the anchor migration is a contract change and the v2-⑥-* tickets that cite it MUST be notified.

### §12.5 Acceptance Criteria mapping (this ticket)

The Code AC items in OP-1153 map to this doc:

- "≥600 lines, all 10 sections" — §1 (gap) + §2 (drift class) + §3 (Option C) + §4 (D1) + §5 (D2) + §6 (D4) + §7 (D5) + §8 (7 subcontracts) + §9 (build-time invariant) + §10 (cross-family) + §11 (phasing) + reading order + ticket notes.
- "ADR-0036 ≥150 lines, 4-part ADR format" — see `docs/architecture/ADR-0036-forward-only-deploy-invariant.md`.
- "Parent spec x-ref added" — `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` Family ⑥ section gains a cross-reference paragraph mirroring the Family ⑤/⑦/⑨ cross-references.

The Integration AC items are verified by downstream tickets citing this doc's §-anchors when filing.

The Exercised AC item is verified when `v2-⑥-1bc` or `v2-⑥-2a` lands.

---

*End of contract spec. — OP-1153 / v2-⑥-1a / 2026-05-16*
