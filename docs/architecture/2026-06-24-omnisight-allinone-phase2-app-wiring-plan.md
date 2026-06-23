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
- **Wave 1** (in-launcher qml, no cross-build — fast proof): **diagnostics + settings**. Proves tap→launch→dock-switch→home end-to-end with real views.
- **Wave 2** (qml views w/ device I/O): live-view, streams, storage.
- **Wave 3** (process apps — the big reuse win): camera + scanner (UVCCamera_Qt), ai-vision (RKNN).
- **Wave 4**: conference (conf-touch-ui).

## Decomposition / delivery lanes
- **qml-view apps** (diagnostics/settings/live-view/streams/storage/ai-vision) = new QML in `omnisight-ui` launcher module → **routed runner tickets** (Gerrit), verified by offscreen ctest, then re-vendored into the image. (Watch the `.so`-deploy + NO_CACHEGEN gotchas from [[project_rk3588_qt6_bundle]].)
- **process apps** (camera/scanner/conference) = cross-build the existing Qt apps with our Qt6 sysroot → **hand-driven** (board-specific, like the launcher itself); deliver binary into the overlay + flip the manifest entry. UVCCamera_Qt is GitHub (Case 2), conf-touch-ui is conference-appliance (Gerrit).
- Each wave: build → on-board adb verify (launch + dock-switch) → re-vendor into `update.img` → flash.

## Risks
- **Wayland raise/focus** for process apps is the main unknown (Wave 0 spike de-risks it).
- **qt6multimedia + Mali/V4L2** camera path on the board (the bundle grows ~tens of MB).
- **Two-app GL contention** under weston (multiple GL clients) — verify on HIL.
- Per-app porting surprises (each existing app assumed its own host Qt; our sysroot is 6.8.1).
