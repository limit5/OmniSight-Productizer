# EPIC design — rtsp-onvif-server sub-project (cross-SoC IPCAM base)

Status: v1 draft (operator decisions locked 2026-06-12)
Repo: `omnisight/rtsp-onvif-server` (GitLab project id 45, seeded `f3d6d33`)
Owner decisions: gst-rtsp-server engine · clean-room ONVIF (NO GPL imports,
hard rule) · profiles S→T→G phased · name/location as above.

## 0. The 4-line summary

A standalone sub-project providing RTSP streaming + ONVIF services that runs
on every OmniSight-supported SoC (low-end RV1126 to high-end RK3588/QCS6490)
across vendor kernels 5.5–7.0, packaged so a customer IPCAM (or IPCAM-based
application) starts from ready-made, license-clean material.

## 1. Premise-check summary

- All 8 mirrored vendor BSPs ship GStreamer with vendor hw-encode plugins
  (Rockchip MPP, NXP VPU, TI, QCOM Venus, MTK VCODEC) — verified against the
  vendor-mirrors-nda catalog rows during onboarding.
- Kernel-version fragmentation (5.5–7.0) is concentrated in capture/encode
  drivers, not in RTSP/ONVIF userland → a thin per-board HAL is sufficient;
  no per-kernel code in the serving layers.
- ONVIF open-source implementations (onvif_srvd, gSOAP output) are GPL —
  unusable for customer closed-source firmware → clean-room implementation
  is the differentiating asset of this sub-project.
- `imaging` skill pack + embedded_catalog already model camera-ish products;
  this sub-project becomes their streaming backend, not a competitor.

## 2. Target architecture

- **L0 HAL — board pipeline profiles** (`hal/profiles/*.yaml`): declarative
  capture→ISP→encode GStreamer fragments + defaults; `generic-v4l2` (kernel
  vivid) is the CI reference contract; every real profile carries
  `verified: true/false` flipped only by a HIL run on the P0.E.2 bench.
- **L1 RTSP** (gst-rtsp-server, LGPL dynamic): mount manager, basic/digest
  auth, multicast optional, stream URIs stable per ONVIF profile token.
- **L2 ONVIF (clean-room)**: WS-Discovery responder; Device + Media (+Media2
  in T) + Events services as hand-written HTTP/SOAP-XML (no gSOAP, no wsdl
  codegen at build time); Profile S first, T (H.265/Media2/metadata) second,
  G (recording/search + storage ring) third.
- **L3 packaging**: Yocto meta-layer + Buildroot external package; consumers
  pin release tags; later a `skill-rtsp-onvif` pack in Productizer stamps
  customer IPCAM products around it.
- **Kernel-span policy**: V4L2 baseline = 5.5; newer APIs need runtime probe
  + fallback; vendor encode ONLY via BSP GStreamer plugins (no raw vendor
  ioctls in this repo).
- **License ledger**: every dependency lands with a row in
  `docs/DESIGN.md §licenses`; GPL = reject at MR. This invariant is copied
  into every leaf ticket of this EPIC.

## 3. Sub-EPIC breakdown (6)

- **R1 rtsp-core** (~5): profile loader + schema check; gst-rtsp-server glue
  (mounts/auth/URI scheme); generic-v4l2 contract tests (vivid in CI);
  config schema + defaults; RV1126 first hardware profile (HIL-gated
  `verified` flip).
- **R2 onvif-s** (~6): WS-Discovery responder; SOAP-XML mini-framework
  (request parse/dispatch/serialize — the clean-room core, NO codegen);
  Device service; Media service (GetProfiles/GetStreamUri wired to L1
  mounts); Events pull-point skeleton; behavioral conformance tests via
  python-onvif-zeep client (official ONVIF Device Test Tool needs
  membership — see risk table).
- **R3 onvif-t** (~4): Media2 service; H.265 profiles (RK3588/RK3576/
  QCS6490); metadata streaming skeleton; imaging service for ISP-capable
  boards.
- **R4 onvif-g** (~4): storage abstraction (ring on eMMC/SD + retention);
  recording control service; search/replay services; recording conformance
  tests.
- **R5 packaging-productization** (~4): Yocto meta-layer; Buildroot external
  package; `skill-rtsp-onvif` pack in Productizer (scaffolder stamps customer
  IPCAM product wired to a pinned release); embedded_catalog row + capability
  matrix entry.
- **R6 board-matrix** (~3 waves): profile per remaining board (rk3588,
  imx93, rk3576, genio1200, am62x, qcs6490, rv1126b), each HIL-verified on
  the P0.E.2 bench; H.265 variants where capable.

Dependency spine: R1 → R2 → (R3 ∥ R5) → R4; R6 trails R1 per-board and is
the only sub-EPIC requiring hardware sessions.

## 4. Phasing + calendar

- Wave 1: R1 complete (vivid-green CI + RV1126 profile stub).
- Wave 2: R2 complete = sellable "RTSP+ONVIF-S IPCAM base".
- Wave 3: R3 + R5 in parallel (different repos/areas).
- Wave 4: R4; R6 boards continue as bench time allows.

## 5. Risks

| risk | mitigation |
|---|---|
| GPL contamination (the defining risk) | clean-room rule in every ticket; license ledger row per dep; MR review gate |
| ONVIF conformance w/o official test tool | behavioral tests via python-onvif-zeep + ODM client; official tool deferred to a customer-funded membership |
| vendor plugin API drift across BSP kernels | HAL profiles isolate; `verified` flag only via HIL; profile changes re-run bench |
| low-end CPU budget (RV1126 ONVIF+RTSP) | clean-room SOAP is static C, no codegen bloat; budget asserted in R1 contract tests |
| dev/review split brain (GitLab repo vs Gerrit habits) | code review = GitLab MR in `omnisight/rtsp-onvif-server`; design docs + skill pack = Gerrit in Productizer (this doc's flow) |
| runner delivery route to a GitLab repo (delivery targets today are Gerrit/GitHub-PR) | Phase 1: leaves developed via operator/assistant push to GitLab MR; a `gitlab_mr_target` delivery backend is a named R5 follow-up, NOT assumed |

## 6. What this EPIC does NOT do

ISP tuning per sensor (BSP/overlay layer); cloud/P2P relay; mobile viewer
apps (Case 3 material covers app-side patterns); motion analytics (future
pack riding npu-detection); ONVIF Profile M/D.

## 7. Next gates

1. This doc +2 → file EPIC structure (META + R1 leaves; H10 ≤3 agent:auto
   per batch; mutex discipline: single designated editor for shared files).
2. R1 leaf 1 (profile loader + vivid contract test) is the blind-test leaf:
   if it round-trips clean through CI, the leaf template is sound.
3. RV1126 `verified: true` flip = first P0.E.2 bench session on this EPIC.
