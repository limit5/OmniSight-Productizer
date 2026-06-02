# EPIC design — Embedded-Linux Camera Platform (Phase 0 shared) + Case 4 Phase 1A (EVK BSP + stitching)

**Filed:** 2026-06-02 (v1) · **Revised:** 2026-06-02 (v2 — Plan-agent review) · **Revised again:** 2026-06-02 (v3 — operator EVK list folded in)
**Author:** claude (session continuing from Case 1 + Case 2 closure)
**Status:** v3 — POST-REVIEW-GATE, EVK-aligned, ready for ticket filing
**Review gate:** Plan-agent claude-side substitute for codex review per OP-1917 (codex CLI 11/11 rc=1 spanning Case 1 / Case 2 / Phase 0 P0.A.1). Review verdict + fold-in record in §11 below.
**Operator EVK list (2026-06-02):** 5 EVKs across Case 4 + Case 5: ATK-DLRK3588 (Rockchip RK3588 aarch64); ATK-DLRV1126 (Rockchip RV1126 armhf 32-bit ARMv7-A); Radxa Dragon Q6A (Qualcomm QCS6490 aarch64); MediaTek Genio 1200-EVK (MTK Genio 1200 aarch64); FT-C600 (Fullhan MC6358 — existing Case 2 hardware). **Each EVK ships its own BSP/SDK as separate deliverable**; Phase 0 = shared substrate; Case 4 Phase 1A META = 5 per-EVK Sub-EPICs.
**Supersedes:** v1/v2 (same file, in-place rewrite). v1 had 3 load-bearing assumptions Plan-agent torpedoed (A1/A3/META-close). v2 fixed those but assumed 1 EVK; v3 expands to 5 EVKs and adds RV1126 as a NEW SoC family (Rockchip's 32-bit ARMv7-A ISP line).
**Cross-references:**
- `docs/product/2026-05-27-customer-delivery-capability-audit-and-roadmap.md` § Cases 4/5/7/8
- `docs/architecture/2026-06-02-case2-uvccamera-qt-multivendor-epic-design.md` (vendor abstraction Phase 0 builds on)
- `docs/architecture/2026-05-24-dag-execution-engine-epic-design.md` (cross-compile path explicitly PARKED there)
- Memory: `project_dag_execution_engine_unimplemented`, `project_customer_delivery_capability_audit_2026_05_27`

---

## 0. The 4-line summary

Operator picked Case 4 + Case 5 with reuse, recommended structure was **Phase 0 shared infra + parallel Phase 1A/1B**. This doc covers **Phase 0 (shared) + Case 4 Phase 1A only**. Case 5 Phase 1B stays out of scope until Phase 0 lands and the codex CLI failure mode (OP-1917) is understood. Phase 0 + Case 4 1A together unlock Case 5/7/8 later without re-doing the embedded substrate.

---

## 1. Premise-check findings (SOP Stage 1)

Verified via grep + sub-agent audit on 2026-06-02. **Many components exist, in varied states:**

### A. Already exists (reuse without rebuild)

| Component | Where | Reuse path | Status |
|---|---|---|---|
| Cross-toolchain catalog framework | `configs/embedded_catalog/cross-toolchain.yaml` (4 generic toolchains: Arm GNU 13.3, Linaro, RISC-V, Xtensa) | **EXTEND** with SoC-specific entries (RK3576/3588 wave 1; QCS6490 + Genio 1200 wave 2 NDA-gated) | extant |
| Yocto/Buildroot catalog seed | `configs/embedded_catalog/embedded.yaml:78-100` + `backend/alembic/versions/0052_catalog_seed.py:78-100` (Buildroot 2024.02 LTS) | **EXTEND** with per-SoC meta-layer dependencies; ⚠️ **MIGRATE off kirkstone** (LTS expired 2026-04; we're at 2026-06) to **scarthgap (5.0 LTS)** | extant **(migration ticket needed)** |
| Platform-profile cross-compile block | `backend/platform_profile.py:166-190` (`CMAKE_TOOLCHAIN_FILE` + `SYSROOT` exposure) | **REUSE** directly; just add SoC profiles | extant |
| V4L2 enumeration | `backend/routers/system.py` + `backend/agents/tools.py:probe_v4l2_devices` | **REUSE** as the host-side feedback path | extant |
| Vendor abstraction (Case 2) | `limit5/UVCCamera_Qt main@cdb830ad` — VendorAdapter contract + VendorRegistry singleton + 4 adapters | **REFERENCE ONLY** — lives in separate repo. P0.B's kernel-side dispatch table is hand-synced from this list (same pattern as Case 2 T2D.1's `VENDOR_ADAPTERS` Python mirror) | **cross-repo, treat as external interface** |
| HIL probe pattern | `limit5/UVCCamera_Qt scripts/hil_probe.py` (Case 2 T2D.1, shipped 2026-06-02) | **REUSE** as the dev-host smoke entry from operator workstation; P0.D's serial smoke script invokes after flash | **extant in UVCCamera_Qt repo** |
| HD bring-up agent skills | `omnisight/agents/skills/hd-bringup-checklist/`, `hd-bringup-live-parse/`, `hd-blob-compat/` | **ACTIVATE** — currently template-only; P0.D activation work is its own leaf ticket (P0.D.3 doc generator uses these) | template-only, needs activation |

### B. Scaffolded but inactive (extend or activate)

| Component | Where | Blocker / activation step |
|---|---|---|
| DAG executor cross-compile path | Design at `docs/architecture/2026-05-24-dag-execution-engine-epic-design.md` Phase 2; explicitly PARKED per memory note `project_dag_execution_engine_unimplemented` | NOT activated in Phase 0; this design defers cross-arch builds to host-x86-with-qemu OR vendor-toolchain-on-host. Phase 0 does NOT depend on un-parking the DAG executor. |
| embedded_planner.py | `backend/embedded_planner.py:173` (reads `tasks.yaml` + emits DAG) | BROKEN schema: 25/27 packs fail `KeyError` on `task_id`/`expected_output` (per 2026-05-27 audit). Phase 0 does NOT touch this — would couple Phase 0 to a fix-someone-else-broke. Stays in TODO under a separate ticket (existing OP-533 / HD.20.1 area). |
| T3 sandbox / SSH primitives | `backend/container.py` `dispatch_t3` + `backend/ssh_runner.py:run_on_target` | Zero non-test callers. Phase 0 uses local QEMU emulation, not real EVK SSH, until the boards arrive. |
| Multi-SoC project schema | OP-533 (`project.platforms:` design) + OP-506 (3-layer manifest design) in TODO.md | Phase 0 does NOT depend; can be folded later. |

### C. Build from scratch (truly new)

| Component | Estimated effort |
|---|---|
| Linux kernel UVC driver scaffold (`uvcvideo` customization hooks for XU dispatch per vendor) | ~500-1000 LOC kernel module |
| V4L2 vendor-dispatch (kernel-side mirror of the C++ VendorAdapter pattern) | ~300-500 LOC per vendor (3 vendors) |
| Yocto meta-layer recipe scaffolds (`meta-omnisight-camera/` skeleton; per-SoC `meta-rockchip/` `meta-qualcomm/` `meta-mediatek/` recipe outline) | ~5-10 `.bb` files |
| Boot bring-up + flash + serial smoke SOP doc | ~3-page markdown + ~150 LOC shell scripts per SoC |
| Phase 0 end-to-end smoke test harness (host-side: build → qemu-aarch64 → probe uvc → vendor dispatch) | ~150 LOC bash + ~50 LOC python wrapper |

---

## 2. Target architecture

```
┌─────────────────────────── Phase 0 (shared) ───────────────────────────┐
│                                                                          │
│  P0.A  SoC toolchain catalog extensions                                  │
│   └─ configs/embedded_catalog/cross-toolchain.yaml (extend, not new)     │
│   └─ + per-SoC sysroot manifest + GPG-checked tarball mirror entries     │
│                                                                          │
│  P0.B  Linux kernel UVC driver scaffold                                  │
│   └─ kernel/uvcvideo/ext-unit-dispatch/ (NEW; module + Kconfig)          │
│   └─ Per-vendor XU GUID dispatch table (mirrors C++ VendorAdapter)       │
│   └─ V4L2 ioctl_ops registration + UVCIOC_CTRL_QUERY routing             │
│                                                                          │
│  P0.C  Yocto meta-layer recipe scaffolds                                 │
│   └─ recipes/meta-omnisight-camera/  (NEW)                                │
│   └─    ├─ recipes-kernel/linux/linux-omnisight.bb (base)                │
│   └─    ├─ recipes-kernel/linux/linux-omnisight_rockchip.bbappend         │
│   └─    ├─ recipes-kernel/linux/linux-omnisight_qualcomm.bbappend         │
│   └─    └─ recipes-kernel/linux/linux-omnisight_mediatek.bbappend         │
│                                                                          │
│  P0.D  Boot bring-up + flash + serial smoke SOP                           │
│   └─ docs/operations/2026-06-XX-embedded-bringup-sop.md (NEW)             │
│   └─ scripts/embedded/flash_{rockchip,qualcomm,mediatek}.sh (NEW)         │
│   └─ scripts/embedded/serial_smoke.sh (NEW; UART monitor + uvc probe)     │
│   └─ scripts/embedded/phase0_e2e_smoke.sh (NEW; build→qemu→probe)         │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────── Case 4 Phase 1A (EVK BSP/SDK + multi-cam stitching) ────────┐
│                                                                          │
│  C4.A  Multi-cam stitching pipeline                                       │
│   └─ Calibration: per-cam intrinsics + extrinsics from chessboard        │
│   └─ Stitching: OpenCV stitcher pipeline (panorama/grid mode)            │
│   └─ Frame-sync: V4L2 BUF queue + monotonic timestamp alignment          │
│                                                                          │
│  C4.B  BSP/SDK packaging deliverable                                      │
│   └─ Per-EVK BSP archive (kernel + dtb + uvc-stitching binary)            │
│   └─ Customer-facing SDK headers + sample app                            │
│   └─ License manifest (per-vendor blob licenses)                         │
│                                                                          │
│  C4.C  EVK delivery + handoff                                             │
│   └─ Flash image + checksum signatures                                   │
│   └─ Bring-up acceptance checklist (per EVK)                             │
│   └─ Customer runbook (similar to T2C.2 for Case 2)                      │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

Hardware-gated: P0.B/C/D need 1+ SoC board for verification.
                C4.A/B/C need the SPECIFIC EVK the customer specified.
```

### Architectural decisions

**A1 (REVISED v2). Phase 0 vendor-XU dispatch lives in USERSPACE, not a kernel module.** Plan-agent review verified that upstream `linux/uvcvideo.h` UAPI exposes ONLY `UVCIOC_CTRL_MAP` and `UVCIOC_CTRL_QUERY` ioctls + userspace data structs (`uvc_xu_control_mapping`, `uvc_xu_control_query`); there is no `EXPORT_SYMBOL`-style API a separate `.ko` could hook to register per-vendor XU handlers. **Phase 0 ships a USERSPACE daemon `uvcvideo-xu-dispatcher` (or library `libuvcxudispatch.so`)** that:
- on startup, walks `/sys/class/video4linux/` + matches each device's VID:PID to a vendor adapter (mirrors C++ VendorRegistry::findForDevice)
- invokes `UVCIOC_CTRL_MAP` per device per vendor's XU GUID + control entries
- exposes a Unix-socket / D-Bus API for application-level callers (Case 4 stitching app uses this)
This **collapses P0.B's scope** — no kernel C, no out-of-tree module, no per-kernel-version maintenance. P0.B becomes 3 leaf tickets (daemon skeleton + per-vendor map tables + integration test) instead of 4 with a kernel module. Tradeoff: vendors that require true kernel-side dispatch (e.g., interrupt-driven XU events) are out of scope; if a future SoC needs that, file an out-of-tree-fork ticket separately.

**(Optional follow-up — P0.B.0 spike — RECOMMENDED before P0.B.1 files):** 0.5-day verification spike that confirms (i) `UVCIOC_CTRL_MAP` works against the FT-C600 with vendor XU GUID, (ii) the userspace daemon model is sufficient for Case 4's control needs, (iii) the userspace API doesn't introduce unacceptable latency for stitching frame-sync. If spike fails, escalate to operator before kernel-module path is considered.

**A2 (UPDATED v2). Vendor dispatch references the Case 2 VendorAdapter as external interface.** Source of truth for "what GUID belongs to what vendor" lives in `limit5/UVCCamera_Qt` repo (Case 2 deliverable, already shipped). Phase 0's userspace daemon holds a parallel data table — hand-synced from the C++ adapter list, same pattern as Case 2 T2D.1's `hil_probe.py` Python mirror. **MUST sync** discipline documented in P0.B's leaf-ticket Spec-ref section; future tickets when adding a 5th vendor follow the (a) update UVCCamera_Qt C++ → (b) update hil_probe.py → (c) update Phase 0 daemon table sequence.

**A3 (REVISED v2). Yocto recipes target SCARTHGAP (5.0 LTS).** Plan-agent caught the v1 contradiction: kirkstone's `lts_until: "2026-04"` is past as of today (2026-06-02). Scarthgap is current Yocto LTS (released April 2024, supported through April 2028). **Migration ticket P0.C.0 (NEW)** updates `configs/embedded_catalog/embedded.yaml` kirkstone entry to scarthgap; runs before any `.bb` recipe files. If a customer pins kirkstone explicitly (uncommon), file a per-customer override layer; scarthgap is the platform default.

**A4 (UNCHANGED). Phase 0 verification path = host x86 + qemu-aarch64.** With A1's userspace-daemon pivot, qemu verification simplifies: no kernel-loading + virtio-uvc complexity — just qemu-aarch64-user can run the cross-compiled daemon binary, and the daemon can be unit-tested against a stub V4L2 file (Linux user-mode helpers via `tmpfs` + `/dev/null`-backed control nodes). Physical EVK adds an Exercised-AC gate for P0.B + integration; see A4b below.

**A4b (NEW v2). META-close criterion (unified per Plan-agent §3 fix).** Phase 0 META closes when:
- All leaf tickets ship code + qemu-verified Code/Deploy/Integration ACs (~9 of 15 leaf tickets, ≈60% of Phase 0, are 100% qemu-doable per the audit).
- Physical-board Exercised-AC for P0.B + P0.D = **separately-filed follow-up META "real-hardware acceptance"** which OPENS when the first EVK arrives.
- Phase 0 META being 公開済み does NOT mean "Phase 0 is fully verified on hardware" — it means "Phase 0 is ready for Case 4 Phase 1A to start consuming it on hardware as soon as hardware exists." This is the same model Case 2 used (META closed before physical SoC arrival; firmware-integration tickets deferred).

**A5 (UNCHANGED). Case 4 Phase 1A is per-EVK, not per-SoC.** EVK name still operator-pending (Q1 in §8).

**A6 (NEW v2). Phase 0 substrate scope = USB-UVC ONLY. MIPI-CSI / media-controller multi-cam = OUT of Phase 0.** Plan-agent caught: for most embedded multi-cam products on a typical RK/QCS/MTK EVK, cameras come in over MIPI-CSI (with CSI-receiver + ISP + per-sensor v4l2 sub-devices via media-controller graph), NOT through `uvcvideo`. P0.B's userspace daemon addresses **control-plane vendor-XU dispatch for USB-UVC cameras only**. If the Case 4 EVK uses MIPI-CSI cameras (likely), C4.A.4 frame-sync is a separate substrate that belongs in C4.A (single-customer scope) or — if Case 5 also needs MIPI-CSI — a follow-up Phase 0 Sub-EPIC P0.E. **Decision deferred to Q1's EVK-naming answer**: if EVK is USB-UVC multi-cam (rare), Phase 0 covers C4.A.4; if MIPI-CSI (typical), C4.A.4 = Case 4-internal or P0.E.

**A7 (REVISED v3). Phase 0 first wave = Rockchip RK3588 + Rockchip RV1126 (both open-source); QCS6490 + MTK Genio 1200 = wave 2 blocked on OP-491 NDA-mirror infra.** Plan-agent's Q5 mitigation (i) plus operator EVK list. Both Rockchip lines are open-source (Linaro-based toolchains, vendor-mirrors-public org). **RV1126 is a NEW SoC family in Phase 0** — Rockchip's ISP-focused 32-bit ARMv7-A line (Cortex-A7 single-core + RISC-V NPU), so toolchain triple is `arm-linux-gnueabihf` (NOT aarch64). P0.A wave-1 grows from 1 entry to 2 entries (P0.A.1 RK3588 already filed as OP-1919; P0.A.1b RV1126 newly added). QCS+MTK leaf tickets stay drafted in §3 below but explicitly marked **(wave 2 — blocked on OP-491)**. When OP-491 lands (separate epic), wave 2 unblocks as a tight follow-up. **Genio 1200 references throughout doc updated to Genio 1200** per operator's MediaTek Genio 1200-EVK selection (bigger SoC than design v1/v2 assumed; cosmetic update + reflects actual hardware on bench).

**A8 (NEW v3). Case 4 Phase 1A = 5 per-EVK Sub-EPICs.** Operator confirmed each EVK has its own BSP/SDK as separate deliverable. Case 4 Phase 1A META roll-up contains:
- C4-A: ATK-DLRK3588 BSP/SDK (Rockchip RK3588 / aarch64)
- C4-B: ATK-DLRV1126 BSP/SDK (Rockchip RV1126 / armhf)
- C4-C: Radxa Dragon Q6A BSP/SDK (Qualcomm QCS6490 / aarch64; **blocks on OP-491 NDA**)
- C4-D: MediaTek Genio 1200-EVK BSP/SDK (MTK G1200 / aarch64; **blocks on OP-491 NDA**)
- C4-E: FT-C600 BSP/SDK (Fullhan MC6358 — already in Case 2; C4-E may collapse into a reference-bundle of Case 2 deliverables)

Each per-EVK Sub-EPIC has its own C4-X.A stitching pipeline (per-EVK camera count + bus type) + C4-X.B BSP packaging + C4-X.C delivery runbook. Cross-EVK reuse comes from Phase 0 substrate (toolchains + userspace daemon + Yocto recipes + flash scripts), not from C4-X-shared code. Wave-1 Case 4 filing = C4-A + C4-B only (open-source EVKs); C4-C + C4-D = wave 2; C4-E = optional reference-bundle wave 3.

---

## 3. Sub-EPIC breakdown

Mirrors the Case 2 (4-sub-EPIC + waves) shape proven this session.

### Sub-EPIC P0.A — SoC toolchain catalog extensions

Goal: `configs/embedded_catalog/cross-toolchain.yaml` gains 3 new entries (RK3576/3588, QCS6490, Genio 1200) with download-URL, GPG-fingerprint, SHA256, install method, CMake toolchain-file path, sysroot path.

**Ticket split (v3 — RV1126 added):**
- **P0.A.1**: Rockchip RK3576/RK3588 aarch64 toolchain catalog entry (Linaro base) — **wave 1** ✅ filed as OP-1919, Gerrit #1369
- **P0.A.1b (NEW v3)**: Rockchip RV1126 armhf toolchain catalog entry (Linaro arm-linux-gnueabihf base; ARMv7-A Cortex-A7 32-bit) — **wave 1**
- **P0.A.2**: Qualcomm QCS6490 toolchain catalog entry — **wave 2 — blocked on OP-491 NDA**
- **P0.A.3**: MediaTek Genio 1200 toolchain catalog entry — **wave 2 — blocked on OP-491 NDA**
- **P0.A.4**: `tools/embedded/verify_toolchain.sh <vendor>` — host-side smoke (download → install → cross-compile a 5-line hello.c → run in qemu-{aarch64|arm} per triple). Covers all 4 SoC entries that exist at run time.

All 5 entries are file-disjoint per Case 2's adapter pattern. Parallelism-friendly (drift-guard lock-step within each yaml+alembic edit).

### Sub-EPIC P0.B — USERSPACE vendor-XU dispatcher daemon (v2 revised per A1)

Goal: a userspace daemon `uvcvideo-xu-dispatcher` (or library `libuvcxudispatch.so`) that registers per-vendor XU control mappings via `UVCIOC_CTRL_MAP` ioctl + exposes a Unix-socket / D-Bus API for application callers. NOT a kernel module. Compiles for x86 dev + cross-compiles for aarch64 via P0.A toolchains.

**Ticket split (v2 — Plan-agent's "split per-vendor like Case 2" fix folded in):**
- **P0.B.0 (NEW): 0.5-day spike** — verify `UVCIOC_CTRL_MAP` against FT-C600 with vendor XU GUID; confirm daemon-model latency is acceptable for Case 4 stitching frame-sync (≤ 1 ms control-plane RTT); confirm no kernel-side dispatch needed. ESCALATE if spike fails.
- **P0.B.1: Daemon skeleton** — main loop + Unix-socket protocol stub + sysfs walk of `/sys/class/video4linux/`; logs vendor-id matches; runs in qemu-aarch64-user without crashing.
- **P0.B.2: Vendor adapter registry** — table of `{vendor_id → {guid, control_entries[]}}` mirroring Case 2 `VendorAdapter`; lookup-by-VID:PID; lookup-by-GUID; thread-safe.
- **P0.B.3a: FT-C600 handler** + Unix-socket integration test (mirrors UVCCamera_Qt `FtC600VendorAdapter`) — **wave 1**
- **P0.B.3b: Rockchip RK3588 handler** (placeholder GUID; mirrors `RockchipVendorAdapter`) — **wave 1**
- **P0.B.3c (NEW v3): Rockchip RV1126 handler** (placeholder GUID; separate vendor entry since RV-line uses different XU dispatch than RK35xx series) — **wave 1**
- **P0.B.3d: Qualcomm handler** (placeholder GUID; mirrors `QualcommVendorAdapter`) — **wave 2 — blocked on OP-491 NDA**
- **P0.B.3e: MediaTek handler** (placeholder GUID; mirrors `MediaTekVendorAdapter`) — **wave 2 — blocked on OP-491 NDA**
- **P0.B.4: Userspace integration test** `tools/embedded/uvc_xu_dispatch_test.c` — connects to the daemon's Unix socket, requests vendor lookup for a stub device, validates the right adapter responds. Runs in qemu-aarch64-user without physical hardware.

### Sub-EPIC P0.C — Yocto recipe scaffolds (v2: per-vendor split + scarthgap)

Goal: a `meta-omnisight-camera` Yocto layer skeleton that any of the SoC stacks can `bbappend` against. NOT a full image build (deferred to per-EVK Case 4 Phase 1A); this is the recipe skeleton + per-SoC bbappend stubs.

**Ticket split (v2 — scarthgap migration + per-vendor split):**
- **P0.C.0 (NEW): Yocto LTS migration** — update `configs/embedded_catalog/embedded.yaml` kirkstone (EOL 2026-04) entry to scarthgap (5.0 LTS, EOL 2028-04). Wave-1 prereq for P0.C.1+; landing 5 minutes of yaml edit + catalog regen.
- **P0.C.1: `meta-omnisight-camera/` layer skeleton** + `conf/layer.conf` + `recipes-core/uvcvideo-xu-dispatcher/uvcvideo-xu-dispatcher.bb` (recipe for the userspace daemon from P0.B; replaces v1's kernel-module recipe)
- **P0.C.2a: Rockchip RK3588 `bbappend` stub** (fetches P0.B daemon binary + applies RK3576/3588 sysroot + boot args) — **wave 1**
- **P0.C.2b (NEW v3): Rockchip RV1126 `bbappend` stub** (separate kernel defconfig + armhf flag set; Cortex-A7 single-core) — **wave 1**
- **P0.C.2c: Qualcomm `bbappend` stub** — **wave 2 — blocked on OP-491 NDA + P0.A.2 landing**
- **P0.C.2d: MediaTek `bbappend` stub** — **wave 2 — blocked on OP-491 NDA + P0.A.3 landing**
- **P0.C.3: Yocto build smoke** `tools/embedded/yocto_smoke.sh` — uses scarthgap + `meta-omnisight-camera` + Rockchip bbappend → builds → asserts `uvcvideo-xu-dispatcher` binary appears in the rootfs at `/usr/bin/`

### Sub-EPIC P0.D — Boot bring-up + flash + smoke SOP (v2: D.4 promoted to Sub-EPIC)

Goal: per-SoC flash scripts + unified serial smoke harness + operator-facing bring-up SOP doc, modeled on Case 2 T2C.2's runbook. **P0.D.4 (end-to-end integration smoke) promoted to its OWN Sub-EPIC P0.E** per Plan-agent SOP "integration-glue ticket" gap — it touches every prior Sub-EPIC + a real board, scope was too big for a single leaf.

**Ticket split (v2 — D.4 carved out):**
- **P0.D.1a: `flash_rk3588.sh`** rkdeveloptool wrapper (ATK-DLRK3588 board) — **wave 1**
- **P0.D.1b (NEW v3): `flash_rv1126.sh`** rkdeveloptool wrapper (ATK-DLRV1126 board; same tool, different mode flags + maskrom button sequence) — **wave 1**
- **P0.D.1c: `flash_qualcomm.sh`** fastboot+EDL wrapper (Radxa Dragon Q6A) — **wave 2 — blocked on OP-491**
- **P0.D.1d: `flash_mediatek.sh`** mtk-brom wrapper (MediaTek Genio 1200-EVK) — **wave 2 — blocked on OP-491**
- **P0.D.2: `serial_smoke.sh`** — UART monitor + uvc probe + vendor-dispatch verify (works post-flash with the SoC connected). Vendor-agnostic; consumes any SoC's serial console.
- **P0.D.3: `docs/operations/2026-06-XX-embedded-bringup-sop.md`** — operator runbook (mirror of T2C.2 structure). Activates HD bring-up agent skills (currently template-only).

**Hardware gating:** P0.D.1a-D.3 ship as code today; P0.D.1a Rockchip is exercise-tested when first Rockchip board arrives. NDA-gated D.1b/D.1c stay in wave 2.

### Sub-EPIC P0.E — End-to-end integration smoke (v2 NEW; promoted from P0.D.4)

Goal: the "wire-it-all-together" gate. Carved out from P0.D.4 because it crosses every prior Sub-EPIC + needs real hardware + is genuinely multi-day. Per SOP this is exactly the kind of integration-glue ticket that deserves explicit attention.

**Ticket split:**
- **P0.E.1: `phase0_qemu_e2e_smoke.sh`** — qemu-only chain: pick Rockchip → toolchain (P0.A.1) → build daemon (P0.B.1+B.2+B.3a+B.3b) → bake Yocto image (P0.C.2a) → boot in qemu-aarch64 → daemon comes up → integration test (P0.B.4) passes. Hardware-independent; CLOSES at qemu-green. This is what makes Phase 0 META closable WITHOUT EVK.
- **P0.E.2: `phase0_realboard_e2e_smoke.sh`** — physical-board chain: same as E.1 but flashes (P0.D.1a) + boots real Rockchip board + serial-smokes (P0.D.2) + daemon-verifies on hardware. **Filed as a separate META-tier follow-up "real-hardware acceptance"** that OPENS when first EVK arrives; does NOT block Phase 0 META.

---

## 4. Phase 1A — Case 4 EVK BSP/SDK + multi-cam stitching

### Open question (operator-side): which EVK?

The audit says "specified EVK" without naming. Phase 1A's C4.A stitching topology depends on:
- Number of cameras (2-way panorama vs 4-way grid vs 6-way 360°)
- Camera bus type (parallel MIPI-CSI vs USB UVC vs Ethernet)
- Frame-sync mechanism (hardware GPIO trigger vs software queue alignment)

**ACTION:** before filing C4.* tickets, operator names the EVK + camera count + bus type. Until then, Phase 1A stays scoped but unfilled.

### Sub-EPIC C4.A — Multi-cam stitching pipeline
- C4.A.1: Per-cam intrinsic calibration (chessboard) + tooling
- C4.A.2: Multi-cam extrinsic calibration (pairwise + global bundle-adjust)
- C4.A.3: Stitching pipeline (OpenCV stitcher OR custom GLSL for performance)
- C4.A.4: Frame-sync layer (V4L2 BUF queue + monotonic timestamp align)

### Sub-EPIC C4.B — BSP/SDK packaging
- C4.B.1: BSP archive layout (kernel + dtb + binary + license manifest)
- C4.B.2: SDK headers + sample app for customer
- C4.B.3: License manifest generator (per-vendor blob licenses, output: SPDX JSON)

### Sub-EPIC C4.C — EVK delivery + handoff
- C4.C.1: Flash image build (per EVK) + SHA256 + cosign signature
- C4.C.2: Customer-facing bring-up checklist
- C4.C.3: Customer runbook (modeled on T2C.2)

---

## 5. Phasing + estimated effort

| Phase | Calendar | Hardware-gated | Codex-CLI dependency |
|---|---|---|---|
| Phase 0 P0.A (toolchain) | ~1 week | No (qemu) | low — yaml + CMake fragments |
| Phase 0 P0.B (kernel module) | ~2 weeks | Partial (qemu OK; real board for confidence) | medium — C kernel code |
| Phase 0 P0.C (Yocto) | ~1-2 weeks | Partial (host build) | low — `.bb` recipes |
| Phase 0 P0.D (flash + SOP) | ~1 week | **YES** (real boards) | low — bash + docs |
| Phase 0 META close | ~5-6 weeks total | EVK board arrival | — |
| Case 4 Phase 1A C4.A (stitching) | ~3-4 weeks | EVK + cameras | medium — OpenCV C++ |
| Case 4 Phase 1A C4.B (BSP packaging) | ~1-2 weeks | EVK | low |
| Case 4 Phase 1A C4.C (delivery) | ~1 week | EVK | low |
| Case 4 Phase 1A META close | ~5-7 weeks (after P0) | EVK | — |

**Total calendar: ~10-13 weeks for Phase 0 + Case 4 Phase 1A combined (sequential).**
Case 5 Phase 1B can start in parallel with Case 4 Phase 1A AFTER Phase 0 lands — estimated +6-9 months on top, mostly because of UAC + PoE + PD3.0 + custom hardware + form-factor cert (per audit).

---

## 6. Codex CLI dependency note

Per OP-1917 (filed 2026-06-02), codex CLI is 10/10 rc=1 on Case 2 EPIC tickets. **Before Phase 0 starts filing leaf tickets**, OP-1917 should either:
- (a) confirm a root cause + ship a fix (preferred), OR
- (b) confirm that hand-author is permanent for this product line (so we budget hand-author time accordingly)

Phase 0 estimates ABOVE assume best-case codex (40% codex / 60% hand-author rate, mirroring Case 1 Sub-EPIC 4). Worst-case (Case 2 pattern, 100% hand-author): add ~30-50% calendar to Phase 0.

---

## 7. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | EVK boards never arrive / arrive much later than expected | **High** | Phase 0 ships everything except P0.D's physical-board gate on qemu; META closes at qemu-verified; physical-board adds a follow-up "real-hardware acceptance" ticket |
| R2 | Customer's EVK uses a non-listed SoC (not RK/QCS/MTK) | Medium | Document Phase 0's vendor-extension pattern; adding a 5th vendor = follow Case 2 pattern (~1 week per SoC) |
| R3 | Yocto scarthgap goes EOL during the project | Low | scarthgap LTS until April 2028 (~22 months runway); migration to next LTS = future ticket; kirkstone (v1 default) already EOL — migration to scarthgap is P0.C.0 in v2 |
| R4 | OP-1917 codex investigation finds codex unfixable | Medium | Hand-author becomes the default; calendar +30-50%; consider filing a "claude-only EPIC mode" SOP that omits codex review gates |
| R5 | embedded_planner schema mismatch breaks if Phase 0 wants to drive builds through it | Low | Phase 0 does NOT depend on embedded_planner; if/when fixed, fold in via separate ticket |
| R6 | Kernel module breaks across kernel version upgrades | Medium | Pin kernel version per SoC (kirkstone bundles 5.15 LTS); compatibility matrix doc in P0.D |
| R7 | Phase 0 + Case 4 Phase 1A scope-creeps into Case 5 territory | Medium | Hard-stop on UAC / PoE / PD3.0 / form-factor work; those are Case 5 Phase 1B, file ALWAYS separately |

---

## 8. Open questions for review-gate

1. **EVK choice for Case 4 Phase 1A:** operator must name. Until then C4.* tickets stay drafts.
2. **Customer for Case 4** — is it the same customer as Case 5, or different? Affects deliverable packaging (one BSP vs two).
3. **Qemu verification depth:** is qemu-aarch64 + virtio-uvc sufficient for P0.B's "Code AC" gate, or do we hard-gate on physical board?
4. **embedded_planner fix:** should Phase 0 take on the OP-533/HD.20.1 area as a stretch goal (~1 ticket), or leave entirely out of scope?
5. **License + NDA boundaries:** Qualcomm QCS6490 + MediaTek Genio are Tier-3 NDA per the audit's vendor-mirrors-NDA org (OP-491). Phase 0 P0.A.2 + P0.A.3 may need NDA-cleared mirrors. Operator confirms NDA path before P0.A.2/3 file.
6. **Review gate:** per camviewpro EPIC SOP, design doc gets 1 round codex review. Given OP-1917, do we (a) try codex anyway as data point, (b) use Plan-agent claude-side instead, or (c) skip the review gate for this EPIC and rely on operator-side review only?

---

## 9. What this EPIC does NOT do

- Does NOT un-park the DAG executor cross-compile path (separate epic when needed)
- Does NOT fix embedded_planner.py schema (separate OP-533 area)
- Does NOT cover Case 5 Phase 1B (UAC + PoE + PD3.0 + custom hardware)
- Does NOT cover Cases 7/8 multi-domain / on-site / PCI-EMV cert
- Does NOT add new third-party Python deps to the productizer backend
- Does NOT touch the runner sandbox (T3 / SSH primitives stay where they are)
- Does NOT promise mainline-kernel upstreaming (`uvcvideo_ext_dispatch.ko` ships out-of-tree; upstreaming is a future ticket if customer demands)

---

## 10. Next gates (this doc → tickets) — v3 status (RV1126 + 5-EVK split folded in)

1. ✅ **Operator approved the EPIC structure** (2026-06-02 this session, picked "Phase 0 + Case 4 Phase 1A; Case 5 1B deferred").
2. ✅ **Review gate substitute resolved** — Plan-agent (claude-side) per Q6 + OP-1917. First review pass complete; v2 folded in.
3. 🟡 **Q3 + Q4 + Q6 answered** — qemu OK for Code-AC; embedded_planner stays separate; Plan-agent review path.
4. 🟡 **Q5 pre-picked** — Rockchip-only wave 1; NDA wave 2 blocks on OP-491.
5. ⏳ **Q1 + Q2 still backgrounded** — EVK name + customer mapping; needed before C4.* leaf tickets file.
6. **Filing order (v3 — RV1126 added; META filed = OP-1918; P0.A.1 filed = OP-1919 → Gerrit #1369):**
   - **First batch (wave 1, today + ongoing):** META OP-1918 ✅ → Sub-EPICs P0.A/B/C/D/E (5 sub-EPICs, NOT YET FILED) → leaf tickets for Rockchip-only wave 1: P0.A.1 ✅, P0.A.1b RV1126, P0.A.4, P0.B.0 spike, P0.B.1, P0.B.2, P0.B.3a FT-C600, P0.B.3b RK3588, P0.B.3c RV1126, P0.B.4, P0.C.0 scarthgap migration, P0.C.1, P0.C.2a RK3588, P0.C.2b RV1126, P0.C.3, P0.D.1a RK3588, P0.D.1b RV1126, P0.D.2, P0.D.3, P0.E.1. **20 leaf tickets** in wave 1 (was 16; +4 for RV1126: toolchain + handler + bbappend + flash).
   - **Second batch (wave 2, when OP-491 NDA infra exists):** P0.A.2, P0.A.3, P0.B.3d, P0.B.3e, P0.C.2c, P0.C.2d, P0.D.1c, P0.D.1d. **8 leaf tickets** in wave 2.
   - **Third batch (real-hardware acceptance META):** P0.E.2. Opens when first Rockchip board arrives.
   - **Case 4 Phase 1A (v3 = 5 per-EVK Sub-EPICs):** META Case4-Phase1A filed alongside Phase 0 wave 1; Sub-EPICs C4-A RK3588 + C4-B RV1126 + C4-E FT-C600 (collapse-bundle of Case 2) filed wave 1; C4-C QCS + C4-D Genio 1200 filed wave 2 with NDA gate. Each Sub-EPIC carries its own C4-X.A stitching + C4-X.B packaging + C4-X.C delivery (3 leaf tickets each = 9 leafs for wave-1 Case 4).
7. **3-ticket blind-test (v2):** before bulk-filing, run 3 highest-risk wave-1 leafs through a 2nd Plan-agent review for ticket-spec clarity:
   - P0.B.0 (spike — verifies the A1 pivot)
   - P0.B.2 (vendor adapter registry — the lookup table that everything else consumes)
   - P0.E.1 (qemu e2e smoke — the META-close gate)

## 11. Plan-agent review verdict + fold-in record (v2)

**Review run:** 2026-06-02 immediately after v1 draft; verdict NEEDS-CHANGES.

**Plan-agent's 3 must-fix torpedoes (all folded in):**
1. ✅ **A1 nonexistent upstream kernel callback API** — verified by grepping `/usr/include/linux/uvcvideo.h` (UAPI exposes only `UVCIOC_CTRL_MAP` + `UVCIOC_CTRL_QUERY` ioctls; no `EXPORT_SYMBOL` for vendor-XU-handler registration). v2 pivoted P0.B to USERSPACE daemon model; new P0.B.0 spike ticket verifies the pivot before P0.B.1 files.
2. ✅ **A3 expired kirkstone LTS** — verified `embedded.yaml:88 lts_until: "2026-04"` against today (2026-06-02). v2 migrates to scarthgap (5.0 LTS, EOL 2028-04); new P0.C.0 migration ticket lands the catalog change first.
3. ✅ **META-close criterion contradicted across §2 A4 / §3 P0.D / §7 R1** — v2 unified at A4b: META closes at qemu-verified; real-hardware acceptance is a separately-filed follow-up META (P0.E.2).

**Plan-agent's 2 ticket-granularity fixes (folded in):**
4. ✅ **P0.B.3 split per vendor** — was 1 ticket covering 4 vendors; v2 split into B.3a (FT-C600) + B.3b (Rockchip) + B.3c (Qualcomm wave 2) + B.3d (MediaTek wave 2).
5. ✅ **P0.D.4 was integration-glue megaticket** — v2 promoted to Sub-EPIC P0.E with P0.E.1 qemu-only (closes Phase 0) and P0.E.2 real-board (separate follow-up META).

**Plan-agent's 1 scope-blocker pre-pick (folded in):**
6. ✅ **Q5 NDA path "blocks 40% of Phase 0"** — v2 added A7 deciding Phase 0 wave 1 = Rockchip-only, wave 2 = Qualcomm + MediaTek blocked on OP-491. Filing batch in §10 reflects this.

**Plan-agent's 2 stretch recommendations (folded in):**
7. ✅ **§1A reuse table status column** — added with extant / forward-dep / template-only labels; Case 2 VendorAdapter explicitly marked as cross-repo external interface.
8. ✅ **A6 added** — Phase 0 = USB-UVC only; MIPI-CSI substrate decision deferred to Q1 EVK-naming answer.

**Plan-agent verdict on §3 P0.A:** "Probably the cleanest sub-EPIC in the doc. RULED OUT — sound." → P0.A.1 Rockchip toolchain entry can file as soon as v2 is committed, without further review.

**Plan-agent verdict on overall v2:** with these 8 folds, structurally sound; Plan-agent's residual concerns are now properly catalogued in §7 risk register (R1/R3/R4/R5) and §8 open questions (Q1/Q2 backgroundable; Q3/Q4/Q5/Q6 resolved).
