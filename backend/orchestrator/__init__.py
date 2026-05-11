"""Backend orchestrators for staged deploy workflows.

The first inhabitant is :mod:`backend.orchestrator.prod_deploy` (OP-881
D9 / Sprint D Phase 3): wraps the four-step prod deploy sequence
(image pull -> secrets decrypt -> staging-mirror smoke -> blue-green
switch) behind a single ``POST /api/v1/prod/deploy`` endpoint with an
operator approval gate.
"""
