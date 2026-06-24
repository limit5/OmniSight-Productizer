# OmniSight 全功能機 — Phase 2: wire the "active" apps to launchable apps

**Date:** 2026-06-24 · **Context:** Phase 1 delivered the RK3588 "全功能機" launcher — 40-app catalog, dock/recents switching, planned-tile placeholders ([[project_rk3588_qt6_bundle]]). The 9 `status:active` apps currently have manifest entries but no real binary/view behind them. **Phase 2 = make each active app actually launch + switch.**

## How an app launches (already built — U5.1 AppLauncher)
- **qml entry** (`entry.qml`) → launcher pushes the QML view onto AppHost's StackView, **in-process**. switchTo re-raises; closeApp pops. No separate binary. Best for views authorable in QML.
- **process entry** (`entry.process`) → launcher spawns the argv via QProcess (no shell), tracks it, emits `processFocusRequested` for raise/focus. **Separate wayland client** — the ATK model. Lets our existing Qt apps drop in with ~zero rewrite.

## App inventory + approach

| App | Type | Source asset | Effort | Wave |
|---|---|---|---|---|
| **diagnostics** | qml view | new (read /proc,/sys: CPU/NPU/temp/storage) | low | 1 |
| **settings** | qml view | new (network/display/locale) | low–med | 1 |
| **live-view** | qml view | QtMultimedia VideoOutput ← board IMX415 / rtsp | med | 2 |
| **streams** | qml view | control rtsp-onvif-server (already a board daemon, Case 1) | med | 2 |
| **storage** | qml view | recordings browser | med | 2 |
| **ai-vision** | qml view | RKNN person-detect overlay on camera feed (Case 5 ai) | med–high | 3 |
| **camera** | process | **UVCCamera_Qt** (Case 2, the redesigned Camera Studio) cross-built w/ our Qt6 sysroot | med–high | 3 |
| **scanner** | process | UVCCamera_Qt scanner mode (same binary) | low (rides camera) | 3 |
| **conference** | process | **conf-touch-ui** (Case 5) cross-built | high | 4 |

## Cross-cutting infra (Wave 0 — unblocks the rest)
1. **Process-app launch + switch on weston**: verify QProcess-spawned wayland clients raise/focus via the launcher's `processFocusRequested` (wayland has no generic "raise"; may need `xdg-activation` or weston's `activate` — spike + pick the mechanism). Deterministic ctest + on-board HIL.
2. **qt6multimedia in the bundle**: the current Qt6 6.8.1 bundle is Quick+Wayland only; camera/live-view apps need `qt6multimedia` (+ gstreamer/V4L2 backend) — add to the buildroot Qt6 build + bundle.
3. **App-deploy convention**: cross-built process binaries land in `/opt/omnisight/apps/<id>` (overlay); manifest `process` entries already point there.

## Wave plan (effort-ascending, value-weighted)
- **Wave 0** (infra): process-launch-on-weston spike + qt6multimedia bundle. *Gate for process apps.*
- **Wave 1** (in-launcher qml, no cross-build — fast proof): **diagnostics + settings**. Proves tap→launch→dock-switch→home end-to-end with real views. ✅ **DONE 2026-06-24** (OP-2341 W1.1, OP-2342 W1.2, OP-2343 target-Qt6 XHR fix). Both live on board, baked into update.img.
- **Wave 2** (qml views w/ device I/O): live-view ✅ (OP-2347, process-app), storage ✅ (OP-2344), streams (re-scope pending).
- **Wave 3** (process apps): camera / scanner / ai-vision — **NOTE: re-audited, see below**.
- **Wave 4**: conference (conf-touch-ui).

> **⚠ Wave 2 streams + Wave 3 camera were RE-AUDITED 2026-06-24** against board ground-truth — the original "UVCCamera_Qt camera" + "rtsp-onvif-server streams" assumptions are INVALID on this board (MIPI-only camera, no UVC, no onvif daemon, Qt Camera broken). **Read `2026-06-24-wave2-streams-wave3-camera-reaudit.md` for the revised plan** (gst/V4L2 not Qt-Camera; MIPI Camera Studio not UVCCamera_Qt; RKNN ai-vision; streams needs an RTSP-out-vs-dual-cam product decision; all gated on OP-2346 compositor nav).

### ⚠ Wave 2 RE-SEQUENCING (board probe 2026-06-24, ATK-DLRK3588 @ adb 192.168.0.113)
Two of the three Wave 2 apps have unmet upstream deps on THIS board — only **storage** is buildable + board-verifiable today:
- **storage** → recordings/files browser over **`/userdata`** (mmcblk0p8, 101 GB, empty). No streaming dep. **Do this first (Wave 2a).** ⚠ Per [[project_rk3588_qt6_bundle]] OP-2343: the target Qt6 has NO `qml_xmlhttprequest` and likely no `Qt.labs.folderlistmodel` either — directory listing MUST go through a **C++ helper** (extend `SystemReadout` or a sibling `FsBrowser`), never QML XHR/JS file APIs. Bake this into the ticket AC.
- **live-view** → needs **QtMultimedia (VideoOutput)** which is NOT in the current bundle (Quick+Wayland only). **Hard-blocked on Wave 0 qt6multimedia bundle.** Source = local IMX415 (V4L2/Mali) or RTSP.
- **streams / ONVIF** → there is **no rtsp-onvif-server / onvif daemon on this board** (that daemon is the Case 1 EVK, [[project_rtsp_onvif_server_evk_bringup]]). On the allinone box the only stream source is the local IMX415, so "streams" overlaps Wave 3's camera path + also needs QtMultimedia. **Re-scope or defer until camera path + qt6multimedia land.**
- **Implication:** qt6multimedia bundle (Wave 0 #2) is the real gate for live-view AND streams AND Wave 3 camera — promote it. Sensible new order: **storage (now) → qt6multimedia bundle → live-view + streams + camera (share the multimedia stack)**.

## Decomposition / delivery lanes
- **qml-view apps** (diagnostics/settings/live-view/streams/storage/ai-vision) = new QML in `omnisight-ui` launcher module → **routed runner tickets** (Gerrit), verified by offscreen ctest, then re-vendored into the image. (Watch the `.so`-deploy + NO_CACHEGEN gotchas from [[project_rk3588_qt6_bundle]].)
- **process apps** (camera/scanner/conference) = cross-build the existing Qt apps with our Qt6 sysroot → **hand-driven** (board-specific, like the launcher itself); deliver binary into the overlay + flip the manifest entry. UVCCamera_Qt is GitHub (Case 2), conf-touch-ui is conference-appliance (Gerrit).
- Each wave: build → on-board adb verify (launch + dock-switch) → re-vendor into `update.img` → flash.

## Risks
- **Wayland raise/focus** for process apps is the main unknown (Wave 0 spike de-risks it).
- **qt6multimedia + Mali/V4L2** camera path on the board (the bundle grows ~tens of MB).
- **Two-app GL contention** under weston (multiple GL clients) — verify on HIL.
- Per-app porting surprises (each existing app assumed its own host Qt; our sysroot is 6.8.1).
