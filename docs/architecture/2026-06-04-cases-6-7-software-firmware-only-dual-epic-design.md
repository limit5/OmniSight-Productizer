# EPIC design (combined) — Case 6 UAV + Case 7 POS/KIOSK, software-firmware-only

**Filed:** 2026-06-04
**Author:** claude (session continuing from Case 5 Phase 1B META close earlier this session)
**Status:** v1 DRAFT — software-firmware-only scope per operator decision (mirrors Case 5 D2 pattern)
**Combined doc rationale:** Cases 6 + 7 are both post-Phase 0 next-tier customer cases with similar shipping model (software-track first; hardware + cert = follow-up METAs); writing one doc reduces operator-review surface vs. two separate docs.
**Cross-references:**
- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md` v3 — Phase 0 substrate Cases 6/7 inherit
- `docs/architecture/2026-06-03-case5-conference-device-epic-design.md` v3 — Case 5 software-firmware-only / hardware-deferred pattern this doc mirrors
- `docs/product/2026-05-27-customer-delivery-capability-audit-and-roadmap.md` §0 Cases 6 + 7
- Premise check (this session, Explore-agent run) — categorized A/B/C reuse per case

---

## 0. The 4-line summary

Operator picked "Software-firmware-only dual track, mirror Case 5" after Case 5 Phase 1B META OP-1973 closed (2026-06-04, ~02:00). **Both cases scoped as software/firmware ONLY**; hardware (drone airframe / POS terminal PCB / NFC reader hardware / EMV smart-card readers) + regulatory cert (DO-178C / FAA Part 107 / Remote ID / PCI-DSS L1 ROC / EMV L1-L3 lab) = separate follow-up METAs that open when hardware + cert-lab access lands. Both cases inherit Phase 0 substrate (toolchains + Yocto + flash) but otherwise have minimal Case 4/5 carry-forward (different verticals).

---

## 1. Premise-check summary (Stage 1 SOP; full report in session log)

### Case 6 UAV
| Domain | Status | Reuse |
|---|---|---|
| Phase 0 substrate (toolchains + Yocto + flash) | ✅ EXISTS | full |
| Sensor fusion (IMU/EKF base) | ✅ EXISTS (partial) — `configs/skills/sensor_fusion/` has MPU6050/LSM6DS3/BMI270 drivers + `ekf_orientation.c` | scaffold reuse |
| Computer vision for UAV (target track, optical flow) | 🟡 PARTIAL — Case 4/5 stitching reusable for nose-camera; advanced CV greenfield | partial |
| Flight controller (PX4 / ArduPilot / MAVLink) | ❌ ENTIRELY NEW | none |
| RF link / telemetry (sub-GHz RC / LoRa / DJI-style) | ❌ ENTIRELY NEW | none |
| Gimbal stabilization (3-axis BLDC / PID) | ❌ ENTIRELY NEW | none |
| GPS / NMEA / UBX drivers | ❌ ENTIRELY NEW | none |
| Aviation cert (FAA Part 107 / Remote ID / DO-178C) | ❌ ENTIRELY NEW — out of this EPIC's scope per operator decision | none |

**Case 6 reuse: ~15-20%** (Phase 0 toolchains + sensor_fusion scaffolds). Rest = greenfield software-firmware.

### Case 7 POS/KIOSK
| Domain | Status | Reuse |
|---|---|---|
| Phase 0 substrate (toolchains + Yocto + flash) | ✅ EXISTS | full |
| PCI-DSS compliance matrix + presence-gate | ✅ EXISTS — `backend/payment_compliance.py` (~750 LOC) + `backend/routers/payment.py` | scaffold reuse |
| Payment terminal scaffold | 🟡 PARTIAL — `configs/skills/payment/scaffolds/payment_terminal.c` (86 LOC; stubs `NotImplementedError`) | scaffold framework |
| Barcode scanner skill | 🟡 PARTIAL — `configs/skills/barcode_scanner/` (skill.yaml + tasks.yaml; scaffolds empty) | partial |
| Receipt printer skill | 🟡 DESIGN-ONLY — `configs/skills/printing/` (no ESC/POS code yet) | partial |
| Security skill (secure boot / TEE / attestation) | ✅ EXISTS — `configs/skills/security/` | direct apply for PCI-DSS hardware binding |
| EMV kernel (real ISO 7816 / ISO 14443 state machine) | ❌ ENTIRELY NEW (current stubs return "passed (stub)") | scaffold-only |
| HSM integration (real Thales/Utimaco/SafeNet) | ❌ ENTIRELY NEW (current = fake bytes) | scaffold-only |
| P2PE (real ANSI X9.24-3) | ❌ ENTIRELY NEW (current = XOR placeholder) | scaffold-only |
| NFC driver | ❌ ENTIRELY NEW | none |
| MSR magstripe driver | ❌ ENTIRELY NEW | none |
| Cert lab workflow (Visa/MC/Amex EMV labs; PCI-DSS L1 ROC) | ❌ ENTIRELY NEW — out of this EPIC's scope per operator decision | none |
| Retail backend (POS UI / inventory / Stripe-Square integration) | ❌ ENTIRELY NEW — also out of scope (customer's domain) | none |

**Case 7 reuse: ~25-30%** (Phase 0 + PCI-DSS scaffold + security skill + payment-terminal scaffold). Rest = make-real on stubs OR new.

**Case 7 cross-cutting prerequisite per Explore agent**: 1D schema fix + 1F system-of-systems planner. Without these the runner can't compose `{payment + barcode + printer + connectivity}` as a unified POS pack. Two options:
- (a) File 1D + 1F as Case 7's first 2 leaves (couples Case 7 to spine work)
- (b) Defer 1D + 1F to separate spine ticket; scope Case 7 around individual skill packs that don't require composition yet
- **Decision in v1: (b)** — Case 7 software-firmware leaves target individual sub-domains (payment driver, barcode driver, printer driver, EMV kernel) independently. Composition planner = separate spine ticket (file outside this EPIC).

---

## 2. Target architecture (both cases — parallel software-firmware-only tracks)

```
┌──── Case 6 Phase 1B (UAV software/firmware ONLY) ──────────────────────────┐
│                                                                              │
│  C6.A  Flight stack scaffolding (NOT a full autopilot)                       │
│   ├─ MAVLink protocol implementation (libmavconn or rolled-own)              │
│   ├─ PX4/ArduPilot abstraction layer (decision: integrate vs reimplement)    │
│   └─ Flight-mode state machine stub                                          │
│                                                                              │
│  C6.B  Sensor fusion (extend existing scaffolds)                             │
│   ├─ Activate ekf_orientation.c for flight use; add 9-DoF EKF tuning hooks   │
│   ├─ GPS / NMEA / UBX driver                                                 │
│   ├─ Barometer driver (BMP388 / MS5611)                                      │
│   └─ Sensor fusion integration test                                          │
│                                                                              │
│  C6.C  Gimbal control (software only; PWM/PID; no motor)                     │
│   ├─ 3-axis PID controller library                                           │
│   ├─ PWM output abstraction (RK3588 + RV1126 + QCS6490 PWM hooks)            │
│   └─ Hall sensor input abstraction                                           │
│                                                                              │
│  C6.D  RF link protocol (software stack only; no radio chip)                 │
│   ├─ MAVLink-over-serial / over-UDP transport                                │
│   ├─ LoRa packet framing (LoRaWAN class A)                                   │
│   └─ Telemetry rate-limiting + QoS                                           │
│                                                                              │
│  C6.E  Computer vision for UAV (extend Case 4/5 stitching)                   │
│   ├─ Optical flow (Lucas-Kanade or Farneback) for visual odometry            │
│   ├─ Object detection wire-up (NPU stub for now)                             │
│   └─ Video stabilization (gyro-assisted)                                     │
│                                                                              │
│  C6.F  Integration smoke (qemu only; real-board = follow-up META)            │
│   └─ phase6_qemu_e2e_smoke.sh                                                │
│                                                                              │
│  ── DEFERRED to follow-up METAs ──                                            │
│  - Aviation cert (FAA Part 107 / Remote ID / DO-178C) — Phase 1C track       │
│  - Real airframe + motors + radio + GPS hardware = real-HW acceptance META   │
└──────────────────────────────────────────────────────────────────────────────┘

