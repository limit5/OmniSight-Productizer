# EPIC design — Case 8 production-line machine-vision inspection, software-firmware-only

Date: 2026-06-12
Status: v2 — Plan-agent review folded (13 findings: 5 MUST + 6 SHOULD + 2 NOTE); ready for operator +2 → wave filing
Template: docs/architecture/2026-06-04-cases-6-7-software-firmware-only-dual-epic-design.md
Related: 1F system-of-systems planner (verified case-8 composition:
backend/tests/test_product_planner.py::TestRealCaseEightComposition),
Phase 0 substrate, vendor-mirrors-nda (RV1126 SDK + rknn-toolkit 1.7.0/1.7.3 +
rknpu + RK3588 SDK mirrored; catalog rows in vendor-mirrors-nda/catalog).

## 0. The 4-line summary

Case 8 = the last T5 customer: AI defect-detection on a factory line (camera →
NPU inference → reject actuation via PLC → traceability). This EPIC ships the
**software/firmware stack only**, verified against a line-simulation harness;
real cameras/PLC hardware, machine-safety certification and on-site
commissioning are explicitly deferred follow-up METAs.

## 1. Premise-check summary (Stage 1 SOP; reviewer-verified)

- **1F composes the case-8 shape** — verified: `TestRealCaseEightComposition`
  (test_product_planner.py:456-549) composes the real imaging + connectivity +
  npu-detection packs, asserts multi-require wiring + SoC reconciliation.
  ⚠️ **BUT only on rk3566/rk3568**: `imaging/skill.yaml` `compatible_socs` does
  NOT include rv1126/rk3588 — retargeting raises `SoCConflict` (planner
  hard-fail, test at :539-549). Extending imaging's SoC list is a Wave-1 leaf.
- **Pack inventory**: imaging/connectivity/telemetry are full packs;
  **npu-detection is a STUB** (tokens-only skill.yaml, no tasks/scaffolds/
  tests — falls back to `_embedded_base`, see test:486). C8.B *creates* its
  real content, not "extends" it. Missing entirely: inspection-recipe,
  line-hmi, traceability, reject/tact logic.
- **connectivity already owns Modbus + the `plc-transport` token**
  (connectivity/tasks.yaml:169,181; skill.yaml:14). A second provider of
  `plc-transport` raises `DuplicateProvideError` (product_planner.py:117-124).
  C8.C therefore builds ON TOP of connectivity (token untouched) and provides
  NEW tokens (`reject-gate`, `tact-sync`).
- **NPU toolchains are TWO generations**: rknn-toolkit 1.7.x = RKNPU-v1
  (RV1126); RK3588 = RKNPU2 and needs **rknn-toolkit2 + librknnrt** — a 1.7.3
  .rknn will not load on RK3588. We mirror 1.7.0/1.7.3 + rknpu (RV1126 leg ✅);
  **rknn-toolkit2 is NOT mirrored** → RK3588 inference is descoped to the
  hardware follow-up META unless/until toolkit2 is mirrored (one vendor-mirror
  errand, listed in §6).
- **Machine-safety**: ISO 13849/IEC 62061 is a physical/cert track. Software
  here MONITORS safety state and fails closed on product disposition only.
  Canonical invariant in D3.

## 2. Target architecture

```
[camera (V4L2/MIPI | GigE sim)] →(strobe/exposure sync)→ frame ring
        ↑ trigger                       ↓
[line-tact sync] ──── [RKNN defect-detect (NPU, RV1126)] → verdict {part_id,…}
        ↑                                   ↓
[PLC adapter: connectivity/modbus] ← [part-tracking verdict queue] → reject @ part N
                                            ↓
[recipe engine (versioned, audited)]   [traceability: results DB, image archive,
[HMI: Qt operator panel + SPC]          batch report, MES export (CSV/REST)]
```

Architectural decisions:
- **D1 platform**: primary **RV1126** (AI camera SoC; rknn 1.7.3 + SDK
  mirrored). RK3588 = composition/build target only in this EPIC (its
  *inference* leg needs rknn-toolkit2 — not mirrored; follow-up). CI runs
  qemu/host-sim. Wave 1 includes the `imaging` `compatible_socs` extension
  leaf (+ registry/planner test updates) so the composition premise holds on
  RV1126.
