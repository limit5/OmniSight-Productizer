# windows-uvc-host — Case-2 Windows UVC HOST pack (OP-1815 + OP-1817)

**Pack status**: B-1 content. Reuses the `desktop-tauri` scaffolder and
ships its own `scaffolds/` overlay carrying the UVC enumerate + capture
domain (OP-1817).

## What This Is

This is the **HOST** side of UVC support: a Windows desktop app that
enumerates and captures from an attached USB Video Class camera.

Do not confuse this with the existing `uvc` pack. That pack is
device-side USB gadget work. This pack is the consumer application that
connects to a UVC device.

## How Rendering Works

`scripts/scaffold.py` resolves this pack to the existing
`backend.tauri_scaffolder` through the explicit override in
`backend/skill_registry.py`:

```text
windows-uvc-host -> backend.tauri_scaffolder
```

Because this pack ships its own `scaffolds/` dir (distinct from the
desktop-tauri scaffolder's base dir), `resolve_scaffolder` surfaces it as
an **overlay**: the dispatcher renders the standard Tauri 2.x desktop
skeleton from `configs/skills/skill-desktop-tauri/scaffolds/` and then
layers this pack's UVC templates on top. One render produces the full host
project (skeleton + enumerate + capture).

```bash
scripts/scaffold.py --list
scripts/scaffold.py windows-uvc-host \
    --out-dir ./UvcHost --project-name UvcHost
```

## Overlay Contents

```text
src-tauri/src/uvc/device.rs    — UvcDevice/UvcFormat model + UvcEnumerator
                                  trait (NullEnumerator scaffold default)
src-tauri/src/uvc/capture.rs   — PixelFormat + frame geometry + the
                                  CaptureLifecycle state machine
src-tauri/src/uvc/commands.rs  — Tauri IPC commands (uvc_enumerate /
                                  uvc_start_capture / uvc_stop_capture /
                                  uvc_capture_status) over a managed UvcHost
src-tauri/src/uvc/mod.rs       — domain root + lib.rs wiring instructions
src/uvc.ts                     — typed frontend client over useTauri's call<T>
```

The overlay is **additive** — it does not edit the base skeleton's files.
`src-tauri/src/uvc/mod.rs` documents the manual `lib.rs` / capability
wiring a developer applies to activate the handlers.

## Scope

In this pickup (OP-1817): the UVC enumerate + capture scaffold templates,
the `tasks.yaml` build tasks (now `active`), and the dispatcher render
test. No `skill_registry.py` change (the routing override landed with the
OP-1815 skeleton) and no other pack is touched.

Follow-on: the real Media Foundation enumeration/capture backend behind
the `UvcEnumerator` trait, and a desktop preview surface.
