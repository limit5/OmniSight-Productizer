# windows-uvc-host - Case-2 Windows UVC HOST pack (OP-1815)

**Pack status**: B-1 skeleton. Reuses the `desktop-tauri` scaffolder; ships
no UVC capture code or templates of its own yet.

## What This Is

This is the **HOST** side of UVC support: a Windows desktop app that
captures from an attached USB Video Class camera.

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

Rendering produces the standard Tauri 2.x desktop skeleton from
`configs/skills/skill-desktop-tauri/scaffolds/`, which covers Windows
desktop packaging through the existing desktop-tauri path.

```bash
scripts/scaffold.py --list
scripts/scaffold.py windows-uvc-host \
    --out-dir ./UvcHost --project-name UvcHost
```

## Scope

In this pickup: pack directory, manifest, DAG tasks, dispatcher routing,
manifest validation, and a dispatcher render test.

Follow-on: UVC camera enumeration, capture lifecycle, and preview domain
code/scaffolds.
