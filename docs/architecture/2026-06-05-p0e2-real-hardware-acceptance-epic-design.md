# P0.E.2 Real-Hardware Acceptance EPIC — Design v2

Date: 2026-06-05
Author: claude (sora session)
Status: v2 post-Plan-agent-review fold; ready for filing
Supersedes: v1 (this file at prev SHA)
Plan-agent review: 16 findings folded; full review in commit msg
Related (read first):
  - Phase 0 substrate EPIC (closed): docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md
  - Case 4 5-EVK matrix (closed): same as Phase 0 §C4 + Sub-EPICs OP-1927/1928/1965/1966/1929
  - Bring-up SOP (qemu-validated): docs/operations/2026-06-XX-embedded-bringup-sop.md
  - UVC-XU spike: docs/audit/2026-06-02-uvc-xu-userspace-dispatch-spike.md

---

## 0. Substrate-repair PREREQUISITES (must land before any Wave 1 ticket)

Plan-agent audit found 4 substrate bugs that ALL silently passed qemu smoke
(OP-1925) because the qemu test path likely uses host-gcc + qemu-user-mode and
**never exercises the Yocto build path**. Real-hardware acceptance is impossible
until these are fixed.

| ID | Bug | File:line | Symptom on first `bitbake` |
|----|-----|-----------|----------------------------|
| **SUB-1** | 3 of 4 bbappends use deprecated `_<override>` syntax | `yocto/meta-omnisight-camera/recipes-core/uvcvideo-xu-dispatcher/uvcvideo-xu-dispatcher_%.bbappend:9-25`, `_qcs6490.bbappend:4-13`, `_genio1200.bbappend:8-22` | `COMPATIBLE_MACHINE_rk3588` parsed as literal var name (scarthgap dropped underscore-override); recipe silently skips machine gating; `_append_rk3588` does NOT append on rk3588 build |
| **SUB-2** | `SRC_URI = "file://uvcvideo-xu-dispatcher"` but no `files/` dir + no `FILESEXTRAPATHS_prepend` | `uvcvideo-xu-dispatcher.bb:5` | `do_fetch` fails: "Unable to find file" |
| **SUB-3** | Handlers (4 of 5: ft_c600/qualcomm/rockchip_rv1126/rockchip_rk35xx/mediatek) self-typedef `struct uvc_xu_vendor_adapter`; registry header declares `struct vendor_adapter`. Plus handler `extern void register_vendor(...)` ≠ registry `int register_vendor(...)`. Plus `.vid_pid = NULL, .vid_pid_count = 0` → `adapter_is_valid()` returns false silently | `src/embedded/uvc-xu-dispatcher/*.c` vs `vendor_registry.h` | Type mismatch + link-time UB; in best case all `register_vendor` calls silently return -EINVAL, `__attribute__((constructor))` discards rc, NO handler ever registers |
| **SUB-4** | `omnisight-camera-image.bb` doesn't exist — design + SOP reference `bitbake omnisight-camera-image` but layer has only 1 recipe (the dispatcher) | `yocto/meta-omnisight-camera/recipes-core/` (no `images/` dir) | `bitbake omnisight-camera-image` fails immediately |

**SUB-5** (related infrastructure gap): bench host needs **qemu-aarch64 cross-Yocto
build smoke** that exercises the full Yocto path WITHOUT real hardware — this
catches SUB-1..SUB-4 in CI rather than on bench at Wave 1 L11. The current "qemu
e2e smoke" (OP-1925) does NOT do this.

**Wave 0 includes 5 substrate-repair leaves (L00.1 - L00.5) that MUST land before
any per-board Wave 1/2 work.** These are pure software, no hardware needed,
codex-buildable.

---

## 1. Problem

Phase 0 substrate + Case 4 5-EVK matrix shipped a **qemu-passing-but-substrate-broken**
software stack: toolchain catalog, userspace UVC-XU dispatcher daemon (compiles on
host-gcc, registers nothing at runtime), meta-omnisight-camera Yocto layer
(scarthgap LTS, 3 of 4 bbappends silently inactive, no image recipe), flash scripts
for 4 SoCs (qemu-untested for real maskrom), per-vendor handlers (dead code at
link-time), bring-up SOP (references non-existent image recipe).

Operator now reports **4 EVKs in hand**:
- ATK-DLRK3588 (Rockchip RK3588 aarch64)
- ATK-DLRV1126 (Rockchip RV1126 armhf — Cortex-A7 + RISC-V NPU dual)
- Radxa Dragon Q6A (Qualcomm QCS6490)
- MediaTek Genio 1200-EVK

