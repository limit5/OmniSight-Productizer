# scaffolds/ — intentionally empty in the OP-1816 skeleton

This Case-3 pack ships **no scaffolder or templates of its own**. The
dispatcher (`scripts/scaffold.py`) routes `ios-map-ar` to the existing
`backend.ios_scaffolder` via the explicit override in
`backend/skill_registry.py` (`_SCAFFOLDER_MODULE_OVERRIDES`). Rendering
this pack therefore emits the standard SwiftUI + Swift Package Manager iOS
app skeleton from `configs/skills/skill-ios/scaffolds/`.

The ARKit (RealityKit `ARView`, world-tracking session) and MapKit
(SwiftUI `Map` view, CoreLocation feed) domain scaffolds — plus the glue
that anchors AR content to map points-of-interest — are sequenced
**follow-on** tickets in this same pack and will add their templates here.
This directory exists so the `scaffolds` artifact declared in `skill.yaml`
validates (`skill_manifest`).
