# android-rtsp-onvif-client — Case-1 Android RTSP/ONVIF CLIENT pack (OP-1799)

**Pack status**: B-1 skeleton. Reuses the `android` scaffolder; ships no
scaffolder or templates of its own yet.

## What this is (and the naming trap it fixes)

This is the **CLIENT** side of IP-camera support: an Android app that
**consumes** camera streams — discovers cameras on the LAN over
**ONVIF / WS-Discovery** and plays their **RTSP** streams.

⚠️ Do not confuse this with the existing `ipcam` / `uvc` packs. Those are
**device-SIDE** (the box *serves* / exposes a stream as a USB gadget or
network camera). The client side was the missing half — this pack fills
it.

## How rendering works (no new scaffolder)

`scripts/scaffold.py` resolves this pack to the existing
`backend.android_scaffolder` through the explicit override in
`backend/skill_registry.py` (`_SCAFFOLDER_MODULE_OVERRIDES`):

```
android-rtsp-onvif-client → backend.android_scaffolder
```

So a render produces the standard Jetpack Compose + Gradle 8 + Kotlin 2.0
Android app skeleton (from `configs/skills/skill-android/scaffolds/`).

```bash
# discover the pack
scripts/scaffold.py --list            # lists android-rtsp-onvif-client

# render the client-app skeleton
scripts/scaffold.py android-rtsp-onvif-client \
    --out-dir ./CameraClient --project-name CameraClient
```

## Scope (B-1 skeleton — what is and is NOT here)

| In this pickup                                  | Follow-on (same pack)                       |
| ----------------------------------------------- | ------------------------------------------- |
| Pack dir + `skill.yaml` + `tasks.yaml`          | ONVIF WS-Discovery probe + device client    |
| Dispatcher routing to `android_scaffolder`      | RTSP playback surface (Media3/ExoPlayer)    |
| Renders the base Android app skeleton           | Compose player screen + domain code         |
| Dispatch/render + manifest-validation tests     | Per-layer scaffolds, tests, HIL recipes     |

Safe-local internal dev (authored under omnisight-self, not a customer
run). Needs no platform/sandbox.
