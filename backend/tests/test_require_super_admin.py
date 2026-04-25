"""Y2 (#278) row 6 — dedicated drift guard for ``require_super_admin``.

Why this file exists
====================

Rows 1-5 of Y2 each ship one tenant CRUD endpoint and each carries
its own "handler depends on require_super_admin" sentinel inside the
endpoint test file (``test_admin_tenants_{create,list,detail,patch,
delete}.py``). Those sentinels are necessary but they each live inside
a per-endpoint scope. This file consolidates the *contract itself* —
what ``require_super_admin`` promises to its callers — as a single
source of truth so future Y3 work (``POST /api/v1/admin/super-admins``)
and any new admin-tier endpoint can grep for ONE place to understand
"what does ``require_super_admin`` guarantee".

Contract surface this file pins down
====================================

1. ``auth.ROLES`` is the exact tuple ``("viewer", "operator", "admin",
   "super_admin")`` — order encodes rank; reordering would silently
   demote ``super_admin`` below ``admin``.
2. ``auth.role_at_least`` matrix is correct for the new top role.
3. ``auth.require_super_admin`` is a module-level constant produced by
   ``require_role("super_admin")`` — stable object identity across
   imports so ``app.dependency_overrides[require_super_admin]`` in the
   per-endpoint tests resolves to the same key.
4. ``auth._ANON_ADMIN.role == "super_admin"`` — open-mode dev fallback.
   Without this, every admin-tier endpoint would 403 in local dev and
   CI under the default ``OMNISIGHT_AUTH_MODE=open``.
5. End-to-end ASGI roundtrip: a mini FastAPI app mounting
   ``Depends(require_super_admin)`` returns 200 for a super_admin
   ``current_user`` override and 403 (with the standard detail message
   ``Requires role=super_admin or higher (you are <role>)``) for admin
   / operator / viewer overrides. This is the production deny path
   without needing PG.
6. Privilege-escalation guards on the tenant-admin ``/users`` endpoints
   (POST /users + PATCH /users/{id}) — these are NOT the
   ``require_super_admin`` gate itself, but they exist *because* of the
   new role: a tenant admin must not be able to mint a super-admin via
   the existing user-management API. Y3's ``POST /admin/super-admins``
   is the canonical path. Source-text grep is enough — the full HTTP
   path coverage already lives in ``test_admin_tenants_create.py``.

SOP module-global state audit
=============================

This file imports ``backend.auth`` whose module-level singletons
(``ROLES``, ``_RANK``, ``_ANON_ADMIN``, ``require_super_admin``) are
all immutable / re-derived per worker — answer #1 in the SOP audit.
No PG state, no in-memory cache, no Redis — the dependency is a pure
RBAC predicate.

No prod file edits — the dependency itself shipped in row 1's commit
(``feat(Y2/#278): row 1 — POST /api/v1/admin/tenants``); this row only
ships the contract drift guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (1) ROLES tuple — exact shape & rank order
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_roles_tuple_exact_shape_and_order():
    """``ROLES`` defines the rank order — the tuple position IS the
    rank. Reordering or inserting between would silently break every
    ``role_at_least`` comparison in the codebase."""
    from backend import auth
    assert auth.ROLES == ("viewer", "operator", "admin", "super_admin")


def test_rank_map_monotonically_increasing():
    """``_RANK`` is derived from ``ROLES`` enumerate; assert the
    derivation produced a strictly-increasing rank for the canonical
    chain ``viewer < operator < admin < super_admin``."""
    from backend import auth
    assert auth._RANK["viewer"] < auth._RANK["operator"]
    assert auth._RANK["operator"] < auth._RANK["admin"]
    assert auth._RANK["admin"] < auth._RANK["super_admin"]
    assert auth._RANK["super_admin"] == len(auth.ROLES) - 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (2) role_at_least matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("have", ["viewer", "operator", "admin",
                                  "super_admin"])
def test_super_admin_satisfies_every_lower_threshold(have):
    """A holder of role ``have`` should clear thresholds at-or-below
    ``have`` and fail thresholds strictly above. Verified row by row
    for the new ``super_admin`` axis."""
    from backend import auth
    rank = auth._RANK[have]
    for need in auth.ROLES:
        expected = auth._RANK[need] <= rank
        assert auth.role_at_least(have, need) is expected, (
            f"role_at_least({have!r}, {need!r}) "
            f"expected={expected!r}"
        )


def test_role_at_least_unknown_role_returns_false():
    """Defensive: an unknown ``have`` or ``need`` (typo, dropped enum)
    must hard-fail the predicate rather than silently grant access."""
    from backend import auth
    assert not auth.role_at_least("super_admin", "godmode")
    assert not auth.role_at_least("ghost", "super_admin")
    assert not auth.role_at_least("", "super_admin")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (3) require_super_admin object identity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_require_super_admin_is_module_level_constant():
    """The dependency must be a module-level constant so
    ``app.dependency_overrides[auth.require_super_admin] = ...`` in
    test code resolves to the SAME key the FastAPI route's
    ``Depends(auth.require_super_admin)`` registered. Re-imports must
    not produce a new closure."""
    from backend import auth as a1
    import backend.auth as a2
    assert a1.require_super_admin is a2.require_super_admin
    assert callable(a1.require_super_admin)


def test_require_super_admin_has_dep_signature_shape():
    """``require_role`` returns an async ``_dep(request, user=...)``
    closure. Walk the signature and confirm both params are present —
    this is the contract FastAPI relies on to inject Request + the
    upstream ``current_user``."""
    import inspect
    from backend import auth
    sig = inspect.signature(auth.require_super_admin)
    names = list(sig.parameters)
    # First positional is the FastAPI Request; second is the
    # current_user dependency injection.
    assert "request" in names, (
        f"require_super_admin missing Request param; got {names!r}"
    )
    assert "user" in names, (
        f"require_super_admin missing user param; got {names!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (4) Anonymous open-mode user is super_admin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_anon_admin_is_super_admin():
    """In ``OMNISIGHT_AUTH_MODE=open`` (the dev / pytest default) every
    request resolves to ``_ANON_ADMIN``. If this synthetic user is not
    ``super_admin`` then every Y2 admin endpoint would 403 in dev,
    breaking the pre-Y2 contract that 'open mode == do everything'."""
    from backend import auth
    assert auth._ANON_ADMIN.role == "super_admin"
    assert auth._ANON_ADMIN.enabled is True
    assert auth._ANON_ADMIN.tenant_id == "t-default"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (5) End-to-end ASGI: super_admin → 200, lower roles → 403
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_mini_app():
    """Tiny FastAPI app whose only route depends on
    ``require_super_admin``. Lets us exercise the production deny path
    end-to-end (CSRF + role check) without standing up the full
    backend.main app and its DB / bootstrap requirements."""
    from fastapi import Depends, FastAPI
    from backend import auth

    app = FastAPI()

    @app.get("/probe")
    async def _probe(user: auth.User = Depends(auth.require_super_admin)):
        return {"id": user.id, "role": user.role}

    return app


def _override_user(app, user):
    from backend import auth

    async def _fake():
        return user
    app.dependency_overrides[auth.current_user] = _fake


@pytest.fixture()
def _open_mode(monkeypatch):
    """Pin auth_mode to ``open`` so ``csrf_check`` short-circuits and
    the only gate left in ``require_role._dep`` is the rank check we
    want to assert."""
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    yield


async def test_super_admin_user_passes_dependency(_open_mode):
    """Happy path: a real super_admin user override clears the gate."""
    from httpx import ASGITransport, AsyncClient
    from backend import auth

    app = _build_mini_app()
    _override_user(app, auth.User(
        id="u-sa-probe", email="sa@local", name="Super",
        role="super_admin", enabled=True,
    ))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport,
                           base_url="http://test") as ac:
        res = await ac.get("/probe")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["role"] == "super_admin"
    assert body["id"] == "u-sa-probe"


@pytest.mark.parametrize("denied_role", ["admin", "operator", "viewer"])
async def test_lower_roles_get_403_with_standard_detail(_open_mode,
                                                       denied_role):
    """Deny path: any role strictly below super_admin must 403, and
    the detail string must follow the format every UI / runbook
    expects: ``Requires role=super_admin or higher (you are <role>)``.
    """
    from httpx import ASGITransport, AsyncClient
    from backend import auth

    app = _build_mini_app()
    _override_user(app, auth.User(
        id=f"u-{denied_role}-probe",
        email=f"{denied_role}@local", name=denied_role.title(),
        role=denied_role, enabled=True,
    ))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport,
                           base_url="http://test") as ac:
        res = await ac.get("/probe")
    assert res.status_code == 403, res.text
    detail = res.json().get("detail", "")
    assert "Requires role=super_admin" in detail, (
        f"unexpected detail format: {detail!r}"
    )
    assert f"you are {denied_role}" in detail, (
        f"detail must echo actor role; got {detail!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (6) Privilege-escalation guards on tenant-admin /users endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_ROUTERS_AUTH_PATH = (
    Path(__file__).resolve().parent.parent / "routers" / "auth.py"
)


def test_post_users_blocks_super_admin_role_in_source():
    """Source-text drift guard: POST /users must contain the literal
    ``if req.role == "super_admin"`` short-circuit. Without this guard
    a tenant admin (the audience of the ``require_admin`` dependency
    that gates POST /users) could promote themselves or anyone else to
    platform tier — bypassing Y3's bootstrap-only super-admin path."""
    src = _ROUTERS_AUTH_PATH.read_text(encoding="utf-8")
    # Locate the create_user handler block.
    needle_create = "async def create_user("
    assert needle_create in src, (
        f"create_user handler not found in {_ROUTERS_AUTH_PATH}"
    )
    create_idx = src.index(needle_create)
    # Find the next def to bound the handler block.
    next_def = src.index("\nasync def ", create_idx + 1)
    create_block = src[create_idx:next_def]
    assert 'if req.role == "super_admin"' in create_block, (
        "POST /users handler missing super_admin escalation guard"
    )
    assert "must be assigned via" in create_block, (
        "POST /users guard missing the Y3 redirect message"
    )


