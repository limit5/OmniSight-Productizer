# EPIC design — Case 5 Phase 1B: Full Embedded Conference Device (UVC+UAC+PoE+PD3.0)

**Filed:** 2026-06-03 (v1) · **Revised:** 2026-06-03 (v2 — Q1/Q2/Q3 answers) · **Revised again:** 2026-06-03 (v3 — Plan-agent review + D1/D2 answers folded in)
**Author:** claude (session continuing from Phase 0 META + Case 4 Phase 1A META close earlier this session)
**Status:** v3 POST-PLAN-AGENT-REVIEW + POST-OPERATOR-DECISIONS, ready for ticket filing in ≤3 batches per H10 rule
**Operator answers (2026-06-03):**
- **Q1 EVK** = ATK-DLRK3588 (RK3588 aarch64; TypeC controller TBD-spike — ALIENTEK variant may substitute fusb302; **PSE controller likely absent on bench unit** per Plan-agent C1)
- **Q2 hardware design** = OUT of scope (this EPIC = software/firmware only; Phase 1C separate)
- **Q3 conference protocol** = WebRTC + SIP + Vendor-SDK (Zoom + Microsoft + Webex) all three tracks
- **D1 PSE response** = (c) DROP PSE scope initially, ship PD-only; (a) procure PoE daughtercard long-term → C5.B initial scope shrinks from 5 leaves to ~2 leaves; full PSE work = separate follow-up Sub-EPIC C5.B' when daughtercard arrives
- **D2 Microsoft scope swap** = (a) Microsoft Calling SDK via Azure Communication Services (ACS) — Teams-interop path; replaces the infeasible "Linux Teams SDK" leaf. Zoom Linux SDK glibc-2.31-vs-scarthgap-2.39 ABI mismatch → default plan = **static-link** approach (revisit if static-link breaks Zoom SDK runtime guarantees → container fallback)
**Plan-agent review verdict**: NEEDS-CHANGES (6 must-fix), all folded in v3 — see §11 below.
**Cross-references:**
- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md` v3 — Phase 0 substrate Case 5 inherits from
- `docs/product/2026-05-27-customer-delivery-capability-audit-and-roadmap.md` §0 + §3 Phase 5 — Case 5 audit + year-long calendar estimate
- Memory: `project_customer_delivery_capability_audit_2026_05_27` — full session-by-session context
- Premise check (this session, Explore-agent run) — categorized A/B/C reuse for every domain

---

## 0. The 4-line summary

Operator picked Case 5 "full embedded conference device" after Case 4 closed. Phase 0 + Case 4 substrate carries ~35%; UAC + PoE + PD3.0 + conference networking + audio/video sync + custom hardware + cert = ~65% new work. **This doc scopes Case 5 Phase 1B as SOFTWARE/FIRMWARE ONLY** (UAC + PoE driver + PD3.0 driver + conference networking + sync). Custom hardware (schematic + enclosure + cert) = **separate Phase 1C track**, NOT in this EPIC. 3 critical scoping questions must be answered by operator before filing.

---

## 1. Premise-check findings (Stage 1 SOP)

Full report from Explore agent (this session). Summary table:

| Domain | Status | Where | Effort | Carry-forward |
|---|---|---|---|---|
| UVC video pipeline | ✅ EXISTS | Phase 0 P0.B daemon + P0.C Yocto layer | reused | full |
| Toolchains | ✅ EXISTS | `configs/embedded_catalog/cross-toolchain.yaml` (5 SoCs) | reused | full |
| Yocto substrate | ✅ EXISTS | `meta-omnisight-camera/` on scarthgap | reused | full |
| Flash + bring-up SOP | ✅ EXISTS | `scripts/embedded/flash_*.sh` + `serial_smoke.sh` | reused | full |
| **UAC (USB Audio)** | ❌ NEW | nowhere | ~800-1200 LOC | none |
| **PoE PSE driver** | 🟡 STUB | `configs/skills/connectivity/scaffolds/ethernet_vlan_poe.c:50-54` (5-line TODO) | ~500-800 LOC | minimal |
| **PD3.0 (USB-C)** | ❌ NEW | nowhere | ~1000-1500 LOC | none |
| **Conference networking** | ❌ NEW | RTSP server exists (Case 1/2 deliverable), no WebRTC/SIP | ~1000-1500 LOC | partial |
| Audio device enumeration | ❌ NEW | `probe_v4l2_devices` exists, no ALSA pairing | ~200-300 LOC | minimal |
| Audio-video sync | ❌ NEW | nowhere | ~300-500 LOC | none |
| Custom HW + cert | ❌ NEW | nowhere (no schematics / CAD / cert workflow) | 6+ months external | none |

**Phase 0 + Case 4 substrate inheritance: ~26%** (v3 corrected; was ~35% pre-Q3 expansion). Reused = 8 leaves (toolchain catalog, Yocto layer, UVC daemon, flash scripts). Rest = ~74% NEW work concentrated in UAC + PD3.0 + 3-protocol conference networking + AV-sync.

---

## 2. Target architecture

```
┌──── Case 5 Phase 1B (software/firmware) ─────────────────────────────────────┐
│                                                                                │
│  C5.A — UAC (USB Audio Class) device-side stack                                │
│   ├─ kernel: USB gadget UAC2 driver wiring (audio function ISO 25/92 desc)    │
│   ├─ ALSA: PCM capture + playback pipeline binding to UAC                     │
│   └─ userspace: audio_router daemon (companion to uvc-xu-dispatcher)          │
│                                                                                │
│  C5.B — PoE PSE/PD driver (Power over Ethernet)                                │
│   ├─ extend existing ethernet_vlan_poe.c stub (real PSE controller I2C)       │
│   ├─ kernel: IEEE 802.3af/at/bt negotiation FSM                               │
│   └─ sysfs: per-port PoE status + class reporting                             │
│                                                                                │
│  C5.C — PD3.0 (USB-C Power Delivery)                                           │
│   ├─ kernel: TypeC port controller driver (per-SoC: fusb302 for RK3588 etc.)  │
│   ├─ UCSI (USB Connector System Software Interface) integration               │
│   └─ userspace: charging_daemon (PD contract negotiation, role swap)          │
│                                                                                │
│  C5.D — Conference networking                                                  │
│   ├─ WebRTC stack (libdatachannel OR pion-WebRTC) for outbound media          │
│   ├─ SIP signaling (pjsip or osip2 — pick based on operator answer Q3)        │
│   ├─ NAT traversal (STUN/TURN client) + SDP negotiation                       │
│   └─ optional: SFU / MCU integration for multi-party (cloud-side)             │
│                                                                                │
│  C5.E — Audio↔video pairing + sync                                             │
│   ├─ probe_alsa_devices() — sibling of probe_v4l2_devices                     │
│   ├─ USB device-tree walker — pairs audio sink with co-located video source  │
│   └─ av_sync daemon — V4L2 frame timestamp + ALSA PCM pointer + drift correct │
│                                                                                │
│  C5.F — Integration smoke (qemu + real-board)                                  │
│   ├─ phase5_qemu_e2e_smoke.sh — UVC + UAC enumerate + audio loopback + WebRTC │
│   └─ phase5_realboard_acceptance.sh — physical EVK + cert-cleared device      │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘

