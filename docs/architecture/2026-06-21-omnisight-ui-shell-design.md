# OmniSight UI — one shell across all SDKs (design + app-registration contract)

**Status: DESIGN (2026-06-21).** Goal: a single, consistent OmniSight UI — the
"ATK-QDesktop kind of launcher" look — applied across **all** SDKs (pos-kiosk,
conference-appliance, camera-sdk/IPCAM, future smart terminals), **including the
headless IPCAM** (operator: "全部 SDK 都配 UI"). Build the shell ONCE; each SDK
registers its own apps. This is the UI analogue of our HAL/skill-pack reuse.

## The core insight (because "all SDKs" includes headless)
A headless IPCAM has no local screen, so it cannot run a local Qt launcher. The
only way ONE UI spans display devices AND headless devices is to split into:
- **One design system** (tokens + component specs + the ATK-style launcher UX) —
  the single source of truth for *look*.
- **One app-registration contract** (`apps.manifest`) — the single source of
  truth for *what each device exposes*.
- **Two renderers** that both implement the design system + consume the manifest:
  1. **Qt6/QML launcher** — on-device local display (KIOSK, conference, smart
     terminal, IPCAM-with-HDMI).
  2. **Web launcher** — served by the device for headless + remote admin (the
     IPCAM's local-config screen = a web UI, like ATK's `ipcweb-backend`), and
     reused for fleet/remote management.

Same design tokens + same manifest ⇒ the launcher looks and behaves identically
whether it's painted by Qt on a panel or by React in a browser.

```
            ┌─────────────────────┐   ┌──────────────────────┐
            │  design-system/      │   │  apps.manifest (per   │
            │  tokens + components │   │  SDK: what apps exist)│
            └──────────┬──────────┘   └───────────┬──────────┘
                       │  (both renderers consume both)        
        ┌──────────────┴───────────┐   ┌──────────┴───────────┐
        │ launcher-qt (Qt6/QML)    │   │ launcher-web (Next/   │
        │ local panel              │   │ React) headless+remote│
        └──────────────────────────┘   └──────────────────────┘
```

