# Cold-start Startup-1 must honor the audit's `expected` field — DESIGN (for codex audit)

**Status**: DRAFT for codex audit (2026-05-23). Small, surgical fix. Found by the
OP-1619 acting-flip shadow preview (2026-05-23): a fresh shadow cold-start on the
shipped code (~/sora-bridge @ e39432c, OP-1618) halted at Startup-1 trying to
`start_unit` **RETIRED** release-autopilot units.

## Problem
`run_deployment_audit` (pipeline_coordinator.py:916-963) parses
`scripts/deployment-audit.sh`'s JSONL rows but **drops the `expected` field** — it
builds each `InfraUnit` from `name` + `status` only (:957-963):
```python
units.append(InfraUnit(name=row.get("name","?"), live=row.get("status")=="OK"))
```
`InfraAuditResult.down` (:527-529) then returns every non-OK systemd row, and
`_startup_1_infra` (:1445-1473) calls `gw.start_unit(unit)` for EVERY down unit
(capped per `cold_start_max_infra`, OP-1618), then halts boot if any remain down.

But `deployment-audit.sh` already classifies each row `expected ∈ {yes, gated,
n-a}` (header :22/:32; the script only counts `expected=yes` reds as failures,
:60). Units the operator deliberately retired/gated are RED-but-not-failures.

**Live evidence (2026-05-23 audit):**
| unit | status | expected | note |
|---|---|---|---|
| `auto-promote-main.service` | RED | **n-a** | "RETIRED by release-train (ADR-0040/RT-01) — main being retired" (audit.sh:268) |
| `auto-promote-develop.timer` | RED | **n-a** | RETIRED (audit.sh:267) |
| `staging-gate-smoke.timer` | RED | **gated** | red until bucket-D digest-resolution (OP-1607) (audit.sh:273) |

Because the coordinator drops `expected`, all three land in `down`. In **shadow**
this is only recorded (ShadowColdStartGateway is observe-only — verified: the
logged `start_unit ok:true` did NOT actually start the unit). In **acting mode**
(OP-1619) the live gateway would `systemctl --user start` them — i.e. **start a
RETIRED develop→main release-autopilot unit** (`auto-promote-main.service`),
risking an unintended release promotion, and then halt boot on the still-down set.
The OP-1618 `cold_start_max_infra=1` cap limits the COUNT but not WHICH unit, so
it does not protect against this.

## Design — filter `down` to `expected=yes` only
1. `InfraUnit` (pipeline_coordinator.py:513-518) gains an `expected: str = "yes"`
   field (the audit row's classification).
2. `run_deployment_audit` (:957-963) reads `row.get("expected")` into it.
3. `InfraAuditResult.down` (:527-529) returns only units that are **`expected ==
   "yes"` AND not live**. `gated` / `n-a` non-OK units are NOT down (their
   inactivity is correct, not a failure → cold-start neither starts them nor
   halts on them).
4. No change to `_startup_1_infra`'s start/cap/halt loop — it just receives a
   correctly-filtered `down` list. (Halt semantics preserved for genuinely-down
   `expected=yes` infra.)

This mirrors the audit script's own failure rule (`status==RED && expected==yes`,
audit.sh:60) — the coordinator simply stops being more aggressive than the audit
it wraps.

## Design decisions (for audit)
- **D1 — unknown/missing `expected` → NOT expected-live (default-deny start).**
  A row with no `expected` (malformed / older audit) is treated as NOT
  must-start, so the coordinator never auto-starts a unit it cannot confirm is
  expected. Rationale: the failure mode we are fixing is *starting things we
  shouldn't*; the safe default is to under-start (the operator is alerted via the
  normal audit anyway), not to start an unknown unit. (Counter-view for codex: a
  genuinely-down `expected=yes` unit whose field went missing would then be
  silently not-started — but the audit script always emits `expected`, so a
  missing field signals a parse/contract break, where not-starting is safer.)
- **D2 — `gated` is treated like `n-a` (not started, not halted).** A gated unit
  is expected-red-until-a-condition; auto-starting it would fight the gate.
- **D3 — scope is read-side only.** No change to `deployment-audit.sh` (it is
  already correct). The bug is purely the coordinator dropping the field.

## Open questions for codex
- Q1: should `gated` ever be auto-started (e.g. a gate that's actually satisfiable
  by a start), or is "never start gated/n-a" correct for all current rows?
- Q2: is `expected` the right discriminator, or should the coordinator also honor
  a per-row "auto_start: false" hint (the audit has no such field today)?
- Q3: D1 default — confirm "missing expected → not-started" can't mask a real
  expected=yes outage (given the script always emits the field).
- Q4: anything else the acting-flip relies on from Startup-1 that this changes.

## Tests (planned)
- A non-OK `expected=n-a` row (e.g. auto-promote-main.service) → NOT in
  `audit.down` → `_startup_1_infra` does not call `start_unit`, does not halt.
- A non-OK `expected=gated` row → not in `down`.
- A non-OK `expected=yes` row → still in `down` → started/halted as today.
- Missing `expected` → not in `down` (D1).
- `InfraAuditResult.down` filter unit test + a `run_deployment_audit` parse test
  with a mixed-row fixture.
- Regression: shadow cold-start on the current host → Startup-1 no longer lists
  the 3 retired/gated units in `still_down`; does not halt on them.

## ✅ codex verdict: file-ready-with-conditions (audit 2026-05-23, 0 BLOCKER)
Root cause + fix confirmed correct (`coldstart-startup1-expected-units-codex-audit-2026-05-23.txt`).
Shipped-code anchors (develop == ~/sora-bridge @ OP-1618): the field is dropped at
`run_deployment_audit` (pipeline_coordinator.py:1471-1475), `down` at :1040-1042,
`_startup_1_infra` start loop at :2077-2089, halt/mention at :2096-2110. Carry these
conditions into the ticket ACs:
1. **Use an EXACT `expected == "yes"` predicate** in `InfraAuditResult.down` — do
   NOT `row.get("expected", "yes")` (defaulting missing→yes contradicts D1). Missing
   `expected` → not expected-live → not started.
2. **Regression guard**: the fix must KEEP starting the genuinely expected-live units
   `release-milestone-checker.timer` + `sora-bridge-sync.timer` (audit.sh:266/270) when
   they are down. A test must cover an `expected=yes` down unit still landing in `down`.
3. **Add a malformed-row test** (missing/unknown `expected` → not in `down`) — the only
   place D1 could silently regress (`test_run_deployment_audit_parses_units`,
   test_pipeline_coordinator_coldstart.py:939-965, doesn't cover `expected` today).
4. Confirmed no interaction with OP-1618 caps / halt / shadow-acting split (fix changes
   the candidate set BEFORE the cap loop).

## Relationship to the acting flip (OP-1619)
This is a HARD pre-flip blocker: until it lands + is shadow-verified, flipping
`acting=True` would attempt to start retired release-autopilot units. It is NOT
part of the §6 executor epic (OP-1612) — different subsystem (Startup-1 infra).
File standalone; **OP-1619 blockedBy this ticket**.