┌──── Case 7 Phase 1B (POS/KIOSK software/firmware ONLY) ─────────────────────┐
│                                                                              │
│  C7.A  Payment terminal driver framework (make-real on stubs)                │
│   ├─ EMV kernel skeleton (ISO 7816 contact + ISO 14443 contactless)          │
│   ├─ PED (PIN Entry Device) abstraction                                      │
│   ├─ Tamper detection signal handling                                        │
│   └─ Vendor adapter pattern (Verifone / PAX / Ingenico)                      │
│                                                                              │
│  C7.B  HSM integration (replace fake-bytes scaffolds with real abstraction)  │
│   ├─ Thales nShield client wrapper                                           │
│   ├─ Utimaco SecurityServer client wrapper                                   │
│   ├─ SafeNet Luna client wrapper                                             │
│   └─ Unified HSMClient interface (mirror Case 2 VendorAdapter pattern)       │
│                                                                              │
│  C7.C  P2PE (replace XOR placeholder with ANSI X9.24-3)                      │
│   ├─ TDES-DUKPT key derivation                                                │
│   ├─ AES-DUKPT key derivation (X9.24-3 modern)                                │
│   └─ KSN (Key Serial Number) management + counter persistence                 │
│                                                                              │
│  C7.D  Barcode + printer drivers (de-stub)                                   │
│   ├─ Zebra SNAPI / Honeywell SDK adapter                                     │
│   ├─ ESC/POS protocol implementation (Bixolon/Star/Epson)                    │
│   └─ Unified scanner+printer pack integration                                │
│                                                                              │
│  C7.E  NFC + MSR drivers                                                     │
│   ├─ NFC PN532 / PN5180 driver                                                │
│   ├─ MSR magstripe driver (USB HID or serial)                                │
│   └─ Combined card-input abstraction                                         │
│                                                                              │
│  C7.F  Integration smoke (qemu only; real-board = follow-up META)            │
│   └─ phase7_qemu_e2e_smoke.sh                                                │
│                                                                              │
│  ── DEFERRED to follow-up METAs ──                                            │
│  - Cert labs (Visa/MC/Amex EMV labs; PCI-DSS L1 ROC; lab queue is months)    │
│  - Real HSM hardware integration (Thales/Utimaco licenses + appliances)      │
│  - Retail backend (POS UI / inventory / payment processor APIs)              │
│  - 1D schema fix + 1F composition planner = separate spine tickets           │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Architectural decisions