- **D2 fieldbus**: reuse connectivity's Modbus TCP/RTU (transport + token stay
  in connectivity). C8.C adds the line-logic layer above it: `FieldbusAdapter`
  interface (PROFINET/EtherCAT/OPC-UA land later), reject-gate + tact-sync
  protocol, register maps **recipe-driven** (Wave-1 code carries a marked
  re-plumb point until C8.D lands — accepted, noted in the leaf). **Timing
  ownership: the PLC owns actuation timing.** Software's contract = "verdict
  for part N delivered ≥ X ms before its reject window", asserted in the PLC
  sim; Modbus poll cycle must fit inside tact budget; discrete-I/O / hardware
  trigger is the documented fallback when it can't.
- **D3 safety posture — canonical invariant (copy verbatim into every C8.C
  ticket):** *"This software is never part of the e-stop/safety chain and
  claims no safety function (ISO 13849/IEC 62061 out of scope). It MONITORS
  safety state; on unknown/violated state it WITHHOLDS the pass signal —
  fail-closed protects product disposition only; the PLC decides line
  behavior."*
- **D4 inference**: C8.B builds npu-detection into a real pack (tasks/
  scaffolds/tests/hil) with a defect-inspection profile (anomaly threshold +
  classification head + golden-sample diff). Conversion recipe pins the
  mirrored rknn-toolkit 1.7.3 (RV1126). Host-ONNX path exists for **pipeline
  CI only** — FP32 vs INT8: it validates plumbing, never numerics; tickets
  must not claim accuracy from it. Models are placeholders; customer models =
  on-site phase.
- **D5 HMI**: Qt operator panel. Pattern references: the mirrored AlienTek
  factory-qt suite (vendor-mirrors-nda catalog row
  `alientek-atk-rv1126-examples`, project atk-rv1126-examples — QDesktop incl
  rknn model usage) and Case 2 UVCCamera_Qt (reference only). HMI includes
  auth/roles (reuse `security` pack) — recipe/threshold changes are
  authenticated + audit-logged (traceability without change-control is not
  credible inspection software). Thin read-only web status endpoint rides on
  connectivity.

## 3. Sub-EPIC breakdown (6, ~33 leaves)

- **C8.A vision-acquisition** (~6): `imaging` compatible_socs extension
  (rv1126, rk3588) + planner/registry test updates; camera abstraction
  (V4L2/MIPI via Phase 0 + GigE-Vision *simulator* client); HW/SW trigger
  model **incl. strobe/exposure-sync contract** (sim-level); frame ring buffer
  + zero-copy handoff; line-scan stitching stub; acquisition conformance test.
- **C8.B defect-inference** (~7): make npu-detection a real pack (tasks/
  scaffolds/tests); **verdict contract** = shared result schema
  {part_id, pass/fail, defect class/loc, model+recipe version} — **Wave-1
  leaf** (C8.C codes against it); golden-sample registry + diff scorer;
  defect-inspection profile; RKNN conversion recipe (toolkit 1.7.3 → RV1126);
  inference smoke (RV1126 target + host-ONNX plumbing-CI); latency budget
  harness — **assertion deferred to a named P0.E.2 HIL run on ATK-DLRV1126**
  (sim cannot measure NPU latency; risk marked not-mitigated-in-EPIC until
  that run).
- **C8.C line-logic / fieldbus** (~6): FieldbusAdapter interface over
  connectivity's modbus; e-stop/interlock state feed + D3 fail-closed gating
  (Wave 1); **part-tracking verdict queue** (encoder-count/shift-register
  position-indexed — the core reject-correctness mechanism); reject-gate
  protocol (Wave 2, against the verdict contract); tact-sync + verdict-
  delivery-deadline assertion; PLC line simulator (CI stand-in, incl. fault
  injection: missed trigger, late verdict).
- **C8.D recipe-engine** (~5): product recipe schema (ROI/thresholds/model
  ref/register maps/changeover); recipe store + versioning; **authenticated +
  audited recipe changes** (security pack); golden-sample lifecycle;
  calibration workflow stub (intrinsics = follow-up).
- **C8.E line-hmi** (~5): Qt operator panel (live view + verdict overlay);
  defect review queue; SPC charts (p-chart/x̄-R minimal); alarm/andon view;
  auth/roles integration + thin web status endpoint.
