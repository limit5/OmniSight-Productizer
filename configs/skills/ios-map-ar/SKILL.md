# ios-map-ar — Case-3 iOS map-AR app pack (OP-1816)

**Pack status**: B-1 skeleton. Reuses the `ios` scaffolder; ships no
scaffolder or templates of its own yet.

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
skeleton (from `configs/skills/skill-ios/scaffolds/`).

```bash
# discover the pack
scripts/scaffold.py --list            # lists ios-map-ar

# render the app skeleton
scripts/scaffold.py ios-map-ar \
    --out-dir ./MapAR --project-name MapAR
```

## Scope (B-1 skeleton — what is and is NOT here)

| In this pickup                                  | Follow-on (same pack)                       |
| ----------------------------------------------- | ------------------------------------------- |
| Pack dir + `skill.yaml` + `tasks.yaml`          | MapKit `Map` view + CoreLocation feed       |
| Dispatcher routing to `ios_scaffolder`          | ARKit/RealityKit `ARView` world tracking    |
| Renders the base iOS app skeleton               | AR↔map anchoring glue + Info.plist usage    |
| Dispatch/render + manifest-validation tests     | Per-layer scaffolds, tests, HIL recipes     |

Safe-local internal dev (authored under omnisight-self, not a customer
run). Needs no platform/sandbox.

> **Note:** an actual iOS **build** needs a macOS host — that is J2 infra,
> out of scope for this skeleton ticket.
