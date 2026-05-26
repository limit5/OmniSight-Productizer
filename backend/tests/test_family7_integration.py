"""OP-1766 (G.A-v2 Family ⑦ · v2-⑦-Integration, family7 §10) — E2E allowlist
probe-matrix.

End-to-end verification that EVERY path-gating consumer treats EVERY public
probe-path variant as public — i.e. lets it through with a 200 and never
emits a 401 / 429 / 503 — when the consumer is armed into its deny state and
``OMNISIGHT_AUTH_BASELINE_MODE=enforce``.

This is the Family ⑦ *integration* capstone: the unit + reproduction work
(``test_middleware_allowlist.py``, ``test_health_allowlist_drift.py``) proved
the single-source allowlist (``backend.middleware_allowlist.is_public``, landed
by OP-1752) is correct in isolation and that the 2026-05-14 ``/api/v2/health
→ 401`` drift is fixed. This module wires the SHIPPED middleware functions
together and probes the cross-product end-to-end:

    consumers (all 5, all routed through is_public() by OP-1752)
      ① _graceful_shutdown_gate   (main.py)        — 503 on deny
      ② _bootstrap_gate           (main.py)        — 503/307 on deny
      ③ _rate_limit_gate          (main.py)        — 429 on deny
      ④ _must_change_password_gate(main.py)        — 428 on deny
      ⑤ auth_baseline             (auth_baseline.py)— 401 on deny
    ×
    path variants {/health, /livez, /readyz, /healthz, /version}
      × prefix {bare, /api/v1, /api/v2}

Each consumer is exercised on an ISOLATED app whose only path-gating layer is
that one consumer's REAL shipped dispatch function (grabbed unmodified from
``backend.main`` / installed via ``auth_baseline.install``). Because nothing
else can answer the request, any 401/429/503 on a public probe is
unambiguously attributable to that consumer's allowlist decision — the same
attribution discipline ``test_health_allowlist_drift._make_enforce_app`` uses.
Every consumer is first ARMED into its deny state (a control probe of a
*non*-public path must be rejected, proving the gate is live) and then the
public-probe matrix must come back all-200.

A combined app stacking all five real gates — every one armed simultaneously —
then re-runs the matrix end-to-end, and the OP-1760 ``/api/v2/health``
reproduction is asserted as a HARD 200 (not merely ``!= 401``).

MUST-NOT (ticket): verification-only. This module never edits the allowlist,
``is_public()``, or any middleware — it imports and exercises them unmodified.

Spec: ``docs/sprint-s12/2026-05-16-v2-family7-allowlist-contract.md`` §4/§5/§10.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware

from backend import auth_baseline
from backend import main as backend_main
from backend.middleware_allowlist import is_public

# ───────────────────────────────────────────────────────────────────────
#  The probe matrix axes (ticket §10 / family7 §10)
# ───────────────────────────────────────────────────────────────────────
# Public probe basenames. /version is the Family ⑤ image-identity probe
# (OP-1745); the rest are the liveness/readiness family (OP-1131).
_PROBE_BASENAMES = ("/health", "/livez", "/readyz", "/healthz", "/version")
# Each basename is probed bare AND under both versioned API prefixes — the
# drift the single-source allowlist dissolved lived in the /api/v2 column.
_PREFIX_VARIANTS = ("", "/api/v1", "/api/v2")


def _probe_paths() -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for base in _PROBE_BASENAMES:
        for prefix in _PREFIX_VARIANTS:
            seen[prefix + base] = None
    return tuple(seen)


PUBLIC_PROBE_PATHS: tuple[str, ...] = _probe_paths()

# Control paths that MUST be gated (non-public): a deny on these proves the
# armed consumer's gate is actually live, so an all-200 public matrix is
# meaningful rather than a vacuously-disabled middleware.
PRIVATE_CONTROL_PATHS: tuple[str, ...] = (
    "/private",
    "/api/v1/private",
    "/api/v2/private",
)

# The deny codes the ticket forbids on any public path.
FORBIDDEN_PUBLIC_CODES = frozenset({401, 429, 503})

# Module attribute names of the four main.py path-gating dispatch functions.
_MAIN_GATE_ATTR = {
    "graceful_shutdown": "_graceful_shutdown_gate",
    "bootstrap": "_bootstrap_gate",
    "rate_limit": "_rate_limit_gate",
    "password_change": "_must_change_password_gate",
}

# All five consumers in registration-ish order (auth_baseline is its own
# installable middleware, the rest live on main.py's app).
CONSUMERS = (
    "graceful_shutdown",
    "bootstrap",
    "rate_limit",
    "password_change",
    "auth_baseline",
)

# The control deny code each consumer emits for a gated, denied request.
CONSUMER_DENY_CODE = {
    "graceful_shutdown": 503,
    "bootstrap": 503,  # /api/* path → JSON 503 (not the 307 browser redirect)
    "rate_limit": 429,
    "password_change": 428,
    "auth_baseline": 401,
}

_SESSION_COOKIE_VALUE = "probe-matrix-session-token"


# ───────────────────────────────────────────────────────────────────────
#  App + arming helpers — exercise the REAL shipped middleware unmodified
# ───────────────────────────────────────────────────────────────────────
async def _ok_handler():
    return {"status": "ok"}


def _build_probe_app(consumers: list[str]) -> FastAPI:
    """A bare app mounting every probe + control path, with ONLY the given
    path-gating consumers installed (real shipped dispatch functions)."""
    app = FastAPI()
    for path in PUBLIC_PROBE_PATHS + PRIVATE_CONTROL_PATHS:
        app.add_api_route(path, _ok_handler, methods=["GET"])

    for consumer in consumers:
        if consumer == "auth_baseline":
            # The shipped installer — adds the real auth_baseline middleware.
            auth_baseline.install(app)
        else:
            dispatch = getattr(backend_main, _MAIN_GATE_ATTR[consumer])
            app.add_middleware(BaseHTTPMiddleware, dispatch=dispatch)
    return app


class _DenyLimiter:
    """RateLimiter stub whose ``allow`` always denies — drives the real
    ``_rate_limit_gate`` to its 429 branch for non-public paths."""

    def allow(self, key: str, capacity: int, window_seconds: float):
        return (False, 5.0)


def _arm_consumer(consumer: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``consumer`` into its reject state so a non-public probe is
    denied. All arming is hermetic (no DB / no network)."""
    # The family contract runs the whole matrix under enforce mode.
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")

    if consumer == "graceful_shutdown":
        from backend import lifecycle

        monkeypatch.setattr(lifecycle.coordinator, "shutting_down", True)

    elif consumer == "bootstrap":
        from backend import bootstrap

        async def _not_finalized() -> bool:
            return False

        monkeypatch.setattr(bootstrap, "is_bootstrap_finalized", _not_finalized)

    elif consumer == "rate_limit":
        from backend import rate_limit

        # Open auth mode keeps the gate off the DB session path; the deny
        # limiter trips the per-IP 429 branch for non-public paths.
        monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
        monkeypatch.setattr(rate_limit, "get_limiter", lambda: _DenyLimiter())

    elif consumer == "password_change":
        from backend import auth

        monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")

        async def _get_session(cookie: str):
            return auth.Session(
                token=cookie,
                user_id="u-probe",
                csrf_token="csrf",
                created_at=0.0,
                expires_at=9_999_999_999.0,
            )

        async def _get_user(user_id: str, conn=None):
            return auth.User(
                id="u-probe",
                email="probe@example.test",
                name="Probe",
                role="admin",
                must_change_password=True,
            )

        monkeypatch.setattr(auth, "get_session", _get_session)
        monkeypatch.setattr(auth, "get_user", _get_user)

    elif consumer == "auth_baseline":

        async def _no_session(_request) -> bool:
            return False

        monkeypatch.setattr(auth_baseline, "_has_valid_session", _no_session)

    else:  # pragma: no cover - guards a typo in CONSUMERS
        raise AssertionError(f"unknown consumer {consumer!r}")


