# pos-kiosk → Case 7 integration + POS domain-layer design (2026-06-21)

## Summary
`~/work/sora/pos-kiosk` is a **real, complete, cross-SoC embedded Linux POS/KIOSK +
industrial DataLogger PLATFORM BASE** (172/172 tasks, 48 ctest green, `-Werror`
0-warning, adversarially audited — 49 defects fixed). It flips **Case 7** from
"🔴 not started / stub skill packs" to "🟢 platform base built (host-real;
on-target deferred)". **Decision (operator, 2026-06-21): onboard it (納管) AND plan
the domain layer.** It is the POS/KIOSK analogue of what `omnisight-camera-sdk`
is for Case 4 — a **底座**, not a full product.

## What pos-kiosk is (and is NOT)
- **IS:** 4-layer (UI Qt6/Weston · Core C++ daemon · dynamic dlopen HAL · Yocto OS),
  microservice + IPC. Dynamic HAL C ABI (`interfaces/hal/*.h`: `hal_module` =
  input/display/gpio/power ops + version/caps/lifecycle) + plugin SDK + **5 real
  SoC plugins** (rk/mtk/qcom/ti/nxp). Pluggable storage (SQLite/DuckDB/**RocksDB
  1.59M ev/s**), transaction FSM + promo engine, A/B OTA (RAUC+U-Boot rollback),
  Weston kiosk multi-display + hotplug, watchdog/liveness. Yocto dual self-layer
  (`meta-omnisight-core` + `-bsp-override`) with machine confs incl **rv1126 +
  rk3588** (+ Genio/QCS6490/AM62x/i.MX93). CMake cross (arm64/arm32 toolchain).
- **IS NOT (the gap = the domain layer):** NO EMV/payment, barcode-app, NFC,
  receipt-printer, or PCI/EMV cert. It built the hard cross-SoC base + retail
  transaction core; the **POS peripheral + payment + cert domain still builds on
  top** (= today's stub `payment`/`barcode_scanner`/`printing` skill packs).

## Onboarding status (2026-06-21)
- ✅ **GitLab `omnisight/pos-kiosk`** created (group 42) + pushed (`develop` + `master`).
- ✅ **Gerrit project `omnisight/pos-kiosk`** created.
- ⛔ **Gerrit seed + ACL = OPERATOR step.** Pushing `refs/heads/develop` and
  `refs/meta/config` is refused: pos-kiosk's commits are committed by
  `rt3628@gmail.com` (NOT a registered Gerrit identity — registered are
  `sora@sora-apps.com` + `rt3628+<bot>@gmail.com`), and `sora` lacks
  `Push`/owner on the new project's `refs/meta/config` (O10-style ACL lockdown).
  I did **not** touch All-Projects ACL (global blast radius). See runbook below.

### Remaining operator runbook (mirror `onboard-uvc-uac-app-to-runner.md`)
With `sora@sora.services` admin (and the meta/config-push permission):
1. Grant the project owner / `Push` on `refs/meta/config` (or run as a Gerrit
   user who has it), then push the conference-appliance ACL (project.config +
   `groups`) — it already carries the dual-sign submit gate + `forgeCommitter`.
2. Seed `develop` (the `forgeCommitter` allow lets the `rt3628@gmail.com`-committed
   history through; or rewrite committer to `sora@sora-apps.com`). Source =
   GitLab `develop` (current).
3. Add to `~/.config/omnisight/runner.env` `OMNISIGHT_ROUTED_REPOS`:
   `"pos-kiosk":{"gerrit_url":"ssh://claude-bot@sora.services:29418/omnisight/pos-kiosk","ref":"refs/for/develop","context":"pos-kiosk"}`
   → `daemon-reload` + restart the 4 runner units.
4. Gerrit→GitLab mirror timer (copy `conf-gitlab-sync.sh`, swap names + token).
5. E2E gate: one tiny `repo:pos-kiosk`+`agent:auto` ticket → verify the runner
   clones pos-kiosk (not productizer) + pushes `refs/for/develop` there.

Until then pos-kiosk is **GitLab-primary** (human-facing, managed) — fine for
the domain-layer planning; runner-autonomous work waits on the Gerrit activation.

## Architecture integration decisions
1. **Two bases coexist (NOT merge now).** camera-sdk = **buildroot** (ATK SDK);
   pos-kiosk = **Yocto**. They overlap on RV1126/RK3588 but are different domains
   (camera/ISP/codec vs POS peripherals/UI/storage) with **different HAL ABIs**
   (camera: V4L2/ISP/MPP; pos: input/display/gpio/power). Keep both; do not force
   a build-system unification. Revisit convergence only if a real product needs
   one rootfs.
2. **A product needing both = compose, not merge.** A KIOSK that also needs a
   camera (camera-barcode, face/AI, Case-8-style vision) composes the two bases
   via the **1F system-of-systems planner** (`compose_product`) — exactly its
   purpose. pos-kiosk's HAL ABI + camera-sdk's stay independent; the planner wires
   at the product DAG level.
3. **Namespace:** pos-kiosk's components are `omnisight-core/-hal/-ui` (generic).
   They live in their own repo so there is no code collision with the productizer;
   if ever vendored in-tree, prefix `omnisight-pos-*`.
4. **Planner/runner wiring:** pos-kiosk is a **routed device repo** (like
   conference-appliance / rtsp-onvif-server), `repo:pos-kiosk` label → runner
   clones + pushes there. ZERO new runners (multi-repo routing is live, EPIC OP-2191).

## POS domain-layer roadmap (builds ON the base)
Each domain capability = make-real a today-stub skill pack **as a pos-kiosk
module/service**, so the planner's `payment`/`barcode_scanner`/`printing` pack
`artifacts` map to real pos-kiosk output (closes the "pack visible ≠ executable"
gap from the 2026-05-27 audit + codex gate).

| Phase | Capability | How it sits on the base | Realness ceiling |
|---|---|---|---|
| **D0** | Peripheral HAL ABI ext + `peripherals` Core service | extend HAL ABI with optional peripheral ops (or a peripheral daemon over the existing IPC); reuse evdev | host-real + mock; HIL on bench |
| **D1** | Receipt printer (ESC/POS, USB/serial) | Core `printing` service; pack `printing` → this module | **host-real** (ESC/POS is open) |
| **D2** | Barcode scanner (USB-HID) | mostly already there — HAL evdev input → Core; pack `barcode_scanner` | **host-real** (HID is open) |
| **D3** | NFC / contactless reader (PC/SC) | reader integration as a Core service | reader real; payment-app gated |
| **D4** | EMV payment (L2 kernel + L3 acquirer, P2PE) | integrate a **licensed/certified EMV L2 stack** as a service — NOT self-written; pack `payment` | **license + cert gated**; honest sandbox until then |
| **D5** | Certification (PCI-PTS / PCI-DSS / EMVCo L1-L3) | external process track, parallel | external, quarters-long |

**Key honesty:** D0-D2 are genuinely AI-buildable + host-testable (open
protocols). D3 reader is buildable; the contactless **payment app** is gated. D4
EMV is a **licensed vendor stack + cert**, not code we author — until licensed,
the `payment` pack stays an explicitly-labelled simulator (not "done"). D5 cert
is the uncompressible long pole. This matches the camera-sdk pattern: base proven,
HIL/cert deferred to the real-world-gated track.

## Next step
File the **Case-7 domain-layer EPIC** (META) referencing this doc; decompose D0-D2
first (the host-real, runner-buildable slice) per the EPIC-decomposition SOP, gated
behind the Gerrit/runner activation above. D4/D5 = operator/vendor/cert track.