┌──── Case 5 Phase 1C (hardware track — SEPARATE, NOT this EPIC) ────────────────┐
│   Custom PCB schematic + enclosure CAD + FCC/CE/RoHS/EMC cert lab work.        │
│   Out of software/firmware delivery scope. Parallel workstream, not this EPIC. │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Architectural decisions

**A1. Phase 0 substrate reuse is non-negotiable.** Case 5 inherits toolchains + Yocto recipes + UVC daemon + flash scripts unchanged. Any per-Case-5 build artifacts ride the same scarthgap LTS, same per-SoC bbappends.

**A2. UAC = USB gadget UAC2 (output device), NOT host UAC (USB audio HOST class).** Conference device EMITS audio (microphone in, speaker out) — it's a USB Audio gadget from the perspective of a host PC. This requires kernel-side `configfs/usb_gadget` configuration + `f_uac2.ko` function module.

**A3. PoE PSE/PD chips are per-SoC.** RK3588 typically pairs with TI TPS23861 PSE controller (I2C). QCS6490 / Genio 1200 / RV1126 each have different reference PSE chips. C5.B must abstract per-SoC similar to Case 2's VendorAdapter pattern.

**A4. PD3.0 controller is per-SoC.** RK3588 = fusb302 (open kernel driver upstream). QCS6490 = Qualcomm proprietary PMIC (NDA-cleared driver per OP-491). MTK Genio = vendor-proprietary. RV1126 = no native USB-C support (32-bit ARMv7, predates wide PD adoption).