**A1. Software-firmware-only scope is non-negotiable.** Hardware + cert work expressly deferred to follow-up METAs. This mirrors Case 5 Phase 1B's pattern (D1=(c) PoE PSE deferred; D2=(a) Microsoft Teams → ACS swap; etc.).

**A2. Case 6 is INDEPENDENT (no prerequisites).** Pure software-firmware stack; no platform-spine dependencies. Can run fully in parallel with anything.

**A3. Case 7 is COUPLED to 1D + 1F (composition planner + schema fix).** v1 decision: file Case 7 around individual sub-domain leaves (each pack independently), NOT relying on composition. Composition planner = separate spine epic, filed outside this dual EPIC.

**A4. Vendor adapter pattern (proven in Case 2 VendorRegistry + Case 5 ConferenceProvider) reused for**:
- C7.A payment terminals (Verifone / PAX / Ingenico)
- C7.B HSM clients (Thales / Utimaco / SafeNet)
- C7.D scanner SDKs (Zebra / Honeywell / Datalogic)

**A5. qemu verification mostly works for software-only scope** (per Case 5 A7 split clocks); real-hardware verification gates on each domain's specific hardware procurement.

**A6. Aviation cert (Case 6) + payment cert (Case 7) = explicit non-goals**. DO-178C, FAA Part 107, Remote ID, PCI-DSS L1 ROC, EMV L1-L3 lab — all require accredited cert labs + multi-month queues + hired domain experts; not codex-shippable.

---

## 3. Sub-EPIC breakdown

### Case 6 — 6 Sub-EPICs

| Sub-EPIC | Description | Leaves estimate |
|----------|-------------|-----------------|
| C6.A flight stack | MAVLink + PX4/ArduPilot abstraction + flight-mode FSM | 4-5 |
| C6.B sensor fusion | Activate EKF + GPS + barometer + integration test | 4 |
| C6.C gimbal control | 3-axis PID + PWM/Hall abstraction | 3 |
| C6.D RF link | MAVLink-over-serial/UDP + LoRa framing + QoS | 4 |
| C6.E UAV CV | optical flow + object detect wire-up + video stab | 4 |
| C6.F integration smoke | qemu e2e | 1 (+ F.2 follow-up META) |
| **Total** | | **~20-21 leaves** |

### Case 7 — 6 Sub-EPICs

| Sub-EPIC | Description | Leaves estimate |
|----------|-------------|-----------------|
| C7.A payment terminal | EMV skeleton + PED + tamper + vendor adapters (3) | 6 |
| C7.B HSM integration | Thales + Utimaco + SafeNet adapters + unified interface | 5 |
| C7.C P2PE | TDES-DUKPT + AES-DUKPT + KSN mgmt | 3 |
| C7.D scanner + printer | Zebra/Honeywell adapter + ESC/POS impl + integration | 4 |
| C7.E NFC + MSR | PN532/PN5180 driver + MSR + card-input abstraction | 4 |
| C7.F integration smoke | qemu e2e | 1 (+ F.2 follow-up META) |
| **Total** | | **~23 leaves** |

