"""OP-1147 -- /health enforce-mode 401 reproduction and auth-gate audit.

Discovery output for v2-7-2bc
=============================

Current branch note
-------------------
The 2026-05-14 incident was ``GET /health`` returning 401 when
``OMNISIGHT_AUTH_BASELINE_MODE=enforce``. Today's branch already carries the
OP-1131-era mitigation recorded in
``docs/sprint-s12/2026-05-16-v2-family7-allowlist-contract.md``: ``/health``
is now present in ``AUTH_BASELINE_ALLOWLIST``. This test therefore pins the
historical drift condition by temporarily restoring the old auth-baseline
consumer state where the four main.py allowlists exempted ``/health`` and the
fifth consumer did not.

Auth decision points and mount order for ``GET /health``
--------------------------------------------------------
Starlette executes user middleware in reverse registration order. For the
``/health`` request, the effective path-gating order is:

1. ``_bootstrap_gate`` in ``backend/main.py``: ``_BOOTSTRAP_EXEMPT_REL`` and
   ``_BOOTSTRAP_EXEMPT_RAW`` both include health probe paths. A red bootstrap
   state must not redirect ``/health``.
2. ``_graceful_shutdown_gate`` in ``backend/main.py``:
   ``_GRACEFUL_SHUTDOWN_EXEMPT_RAW`` includes ``/health`` so liveness probes
   continue through drain.
3. ``auth_baseline`` in ``backend/auth_baseline.py``:
   ``AUTH_BASELINE_ALLOWLIST`` is the historical drift point. When ``/health``
   is missing and mode is ``enforce``, this middleware short-circuits with 401
   before routing.
4. ``_rate_limit_gate`` in ``backend/main.py``: ``_RATE_LIMIT_EXEMPT`` includes
   ``/health`` via API-relative matching, so it does not rate-limit probes.
5. K1 password-change gate in ``backend/main.py``:
   ``_PASSWORD_CHANGE_EXEMPT`` includes ``/health`` so a forced-password-change
   user state does not block probes.

Other audited surfaces
----------------------
* ``backend/auth.py`` has no independent request-path allowlist for ``/health``;
  it provides session and dependency helpers consumed by other gates.
* Reverse proxy health paths currently prefer ``/livez`` in
  ``deploy/reverse-proxy/Caddyfile`` active checks. Legacy compose and external
  smoke paths still reference ``/api/v1/health`` in several deployment files,
  so v2-7-FixHealth must decide whether root ``/health`` remains a public alias.
* ``/api/v1/health`` is the mounted legacy health router endpoint. Root
  ``/health`` is an allowlist/proxy compatibility concern and may be resolved
  by v2-7-FixHealth.
* No FastAPI WebSocket route was found under ``backend/main.py`` or
  ``backend/routers``. Current WebSocket references are HMR/proxy pass-through
  support, not backend path-gating middleware.
* Docs/OpenAPI exceptions exist in both ``auth_baseline.py`` and
  ``backend/main.py`` for ``/docs``, ``/redoc``, and ``/openapi.json`` plus
  API-prefixed variants in the auth-baseline allowlist.

Post-v2-⑦-2bc state (OP-1752)
-----------------------------
v2-⑦-2bc routed ``auth_baseline._path_allowed`` through the single-source
``backend.middleware_allowlist.is_public``. The historical drift could only
exist because the auth floor read its OWN ``AUTH_BASELINE_ALLOWLIST`` literal;
now it does not. This test therefore FLIPS: mutating the (now-defunct)
``AUTH_BASELINE_ALLOWLIST`` literal can no longer reintroduce the 401 — the
drift class is dissolved. ``GET /health`` is no longer 401'd by the baseline.
(It returns 404 here because no bare ``/health`` ROUTE is mounted in the test
app; whether root ``/health`` becomes a 200 public alias is the separate
v2-⑦-FixHealth decision, NOT this ticket. The invariant 2bc pins is the
absence of the auth-floor 401.)
"""

from __future__ import annotations

import pytest

from backend import auth_baseline


@pytest.mark.asyncio
async def test_health_drift_dissolved_baseline_literal_no_longer_gates(
    client,
    monkeypatch,
):
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")
    # Remove "/health" from the historical literal to reproduce the
    # PRE-2bc drift trigger. Post-2bc this literal is no longer consulted
    # by _path_allowed (it delegates to is_public), so the mutation must
    # have NO effect on gating — the single source still treats /health as
    # public.
    monkeypatch.setattr(
        auth_baseline,
        "AUTH_BASELINE_ALLOWLIST",
        tuple(
            prefix
            for prefix in auth_baseline.AUTH_BASELINE_ALLOWLIST
            if prefix != "/health"
        ),
    )

    response = await client.get("/health", follow_redirects=False)

    # FLIPPED from `assert 401` (pre-2bc) — the auth-baseline floor no
    # longer 401s /health regardless of the defunct literal. 404 = no bare
    # /health route mounted (v2-⑦-FixHealth territory); the point is it is
    # NOT the historical 401.
    assert response.status_code != 401
    assert response.status_code == 404
