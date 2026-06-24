# OP-2346 spike — a layer-shell compositor for process-app navigation

**Date:** 2026-06-24 · **Context:** Phase 2 live-view (OP-2347) ships the IMX415 feed as a fullscreen gst `waylandsink` process-app, but there is **no on-screen way to close/switch back** to the launcher. This blocks every process-app (live-view + Wave 3 camera/scanner/conference). [[project_rk3588_qt6_bundle]]

## Why the obvious fixes are dead (on the current stack)

The board runs **weston 14.0.2** (ATK rootfs, sysv-init). Empirically:
- **No `wlr-layer-shell`** — weston only advertises its private `weston_desktop_shell` + `xdg_wm_base` (`wayland-info`). So an OmniSight-styled always-on-top nav overlay is impossible.
- **weston exclusively grabs the touchscreen** (EVIOCGRAB) — a neutral second reader (`evtest /dev/input/event2`) captured **0 events** during live taps. So **no background gesture/evdev daemon can read touches** (the 3-finger-tap daemon I built — `omnisight-nav-daemon.py` — works code-wise but receives nothing).
- A gst **fullscreen** surface sits in weston's fullscreen layer, **above** the weston panel, so weston's own panel can't be the home button unless apps are non-fullscreen (windowed-camera + weston-styled bar = aesthetic regression + placement mess).
- Over a fullscreen surface, only a **keyboard** key reaches weston — there's no keyboard on the kiosk (touch only; hardware keys are also libinput-grabbed).

⇒ The trigger must come *through* the compositor, and weston gives us no touch-reachable, always-on-top affordance. **Decision (operator, 2026-06-24): investigate a layer-shell-capable compositor** so an OmniSight-styled always-on-top nav overlay becomes possible.

## Option survey (buildroot 2025.02 — the Qt6-bundle buildroot)

| Compositor | layer-shell | systemd? | deps | Fit |
|---|---|---|---|---|
| **cage 0.2.0** (wlroots 0.18.2) | ✅ (added in 0.2.0) | ❌ no (uses **seatd**) | wlroots + seatd | **kiosk; multi-toplevel + layer-shell — BEST FIT** |
| sway | ✅ | ⚠️ **forces systemd** (buildroot dep) | +json-c/cairo/pango/gdk-pixbuf | **blocked** — board is sysv-init |
| cog | ✅ | no | WebKit | web-only kiosk — irrelevant |
| labwc / wayfire / river | ✅ | — | — | **not packaged** in buildroot 2025.02 |

Supporting facts:
- `wlroots 0.18.2`, renderer = **gles2**, needs libgbm — satisfied by the board's **libmali** (Mali Valhall G610). weston already renders on Mali EGL/GBM via drm-backend, so the EGL/GLES2/GBM stack is proven; wlroots uses the same → **high confidence Mali works**. DRM nodes present (`card0/card1/renderD128/129`).
- **seatd is packaged** → systemd-free seat/DRM session management (the thing that blocks sway). cage+seatd is the systemd-free combo.
- **No `LayerShellQt` / `gtk-layer-shell` / `wlr-protocols`** in buildroot → the nav overlay can't trivially be a Qt window. Two ways to get the overlay:
  1. **Minimal standalone layer-shell client** (C, ~200–300 lines, wayland + the `wlr-layer-shell-unstable-v1` protocol XML) drawing a "⌂ Home / ← Back" bar; on tap → `go-home.sh`. Smallest, compositor-agnostic.
  2. **Add a `LayerShellQt` buildroot package** (small KDE lib) so the launcher itself owns an always-on-top Qt nav surface. Cleaner integration (OmniSight design tokens), more packaging work.

## Recommended architecture

**cage 0.2.0 + seatd** as the compositor, replacing weston for the OmniSight kiosk; **a layer-shell "home/back" bar** always on top; process-apps stay **true-fullscreen** (good immersive camera) because the layer overlay floats above them.

