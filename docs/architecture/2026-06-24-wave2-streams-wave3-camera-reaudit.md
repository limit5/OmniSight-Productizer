# Wave 2 streams + Wave 3 camera — re-audit (2026-06-24)

Re-audit after live-view (OP-2347) shipped, correcting the original Phase-2 plan's
assumptions against **on-board ground truth** (ATK-DLRK3588 @ adb 192.168.0.113).
See also [[project_rk3588_qt6_bundle]] and the Phase-2 plan doc.

## Ground-truth that invalidates original assumptions

| Original plan said | Reality on this board |
|---|---|
| streams = "control rtsp-onvif-server (Case 1 daemon)" | **No onvif/rtsp daemon on this board** — that's the Case 1 EVK. Only source here = local cameras. |
| camera = "UVCCamera_Qt (Case 2) as process-app" | **No USB UVC camera** (`/dev/v4l/by-id` empty; all nodes are `rkisp_*`). Camera = **MIPI IMX415 via the RK ISP**. UVCCamera_Qt is a UVC-customer app — **does not fit this board**. |
| qml-view apps use Qt Multimedia `Camera` | **Qt6 `Camera`/`MediaDevices` returns 0 cameras here** (libv4l2 >16-node limit / gstreamer-backend quirk). gstreamer itself works (`v4l2src device=/dev/video44`). |
| process-apps switch via "Wave 0 spike" | weston has **no layer-shell + grabs touch** → process-app close/switch needs a **compositor change** (OP-2346 spike → cage+layer-shell). |

Confirmed assets: **dual IMX415** (multiple `rkisp` ISP paths — mainpath `/dev/video44` + others), **RKNN NPU** (`librknnrt.so`, `rknn_server`, `/sys/kernel/debug/rknpu`), system **gstreamer 1.24.13** (v4l2src + waylandsink + full plugin set).

## Cross-cutting decisions (apply to all camera apps)
1. **Never use Qt6 `Camera`/`MediaDevices` on this board** — it's broken. Drive **gstreamer/V4L2 directly** (proven). Either a gst-launch process-app (like live-view) or, for a styled in-launcher view, a C++ `gstreamer→QVideoSink` bridge.
2. **All camera/scanner/ai-vision/conference apps are process-apps** → their close/switch is **gated on OP-2346** (compositor nav). They can be *built + demoed* before nav lands; they're not *usable* as a product until it does.
3. **UVCCamera_Qt stays a Case-2 (UVC customer) deliverable**, not an allinone-board app. Don't force-fit it onto the MIPI board.

## Revised Wave 2 — streams (NEEDS ONE PRODUCT DECISION)
"streams / ONVIF" as originally scoped is dead (no daemon, no external source). Two coherent re-scopes given the hardware — **operator to choose**:
- **(A) RTSP-out server** — make the board an **IP camera**: expose the IMX415 over RTSP/ONVIF (gst `rtspclientsink`/`test-launch` + an ONVIF responder). Aligns with the IPCAM product identity; the "Streams" tile shows/manages the outbound stream + clients. *This is the more on-brand option.*
- **(B) Dual-camera viewer** — the board has **2× IMX415**; "Streams" = a 2-up live grid (CSI1 + CSI3), each a gst pipeline. Pure on-board, no networking.
- (Deferring is fine — streams is the least-defined tile; live-view already covers single-camera viewing.)

## Revised Wave 3 — camera / scanner / ai-vision / conference
All **gst/V4L2-based** (not Qt Camera, not UVCCamera_Qt), process-apps, gated on OP-2346 for nav:
- **camera** = **MIPI Camera Studio**: live preview + capture photo / record clip (gst → mp4 on /userdata) + CSI1/CSI3 switch + resolution/format per the [[project_uvc_format_matrix_spec]]. Builds on the live-view pipeline + controls. *(Replaces "UVCCamera_Qt".)*
- **scanner** = barcode/QR from the live camera frames — gst `v4l2src ! ... ! appsink` → a decoder (zbar / zxing-cpp) → result overlay. Needs a barcode lib in the bundle. *(No longer "rides UVCCamera_Qt".)*
- **ai-vision** = **RKNN detection overlay** on the camera feed (person/object) — `librknnrt.so` + a `.rknn` model + gst pipeline + box overlay. NPU confirmed present → the real edge-AI differentiator. Highest value, med-high effort.
- **conference** = conf-touch-ui (Case 5), camera + ES8388 audio — heaviest; separate cross-build; last.

## Recommended sequencing (revised)
1. **OP-2346 P0 PoC** (compositor/layer-shell) — unblocks usable switching for ALL process-apps. Do FIRST.
2. **camera** (MIPI Camera Studio) — highest-utility, reuses live-view pipeline.
3. **ai-vision** (RKNN) — flagship differentiator; NPU ready.
4. **streams** — once the (A)/(B) product decision is made.
5. **scanner** — rides the camera pipeline + a decoder lib.
6. **conference** — last (heaviest).

## Tickets
- File per-app Wave 3 tickets (camera/scanner/ai-vision) when their wave starts, with the cross-cutting decisions above baked into AC (gst not Qt-Camera; process-app; nav via OP-2346).
- streams: hold for the (A)/(B) decision before filing.
