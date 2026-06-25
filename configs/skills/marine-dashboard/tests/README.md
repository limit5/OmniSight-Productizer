# Marine Dashboard Selftest

Selftest coverage is the registry/routing smoke for OP-2428: load
`marine-dashboard` through `backend.skill_registry`, parse a yacht / marine
helm dashboard intent, and verify `backend.planner_router` selects the pack.