- **C8.F traceability-integration** (~4): results DB schema + writer (keyed by
  part-tracking identity); image archive policy (ring + flagged-keep); batch
  report + MES export (CSV + REST hook); **full-line e2e sim** (camera-sim →
  inference → PLC-sim reject → traceability) composed via 1F, **asserting
  "verdict N actuates on part N" under fault injection** — the META-close gate.

Filing disciplines: ≤3 same-class agent:auto per batch (H10); gates in labels/
blockedBy not prose (H9); intra-wave `blockedBy` links filed explicitly
(C8.D-on-C8.B-schema etc.).

## 4. Phasing + calendar

- **Wave 1**: C8.A + C8.C(transport/e-stop/part-tracking) + C8.B verdict-contract leaf
- **Wave 2**: C8.B(rest) + C8.D + C8.C reject-gate
- **Wave 3**: C8.E + C8.F (e2e close)
- Codex code wall-clock ~3-5 h/wave (post-H8 throughput, as measured on
  Cases 5-7). **Calendar is dominated by review/+2 cadence: siblings measured
  0.5-2 days/leaf → ~33 leaves ≈ 4-8 weeks to sim-close** (NOT 1-2 weeks);
  compressible if the operator batches +2 reviews as in Cases 5-7 peaks.

## 5. Risks

| Risk | Mitigation |
|---|---|
| imaging SoC-extension breaks existing planner contracts | dedicated Wave-1 leaf updates tests in lock-step (BS.1.5-style) |
| GigE Vision real-protocol complexity | sim-first; real GigE = hardware follow-up META |
| tact-time too tight for RV1126 NPU | **not mitigated in-EPIC** — named P0.E.2 HIL latency run on ATK-DLRV1126; RK3588 escape requires rknn-toolkit2 mirror first |
| wrong-part rejection | part-tracking verdict queue leaf + e2e fault-injection assertion (C8.F gate) |
| Modbus timing vs tact | PLC owns actuation; verdict-deadline assertion in sim; discrete-I/O fallback documented |
| token/provider collision with connectivity | C8.C provides new tokens only; `plc-transport` untouched (DuplicateProvideError is a hard planner error) |
| safety-claim creep | D3 canonical invariant copied verbatim into every C8.C ticket |
| model availability | placeholders; conversion recipe proves toolchain, not accuracy (host-ONNX = plumbing only) |

## 6. What this EPIC does NOT do (named follow-ups)

Real camera/PLC hardware-in-loop (→ P0.E.2-style HIL META); machine-safety
certification (ISO 13849/IEC 62061); on-site commissioning; customer models /
accuracy tuning; **RK3588 inference leg** (needs rknn-toolkit2 + rknpu2
mirrored — one vendor-mirrors errand, then a small leaf-set); camera
intrinsic/scale calibration; PTP/NTP multi-station time sync; multi-station
composition; image-retention/PII posture (line images can capture operators);
OTA delivery of models/recipes (ota pack exists, unused here); MES
vendor-specific connectors beyond CSV/REST.

## 7. Next gates

1. ~~Plan-agent adversarial review~~ ✅ folded (this rev)
2. Operator +2 of this doc on Gerrit
3. File META + 6 Sub-EPIC parents (tier:X) + Wave-1 leaves (≤3/batch)

## 8. Review fold log (v1→v2)

MUST: (1) imaging compatible_socs lacks rv1126/rk3588 → Wave-1 extension leaf;
(2) RKNN two-generation split → RV1126-only inference, RK3588 descoped (toolkit2
not mirrored); (3) plc-transport collision → C8.C layers on connectivity, new
tokens only; (4) verdict contract pulled to Wave 1; (5) part-tracking verdict
queue added as core leaf + e2e assertion. SHOULD: (6) npu-detection is a stub →
C8.B creates it; (7) latency mitigation tied to named P0.E.2 HIL run; (8) PLC
owns actuation timing + deadline assertion + discrete-I/O fallback; (9) D3
canonical copy-paste invariant; (10) D5 cites the actual mirror catalog row;
(11) strobe/exposure-sync leaf + HMI auth/recipe-audit leaf added, follow-ups
named in §6. NOTE: (12) calendar corrected to sibling-measured 4-8 weeks;
(13) intra-wave blockedBy + register-map re-plumb noted.