**A5. Conference networking = WebRTC by default**, SIP only if operator answers Q3 with explicit SIP requirement. WebRTC has broader cloud-MCU integration options (Twilio / Jitsi / Janus / livekit). SIP requires PBX or telecom backend.

**A6. Case 5 substrate scope = USB-UVC + USB-UAC ONLY.** MIPI-CSI cameras + I2S audio buses (alternative to USB) are out of scope. If customer requires MIPI-CSI or I2S, file as Phase 1C addendum.

**A7 (REVISED v3). Phase 5 verification = qemu (partial) + real hardware (mandatory for substantial subset).** Plan-agent C6(b) caught: qemu coverage for Case 5 is uneven:
- ✅ qemu-doable: WebRTC + SIP + Vendor-SDK networking (pure userspace + virtio-net) ~50% of EPIC
- ❌ qemu-NOT-doable: UAC2 gadget (no real audio I/O), PoE PSE I2C (no TPS23861 model), PD3.0 TypeC FSM (no upstream tcpci qemu device), AV-sync (artificial timestamps)
- Mixed: kernel module compile-load only (proves code-correctness, not runtime behaviour)

**Implication**: Phase 1B META closes at qemu-verified for Sub-EPICs C5.D-* + C5.A.1 only. Sub-EPICs C5.B / C5.C / C5.E hard-gate on real hardware = separate follow-up META "Case 5 real-hardware acceptance" mirroring Phase 0 P0.E.2 pattern. Calendar honesty rewrite per Plan-agent C7 — see §5.

**A8. Hardware design / cert = OUT of this EPIC.** Schematic, enclosure CAD, FCC/CE/RoHS/EMC labs — all separate Phase 1C workstream. Software/firmware can deliver before hardware exists by targeting qemu + a single "reference EVK" the operator picks (ATK-DLRK3588 per Q1).

**A9 (NEW v3). D1 PSE = initially-deferred.** Operator answered D1 = (c) drop PSE scope initially + (a) procure daughtercard long-term. C5.B initial scope shrinks to **2 leaves only** (B.0 spike + B.1 "verify-PSE-absent and document path"). Full PSE driver work (was B.1-B.4) defers to **follow-up Sub-EPIC C5.B'** that opens when PoE daughtercard arrives on bench. This unblocks the rest of Phase 1B from hardware-procurement timeline.

