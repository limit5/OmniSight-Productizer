---
id: L-OP-774
ticket: OP-774
title: URL versioning needs compatibility gates, not just prefixes
date: 2026-05-08
tags: [api, versioning, ci, compatibility]
---

# URL versioning needs compatibility gates, not just prefixes

**Situation**: Sprint D needed `/api/v1` and `/api/v2` URLs so a frontend
bundle cached during deploy can keep calling the API version it was built
against. A prefix alone would not prevent a new router from accidentally
dropping an old operation or changing its request/response contract.

**Fix**: Keep version routers separate, advertise supported versions through
`/api/version`, and emit `Deprecation` plus `Sunset` headers on the old
version. Add a CI test that builds per-version OpenAPI schemas and compares
operation inputs and responses after stripping the version prefix.

**Verification**: `backend/tests/test_api_versioning_op774.py` covers router
separation, `/api/version`, v1 deprecation headers, v2 non-deprecation, and
the v1-to-v2 schema compatibility tripwire.

**Generalisation**: URL versioning buys deploy safety only when paired with
an automated backwards-compatibility check. Otherwise the new prefix can hide
a breaking change until a stale client exercises it.
