# Item-3 SLO-monitor fix family — TICKET DRAFT (codex-audited, r2)

**Revision r2 (2026-05-23)** — codex coverage + code-correctness audit
(`slo-monitor-fix-ticket-draft-codex-audit-2026-05-23.txt`) returned **needs-revision**;
all fixes applied (coverage + dependency graph + F4 manifest/deploy wiring confirmed OK):
- F1: tightened the metric/label contract — exact names, `route` (not default `handler`)
  + raw-numeric `status` (so `=~"5.."` matches, NOT grouped `5xx`); must register on
  `backend.metrics.REGISTRY`; don't assume a stock instrumentator matches.
- F2: corrected OP-883 line refs (cooldown :347-355 / suppress :357-364 /
  MetricSourceUnavailable :372-381); changed "retire" → **deprecation shim** + update 3
  referrers; added an explicit **G6 canary-guard** Code AC.
- F3: corrected backend-b port to **8001** (not 8000).
Ready to file pending operator OK.

# Item-3 SLO-monitor fix family — TICKET DRAFT (for codex coverage + code-correctness audit)

**Status**: DRAFT (2026-05-23). Source = `docs/operations/2026-05-23-slo-monitor-autorollback-audit.md`
(codex-verified: core risk real; fix order G2→G4→G8→G3→G5/G1). META + F1/F2/F3 impl +
F4 GATE. codex is asked to confirm BOTH (a) 1:1 coverage of the audit's fix scope and
(b) the tickets' technical claims + file:line are CORRECT against the shipped code
(`~/sora-bridge`), so nothing is mis-specified before filing.

Conventions: 4-AC each; area:* spans all AC areas; `[META|OP|GATE][scope:slo-monitor]`;
F4 is the only operator-gated tier:X (it activates a prod-mutating auto-rollback).
Default = nothing activated until F4. Filing protocol: commit the audit + this draft to
develop tagged with the META key (**type:meta** so the bridge doesn't false-walk it —
see [[feedback_doc_commit_impl_key_h12_autowalk]]); file F1/F2/F3 pickable; F4 parked tier:X.

---

## META — [META][slo-monitor] Repair + activate SLO-monitor auto-rollback (Boreas-B item 3)
- **type**: meta · **labels**: `scope:slo-monitor`, `area:backend`, `area:devops`
- **Goal**: make the OP-772/OP-883 SLO-monitor + auto-rollback FUNCTIONAL and DEPLOYED.
  Today it is shipped-but-not-deployed AND would be a dead safety net even if installed
  (queries metrics the app doesn't export → always-healthy → never rolls back).
- **Children**: F1 (metric contract) / F2 (canonical monitor + unit repoint) / F3 (scrape
  both replicas) / F4 (GATE: install + activate). **Relates**: OP-1133 Boreas-B.

## F1 — [OP][slo-monitor] Export HTTP RED metrics (http_requests_total + duration) — UNBLOCKER
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests`
- **labels**: `scope:slo-monitor`, `type:bug`, `agent:auto`, `tier:M`, `area:backend`, `area:tests`, `capability:enable=gerrit_push`, `class:subscription-codex`
- **## Files/Paths**: `backend/main.py` (add ASGI metrics middleware/instrumentator),
  `backend/requirements.in` (add the instrumentator dep; currently only `prometheus-client`
  ~:88), `backend/metrics.py` / `backend/routers/observability.py` (the /api/v1/metrics
  registry, ~:57/:962 — ensure the new series register on the SAME exposition).
  **Out of scope**: the monitors themselves (F2), prometheus scrape (F3).
- **Problem**: the SLO monitors query `http_requests_total{route,status}` +
  `http_request_duration_seconds_bucket` but NOTHING exports them (no instrumentator;
  `backend/metrics.py` exports only domain metrics) → PromQL empty → `total<=0` →
  monitor reads healthy → never rolls back (audit G2). codex confirmed.
- **Code AC**: the backend exposes, at `/api/v1/metrics`, EXACTLY
  `http_requests_total` (Counter) + `http_request_duration_seconds_bucket` (Histogram),
  the metric names the monitors hard-code (slo_monitor.py:129-136 / orchestrator
  slo_monitor.py:595-603). Labels MUST match what the monitors query:
  - `route` = the matched route TEMPLATE (not raw path — cardinality) AND
  - `status` = the **raw numeric HTTP status code** (e.g. `500`, `502`), so the
    monitors' `status=~"5.."` matches. Do NOT emit a grouped `5xx` status (`=~"5.."`
    would NOT match "5xx") and do NOT use the default instrumentator label `handler`
    (the monitors expect `route`).
  - **Implementation note (codex):** a stock FastAPI/ASGI instrumentator emits `handler`
    + may group status — so it must be CONFIGURED to emit `route`+raw-`status`, OR
    custom middleware must register the series directly. Either way, register into the
    EXISTING `backend.metrics.REGISTRY` that `/api/v1/metrics` renders
    (routers/observability.py:57-61, metrics.py:46-48/:962-966) — NOT the default global
    registry, or the series won't appear on that endpoint.
- **Deploy AC**: ships in the backend image; visible at `/api/v1/metrics` after deploy.
- **Integration AC**: hit a 5xx route → `http_requests_total{status=~"5..",route=...}`
  increments (assert a 5xx status VALUE, not literal "500"); the SLO monitor's
  `_fetch_route` (slo_monitor.py:131-154) now returns a non-zero `total_value` + real
  `error_rate` against the live `/api/v1/metrics`.
- **Exercised AC**: test asserts both series on `/api/v1/metrics` with labels `route` +
  raw-`status` (registered on backend.metrics.REGISTRY); a request → counter increments;
  the duration histogram (`_bucket` + `le`) populated; a 5xx → error_rate computable by
  `status=~"5.."`.
- **Go-Live**: safe (additive metrics). UNBLOCKS F4.

## F2 — [OP][slo-monitor] Make OP-883 orchestrator monitor canonical + repoint the unit
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:devops`, `area:tests` · **blockedBy**: (none — independent of F1)
- **labels**: `scope:slo-monitor`, `type:bug`, `agent:auto`, `tier:M`, `area:backend`, `area:devops`, `area:tests`, `capability:enable=gerrit_push`, `class:subscription-codex`
- **## Files/Paths**: `deploy/systemd/omnisight-slo-monitor.service` (WorkingDirectory :14
  + ExecStart :17 — repoint to `~/sora-bridge` + its venv; module →
  `backend.orchestrator.slo_monitor`), `backend/slo_monitor.py` (OP-772 dup — retire or
  reduce to a thin shim), `config/slos.yaml` + `config/slo_thresholds.yaml` (G7: reconcile
  to ONE source for the canonical monitor). **Out of scope**: metric export (F1), scrape (F3).
