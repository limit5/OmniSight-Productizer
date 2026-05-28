# tests/ — ios-map-ar

Dispatch/render coverage for this pack lives in the backend test suite at
`backend/tests/test_skill_ios_map_ar.py` (discovery, manifest validation,
dispatcher routing to `backend.ios_scaffolder`, overlay resolution, and
an end-to-end render of the full iOS map-AR project).

The rendered project also includes `Tests/MapLocationStoreTests.swift`
from the overlay, covering map/AR point selection state.