def test_patch_users_blocks_super_admin_role_in_source():
    """Same drift guard on the PATCH path — promote-by-edit must be
    blocked too."""
    src = _ROUTERS_AUTH_PATH.read_text(encoding="utf-8")
    needle_patch = "async def patch_user("
    assert needle_patch in src, (
        f"patch_user handler not found in {_ROUTERS_AUTH_PATH}"
    )
    patch_idx = src.index(needle_patch)
    # Bound by next async def or end-of-file.
    end_idx = src.find("\nasync def ", patch_idx + 1)
    if end_idx == -1:
        end_idx = len(src)
    patch_block = src[patch_idx:end_idx]
    assert 'if req.role == "super_admin"' in patch_block, (
        "PATCH /users handler missing super_admin escalation guard"
    )
    assert "must be assigned via" in patch_block, (
        "PATCH /users guard missing the Y3 redirect message"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (7) Cross-endpoint sanity: every Y2 admin tenant route depends on
#      require_super_admin (no endpoint silently downgrades to
#      require_admin)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_every_admin_tenant_handler_depends_on_super_admin():
    """Walk the handler functions defined by the Y2 admin_tenants
    router and assert every one of them lists
    ``auth.require_super_admin`` in its dependency surface. A future
    edit that copy-pastes ``Depends(auth.require_admin)`` onto a new
    admin endpoint would be caught here at module-load time."""
    import inspect
    from backend import auth
    from backend.routers import admin_tenants

    handler_names = [
        "create_tenant",
        "list_tenants",
        "get_tenant_detail",
        "patch_tenant",
        "delete_tenant",
    ]
    for name in handler_names:
        fn = getattr(admin_tenants, name, None)
        assert fn is not None, (
            f"expected handler {name!r} on admin_tenants router"
        )
        sig = inspect.signature(fn)
        deps = []
        for _pname, param in sig.parameters.items():
            target = getattr(param.default, "dependency", None)
            if target is not None:
                deps.append(target)
        assert auth.require_super_admin in deps, (
            f"{name} must depend on require_super_admin; "
            f"deps were {deps!r}"
        )