- **Problem (audit G4+G8+G7)**: the unit runs the weaker OP-772 `backend/slo_monitor.py`
  (no cooldown, no measurement-plane-unavailable handling), while the canonical OP-883
  `backend/orchestrator/slo_monitor.py` (cooldown :347-355, suppress :357-364,
  MetricSourceUnavailable :372-381 — per docs/operations/slo-runbook.md:15) is NOT wired.
  AND the unit's paths point to the stale `~/work/sora/OmniSight-Productizer` workdir
  (current unit :14-17), not `~/sora-bridge`.
- **Code AC**:
  - Unit `WorkingDirectory` + venv ExecStart (currently `omnisight-slo-monitor.service`
    :14-17) point to `~/sora-bridge` (the live develop checkout the coordinator runs
    from), and run the OP-883 module `python -m backend.orchestrator.slo_monitor`
    (confirmed runnable — has `main()`/`__main__` at orchestrator slo_monitor.py:739-756).
  - **Replace `backend/slo_monitor.py` (OP-772) with a deprecation SHIM** (import/forward
    to the orchestrator) — do NOT delete (codex: it's referenced by
    `backend/tests/test_slo_monitor.py:10`, `docs/operations/slo-monitor-rollback.md:10`,
    `docs/operations/release-runbook.md:322`). Update those 3 referrers to the canonical
    monitor. ONE canonical monitor, no live duplicate.
  - Reconcile the two SLO config files (OP-772 reads `config/slos.yaml` 0.5%; OP-883 reads
    `config/slo_thresholds.yaml` 1%) to ONE source of truth for the canonical monitor;
    document the chosen error budget.
  - **(G6) Guard the canary branch**: progressive prod canary is HOLD'd (OP-931/932/933),
    so `canary_state.json` never exists → ensure the canonical monitor defaults to full
    rollback + the canary path (orchestrator slo_monitor.py:652) is explicitly dormant /
    cannot error on the missing file. (Or split to a follow-up if larger than expected.)
  - NOTE: codex confirmed OP-883's refuse-when-blind only catches Prometheus CONNECTION
    failure, NOT missing-series — so F1 (real metrics) is still required; F2 does not
    make the monitor functional alone.
- **Deploy AC**: unit file updated in repo; takes effect on F4 install.
- **Integration AC**: `python -m backend.orchestrator.slo_monitor` (the new ExecStart)
  starts from ~/sora-bridge; the OP-772 path is gone/shimmed.
- **Exercised AC**: test/asserts the unit ExecStart references the orchestrator module +
  ~/sora-bridge; single canonical monitor importable; one slo config source.
- **Go-Live**: safe (no activation yet — F4 installs).