```
 cage (wlroots/Mali, seatd session)
 ├─ xdg-toplevel: omnisight-launcher (Qt6)         ← home / app grid / dock
 ├─ xdg-toplevel: live-view gst waylandsink (fs)   ← process-app, fullscreen
 └─ layer-surface (overlay, anchored bottom): nav bar  ← ALWAYS on top
       └─ tap "⌂" → go-home.sh → close foreground toplevel → launcher
```
- `go-home.sh` (already written, OP-2346) closes the foreground process-app generically (children of `omnisight-launcher`) — reused verbatim.
- The nav bar can later grow Back / Recents / status — a real OmniSight system bar.

## Risks / unknowns (de-risk in the PoC, do NOT migrate blind)
1. **Mali on wlroots 0.18 actually initialises** (high confidence, but the ATK libmali variant + kernel 6.1 must cooperate — weston proves EGL/GBM, not wlroots specifically).
2. **cage 0.2.0 multi-toplevel + layer-shell input on touch** (does a layer surface receive touch over a fullscreen toplevel?).
3. **Whole UI stack under cage**: Qt6 launcher (xdg-shell — fine on any compositor), gst `waylandsink`, the Mesa-EGL-shadowing lesson ([[project_rk3588_qt6_bundle]]) still applies.
4. **seatd session** bring-up on this sysv board (seatd daemon + the compositor as a seatd client).
5. **Input grab**: cage will also grab input — but now the overlay is a real wayland client that receives touch when the user hits the bar (no evdev needed).
6. Migration touches the boot path (replace `S49weston` + the launcher autostart) — keep weston as a fallback during PoC.

## Staged plan (low-risk, weston stays bootable until proven)
- **P0 — PoC (gate):** cross-build cage + wlroots + seatd (+ deps) from buildroot-2025.02; vendor to the overlay; boot cage **alongside/instead of** weston *manually* (not autostart) with: launcher + a hello-world layer-shell bar. Verify on-board: Mali render, launcher draws, touch works, the layer bar stays on top of a fullscreen gst app and receives a tap. **This is the go/no-go.**
- **P1:** build the real nav bar (home/back) as a layer client (option 1) → wire `go-home.sh` + switch.
- **P2:** make cage+seatd the boot compositor (init scripts), keep a weston fallback; re-vendor + bake; on-board soak.
- **P3:** generalise to Wave 3 process-apps (camera/scanner/conference) + grow the bar (recents/status). Optionally add `LayerShellQt` so the launcher owns the bar in OmniSight design.

## P0 PoC RESULT (2026-06-24) — PARTIAL, blocker found
- ✅ cage 0.2.0 cross-built (buildroot-2025.02: +cage +wlroots +seatd +eudev +libinput +pixman; flip /dev mgmt to eudev for `BR2_PACKAGE_HAS_UDEV`). Vendored to `/opt/omnisight/cage/` (3.1M; **excluded Mali EGL/GLES/GBM** so it uses the board's libmali — the Mesa-shadow lesson).
- ✅ cage RUNS on the board: gets **DRM master** (after killing weston — only one DRM master; weston `S49weston` had to be killed, no respawn), **initializes Mali** (`arm_release_ver: g24p0-00eac0` banner), runs its child (`libseat` builtin backend, root — no seatd daemon needed). So **layer-shell compositor on this HW is viable in principle.**
- 🚫 **BLOCKER: the Qt launcher CLIENT fails EGL under cage** — `qt.qpa.wayland: Failed to initialize EGL display 3001` / `QRhiGles2: Failed to create context` / `Failed to create RHI`. Hypothesis: the ATK **Mali blob is weston-coupled** — weston advertises a Mali-specific **`mali_buffer_sharing`** wayland global (confirmed in weston `wayland-info`) that the Mali client libEGL uses for buffer sharing; cage/wlroots exposes standard **`linux-dmabuf`** instead, so Mali client-EGL can't init. Compositor-side Mali (GBM/KMS) works; CLIENT-side is the gap.
- **Next investigation (before committing to migration):** (a) try a **GBM/dmabuf libmali variant** (Rockchip libmali has `-gbm` / `-wayland-gbm` builds; the ATK one may be the weston-specific variant) so clients use standard wayland-egl+dmabuf; (b) Qt EGL platform/env knobs under wlroots; (c) confirm wlroots advertises `zwp_linux_dmabuf_v1` + `wl_drm` that Mali expects; (d) test a NON-Qt GL client (e.g. `weston-simple-egl`/glmark2-wayland) under cage to isolate Qt-vs-Mali. If the Mali blob can't do standard dmabuf clients, cage is blocked on THIS board's GPU userspace → fall back to weston-panel-bar or revisit with a different libmali.

## P0 PoC VERDICT (2026-06-24) — NO-GO for cage with the current Mali blob
The decisive isolation test: **`weston-simple-egl` (a minimal NON-Qt EGL client) also fails under cage** — `init_egl: Assertion 'ret == EGL_TRUE' failed` (SIGABRT). So the Qt EGL failure is NOT Qt-specific — the **ATK Mali userspace blob cannot initialise a client EGL display under wlroots**. The blob is weston-coupled (`mali_buffer_sharing`); wlroots exposes standard `linux-dmabuf`, which this libmali variant's client EGL doesn't accept. Compositor-side Mali (GBM/KMS) works; **client-side EGL is the hard wall.**

⇒ **cage/wlroots is not viable on this board without swapping libmali** for a gbm/standard-dmabuf variant (e.g. Rockchip `libmali-valhall-g610-*-gbm`). That's a GPU-blob swap matched to the kernel — a separate, risky effort that could also destabilise weston. **Recommend NOT pursuing cage now.** Fallbacks: (1) weston desktop-shell **panel nav-bar** (windowed apps, weston-styled bar — works on today's stack, no GPU risk); (2) **park nav**, keep adb/`go-home.sh` for dev, build Wave 3 camera/ai-vision (process-apps demo fine without nav), revisit a libmali swap later as its own spike. Artifacts kept at `/opt/omnisight/cage/` on the board (not autostarted) for any future libmali retry.