def _client_cookies(consumer: str) -> dict[str, str]:
    """The password-change gate only reaches its 428 branch when a session
    cookie is present; everyone else needs none."""
    if consumer == "password_change":
        from backend import auth

        return {auth.SESSION_COOKIE: _SESSION_COOKIE_VALUE}
    return {}


async def _get(app: FastAPI, path: str, cookies: dict[str, str] | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=cookies or {}
    ) as client:
        return await client.get(path)


# ───────────────────────────────────────────────────────────────────────
#  Preconditions — the matrix axes are what we think they are
# ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", PUBLIC_PROBE_PATHS)
def test_every_probe_path_is_public(path: str) -> None:
    """Guard: every probe-matrix path resolves public through the single
    source of truth (so an all-200 expectation is the correct one). The
    /api/v2 column is the regression surface OP-1752 fixed."""
    assert is_public(path), (
        f"{path} is not is_public() — the probe matrix axis is wrong or the "
        "allowlist regressed."
    )


@pytest.mark.parametrize("path", PRIVATE_CONTROL_PATHS)
def test_control_paths_are_not_public(path: str) -> None:
    """Guard: the control paths are genuinely gated, so a deny on them
    proves an armed consumer's gate is live (not vacuously disabled)."""
    assert not is_public(path), f"{path} unexpectedly is_public() — bad control axis."


def test_matrix_covers_all_five_consumers_and_fifteen_paths() -> None:
    """The matrix exercises exactly the family7 §10 cross-product:
    5 consumers × (5 basenames × 3 prefixes)."""
    assert set(CONSUMERS) == set(_MAIN_GATE_ATTR) | {"auth_baseline"}
    assert len(CONSUMERS) == 5
    assert len(PUBLIC_PROBE_PATHS) == len(_PROBE_BASENAMES) * len(_PREFIX_VARIANTS) == 15


