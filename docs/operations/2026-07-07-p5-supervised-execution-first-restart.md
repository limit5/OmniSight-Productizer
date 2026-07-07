# P5 supervised execution — arming + first real restart (2026-07-07)

**Audience**: operators + agents working the Sora P5 propose-and-approve line. This is the
operation record of the day the execution gate was ARMED on prod (`OMNISIGHT_P5_EXECUTE=1`)
and the **first real, human-approved, host-executed restart** ran end-to-end. Companion to
the P5 code line (Gerrit #2000 approve/execute gate, #2002 watch running-digest, #2004 host
executor; shipped across v0.7.35–v0.7.37) and the v0.7.37 ledger (#2005).

> **TL;DR**: the full chain — Sora *proposes* → a human admin *approves* (strict, human-only
> endpoint) → the approve *defers* to the host → the host executor *atomically claims* and runs
> `systemctl --user restart` → terminal row write — was exercised for real on prod on
> 2026-07-07 and behaved exactly as designed, with **zero impact on anything else running on
> the shared host**. Resting state: flag **ON**, executor **manual-only** (no timer). The next
> deliberate gate (autonomous timer) is NOT crossed.

---

## 1. Timeline (CST, 2026-07-07)

| Time | Event |
|---|---|
| 13:02 | prod pre-deploy backup `prod-omnisight-pre-v0.7.37-20260707-130234.dump` (2,035,757 B) |
| 13:12 | **v0.7.37 deployed to prod** (zero-downtime rolling; backend `sha256:c43a1208…`, frontend `sha256:33b4cef5…`; alembic no-op, head 0257). Promote had used `--skip-staging-gate` — staging `/api/version` reported a stale baked bundle_id while the deployed digest was correct and canary green. |
| ~16:5x | Ledger #2005 + stale-test cleanup #2006 merged (operator +2; ledger submit requires the `Human-Plus-2` SR — AI +1 cannot satisfy it) |
| 17:0x | **Phase 0 preflight** (read-only): merges verified; `systemd --user` alive; both allowlist units healthy; executor module loads with allowlist `{omnisight-slo-monitor.service, pipeline-coordinator.service}`; `proposed_actions` clean; flag OFF |
| 17:0x | **Phase 1 guard proof**: executor refused **31/31** foreign/dangerous targets (tcm / neo4j / cognee / pg-primary / pg-standby / prod+staging+dev containers / sshd / docker / injection strings / flag-looking args / trailing-space + non-string variants) and accepted **exactly** the 2 allowlisted units. Real-DB dry-run pass clean. 7 pre-existing failed `--user` units noted (dev/staging compose, mirrors, reports) — unrelated, untouched |
| 17:1x | **Phase 2 arming**: `OMNISIGHT_P5_EXECUTE=1` appended to `/home/user/omnisight-prod/.env`; rolling recreate `backend-a` → readyz (15 s) → `backend-b` → readyz (15 s). Image-neutral by construction: compose `image:` resolves to the digest-pinned `OMNISIGHT_BACKEND_IMAGE_REF@sha256:c43a1208…` == the running image. `is_execute_enabled() == True` verified in-container on both replicas; public health `online` throughout |
| 17:19:32 | Proposal **`pa-81dac14bdf75`** filed (`restart: omnisight-slo-monitor.service`, `proposed_by=sora`, status `pending`) |
| 17:27:10 | **Human approve** by `admin@omnisight.local`, reason *"first supervised restart — slo-monitor"* (browser session: strict cookie + CSRF; the endpoint's `_assert_human_operator` refuses bot/API-key principals). Response: `status=executing`, `execution.deferred_to_host=true` |
| 17:28:0x | Host executor **dry-run** pass: previewed exactly `omnisight-slo-monitor.service`; **zero DB writes** (row stayed `executing`), unit untouched (MainPID still 1511352) |
| 17:28:43 | Host executor **`--execute`**: atomic claim (`executing`→`host_running` CAS) → `systemctl --user restart -- omnisight-slo-monitor.service` → **rc=0** → row → **`executed`** with result + `executed_at` |

## 2. Evidence

**The restart really happened, and only it:**

- `omnisight-slo-monitor.service`: MainPID **1511352 → 2810836**, `active (running)` since 17:28:43, ~15.7 M RSS, healthy.
- Row `pa-81dac14bdf75` terminal: ``executed | host executor ran `systemctl --user restart omnisight-slo-monitor.service` -> executed (rc=0)``.
- Full audit chain on one row: `proposed_by=sora` → `decided_by=admin@omnisight.local` + `decision_reason` → `executed_at`.

**Isolation sweep (the shared host runs OTHER services — none disturbed):**

| Target | State after |
|---|---|
| `pipeline-coordinator.service` (the *other* allowlisted unit) | MainPID 404 unchanged (active since 2026-06-15) |
| `omnisight-tcm-web/-server/-db` | Up 4 h / 4 h / 27 h — unchanged |
| `tcm_pool` | Up 4 days — unchanged |
| `omnisight-neo4j` | Up 2 weeks — unchanged |
| `omnisight-cognee` | Up 3 weeks — unchanged |
| `omnisight-pg-primary` / `-standby` | Up 3 weeks — unchanged |
| prod backend | `/readyz` ready (migrations current=0257), `/api/v1/health` online |
| `proposed_actions` | no lingering `executing` / `host_running` rows |

## 3. The guards in force (why this cannot reach anything else)

1. **Sora can only propose.** Her toolset is `propose_action` + `list_pending_actions` — no
   approve, no execute. A chat-side injection can at worst file an inert `pending` row.
2. **Approve is human-only.** `POST /api/v1/proposed-actions/{id}/approve` = admin auth
   (strict cookie+CSRF) **+** `_assert_human_operator` (403 for `apikey:`/`-bot`/`ci-`/`ai-`
   principals) **+** mandatory reason. Verified live both ways.
3. **Only `restart` can ever execute.** `action_executor.EXECUTABLE_KINDS = {"restart"}`;
   `deploy`/`promote`/`rollback` are `PROPOSAL_ONLY_KINDS` — flag ON or OFF, they never auto-run.
4. **Blast radius = 2 hardcoded units**, both fully recoverable (slo-monitor re-reads state on
   boot; pipeline-coordinator has its own watchdog). Host executor keeps a **hardcoded**
   (deliberately not env-overridable) allowlist **plus** an independent `^(omnisight-|pipeline-coordinator)`
   prefix guard; single verb `systemctl --user restart -- <unit>`, argv (no shell).
5. **Nothing runs autonomously.** No timer / service / cron / live process runs the executor
   (verified). An approved restart just queues as `executing` until a human runs the executor.
6. **Crash-safety**: approve marks `executing` only *after* the gate decides to defer (a dry-run /
   flag-off / refused approval can never be picked up by the host); executor claims atomically
   before the side effect (no double-restart across overlapping passes); stranded `host_running`
   rows are reaped to `failed (stranded)` after 300 s, never auto-retried.

## 4. Resting state (as of 2026-07-07 evening — operator decision: keep armed)

- `OMNISIGHT_P5_EXECUTE=1` at `/home/user/omnisight-prod/.env:201`, live in both backend replicas.
- Host executor `scripts/p5_host_executor.py` — **manual runs only**, dry-run by default.
- Assessed low-risk to keep armed: with no autonomous runner, "armed" only changes what an
  *already-human-approved* restart does when a human *also* runs the executor by hand.

## 5. Recipe — run the next supervised restart

1. Have Sora file the proposal (chat: ask her to restart an allowlisted unit), or insert a
   `pending` `proposed_actions` row (kind `restart`, params `{"service":"<allowlisted unit>"}`).
2. Human admin approves — from a logged-in prod browser session (DevTools console):
   ```js
   fetch('/api/v1/proposed-actions/<id>/approve', {
     method: 'POST',
     headers: { 'X-CSRF-Token': document.cookie.match(/omnisight_csrf=([^;]+)/)[1] },
     body: new URLSearchParams({ reason: '<why>' })
   }).then(r => r.json()).then(console.log)
   ```
   Expect `status: "executing"`, `execution.deferred_to_host: true`.
3. On the host: `python3 scripts/p5_host_executor.py` (**dry-run** — confirm it previews exactly
   the intended unit, nothing else), then `python3 scripts/p5_host_executor.py --execute`.
4. Verify: unit MainPID changed + `active (running)`; row `executed`; spot-check neighbours untouched.

## 6. Disarm (≈30 s)

Remove the `OMNISIGHT_P5_EXECUTE=1` line from `/home/user/omnisight-prod/.env`, then rolling
`docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate backend-a` (wait
readyz) and same for `backend-b`. Approvals then revert to dry-run / proposal-only.

## 7. The next deliberate gate — NOT crossed

Installing the executor as an always-on `systemd --user` timer = **true autonomy**: an approved
restart would fire *without* a human running the executor. Prerequisites before that flip:
monitoring/alerting on `proposed_actions` + unit health, and an explicit operator sign-off.
Until then, "Sora on watch" means: armed, human-approved, **and** human-executed.

## References

- Code: Gerrit #2000 (approve/execute gate, v0.7.35), #2002 (watch running-digest, v0.7.36),
  #2004 (host executor, v0.7.37); propose gate shipped v0.7.34. Migrations 0256 (`proposed_actions`),
  0257 (`decision_reason`, `executed_at`).
- Ledger: #2005 (v0.7.37); test cleanup #2006. Ticket: OP-2530.
- Anti-pattern closed: `docs/sop/architecture-anti-patterns.md` **#13 Shipped-but-not-deployed** —
  this operation is the "Exercised" AC of the P5 gate, run for real on prod.
- Lesson: `docs/sop/lessons/L-OP-2530-prod-env-flag-flip-needs-boot-safety-and-image-neutrality-proof.md`