## libmali swap ATTEMPTED + TESTED (2026-06-24) — also NO-GO
Operator chose to try the libmali swap. Findings:
- Current board libmali (g24p0): **`mali_buffer_sharing` PRESENT, `zwp_linux_dmabuf` ABSENT** — weston-coupled.
- Fetched the matching DDK variant `libmali-valhall-g610-g24p0-wayland-gbm.so` (Rockchip/JeffyCN libmali repo). It is **ALSO `mali_buffer_sharing`-only** (`zwp_linux_dmabuf` absent; tagged `valhall---mbs2`). **Every Rockchip libmali wayland variant uses `mali_buffer_sharing`, not standard linux-dmabuf.**
- Tested it under cage (vendored into a cage-only LD_LIBRARY_PATH, system libmali untouched): `weston-simple-egl` **still fails `init_egl` assertion**. Confirmed: the swap does not help.

**Definitive verdict: cage/wlroots clients cannot run on this board's GPU userspace.** The Rockchip proprietary Mali stack only does wayland buffer-sharing via its `mali_buffer_sharing` protocol (weston has a module for it; wlroots does not). The only ways to wlroots here are both large + out of scope for a nav feature:
1. **Patch wlroots/cage to implement `mali_buffer_sharing`** (Rockchip's protocol) — deep custom-compositor work + ongoing maintenance.
2. **Kernel 6.1 → 6.10+ for Panfrost/panthor** (open Mali driver = standard dmabuf, works with wlroots) — a massive ATK BSP upgrade.

⇒ **OP-2346 layer-shell-compositor path is closed on the current ATK BSP.** Recommended: **weston desktop-shell panel nav-bar** (windowed apps) as the pragmatic on-screen nav, OR **park nav** (interim `go-home.sh`/adb) and build Wave 3; revisit only if/when the BSP moves to a Panfrost-capable kernel. cage artifacts + the candidate libmali kept at `/opt/omnisight/cage/` for a future kernel-6.10 retry.

## Effort / call
Multi-day migration with real risk (new compositor under the whole UI). **Do P0 PoC first** — it's the cheap gate that proves Mali+cage+layer-shell on this board before committing. If P0 fails (Mali/wlroots incompatible), fall back to the weston-panel-bar compromise (windowed apps) or a hardware-key escape.

**Until this lands, live-view close = `go-home.sh` via adb (dev) — documented, tracked here.**
