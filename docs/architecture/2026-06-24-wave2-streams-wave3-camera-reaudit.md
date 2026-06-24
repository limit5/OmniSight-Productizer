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
1. **Never use Qt6 `Camera`/`MediaDevices` on this board** — it's broken. Drive **gstreamer/V4L2 directly** (proven).
2. **PREFER in-launcher QML views via the C++ `GstVideoSource` bridge — NOT process-apps.** ⭐ **Proven 2026-06-24 (OP-2348 camera):** a C++ `gst→QVideoSink` bridge (`v4l2src ! appsink → QVideoFrame → VideoOutput`) inside an AppHost-pushed QML view gives the live feed AND the launcher's ← Home / dock nav **for free** — sidestepping the OP-2346 close/switch gap entirely (that gap only afflicts fullscreen *process-apps* with their own wayland surface). So camera ✅ and ai-vision should both be in-launcher views via the bridge; **OP-2346 is NOT a prerequisite for them.** (live-view shipped as a process-app earlier; it can later migrate to the bridge to gain nav.)
3. **UVCCamera_Qt stays a Case-2 (UVC customer) deliverable**, not an allinone-board app. Don't force-fit it onto the MIPI board.

## Revised Wave 2 — streams (NEEDS ONE PRODUCT DECISION)
"streams / ONVIF" as originally scoped is dead (no daemon, no external source). **DECIDED 2026-06-24 (operator): (B) Dual-camera viewer.**
- **Streams = a 2-up live grid of the board's 2× IMX415** (CSI1 + CSI3), each its own gst pipeline → its own surface/region. Pure on-board, no networking. Process-app (gst, two `v4l2src` pipelines), gated on OP-2346 nav. Builds on the live-view pipeline (two instances / a compositor-side 2-up, or a single gst pipeline with `compositor`/`videomixer` 2-up → one waylandsink).
- (Rejected (A) RTSP-out/IPCAM-server for now — revisit if outbound streaming becomes a product need.)

## Revised Wave 3 — camera / scanner / ai-vision / conference
All **gst/V4L2-based** (not Qt Camera, not UVCCamera_Qt), **in-launcher QML views via the `GstVideoSource` bridge** (nav for free, no OP-2346):
- **camera** = **MIPI Camera Studio** ✅ **v1 DONE (OP-2348, #1742, board-verified)**: live preview via the bridge, in-launcher. FOLLOW-UP: capture photo / record clip (gst → mp4 on /userdata) + CSI1/CSI3 switch + resolution/format per the [[project_uvc_format_matrix_spec]]. *(Replaces "UVCCamera_Qt".)*
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