## F3 — [OP][slo-monitor] Prometheus scrape both backend-a + backend-b
- **issuetype**: Story · **tier**: M · **area**: `area:devops`, `area:tests` · **blockedBy**: (none)
- **labels**: `scope:slo-monitor`, `type:bug`, `agent:auto`, `tier:M`, `area:devops`, `area:tests`, `capability:enable=gerrit_push`, `class:subscription-codex`
- **## Files/Paths**: `configs/prometheus.yml` (job `omnisight-backend`, target
  `["backend:8000"]` ~:7-10). **Out of scope**: metrics (F1), monitor (F2).
- **Problem (audit G3, PARTIAL)**: scrape target is `backend:8000` (resolves to backend-a
  via its legacy `backend` alias, docker-compose.prod.yml:195-218) but **backend-b is
  NOT scraped** → replica-coverage gap (a degraded backend-b is invisible to the SLO
  monitor).
- **Code AC**: the scrape job targets BOTH replicas — **`backend-a:8000` + `backend-b:8001`**
  (codex: backend-b listens on **8001**, not 8000, docker-compose.prod.yml:269-285 — do
  NOT use backend-b:8000); per-replica series distinguishable (instance label).
- **Deploy AC**: prometheus.yml shipped; reloaded on next prod compose up.
- **Integration AC**: Prometheus targets page shows both backend-a + backend-b UP.
- **Exercised AC**: test/asserts both replicas in the scrape config.
- **Go-Live**: safe (monitoring config).

## F4 — [GATE][slo-monitor] Install + activate SLO-monitor auto-rollback 🔒 HUMAN-ONLY
- **issuetype**: Story · **tier**: **X** · **area**: `area:devops`, `area:docs` · **agent**: none
- **labels**: `scope:slo-monitor`, `requires:operator-approval`, `tier:X`
- **blockedBy**: F1, F2, F3
- **Problem (audit G1+G5)**: the unit is not installed/enabled (not in `~/.config/systemd/user/`),
  not in the deployment-audit manifest (scripts/deployment-audit.sh), so no live
  auto-rollback + invisible to the daily audit.
- **Code/Config AC**: install the (F2-fixed) unit to `~/.config/systemd/user/`, `enable
  --now`; add `omnisight-slo-monitor.service` to `scripts/deployment-audit.sh`'s built-in
  manifest (rows at :263-277, whitespace format `<kind> <name> <expected> <ticket>
  [note]`, :21-35) as a `systemd-unit ... yes` row; confirm the prod deploy SOP
  (`scripts/deploy-prod.sh` :275-291, G5) writes `OMNISIGHT_PREVIOUS_IMAGE_TAG` +
  restarts the unit.
- **Deploy AC**: unit active+enabled; appears OK in `deployment-audit.sh`.
- **Integration AC**: in STAGING (or a safe target), inject a synthetic SLO breach →
  monitor detects (real metrics from F1) → auto-rollback fires to previous tag → verify;
  measurement-plane-unavailable path refuses to roll (no false rollback).
- **Exercised AC**: synthetic-breach rollback observed end-to-end + a measurement-blind
  refuse-to-roll observed; update docs/operations/slo-runbook.md.
- **Go-Live**: operator-gated activation (prod auto-rollback goes live).

---

## Coverage map (audit finding → ticket)
| audit finding | ticket |
|---|---|
| G2 metric contract (BLOCKER) | F1 |
| G4 canonical OP-883 (SHOULD-FIX) | F2 |
| G8 unit path stale (BLOCKER) | F2 |
| G7 two slo config files (NICE) | F2 |
| G3 scrape both replicas (PARTIAL) | F3 |
| G1 install/enable + audit manifest (BLOCKER) | F4 |
| G5 deploy path writes previous_tag (PARTIAL) | F4 (confirm) |
| G6 canary dead branch (NICE) | F2 (now an explicit Code AC — guard the dormant canary branch) |

## Audit asks for codex (this round)
1. **Coverage**: does F1–F4 cover the audit's fix scope (G1–G8) with no gap / no scope creep?
2. **CODE-CORRECTNESS (the key ask)**: read the shipped code (`~/sora-bridge`) and confirm
   the tickets' technical claims + file:line are RIGHT — esp: F1's instrumentator approach
   + the `route`/`status` label contract the monitors actually expect (slo_monitor.py
   `_fetch_route` PromQL); F2's orchestrator module path + the unit ExecStart it should
   point to + that retiring backend/slo_monitor.py breaks nothing else (who imports it?);
   F3's prometheus.yml job shape; F4's deployment-audit manifest format + deploy-prod.sh
   wiring. Flag anything mis-specified.
3. 4-AC / area-label / tier correctness; F4 correctly the only tier:X gate; dependency
   graph (F4 blockedBy F1+F2+F3; F1/F2/F3 independent).
Verdict: file-ready / needs-revision.
