# ios-map-ar — Case-3 iOS map-AR app pack (OP-1816 + OP-1820)

**Pack status**: B-1 content. Reuses the `ios` scaffolder (ships no
scaffolder of its own) and layers its **ARKit + MapKit** templates on top
via the dispatcher (OP-1820).

## What this is

An iOS app that combines **ARKit** (augmented reality) with **MapKit**
(maps) — for example an AR points-of-interest / wayfinding experience
that anchors AR content to locations shown on a map.

## How rendering works (no new scaffolder)

`scripts/scaffold.py` resolves this pack to the existing
`backend.ios_scaffolder` through the explicit override in
`backend/skill_registry.py` (`_SCAFFOLDER_MODULE_OVERRIDES`):

```
ios-map-ar → backend.ios_scaffolder
```

So a render produces the standard SwiftUI + Swift Package Manager iOS app
skeleton (from `configs/skills/skill-ios/scaffolds/`), **then layers this
pack's own `scaffolds/` on top as an overlay**. Because the pack's
`scaffolds/` dir is not the iOS scaffolder's own base dir,
`resolve_scaffolder` reports it as an overlay (`ScaffolderHandle.overlay_dirs`)
and the dispatcher renders it after the base skeleton — so one render
emits the full map-AR project (skeleton + ARKit + MapKit).

### Map-AR overlay templates (`scaffolds/`)

| Path | Role |
| ---- | ---- |
| `App/Resources/Info.plist.j2` | Adds ARKit camera + MapKit location usage strings |
| `App/Sources/ContentView.swift.j2` | Routes the app root to the integrated map-AR surface |
| `App/Sources/MapAR/MapLocationStore.swift` | CoreLocation authorization/location feed + POI selection state |
| `App/Sources/MapAR/MapKitMapView.swift` | SwiftUI MapKit map, user location, selectable markers |
| `App/Sources/MapAR/ARKitOverlayView.swift` | RealityKit `ARView` with world tracking and POI marker anchors |
| `App/Sources/MapAR/MapARHomeView.swift` | Integration view: map selection drives the AR overlay |
| `Tests/MapLocationStoreTests.swift.j2` | XCTest coverage for selection state |

```bash
# discover the pack
scripts/scaffold.py --list            # lists ios-map-ar

# render the full map-AR project
scripts/scaffold.py ios-map-ar \
    --out-dir ./MapAR --project-name MapAR
```

## Scope

| In the pack now (OP-1816 skeleton + OP-1820 content) |
| ---------------------------------------------------- |
| Pack dir + `skill.yaml` + `tasks.yaml` (all three tasks active) |
| Dispatcher routing to `ios_scaffolder` + overlay rendering |
| MapKit `Map` view + CoreLocation authorization/location feed |
| ARKit/RealityKit `ARView` world tracking + POI anchors |
| AR↔map integration templates + usage strings |
| Dispatch/render + manifest-validation tests |

Safe-local internal dev (authored under omnisight-self, not a customer
run). Needs no platform/sandbox.

> **Note:** an actual iOS **build** needs a macOS host — that is J2 infra,
> out of scope for this skeleton ticket.