**Combined: ~43-44 leaves across 12 Sub-EPICs.** Per H10 ≤3 same-class batching = ~15 filing rounds. At Case 5 codex throughput (~25-30 leaves/hr at peak), software-only shipping = ~2 hours; review + qemu integration adds days.

---

## 4. Phasing + calendar

| Milestone | Wall-clock | What |
|-----------|-----------|------|
| Code shipping (codex CLI) | ~3-4 hours | 43-44 leaves; mostly C/C++ kernel + userspace + vendor adapters |
| Operator review + +2 cycles | ~10-12 weeks | 43-44 leaves × ~0.5-2 days per leaf review |
| qemu integration debugging | ~3-4 weeks | for the qemu-doable subset |
| **Phase 1B META close (qemu-verified)** | **~10-14 weeks total** | both Case 6 + Case 7 METAs |
| **Real-hardware acceptance follow-up METAs** | **+6-12 months** | airframes / payment hardware / NFC readers / printers / scanners on bench |
| **Cert tracks (Phase 1C/1D)** | **+12-24 months** | DO-178C, FAA, PCI-DSS L1, EMV L1-L3 lab queues |

---

## 5. Risks

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| R1 | Cases 6 + 7 run in parallel + share codex pool with already-busy queue | Medium | File in ≤3-per-batch rounds; alternate Case 6 + Case 7 batches |
| R2 | Vendor SDKs (payment terminal / HSM / scanner) need legal review like Case 5 vendor-SDKs | Medium | Treat same as Case 5 D2; assume NDA-cleared blob mirrors (OP-491-style) for Verifone/PAX/Thales/Utimaco/Zebra |
| R3 | PX4/ArduPilot integration is huge (entire autopilot codebase) — codex can't realistically scaffold | High | C6.A = abstraction layer + integration hooks; actual PX4/ArduPilot fork = separate work-stream (operator decision) |
| R4 | Payment compliance code is stub-heavy (HSM fake; P2PE XOR; EMV "passed (stub)") — "make-real" is hardware-gated | High | C7.A/B/C ship interfaces + algorithm correctness in qemu; actual cert lab work = Phase 1C/1D |
| R5 | Both cases share codex pipeline + H10 burst-rebase race repeats | Medium | Memory rule: ≤3 same-class at once; alternate cases to spread file-mutex pressure across different src/ trees |
| R6 | 1D schema fix + 1F composition planner missing → Case 7 can't compose `{payment+barcode+printer+connectivity}` as unified POS pack | Medium | A3 v1 decision: Case 7 leaves target sub-domains independently; composition = separate spine epic |
| R7 | Aviation/payment cert defer means "shipped" ≠ "deployable" — operator + customer must understand this | High | Explicit non-goal in §7; both METAs' close criteria = "qemu-verified software-firmware"; deployment readiness = follow-up META |

---

## 6. What this dual EPIC does NOT do

- Does NOT cover aviation cert (DO-178C / FAA Part 107 / Remote ID) — Phase 1C track for Case 6
- Does NOT cover payment cert (PCI-DSS L1 ROC / EMV L1-L3 lab) — Phase 1C/1D track for Case 7
- Does NOT cover real hardware (airframes / payment terminals / NFC readers / printers / scanners) — follow-up real-HW acceptance METAs
- Does NOT cover retail backend (POS UI / inventory / payment processor) for Case 7 — customer's domain
- Does NOT cover real autopilot fork (PX4 / ArduPilot full integration) — C6.A = abstraction layer + hooks only
- Does NOT fix 1D schema or 1F composition planner — separate spine tickets
- Does NOT introduce new Python deps to productizer backend

---

## 7. Next gates

1. ⏳ **Operator +2 this design doc** (Gerrit push pending in next batch)
2. ⏳ **File Case 6 META + 6 Sub-EPIC parents** (tier:X structural)
3. ⏳ **File Case 7 META + 6 Sub-EPIC parents** (tier:X structural)
4. ⏳ **Skip leaf-filing this turn** (2 AM session protection; surface scope to operator first; bulk-file next session)
5. ⏳ Plan-agent review optional (lower priority than Case 5's was — design pattern is established)