All 4 vendor SDKs reportedly **downloaded somewhere locally** but **scattered, not
inventoried**. Bench machine = the WSL2 host running runners + Claude Code (USB
pass-through via usbipd-win, which is **not yet installed**).

The cost of finding the substrate gaps with **1 board** is ~1/4 the cost of finding
them with all 4 simultaneously.

## 2. Goal + Non-Goals

**Goal**: deliver real-hardware acceptance for all 4 EVKs — boot a meta-omnisight-camera
image, prove UVC enumeration + UVCIOC_CTRL_MAP + camera capture + per-SoC handler all
work on the physical board, integrate into HIL CI for nightly automated bring-up.

**Non-goals (separate METAs / future)**:
- Application-layer features built on the substrate (Case 4/5/6/7 already cover those
  in qemu; **re-run on real hardware = separate per-Case real-hardware META**, gated
  on P0.E.2 close + per-Case team decision)
- NPU runtime SDK integration (RKNN runtime / NeuronPilot / Qualcomm DSP libs) — these
  enable Case 8 production-line vision; **separate follow-up META per vendor**, after
  P0.E.2 build-time SDK integration proves stable
- PoE PSE driver (C5.B' / Case 5 — gated on PoE daughtercard arrival, separate)
- Production hardware (final product PCB) — these are EVKs, not the final product
- Aviation cert / payment cert tracks (Phase 1C/1D, separate)

## 3. Architectural Approach

### 3.1 Two integration tracks (per board)

**Track 1 — Hardware on-bench**: physical board → WSL2 host via usbipd-win pass-through
+ ethernet to LAN + ssh from runner.

**Track 2 — Vendor SDK into Yocto**: vendor BSP layer added to meta-omnisight-camera
LAYERDEPENDS + bblayers overlay + toolchain catalog yaml + alembic 0052 SEED_ENTRIES
sync per BS.1.5 drift-guard + build-on-clean-checkout.

Track 2 can run software-only (no board needed); Track 1 needs the board. **Both
converge at first-light Phase C** (flash Yocto-built image to board, boot to login).

### 3.2 Per-board 5-phase template

| Phase | Content | Hardware? | Codex-safe? | Est. |
|-------|---------|-----------|-------------|------|
| **A** SDK ingest | sha256-pin local SDK + NDA-mirror upload + license audit (binary YES/NO/CONDITIONAL per SDK, T16) | No | Yes (after operator points to local path) | 1-2 days |
| **B** Yocto integration | Vendor BSP layer add + LAYERDEPENDS + bbappend + catalog yaml/alembic sync + **qemu-cross-Yocto smoke** (SUB-5) + clean-checkout build | No | Yes | 2-3 days |
| **C** First-light | Power + serial console + flash → boot to login | YES | No (tier:X operator-paced; see §3.5) | 1 day |
| **D** Substrate verify | UVC enumerate + UVCIOC_CTRL_MAP smoke + camera capture + per-SoC handler real-hardware + **GUID discovery** (T13) | YES | Split: `.script` codex / `.run` tier:X operator (§3.5) | 1-2 days |
| **E** HIL integration | Bench machine in CI loop + per-board health check + nightly automated | YES (one-time) | Yes (after E1 wiring) | 1-2 days |

### 3.3 Sequencing — substrate-repair → qemu-Yocto smoke → 1 board → 3 parallel

Plan-agent fold: insert a **Wave 0.5 qemu-aarch64 cross-Yocto smoke** before any board
work. This catches SUB-1..SUB-4 in CI rather than at Wave 1 L11 burning a board-day.
1 codex-day saves 1 board-day per board it would otherwise break.

**Order**:
1. **Wave 0 (substrate-repair + parallel-safe bench infra)**: substrate-repair leaves
   L00.1-L00.5 + bench infra L01-L06
2. **Wave 0.5 (qemu-aarch64 cross-Yocto smoke gate)**: 1 leaf; must pass before Wave 1
3. **Wave 1 (serial)**: RK3588 end-to-end (Phase A+B+C+D)
4. **Wave 2 (parallel, post-Wave-1 lessons folded)**: RV1126 + QCS6490 + Genio 1200
5. **Wave 3 (post-Wave-2)**: HIL CI integration

**Why RK3588 first** (Plan-agent confirmed): wave-1 SoC (no NDA), ATK SDK public,
`meta-rockchip` scarthgap support for RK3588 is solid (rk3588-evb upstream), most-mature
ecosystem. Substrate gaps that surface on RK3588 are unambiguously substrate, not
board-specific. **Caveat (T4)**: ATK-DLRK3588 board is NOT upstream in `meta-rockchip`;
expect 1-2 days of board-DTS porting (custom `atk-dlrk3588.conf` MACHINE file under
`meta-omnisight-camera/conf/machine/` that includes `rk3588.conf` + overlays board DTS).

### 3.4 Operationalized H10 filing cadence

Per Plan-agent: spell out concrete numbers, not "≤3 same-class".

| Wave | Concurrent rule | Rationale |
|------|----------------|-----------|
| Wave 0 | L03+L04+L05 concurrent OK (3 same-class `docs`). L06 sub-leaves serial (1 at a time): all 4 vendor uploads write to same NDA-mirror index | NDA-mirror index file = file-mutex |
| Wave 0.5 | 1 leaf, no concurrency | Single qemu smoke gate |
| Wave 1 | Strict serial (L07→L08→L09→L10) | All touch `configs/embedded_catalog/cross-toolchain.yaml` + `backend/alembic/versions/0052_catalog_seed.py` (BS.1.5 file-mutex) and `bblayers.conf.sample` |
| Wave 2 | **≤1 codex agent:auto per vendor sub-EPIC at any time; max 3 concurrent across all 3 Wave-2 sub-EPICs.** Each in-flight ticket must be a DIFFERENT vendor (RV1126 + Q6A + Genio rotation) | File-disjoint per vendor recipe dir, but catalog yaml + alembic are file-mutex; rotation avoids 2-RV1126 race |
| Wave 3 | L30+L33 concurrent (file-disjoint); then L31+L32 concurrent | Systemd + dashboard non-overlapping; health-check + cron file-disjoint |

### 3.5 Phase D `.script` + `.run` split (Plan-agent T6)

Each Phase D leaf splits into:
- **`.script`** (tier:M, agent:auto:true) — codex writes test script + fixtures + dry-run validation
- **`.run`** (tier:X, agent:auto:false, assignee:operator) — operator executes on bench, attaches serial log + screenshots + frame samples

Doubles Phase D leaf count (~14 → ~28) but each is unambiguous to a codex picker.
Without split, codex picks D ticket → can't physically reach bench → abstains → wastes
cycle → another instance picks same ticket → infinite loop under H7.

## 4. Bench infrastructure (Wave 0) — expanded post-Plan-agent

### 4.1 USB pass-through (Windows host → WSL2)

**usbipd-win** (Microsoft official) — Wave 0 L02 SOP. Install command:
```powershell
winget install --interactive --exact dorssel.usbipd-win
```
Per-device attach (each board's USB-UART + rockusb/QDL/BROM mode):
```powershell
usbipd list                          # find busid
usbipd bind --busid <busid>          # one-time per device
usbipd attach --wsl --busid <busid>  # attach to WSL2 (re-attach after Windows reboot)
```

**WSL2-side udev** (Wave 0 L03):
- Rockchip rockusb: `2207:350a` (RK3588 maskrom), `2207:110c` (RV1126 maskrom)
- Qualcomm QDL/EDL: `05c6:9008` (EDL), `18d1:d00d` (QDL)
- MediaTek BROM: `0e8d:0003` (BROM)

Two destinations: (i) `yocto/meta-omnisight-camera/recipes-omnisight/udev/files/`
for the target board image (so the eventual deployed image has the same rules), and
(ii) `tools/embedded/install_bench_udev.sh` for the WSL2 bench host itself.

**Known limits + risks**:
- One USB device → one WSL2 distro at a time (no multi-attach)
- `usbipd attach` doesn't survive Windows reboot; needs re-attach script
- USB 3.0 hub chipsets vary; Renesas + VIA work, no-name often don't
- Rockusb maskrom mode through usbipd-win reportedly works on RK3588
- **Qualcomm QDL through usbipd-win is flaky** (R2-Q6A); fallback: direct-attach to
  a separate Linux machine for Q6A flash, then network-only after first boot
- MediaTek BROM + scatter-loader (mtkclient) reportedly works

### 4.2 WSL2 networking — NAT vs mirrored (Plan-agent §3 add)

WSL2 default is **NAT mode**: boards on LAN cannot ssh INTO WSL2 reliably (NAT
masquerading). HIL nightly bring-up needs **mirrored mode** (Windows 11 22H2+):
```
# %USERPROFILE%\.wslconfig
[wsl2]
networkingMode=mirrored
```
Wave 0 L02.5 SOP: enable mirrored mode + verify bidirectional ssh + verify NTP sync
+ verify mDNS discovery between boards.

**Fallback if mirrored unavailable** (older Windows 10): port-forward ssh from
Windows host via netsh portproxy + accept that mDNS won't work cross-NAT.

### 4.3 Serial console

Each EVK has a USB-UART (FTDI FT2232 / CP2102 / CH340). 4 boards = 4 ttyUSB on WSL2.
Tooling: `apt install minicom tio python3-serial`. Operator wiring per board doc'd in
bench inventory.

### 4.4 USB bandwidth + power budget (Plan-agent §3 add)

**USB bandwidth**: 4 EVKs × USB-UART + 4 × USB OTG (boot mode) + 1+ × UVC camera =
9-12 USB devices. Single USB 3.0 root hub on a typical laptop is ~5 Gbps shared.
UVC at 1080p30 ≈ 600 Mbps; 4 cameras saturate the root. **Wave 0 L05.5**: USB topology
design — at minimum one powered USB 3.0 hub per board's UVC port, ideally on separate
xHCI roots if motherboard has them (verify via `lspci -tv` pre-purchase).

**Power budget** (~60W total bench draw, not counting WSL2 host):
- RK3588 EVK ≈ 15W (12V/2A barrel)
- RV1126 ≈ 3W (5V/1A USB or 12V/1A barrel)
- QCS6490 EVK ≈ 12W (USB-C PD 65W)
- Genio 1200 EVK ≈ 20W (12V/3A barrel)

**Wave 0 L05.6**: PSU rail planning — operator confirms 60W+ headroom available;
per-board barrel-jack vs USB-C-PD distinction documented; risk of brown-out under
simultaneous boot mitigated by staged power-on.

### 4.5 SDK inventory + license-distillation (Plan-agent T16)

First leaf (operator-paced, hands-on with their filesystem): produce
`docs/operations/2026-06-05-bench-sdk-inventory.md` listing
- 4 SDK local paths + sizes + last-modified
- Per-SDK sha256 manifest
- License URL + **binary distillation: "Distribute via NDA mirror: YES/NO/CONDITIONAL"
  citing license clause** (no soft "permission note"; must be a decision row)
- Vendor portal URL for re-download if lost

**Then** codex can run Phase A1 NDA-mirror upload per vendor — but ONLY for SDKs
where the decision row is YES.

### 4.6 NDA-mirror storage primitive (Plan-agent T-storage)

4 SDKs × ~15GB each = ~60GB total. Plan-agent flagged: GitLab Container Registry is
the wrong primitive for tarball blob storage (CR optimizes for layered Docker image
dedup; per-project CR quota may be in effect; Synology-backed CR shared with prod
images).

**Wave 0 L01.5 (NEW, blocks L06)**: storage budget check
1. Query GitLab admin: current CR free space + per-project quota
2. If CR quota tight OR tarball-vs-image distinction matters: **pivot to GitLab
   Generic Packages** (designed for binary blobs) OR S3-compatible bucket on the
   same Synology with sha256-pinned manifests in catalog yaml
3. Confirm primitive choice BEFORE L06 uploads begin (else re-upload cost is steep)

### 4.7 Bench inventory doc

`docs/operations/2026-06-05-bench-inventory.md`: per-board mapping table
- VID:PID for boot mode + runtime mode
- Expected ttyUSB after attach
- Power requirements (PSU rail, current, connector type)
- Ethernet MAC (after first boot) → static DHCP reservation (router auth confirmed)
- **Boot media type** (SD vs eMMC vs maskrom-only; RV1126 ATK is eMMC + maskrom only)
- ssh key fingerprint (after first boot)
- Operator quick-reference: "to flash board X, do these 5 steps"

### 4.8 sstate-cache placement (Plan-agent T12)

First Yocto build = 4-8 hours from scratch on a 16-core 32GB machine; subsequent =
30-60 min with sstate. **L04.5**: configure sstate-cache to a **Windows-side path
mounted into WSL2 via DrvFs** (`/mnt/c/yocto-sstate/`), NOT WSL2 ext4 (overlay-fs IO
penalty kills bitbake). Document the path + recovery procedure if cache corrupts.

## 5. Per-vendor Yocto integration (Wave 1+2) — Plan-agent corrected

### 5.1 Rockchip RK3588 (Wave 1)
- BSP layer: `meta-rockchip` (official, scarthgap branch, RK3588 supported via
  `rk3588-evb` MACHINE upstream)
- ATK overlay: **NO public `meta-atk-rk3588` layer assumed** (Plan-agent T4).
  ATK ships SDK as monolithic Rockchip Buildroot tarball + ATK-specific kernel
  patches. Approach: create `meta-omnisight-camera/conf/machine/atk-dlrk3588.conf`
  that includes upstream `rk3588.conf` + overlays board-specific DTS extracted from
  ATK SDK. Wave 0 L01 SDK inventory must explicitly answer: "what's the actual ATK
  SDK layout, and is there a meta-atk-rk3588 dir, or do we derive from buildroot
  config?"
- Machine: `MACHINE = "atk-dlrk3588"` (custom)
- Yocto build: `bitbake omnisight-camera-image` (created by SUB-4 fix) produces
  ~500MB rootfs + boot.img
- Flash: `tools/embedded/flash_rk3588.sh` — Phase C exercises real maskrom mode

### 5.2 Rockchip RV1126 (Wave 2) — Buildroot-fallback risk
- **Upstream `meta-rockchip` does NOT support RV1126 in scarthgap.** RV1126 is the
  rockchip-linux/buildroot path. Community `meta-rockchip-rv1126` fork exists but
  scarthgap-incompatibility is suspected.
- **Wave 2 needs a spike leaf BEFORE L15-L19**: probe RV1126 Yocto viability. If
  no Yocto path works, P0.E.2 RV1126 boots a Buildroot-built image (acceptable
  compromise — partially breaks "one Yocto layer for 4 boards" goal but is fixable
  later).
- Catalog metadata says `bsp_compatibility: [rockchip-linux-sdk-rv1126]` — note it
  does NOT claim Yocto.
- Cortex-A7 32-bit + RISC-V NPU coproc (NPU not exercised in P0.E.2 — NPU runtime
  follow-up)

### 5.3 Qualcomm QCS6490 (Wave 2) — Radxa carrier risk
- BSP: `meta-qcom` scarthgap supports QCS6490 / RB5 platform (qcom-bsp split with
  `meta-qcom-hwe` happened ~2024)
- Radxa Dragon Q6A is a Radxa carrier board (USB-C SoM-form RB5/IQ-9 sibling).
  **Upstream `meta-qcom` does NOT have a `radxa-dragon-q6a` machine.** Radxa ships
  their own BSP — typically Debian-based, not Yocto. Approach: use `meta-qcom-hwe`
  upstream qcs6490 SoC support + write custom `radxa-dragon-q6a.conf` MACHINE file
  in `meta-omnisight-camera/conf/machine/`
- Qualcomm Linux SDK requires Salesforce-cleared account (NOT Eclipse — Qualcomm
  migrated ~2023). Operator NDA-clearance status: cleared per earlier session;
  Wave 0 L01 license-distillation must verify SDK click-through redistribution
  clause permits NDA-mirror upload (last-known: "no redistribution" even post-NDA
  for developer-tier; may be CONDITIONAL or NO)
- USB QDL flash mode through usbipd-win known-flaky — risk; fallback via separate
  Linux box

### 5.4 MediaTek Genio 1200 (Wave 2)
- BSP: **`meta-mediatek-bsp` + `meta-rity`** (both required; Plan-agent T6)
  - `meta-mediatek-bsp` = BSP layer
  - `meta-rity` = upper distro layer (RITY = MediaTek's reference Linux distro for IoT)
- Catalog metadata at `cross-toolchain.yaml:95` correctly lists both
  (`bsp_compatibility: [meta-mediatek-bsp, meta-rity, genio-1200-evk]`,
  `yocto_release: rity-scarthgap-v25.1.1`)
- Machine: `MACHINE = "genio-1200-evk"`
- BROM flash via `mtkclient` (Python tool, FOSS)

### 5.5 Per-vendor kernel UVC config (Plan-agent T10)
Each vendor's BSP kernel may have `CONFIG_USB_VIDEO_CLASS` disabled by default.
Phase B sub-leaf per board: verify defconfig has UVC enabled; if not, add
`kernel-config-fragment` bbappend. Otherwise Phase C boots but `/dev/video0`
doesn't appear, Phase D blocks.

### 5.6 RV1126 kernel age (Plan-agent T11)
RV1126 ships with Rockchip vendor 4.19 kernel. `UVCIOC_CTRL_MAP` exists at 4.19
but lacks XU error-handling that 5.10+ added. Expect RV1126 handler may need
kernel-version-conditional code paths different from RK3588.

## 6. EPIC structure — META + 8 Sub-EPICs + ~42 leaves (post-fold)

```
META OP-2065 P0.E.2 real-hardware acceptance  [tier:X]
│
├── P0.E.2.substrate-repair (Wave 0 prerequisites — BLOCKS Wave 1+)  [tier:X]
│   ├── L00.1: SUB-1 bbappend override syntax fix (_<x> → :<x>, _append_<x> → :append:<x>)
│   ├── L00.2: SUB-2 SRC_URI repair (FILESEXTRAPATHS + files/ dir OR EXTERNALSRC)
│   ├── L00.3: SUB-3 handler struct unification (typedef vendor_registry.h struct;
│   │         fix return-type to int; populate placeholder vid_pid)
│   ├── L00.4: SUB-4 omnisight-camera-image.bb recipe (core-image-minimal +
│   │         IMAGE_INSTALL_append " uvcvideo-xu-dispatcher v4l-utils ...")
│   └── L00.5: SUB-5 qemu-aarch64 cross-Yocto smoke CI gate (uses qemuarm64 MACHINE
│             + just meta-omnisight-camera + poky + meta-openembedded; runs bitbake
│             omnisight-camera-image to verify SUB-1..SUB-4 all green)
│
├── P0.E.2.bench (Wave 0 — parallel-safe with substrate-repair)  [tier:X]
│   ├── L01: SDK local inventory + sha256 manifest + license distillation
│   │        (YES/NO/CONDITIONAL per SDK)  [operator-paced, tier:X]
│   ├── L01.5: NDA-mirror storage primitive check (CR vs Generic Packages vs S3)
│   ├── L02: usbipd-win install + verify smoke  [operator Windows admin, tier:X]
│   ├── L02.5: WSL2 mirrored networking enablement + verify
│   ├── L03: udev rules (4 vendors) + install_bench_udev.sh
│   ├── L04: serial console SOP doc (4 boards)
│   ├── L04.5: sstate-cache Windows-side DrvFs configuration
│   ├── L05: bench inventory doc skeleton (filled per-board in Wave 1+2)
│   ├── L05.5: USB topology design (hub bandwidth + xHCI root assignment)
│   ├── L05.6: PSU rail planning + power budget doc
│   └── L06: NDA-mirror upload per vendor (4 sub-leaves L06.a-d, SERIAL — file-mutex)
│
├── P0.E.2.atk-rk3588 (Wave 1 — serial, first board)  [tier:X]
│   ├── L07: meta-rockchip + custom atk-dlrk3588.conf MACHINE bblayers integration
│   ├── L08: atk-dlrk3588 board DTS extraction from ATK SDK + layer-overlay
│   ├── L09: ATK SDK toolchain catalog yaml + alembic 0052 sync (BS.1.5)
│   ├── L10: clean-checkout build verify (~4-8h first build, sstate-cached after)
│   ├── L11.script: first-light boot test script (codex)
│   ├── L11.run: operator-hands-on first-light boot  [tier:X assignee:operator]
│   ├── L12.script: UVCIOC_CTRL_MAP smoke + GUID-discovery script (codex)
│   ├── L12.run: operator-runs UVCIOC_CTRL_MAP + GUID-discover on board  [tier:X]
│   ├── L13.script: camera capture + frame-rate measurement script
│   ├── L13.run: operator-runs capture; attach frame samples + timing  [tier:X]
│   ├── L14.script: rockchip_rk35xx_handler integration test (real-hardware)
│   ├── L14.run: operator runs handler test  [tier:X]
│   └── L14.5: kernel UVC defconfig verify + fragment-bbappend if needed
│
├── P0.E.2.atk-rv1126 (Wave 2 — parallel + spike-first)  [tier:X]
│   ├── L14.7: Buildroot-vs-Yocto spike (BEFORE L15) — decide path
│   └── L15-L19: same shape as Wave 1, ~8 leaves (script/run split for Phase D)
│
├── P0.E.2.radxa-q6a (Wave 2 — parallel)  [tier:X]
│   ├── L20-L23: same shape, custom radxa-dragon-q6a.conf MACHINE
│   └── L24: QDL-through-usbipd flakiness fallback (USB-Ethernet direct flash
│            via fastboot if QDL fails)
│
├── P0.E.2.mtk-genio1200 (Wave 2 — parallel)  [tier:X]
│   └── L25-L29: same shape, meta-mediatek-bsp + meta-rity LAYERDEPENDS
│
├── P0.E.2.hil-ci (Wave 3)  [tier:X]
│   ├── L30: bench-runner systemd unit + runner-scheduler cooperative lock
│   │        (defers runner pickup during HIL window)
│   ├── L31: per-board health-check script + P99 boot-to-prompt latency assertion
│   ├── L32: nightly automated bring-up cron + alembic schema for HIL state
│   ├── L33: HIL dashboard (existing fleet UI extension) + shared-host-bias tooltip
│   └── L34: 7-night soak (5 weekday + Sat + Sun) — META-close gate
│
└── P0.E.2.npu-runtime-followup (DEFERRED)  [META, opens after P0.E.2 close]
    — RKNN / NeuronPilot / Qualcomm DSP libs runtime SDK integration; Case 8 enabler.
```

**~42 leaves total** (5 substrate-repair + 11 bench infra + 13 Wave 1 RK3588 +
8 each Wave 2 board × 3 = 24, but minus shared scarthgap-fix savings; + 5 Wave 3).
~26 are codex-buildable (the rest are operator-hands-on Phase C/D or tier:X
operator-Windows-admin).

## 7. BS.1.5 drift-guard reminder

Every catalog change MUST update BOTH:
- `configs/embedded_catalog/*.yaml`
- `backend/alembic/versions/0052_catalog_seed.py` `SEED_ENTRIES`

The drift-guard test asserts per-field equality. Skipping either side → develop red.

## 8. H7-H12 disciplines locked in this EPIC

| H | Discipline |
|---|------------|
| H7 | Per-instance codex auth (already in place) |
| H8 | `model = "gpt-5.5"` (in place) |
| H9 | NO `MUST NOT start until` clauses in ticket bodies. Gates = tier:X label + blockedBy link only |
| H10 | ≤1 codex agent:auto per vendor sub-EPIC; max 3 concurrent across all 3 Wave-2 sub-EPICs; rotate sub-domains (never 2-RV1126 in flight) |
| H11 | Read before Edit on conflict-resolves; grep `<<<<<<<` markers before `git add` |
| H12 | `git merge --no-ff --no-commit FETCH_HEAD` probe (NOT `git cherry-pick`) before push when verifying mergeability |

## 9. Risk register

| R | Risk | Mitigation |
|---|------|------------|
| R1 | usbipd-win install fails on operator's Windows | Wave 0 L02 includes troubleshooting SOP + fallback to "separate Linux bench machine" pivot |
| R2 | Rockusb maskrom mode through usbipd-win doesn't work | Wave 1 L11 prepared with fallback "directly attach to a Linux laptop or VM"; escalate to separate-bench pivot if fails |
| R2-Q6A | QDL through usbipd-win flaky | Wave 2 L24 USB-Ethernet fastboot fallback; or direct-attach to separate Linux box for Q6A flash phase only |
| R3 | Yocto clean-checkout build fails / 4-8h wall-clock per first try | L04.5 sstate-cache on Windows DrvFs; reuse downloads across boards |
| R4 | Qualcomm SDK redistribution license forbids NDA-mirror | L01 license distillation (YES/NO/CONDITIONAL); if NO, use vendor-portal-pinned URL with sha256 |
| R5 | Phase D UVCIOC_CTRL_MAP semantics differ on real hardware vs qemu | Expected; lessons-doc per board feeds back into uvc-xu-dispatcher fixes |
| R6 | Operator bandwidth — Wave 1 serial means RK3588 ~1 week wall-clock | Acceptable; Wave 0 software prep + Wave 2 parallel reduces overall wall-clock |
| R7 | 4 boards on bench = power + USB + cable mgmt overhead | Wave 0 L05+L05.5+L05.6 explicit topology; reduce to 2 boards live + 2 staged if desk limits |
| **R8** | HIL bias under shared-host CPU (Plan-agent §7) | Wave 3 L30 cooperative-lock with runner scheduler; document limitation in L33 dashboard tooltip; planned pivot to dedicated bench Linux box gated on first false-green incident |
| **R9** | Storage budget for NDA mirror (4×15GB = 60GB) | L01.5 primitive check; pivot to Generic Packages or S3 if CR quota tight |
| **R10** | RV1126 Yocto path doesn't exist (Buildroot-only) | Wave 2 L14.7 spike; partial Buildroot fallback acceptable; lesson-doc for future Yocto port |
| **R11** | meta-atk-rk3588 doesn't exist as public layer | Wave 1 L08 custom DTS extraction; expect 1-2 days porting per board |
| **R12** | Vendor kernel UVC defconfig disabled by default | L14.5 per-board kernel-fragment bbappend |

## 10. Wall-clock estimate (Plan-agent T12 corrected)

- Wave 0: 2-3 days (substrate-repair + operator-paced inventory + usbipd install)
- Wave 0.5: 0.5-1 day (qemu-Yocto smoke)
- Wave 1: **1.5 weeks** (RK3588 end-to-end; first Yocto build alone ~4-8h, plus
  Phase C+D operator-paced; codex Phase A+B+SUB-fixes ~1 codex-day each)
- Wave 2: 2-3 weeks (3 boards parallel; bench mgmt overhead non-trivial;
  Buildroot-fallback spike adds 0.5d for RV1126)
- Wave 3: 1 week (HIL CI + 7-night soak gate)

**Total: ~5-7 weeks wall-clock** for full P0.E.2 close (was "~4-5 weeks" v1;
corrected up after Plan-agent fold).

### 10.1 Operator hours estimate (Plan-agent T15)

| Activity | Hours |
|----------|-------|
| Wave 0 SDK inventory + Windows admin + usbipd setup | 4-6 |
| Wave 1 RK3588 hands-on (flash + serial + Phase C+D runs) | 6-8 |
| Wave 2 3-board hands-on (parallel; bench mgmt overhead) | 12-18 |
| Wave 3 HIL bring-up + 7-night soak attention | 3-5 |
| **Total operator hours** | **25-37 hours over 5-7 weeks** |

If operator bandwidth < 25h sustained, Wave 2 must serialize too (adds ~2 weeks
wall-clock).

## 11. META-close criterion (Plan-agent T9 tightened)

P0.E.2 META OP-2065 closes when:
1. All ~42 leaves 公開済み
2. All 4 boards demonstrate: boot to login + UVC enumerate + UVCIOC_CTRL_MAP smoke
   + camera frame capture + per-SoC handler exercised on real hardware
3. **7 consecutive nights green HIL** (5 weekday + Saturday + Sunday) with NO
   manual restart between runs; any false-red root-caused (not a "noise budget")
4. Per-board boot-to-prompt P99 < threshold (RK3588 < 30s; RV1126 < 45s; QCS6490
   < 30s; Genio < 35s) — leak detector for thermal/IO/race regressions
5. SUB-1..SUB-4 substrate bugs all repaired + qemu-Yocto smoke gate green in CI

Per-vendor real-hardware acceptance for Cases 4/5/6/7 application code = separate
follow-up METAs (gated on P0.E.2 + per-Case team decision).

## 12. Plan-agent review fold log

All 16 findings folded:
- T1 (bbappend override syntax) → §0 SUB-1 + L00.1
- T2 (SRC_URI broken) → §0 SUB-2 + L00.2
- T3 (handler struct mismatch) → §0 SUB-3 + L00.3
- T4 (meta-atk-rk3588 may not exist) → §5.1 + R11 + L08 custom DTS extraction
- T5 (meta-qcom QCS6490 + Radxa carrier) → §5.3 + Q-license-distillation in L01
- T6 (meta-mediatek-bsp + meta-rity) → §5.4 corrected
- T7 (UVCIOC_CTRL_MAP semantics) → R5 + §11 criterion 4
- T8 (omnisight-camera-image missing) → §0 SUB-4 + L00.4
- T9 (META-close criterion too lax) → §11 7-night minimum
- T10 (kernel UVC config) → §5.5 + L14.5
- T11 (RV1126 kernel age) → §5.6
- T12 (Yocto build wall-clock fantasy) → §10 corrected + L04.5 sstate
- T13 (GUID placeholders) → §3.2 D row + L12.script GUID discovery
- T14 (catalog drift hazard concurrent) → §3.4 H10 cadence Wave 2 rotation rule
- T15 (operator burden) → §10.1 operator hours sum
- T16 (NDA-mirror binary decision) → §4.5 L01 distillation
- Sequencing critique (RK3588-first confirmed) → §3.3 kept + caveat added
- Wave 0 bench infra gaps (WSL2 networking, USB bandwidth, power, SD media, sstate)
  → §4.2 + §4.4 + §4.7 boot-media + §4.8 sstate
- Phase D codex-vs-operator boundary → §3.5 .script/.run split
- HIL Wave 3 sustainability → R8 + L30 cooperative-lock + L33 tooltip
- NDA-mirror storage budget → §4.6 L01.5 primitive check
- Other torpedoes (T13/T14/T15/T16) all folded as cited
