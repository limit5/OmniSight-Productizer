# android-rtsp-onvif-client — Case-1 Android RTSP/ONVIF CLIENT pack (OP-1799 + OP-1814)

**Pack status**: B-1 content. Reuses the `android` scaffolder (ships no
scaffolder of its own) and now layers its **ONVIF-discovery +
RTSP-playback client** templates on top via the dispatcher (OP-1814).

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

A render produces the standard Jetpack Compose + Gradle 8 + Kotlin 2.0
Android app skeleton (from `configs/skills/skill-android/scaffolds/`),
**then layers this pack's own `scaffolds/` on top as an overlay**. Because
the pack's `scaffolds/` dir is not the android scaffolder's own base dir,
`resolve_scaffolder` reports it as an overlay (`ScaffolderHandle.overlay_dirs`)
and the dispatcher renders it after the base skeleton — so one render
emits the full client project (skeleton + ONVIF + RTSP). A pack bound to
its own conventional scaffolder has no overlay, so this is strictly
opt-in to the reuse case and leaves every other pack's render unchanged.

### Client overlay templates (`scaffolds/`)

| Path                                                   | Role                                                       |
| ------------------------------------------------------ | ---------------------------------------------------------- |
| `…/onvif/WsDiscoveryClient.kt`                         | WS-Discovery multicast probe → enumerate cameras on the LAN |
| `…/onvif/OnvifMediaClient.kt`                          | Device/Media SOAP (GetProfiles → GetStreamUri), digest auth |
| `…/onvif/OnvifModels.kt`                               | Dependency-free domain value types                          |
| `…/rtsp/RtspPlaybackController.kt`                     | Media3/ExoPlayer RTSP player + lifecycle                    |
| `…/rtsp/RtspPlayerScreen.kt`                           | Compose player screen (effect-scoped connect/teardown)      |
| `…/test/…/onvif/OnvifParsingTest.kt`                   | JVM unit tests for the pure parsers + WS-Security digest    |

Overlay Kotlin keeps the skeleton's `com.omnisight.pilot` package root.
The base manifest already grants `INTERNET` + `ACCESS_NETWORK_STATE`;
WS-Discovery's multicast lock additionally needs `CHANGE_WIFI_MULTICAST_STATE`
and RTSP playback needs the Media3 dependencies (both documented in the
template KDoc).

```bash
# discover the pack
scripts/scaffold.py --list            # lists android-rtsp-onvif-client

# render the client-app skeleton
scripts/scaffold.py android-rtsp-onvif-client \
    --out-dir ./CameraClient --project-name CameraClient
```

## Scope

| In the pack now (OP-1799 skeleton + OP-1814 content)              |
| ----------------------------------------------------------------- |
| Pack dir + `skill.yaml` + `tasks.yaml` (all three tasks active)   |
| Dispatcher routing to `android_scaffolder` + overlay rendering    |
| ONVIF WS-Discovery probe + Device/Media SOAP client               |
| RTSP playback (Media3/ExoPlayer) + Compose player screen          |
| Dispatch/render + manifest-validation + ONVIF parser tests        |

Safe-local internal dev (authored under omnisight-self, not a customer
run). Needs no platform/sandbox.