## The app-registration contract (the centerpiece)
Each SDK ships ONE `apps.manifest.yaml`. The launcher (Qt or web) reads it,
shows a tile per app (filtered by the device's capabilities), and launches the
entry. Apps are **declarative**; adding an app = a manifest entry + its entry
component, never editing the shell.

```yaml
# omnisight apps.manifest — schema_version 1
device:
  id: pos-kiosk-rk3588            # which product/build
  display: local                 # local | headless | both
  default_renderer: qt           # qt | web
  locales: [en, zh-Hant]
theme:
  brand: omnisight               # design-system theme variant
  accent: "#2563eb"              # optional per-device accent
apps:
  - id: cashier
    title: { en: "Cashier", zh-Hant: "收銀" }
    icon: cashier                # design-system icon id
    category: core               # core | media | tools | diagnostics | settings
    order: 10
    entry:                       # ONE of: qml | web | process
      qml: "qrc:/apps/CashierView.qml"
      web: "/cashier"
      process: ["/usr/bin/cashier", "--fullscreen"]
    caps_required: [display, payment]   # tile hidden if device lacks these
    roles: [operator]            # optional RBAC gate
  - id: camera
    title: { en: "Camera", zh-Hant: "相機" }
    icon: camera
    category: media
    entry: { qml: "qrc:/apps/CameraView.qml", web: "/camera" }
    caps_required: [camera]
  - id: settings
    title: { en: "Settings", zh-Hant: "設定" }
    icon: settings
    category: settings
    entry: { qml: "qrc:/apps/Settings.qml", web: "/settings" }
  - id: diagnostics
    title: { en: "Diagnostics", zh-Hant: "診斷" }
    icon: diag
    category: diagnostics
    entry: { web: "/diag" }      # web-only app (e.g. headless IPCAM)
    caps_required: []
```
Contract rules: `entry` provides the renderer-appropriate target (qt picks
`qml`/`process`, web picks `web`); a tile is shown only if the device satisfies
`caps_required` (capabilities come from the HAL/profile) and `roles`; `category`
+ `order` drive grouping/sort on the home grid; `title`/icons are i18n via the
design system. The manifest is validated against a JSON schema in CI (like our
hal/profiles validator).

## Renderers (seeded from what we already have)
- **launcher-qt** — Qt6/QML. Seed from **pos-kiosk `src/ui`** (already Qt6:
  `AdaptiveGrid`, `ResponsiveText`, DIP engine, render-tiering Vulkan/pixman,
  multi-display/hotplug). Generalize `AdaptiveGrid` into the paged app-tile home
  screen + top/status bar; add the manifest loader + entry launcher.
- **launcher-web** — Next.js 16 / React 19 / Tailwind 4. Seed from the
  **productizer frontend** (`app/`). Same tokens via a shared Tailwind theme
  preset generated from the design-system tokens. Serves the IPCAM local-config
  + doubles as remote/fleet admin.

## Per-SDK mapping (all SDKs covered)
| SDK / device | display | renderer | example apps registered |
|---|---|---|---|
| pos-kiosk (KIOSK/POS) | local | **qt** | cashier, inventory, settings, diagnostics, factory-test |
| conference-appliance | local | **qt** | call, contacts, camera, settings, diagnostics |
| camera-sdk IPCAM (rtsp-onvif) | **headless** | **web** | live-view, streams/ONVIF, network, storage, settings, diagnostics |
| camera-sdk IPCAM w/ HDMI | local | qt (+web) | same apps, qt tiles |
| smart-terminal (future) | local | qt | per-product |
| productizer (fleet) | web | web | (already its own React app; adopts the design system) |

## Qt version + cross-SoC (and how headless dissolves the hard case)
- Standardize **Qt6** for all *local* launchers (pos-kiosk/conference already Qt6).
  Do NOT reuse ATK's Qt5 QDesktop code (vendor/Alientek license + Qt5); build our
  own, same UX.
- The hard case — **Qt6 on the RV1126 ATK buildroot-2018 (Qt5-only)** — largely
  **dissolves**: the RV1126 product is the **headless IPCAM → web launcher (no
  local Qt needed)**. Local Qt6 launchers run on the newer stacks (pos-kiosk
  Yocto, rk3588) where Qt6 is fine. If a future RV1126 device needs a *local*
  screen, options are a Qt5 build of the same QML (Qt Quick is largely Qt5↔Qt6
  portable) or the web launcher in a kiosk browser.

## Build / packaging
- `omnisight-ui` = a shared repo: `design-system/` (tokens + icons + schema),
  `launcher-qt/` (Qt6/QML lib + app), `launcher-web/` (React component lib).
- Qt SDKs package `launcher-qt` + their `apps.manifest` + qml entries into their
  rootfs (Yocto recipe / buildroot package). Web SDKs serve `launcher-web` + their
  manifest. Tokens compile to both a QML theme singleton and a Tailwind preset
  from one source, so look stays in lock-step.

## Phasing
- **U0** design-system tokens + `apps.manifest` JSON schema + CI validator (safe,
  no device dep) — the contract everything keys off.
- **U1** extract pos-kiosk `src/ui` core → `launcher-qt` shell (manifest-driven
  paged tile grid + entry launcher); prove on pos-kiosk (re-register its apps).
- **U2** `launcher-web` from the productizer frontend + tokens preset; prove as
  the camera-sdk IPCAM local-config (replaces ATK `ipcweb-backend`).
- **U3** conference-appliance + smart terminals register their apps.
- **U4** remote/fleet management reuses launcher-web over the productizer.

## License
Inspired by the ATK QDesktop **UX pattern** only. No Alientek code is copied;
`omnisight-ui` is our own Qt6/React implementation under our license.

## Decisions (LOCKED 2026-06-21, operator)
1. **One shared `omnisight-ui` repo** — `design-system/` + `launcher-qt/` +
   `launcher-web/` live together; SDKs consume it (NOT folded into each SDK).
2. **Headless IPCAM = web launcher ONLY** — no on-panel status screen; the
   device's local-config UI is the served web launcher (replaces ATK ipcweb).
3. **i18n default locales = `en` + `zh-Hant`** (繁中). Manifest `title`/design-
   system strings ship both; the contract's locale list defaults to these.