# ───────────────────────────────────────────────────────────────────────
#  Control — each armed consumer actually rejects a non-public path
# ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_armed_consumer_rejects_private_path(
    consumer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity floor: with the consumer armed, a gated /api/v1/private probe is
    rejected with the consumer's deny code — proving the gate is live before
    we assert it lets the public probes through."""
    _arm_consumer(consumer, monkeypatch)
    app = _build_probe_app([consumer])
    resp = await _get(app, "/api/v1/private", cookies=_client_cookies(consumer))
    assert resp.status_code == CONSUMER_DENY_CODE[consumer], (
        f"armed {consumer} did not reject /api/v1/private as expected "
        f"(got {resp.status_code}, want {CONSUMER_DENY_CODE[consumer]}; "
        f"detail={resp.text!r}) — the gate is not actually live, so the "
        "public-path matrix would be a vacuous pass."
    )


# ───────────────────────────────────────────────────────────────────────
#  The probe matrix — every public path bypasses every armed consumer (200)
# ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", PUBLIC_PROBE_PATHS)
@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_public_path_bypasses_armed_consumer(
    consumer: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core AC: under enforce mode, with ``consumer`` armed to deny, the
    public probe ``path`` is let through with 200 and never a 401/429/503."""
    _arm_consumer(consumer, monkeypatch)
    app = _build_probe_app([consumer])
    resp = await _get(app, path, cookies=_client_cookies(consumer))

    assert resp.status_code not in FORBIDDEN_PUBLIC_CODES, (
        f"public path {path} was {resp.status_code}'d by armed consumer "
        f"{consumer} under enforce mode — the Family ⑦ allowlist regressed "
        f"(detail={resp.text!r})."
    )
    assert resp.status_code == 200, (
        f"public path {path} did not return 200 through armed {consumer} "
        f"(got {resp.status_code}; detail={resp.text!r})."
    )


# ───────────────────────────────────────────────────────────────────────
#  End-to-end — all five gates stacked AND armed at once
# ───────────────────────────────────────────────────────────────────────
def _arm_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for consumer in CONSUMERS:
        _arm_consumer(consumer, monkeypatch)


@pytest.mark.parametrize("path", PUBLIC_PROBE_PATHS)
async def test_all_five_consumers_stacked_pass_public_path(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full E2E: an app stacking all five REAL gates, every one armed to deny
    simultaneously, still lets every public probe through with 200 and no
    401/429/503 — the request traverses all five allowlist decisions."""
    _arm_all(monkeypatch)
    app = _build_probe_app(list(CONSUMERS))
    resp = await _get(app, path)

    assert resp.status_code not in FORBIDDEN_PUBLIC_CODES, (
        f"public path {path} was {resp.status_code}'d while traversing all "
        f"five armed gates (detail={resp.text!r})."
    )
    assert resp.status_code == 200, (
        f"public path {path} did not survive the full five-gate stack "
        f"(got {resp.status_code}; detail={resp.text!r})."
    )


async def test_all_five_stacked_still_reject_private_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stacked-and-armed app is not vacuously open: a non-public path is
    still rejected by one of the five gates (proving the matrix pass above is
    a real exemption, not a disabled stack)."""
    _arm_all(monkeypatch)
    app = _build_probe_app(list(CONSUMERS))
    resp = await _get(
        app, "/api/v1/private", cookies={"omnisight_session": _SESSION_COOKIE_VALUE}
    )
    assert resp.status_code in FORBIDDEN_PUBLIC_CODES | {428, 307}, (
        f"/api/v1/private survived the full armed gate stack (got "
        f"{resp.status_code}) — the gates are not live."
    )


# ───────────────────────────────────────────────────────────────────────
#  OP-1760 reproduction — now a HARD pass
# ───────────────────────────────────────────────────────────────────────
async def test_op1760_v2_health_reproduction_is_hard_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact 2026-05-14 drift surface — ``/api/v2/health`` under
    ``auth_baseline`` enforce mode with no session — is a HARD 200 (not merely
    ``!= 401``). De-xfailed in OP-1760 once OP-1752's single-source
    ``is_public()`` normalised the /api/v2 prefix; this is the family7 §10
    end-to-end restatement of that hard pass on the integration app."""
    _arm_consumer("auth_baseline", monkeypatch)
    app = _build_probe_app(["auth_baseline"])
    resp = await _get(app, "/api/v2/health")

    assert resp.status_code != 401, (
        "/api/v2/health was 401'd under enforce mode — the Family ⑦ allowlist "
        f"drift reappeared (got {resp.status_code}; detail={resp.text!r})."
    )
    assert resp.status_code == 200, (
        f"/api/v2/health reproduction is not a hard pass (got {resp.status_code})."
    )
