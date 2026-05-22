# Cold-start Startup-1 expected-units fix — TICKET DRAFT (codex-audited, for filing)

**Status**: DRAFT (2026-05-23). Spec = `docs/operations/2026-05-23-coldstart-startup1-expected-units-design.md`
(codex verdict: file-ready-with-conditions, 0 BLOCKER). Single standalone tier:M
ticket. **Blocks OP-1619** (the acting flip): until this lands + is shadow-verified,
flipping `acting=True` would attempt to `systemctl start` RETIRED release-autopilot
units (auto-promote-main.service / auto-promote-develop.timer, both `expected=n-a`).

Filing notes (same protocol as the OP-1612 chain):
- issuetype **Story**, **tier:M** (single-file backend change to
  `pipeline_coordinator.py`; no new module / schema / security → not L; production
  code → not S), **class:subscription-codex**, `agent:auto`,
  `capability:enable=gerrit_push`, `scope:coord-coldstart`.
- Independent (no blockedBy) → **pickable immediately** once filed.
- Wire **OP-1619 blockedBy this ticket** after creation.
- Spec-ref requires the design doc on develop first (commit like Gerrit #1148), OR
  the ticket runs self-contained on the ACs below (they are complete).

---

## T — [OP][coord-coldstart] Honor deployment-audit `expected` field in cold-start Startup-1
- **issuetype**: Story · **tier**: M · **area**: `area:backend`, `area:tests`
- **labels**: `scope:coord-coldstart`, `type:bug`, `agent:auto`, `tier:M`,
  `area:backend`, `area:tests`, `capability:enable=gerrit_push`, `class:subscription-codex`
- **## Files / Paths**: `backend/agents/pipeline_coordinator.py` (`InfraUnit` ~:513,
  `InfraAuditResult.down` ~:1040, `run_deployment_audit` ~:1471), 
  `backend/tests/test_pipeline_coordinator_coldstart.py` (extend
  `test_run_deployment_audit_parses_units` + the `_units` helper, ~:314/:939).
  **Out of scope**: `scripts/deployment-audit.sh` (already correct — it emits
  `expected` per row); `_startup_1_infra` start/cap/halt loop (unchanged — it just
  receives a correctly-filtered `down`); the §6 executor (OP-1612).

- **Goal**: cold-start Startup-1 must only try to start units the deployment audit
  marks `expected=yes`. Today `run_deployment_audit` drops the audit row's `expected`
  field, so RETIRED (`n-a`) and `gated` units land in `audit.down` and Startup-1
  tries to `start_unit` them — in acting mode that would start RETIRED develop→main
  release-autopilot units. (Found by the OP-1619 acting-flip shadow preview.)

- **Code AC**:
  - `InfraUnit` (pipeline_coordinator.py ~:513) gains an `expected: str = "yes"` field.
  - `run_deployment_audit` (~:1471) populates it from the JSONL row: pass the RAW
    `row.get("expected")` through (do NOT default a missing value to `"yes"`).
  - `InfraAuditResult.down` (~:1040) returns a unit ONLY when `expected == "yes"`
    (exact predicate) **AND** `not live`. Units with `expected ∈ {gated, n-a}`, or a
    missing/unknown `expected`, are NOT in `down` (default-deny — D1).
  - No change to `_startup_1_infra` (~:2077) / the OP-1618 `cold_start_max_infra` cap
    / halt + mention_operator path: they operate on the filtered `down` unchanged.

- **Deploy AC**: ships in the coordinator package; behavior change is only at
  cold-start boot. `deployed:` = present in the next coordinator build + the running
  `~/sora-bridge` synced + coordinator restarted (Python won't reload in-process).

- **Integration AC**: a deployment-audit row that is non-OK with `expected=n-a`
  (e.g. `auto-promote-main.service`) or `expected=gated` (e.g.
  `staging-gate-smoke.timer`) → NOT in `audit.down` → Startup-1 does not call
  `start_unit` for it and does not halt on it. A non-OK `expected=yes` unit (e.g.
  `release-milestone-checker.timer`, `sora-bridge-sync.timer`) → STILL in `down` →
  started/halted exactly as today.

- **Exercised AC** (tests):
  - `InfraAuditResult.down` filters to `expected=="yes" and not live` (unit test
    with mixed `yes`/`gated`/`n-a` units).
  - **Regression**: an `expected=yes` down unit (release-milestone-checker.timer /
    sora-bridge-sync.timer) still appears in `down` (must keep starting).
  - **Malformed-row guard (D1)**: a row with missing/unknown `expected` → NOT in
    `down` (extend `test_run_deployment_audit_parses_units`,
    test_pipeline_coordinator_coldstart.py:939-965, which doesn't cover `expected`
    today; the `_units` helper ~:314 builds 2-field units → add `expected`).
  - Regression on the live host (manual / Exercised note): a fresh **shadow**
    cold-start no longer lists `auto-promote-main.service` /
    `auto-promote-develop.timer` / `staging-gate-smoke.timer` in `still_down`, and
    does not halt at Startup-1 on them.

- **Go-Live**: shadow-safe (Startup-1 change only affects which units it would
  start; verified in shadow before the OP-1619 flip).

## Dependencies
- No blockedBy (standalone; pickable immediately).
- **Blocks OP-1619** — wire `OP-1619 blockedBy <this>` so the acting flip waits for
  this fix + its shadow verification.

## Verdict
codex file-ready-with-conditions; 4 conditions folded into the ACs above
(exact `=="yes"` predicate; keep expected=yes units starting; malformed-row test;
no cap/halt interaction). Ready to file pending operator OK.