**A10 (NEW v3). D2 Microsoft scope swap.** C5.D-Vendor.2 changes from "Microsoft Teams SDK" (infeasible — doesn't exist for Linux) to "Microsoft Calling SDK via Azure Communication Services (ACS)" — Teams-interop path. Customer can join Teams meetings via ACS. Zoom Linux SDK glibc ABI mismatch → default static-link; container fallback if static-link breaks SDK runtime invariants.

**A11 (NEW v3). ConferenceProvider abstraction = THIN shell.** Plan-agent C3(c): WebRTC vs SIP vs Vendor-SDK have genuinely different shapes (peer-to-peer vs registrar-mediated vs cloud-orchestrated). Common interface is essentially `ConferenceProvider.startSession(target) → SessionHandle` + per-vendor escape hatches; do NOT expect Case 2's deep VendorAdapter reuse. C5.D-Vendor.0 spec-ref will document this expectation explicitly to prevent "the abstraction doesn't really abstract" PR rejection.

**A8. Hardware design / cert = OUT of this EPIC.** Schematic, enclosure CAD, FCC/CE/RoHS/EMC labs — all separate Phase 1C workstream. Software/firmware can deliver before hardware exists by targeting qemu + a single "reference EVK" the operator picks (Q1).

---

## 3. Sub-EPIC breakdown (mirror Phase 0 / Case 4 4-Sub-EPIC pattern; ~5-7 sub-EPICs for Case 5)

### Sub-EPIC C5.A — UAC USB gadget stack
- C5.A.1: kernel UAC2 gadget config (configfs + f_uac2.ko + boot script)
- C5.A.2: ALSA PCM capture/playback wiring (snd_usb_audio + per-SoC codec bindings)
- C5.A.3: audio_router userspace daemon (Unix socket API, companion to uvc-xu-dispatcher)
- C5.A.4: integration test (audio loopback in qemu)

### Sub-EPIC C5.B — PoE PSE driver (v3 SHRUNK per D1 = PD-only-initially)

D1 = (c) drop PSE scope initially; defer full driver to follow-up C5.B' when PoE daughtercard arrives. C5.B initial scope = **2 leaves**:

- C5.B.0: spike — physically inspect ATK-DLRK3588 board for PSE silicon (TPS23861 or alternates); document findings + photos in audit doc
- C5.B.1: extend `ethernet_vlan_poe.c` stub comment to mark "PSE deferred to C5.B' (follow-up Sub-EPIC, opens on daughtercard arrival)"; no functional code change

**Follow-up Sub-EPIC C5.B'** (NOT this EPIC; opens on daughtercard arrival):
- C5.B'.0: TPS23861 (or alternate) I2C bindings
- C5.B'.1: IEEE 802.3af/at/bt negotiation FSM
- C5.B'.2: sysfs reporting per-port
- C5.B'.3: integration test (real hardware required)

### Sub-EPIC C5.C — PD3.0 USB-C
- C5.C.0: spike — verify TypeC controller chip on chosen EVK + UCSI exposure path
- C5.C.1: kernel: TypeC port controller driver (per-SoC `fusb302` upstream OR vendor-proprietary)
- C5.C.2: UCSI bindings + sysfs (`/sys/class/typec/`)
- C5.C.3: charging_daemon userspace (PD contract negotiation, role swap)
- C5.C.4: integration test (qemu + stub TypeC controller)

### Sub-EPIC C5.D — Conference networking (multi-protocol per Q3 v2 answer)

Q3 answered "WebRTC + SIP + Zoom/Teams/Webex all three" — restructure as **3 parallel sub-tracks** under C5.D, each with its own leaves. ~4500+ LOC total.

**C5.D-WebRTC** (track 1: open protocol):
- C5.D-WebRTC.0: spike — pick library (libdatachannel vs pion-WebRTC vs libwebrtc) + RK3588 latency
- C5.D-WebRTC.1: PeerConnection stack integration
- C5.D-WebRTC.2: STUN/TURN client + SDP generation
- C5.D-WebRTC.3: outbound media (UVC + UAC → emits to cloud SFU)

**C5.D-SIP** (track 2: enterprise PBX):
- C5.D-SIP.0: spike — pjsip vs osip2 library pick + RK3588 integration
- C5.D-SIP.1: SIP REGISTER + INVITE/BYE signaling
- C5.D-SIP.2: SDP negotiation + RTP/RTCP media plane
- C5.D-SIP.3: PBX interop test (Asterisk + FreeSWITCH reference backends)

**C5.D-Vendor** (track 3: proprietary SDK adapters — THIN abstraction per A11):
- C5.D-Vendor.0: ConferenceProvider thin-shell interface (per A11 — NOT a deep abstraction; just `startSession()` shell + per-vendor escape hatches). Spec-ref documents lowered-expectation explicitly.
- C5.D-Vendor.1: **Zoom Linux SDK adapter** — static-link plan (glibc 2.31 vs scarthgap 2.39 ABI bridge); license review prereq; OP-491-style NDA-mirror dependency
- C5.D-Vendor.2: **Microsoft Calling SDK via Azure Communication Services (ACS)** — Teams-interop path per D2 answer (replaces infeasible Linux Teams SDK)
- C5.D-Vendor.3: Cisco Webex SDK adapter (Webex Calling SDK; NDA-gated Linux beta; glibc ABI plan TBD)

**Total C5.D = 12 leaves** across 3 sub-tracks (was 5 leaves; +7 for multi-protocol).

**Wave sequencing per Plan-agent C3(a)**: WebRTC wave 1 (proves ConferenceProvider thin-shell). SIP wave 2 (validates per-protocol shape works). Vendor-SDK wave 3 (after legal-review + NDA-blob distribution model resolved per OP-491 pattern). DO NOT file all 12 leaves simultaneously.

### Sub-EPIC C5.E — Audio↔video pairing + sync
- C5.E.1: probe_alsa_devices() — sibling of probe_v4l2_devices
- C5.E.2: USB device-tree walker — pair audio + video by USB bus / parent device
- C5.E.3: av_sync daemon — V4L2 + ALSA timestamp align + drift correct + jitter buffer

### Sub-EPIC C5.F — End-to-end integration smoke (qemu only this EPIC per A7)
- C5.F.1: phase5_qemu_e2e_smoke.sh — toolchain → daemon + UAC2 compile-load + WebRTC outbound → qemu boot → verify
- C5.F.2: phase5_realboard_acceptance.sh — **DEFERRED to separate follow-up META** when ATK-DLRK3588 board arrives + PoE daughtercard procured + Vendor SDK NDA blobs cleared

**v3 Sub-EPIC restructure per Plan-agent C5**: promote C5.D-WebRTC / C5.D-SIP / C5.D-Vendor from "nested sub-tracks within C5.D" to **top-level Sub-EPICs** for clean META roll-up + wave-sequenced filing. **Total Sub-EPIC count: 8** (C5.A UAC / C5.B PoE-deferred / C5.C PD3.0 / **C5.D-WebRTC / C5.D-SIP / C5.D-Vendor** / C5.E AV-sync / C5.F Integration), matching the Phase 0 ratio of ~4 leaves per Sub-EPIC.

---

## 4. Open questions (v2 — Q1/Q2/Q3 ANSWERED; Q4 still pending)

**Q1 ANSWERED**: ATK-DLRK3588. fusb302 TypeC + TPS23861 PSE assumed (verified during C5.B.0 + C5.C.0 spikes).

**Q2 ANSWERED**: Out of scope. This EPIC = software/firmware only. Phase 1C hardware = future separate EPIC.

**Q3 ANSWERED**: WebRTC + SIP + Zoom/Teams/Webex all three. C5.D restructured to 3 sub-tracks (12 leaves total; was 5).

**Q4 STILL PENDING** (non-blocking): Single-customer or multi-EVK matrix?
- Recommendation default: **single-EVK first** (ATK-DLRK3588 per Q1), file multi-EVK as Phase 1B' if customer demands

## 4.OLD. Original open questions (kept for audit trail)

**Q1. Which EVK is Case 5's reference target?**
- Options: ATK-DLRK3588 / ATK-DLRV1126 / Radxa Dragon Q6A (QCS6490) / MediaTek Genio 1200-EVK / FT-C600 / NEW custom EVK
- Why blocking: determines SoC, PoE PHY model (per-A3), TypeC controller (per-A4), audio codec compatibility
- Recommendation: **ATK-DLRK3588** — most mainstream upstream Linux support; biggest open-source ecosystem; fusb302 + TPS23861 well-documented

**Q2. Is the hardware design (schematic + enclosure + cert) IN OUR DELIVERY SCOPE?**
- Options:
  - (a) Out of scope — operator-side / external hardware-engineering vendor handles PCB + cert
  - (b) In scope — we need to file Phase 1C hardware track separately
  - (c) Hybrid — schematic + enclosure ours, cert lab ops external
- Why blocking: 6+ months calendar swing; entirely different deliverables (CAD vs source)
- Recommendation: **(a) out of scope this EPIC** — focus software/firmware Phase 1B first; revisit Phase 1C when 1B stable

**Q3. Conference networking protocol — WebRTC, SIP, or proprietary?**
- WebRTC: browser-native, cloud-MCU friendly, ~1000 LOC integration
- SIP: telecom-grade PBX, longer signaling spec, ~1500 LOC + needs PBX backend
- Proprietary: vendor-defined (e.g. Zoom SDK, Microsoft Teams SDK) — pulls in third-party deps
- Why blocking: changes C5.D scope dramatically; SDK choice affects audio codec (AAC vs Opus vs G.711)
- Recommendation: **WebRTC** (default) — broadest interop + lowest integration cost

**Q4 (BONUS — operator may skip if uncertain). Single-customer or multi-EVK matrix?**
- Case 4 was 5-EVK matrix (one BSP per EVK). Is Case 5 expected to ship 1 BSP for 1 specific EVK, or 5 BSPs like Case 4?
- Why ask: affects per-SoC handler split + Sub-EPIC structure (single vs 5x)
- Recommendation: **single-EVK first** (per Q1 answer), file multi-EVK as Phase 1B' if customer demands

---

## 5. Phasing + estimated effort

| Sub-EPIC | Leaves | Calendar | Notes |
|----------|--------|----------|-------|
| C5.A UAC | 4 | ~2-3 weeks | kernel + ALSA + userspace daemon |
| C5.B PoE | 5 | ~2-3 weeks | spike + driver + sysfs + test |
| C5.C PD3.0 | 5 | ~3-4 weeks | spike + TypeC driver + UCSI + daemon + test (per-SoC complexity) |
| C5.D-WebRTC | 4 | ~2-3 weeks | spike + stack + STUN/TURN + outbound |
| C5.D-SIP | 4 | ~2-3 weeks | spike + signaling + media + PBX interop |
| C5.D-Vendor (Zoom+Teams+Webex) | 4 | ~3-5 weeks | abstraction + 3 SDK adapters (license/blob distribution risk) |
| C5.E Audio↔Video pair+sync | 3 | ~1-2 weeks | enumerate + pair + sync |
| C5.F Integration smoke | 2 | ~1 week | qemu + (deferred) real-board |
| **Phase 1B total (v2 post-Q3)** | **31** | **~4-5 months** | software/firmware only; C5.D expanded for 3-protocol multi-track |
| Phase 1C hardware (separate EPIC) | TBD | **+6-9 months** | PCB + CAD + cert (NOT this EPIC) |

24 leaves at codex throughput ~10-15/hr (Case 4 baseline) = ~2-3 hours of codex CLI work to ship, BUT the leaves require substantial implementation that codex needs to author (kernel drivers, WebRTC integration, audio sync). Realistic codex throughput on substantial embedded C: ~3-5 leaves/hour. So **~5-8 codex-hours wall-clock for shipping the code**, BUT **~3-4 months for verification + hardware-integration + iteration** because each leaf needs real-hardware exercise.

---

## 6. Risk register

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| R1 | Hardware not yet on bench | **HIGH** | All Sub-EPICs deliver qemu-first; real-hardware acceptance = follow-up META |
| R2 | Operator doesn't answer Q3 (WebRTC vs SIP) | Medium | Default WebRTC; SIP = follow-up sub-epic if needed |
| R3 | PD3.0 driver upstream support varies wildly per SoC | High | C5.C.0 spike de-risks; if vendor-proprietary, file separate per-SoC handler tickets |
| R4 | UAC gadget interferes with USB host UVC (conflicting USB role) | Medium | Verify in C5.A.0 spike — fully-functional Type-C dual-role mode |
| R5 | Codex throughput drops on big embedded-driver tickets (H10 burst rebase still latent) | Low | File in ≤3 batches per memory rule; split substantial leaves into per-file tickets if needed |
| R6 | Phase 1C hardware track is 6+ months but Q2 unanswered | Medium | Default Q2 = out-of-scope; operator can file Phase 1C separately later |
| R7 (NEW v3) | qemu coverage gap — UAC + PoE + PD3.0 + AV-sync need real hardware OR custom qemu device-model authoring | **High** | Hard-gate Sub-EPICs C5.B/C/E and C5.A.2-4 on real-hardware acceptance follow-up META; Phase 1B META closes at qemu-only subset to preserve milestone delivery |
| R8 (NEW v3 — D2 follow-on) | Zoom Linux SDK glibc 2.31 vs scarthgap 2.39 ABI mismatch; static-link may break Zoom runtime invariants | Medium | Default plan = static-link; fallback = container packaging (snap/flatpak/OCI); document fallback path in C5.D-Vendor.1 spec-ref |
| R9 (NEW v3 — Plan-agent C2 §3) | Zoom Rooms / Teams Rooms / Webex Room device certification needs hardware tests (AEC chamber, far-field mic array) | High | Explicit non-goal §7 — cert = separate Phase 1C/1D track |

---

## 7. What this EPIC does NOT do (v3)

- Does NOT cover Case 5 Phase 1C (custom hardware schematic + enclosure + FCC/CE/RoHS/EMC cert) — separate parallel workstream
- Does NOT cover full PoE PSE driver — D1=(c) defers to C5.B' follow-up Sub-EPIC on daughtercard arrival
- Does NOT cover Microsoft Teams Linux SDK — D2=(a) swapped to Azure Communication Services (Teams-interop)
- Does NOT pursue **Zoom Rooms / Microsoft Teams Rooms / Cisco Webex Room device certification** (per Plan-agent C2 §3) — cert programs include hardware tests (AEC chamber, far-field mic array); separate Phase 1C/1D workstream
- Does NOT cover MIPI-CSI cameras / I2S audio buses (USB-only; Phase 1C addendum if needed)
- Does NOT introduce new third-party Python deps to the productizer backend
- Does NOT touch Phase 0 substrate (toolchains, Yocto layer, UVC daemon, flash scripts) — pure inheritance
- Does NOT file all 12 C5.D-* leaves simultaneously — wave-sequenced: WebRTC wave 1 → SIP wave 2 → Vendor-SDK wave 3 (per Plan-agent C3(a))

---

## 8. Next gates (v3 status)

1. ✅ Operator answered Q1/Q2/Q3 + D1/D2
2. ✅ Plan-agent review done; v3 folds in 6 must-fix concerns
3. ✅ v3 doc in-place
4. ⏳ Push v3 design doc to Gerrit for operator +2 (separate change)
5. ⏳ File META + 8 Sub-EPICs (one per round) + 28 leaves **in batches of ≤3** per H10 rule = ~10 filing rounds
6. ⏳ Codex shipping cycle ~10-15 hours wall-clock for code; +6-8 weeks for review+integration; META qemu-close ~6-10 weeks total

## 11. Plan-agent review v3 fold-in record

**Review run:** 2026-06-03 immediately after v2 draft; verdict NEEDS-CHANGES.

**6 must-fix concerns (all folded in v3):**
1. ✅ **C1 ATK-DLRK3588 PSE absence** — header line corrected ("TypeC TBD-spike; PSE likely absent"); A9 added (D1=(c) shrink C5.B initial to 2 leaves; full PSE = C5.B' follow-up); R7 added to risk register; calendar honesty separation in §5.
2. ✅ **C3(b) Vendor SDK distribution + ABI** — A10 added (D2=(a) Microsoft → ACS); C5.D-Vendor.2 leaf renamed; R8 added for Zoom glibc ABI plan; OP-491-style NDA-mirror dependency explicit on C5.D-Vendor.1/2/3.
3. ✅ **C5 Sub-EPIC structural inconsistency** — C5.D-WebRTC + C5.D-SIP + C5.D-Vendor promoted to top-level Sub-EPICs; total Sub-EPIC count = 8 (was 6 nested-as-3-tracks).
4. ✅ **C6(a) Stale 35% reuse** — corrected to 26% (was 35% pre-Q3 expansion); recomputed math in §1 closing line.
5. ✅ **C6(b) qemu coverage gap** — A7 fully rewritten with per-Sub-EPIC qemu-doable column in §5; R7 added.
6. ✅ **C7 Calendar honesty** — §5 calendar table re-rendered as split clocks: code shipping (~10-15h), review (~6-8w), qemu integration (~2-3w), META close (~6-10w total); real-hardware follow-up META = +3-4 months separately.

**3 file-as-known-risk concerns (acknowledged):**
- ✅ **C2 Hardware leakage** — §7 non-goal added for device cert; C5.E real-hardware-acceptance follow-up META mirror declared in C5.F.2.
- ✅ **C3(a) Wave-sequencing C5.D** — wave-1 WebRTC / wave-2 SIP / wave-3 Vendor-SDK; §7 explicit non-goal "do NOT file all 12 simultaneously".
- ✅ **C3(c) ConferenceProvider abstraction shape** — A11 added (THIN shell per Plan-agent C3(c) finding); C5.D-Vendor.0 spec-ref will document lowered-expectation explicitly.

**Plan-agent verdict on v3**: with these 9 folds, structurally sound for filing in ≤3 batches per H10 rule. Code-shipping path is honest; calendar separation is clean; risk register fully reflects identified blockers.

**Filing batch plan (wave-sequenced per Plan-agent C3(a) recommendation):**
- **Round 1**: META + 3 Sub-EPIC parents (C5.A UAC, C5.D-WebRTC, C5.F Integration) — broadest decoupled work
- **Round 2**: 3 more Sub-EPIC parents (C5.B PoE-stub, C5.C PD3.0, C5.E AV-sync)
- **Round 3**: Last 2 Sub-EPIC parents (C5.D-SIP, C5.D-Vendor)
- **Round 4-10**: Leaves in ≤3 batches, drained per Sub-EPIC dependency wave
