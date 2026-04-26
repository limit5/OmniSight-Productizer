"""BS.2.3 — RBAC layer for catalog + installer routers (single source of truth).

The PROBLEM this test exists to solve
─────────────────────────────────────
BS.2.1 wired ``require_operator`` / ``require_admin`` on the 9 catalog
endpoints; BS.2.2 did the same for the 6 installer endpoints. Each
router carries its own per-route ``Depends(...)`` reference smoke test
inside ``test_catalog_router_smoke.py`` / ``test_installer_router_smoke.py``,
but those tests are LOCAL to a single router file. There is no
cross-router source of truth that says "the BS.2.3 spec literally is:
read=operator authenticated / install=operator+PEP / write=admin /
source CRUD=admin / sidecar poll=admin". A future router added to the
same surface (eg. BS.4 sidecar token swap, BS.8.5 subscription feed
worker) could silently drift away from that spec without tripping
either local smoke test.

This module defines the BS.2.3 RBAC matrix as a frozen tuple, then
locks it three ways:

  1. **Completeness** — every route registered on the catalog +
     installer routers must appear in the matrix. A new endpoint added
     in either router without an RBAC entry trips ``test_matrix_covers_*``.
  2. **Dep alignment** — the dep callable wired on each route must
     match the matrix's ``min_role`` (``require_operator`` for
     ``operator`` rows, ``require_admin`` for ``admin`` rows).
  3. **Behavioural enforcement** — ASGI ``TestClient`` calls each
     endpoint with a fake ``current_user`` of every role and asserts
     the role gate produces 403 vs not-403 per the matrix.

Spec — BS.2.3 literal mapping
─────────────────────────────
* ``read``           → ``operator`` (any authenticated operator can
                       browse the catalog + jobs)
* ``install``        → ``operator`` + PEP HOLD (``POST /installer/jobs``
                       and ``POST /installer/jobs/{id}/retry``)
* ``write`` (entries)→ ``admin`` (POST/PATCH/DELETE on ``/catalog/entries``)
* ``source`` CRUD    → ``admin`` (every method on ``/catalog/sources``)
* ``cancel`` (jobs)  → ``operator`` (the destructive write is gated by
                       state-machine validation, not RBAC; same actor
                       who created the job can cancel it)
* ``poll`` (sidecar) → ``admin`` (interim until BS.4.1 lands sidecar
                       bearer-token auth — see BS.4.1-followup-sidecar-token)

Why behavioural assertions in addition to dep wiring
────────────────────────────────────────────────────
The smoke tests check ``_au.require_operator in deps`` — they prove
the right object is referenced. They do NOT prove the dep actually
rejects the wrong role at runtime, nor that a swapped-in dep with the
same callable identity (eg. a future refactor that wraps require_admin
inside a CSRF-stripping shim) still enforces the role check. The
behavioural tests below send actual HTTP-level requests through ASGI
with each role injected and read the status code — same shape that
prod sees.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure test code: the only mutable state is ``app.dependency_overrides``,
scoped per-test via the ``_override_current_user`` context manager
(try/finally pop). No module-level singletons; each worker runs the
same fixtures from the same source so there is nothing to coordinate.

Read-after-write timing audit
─────────────────────────────
Not applicable — the router calls past auth fail with 500 (asyncpg
pool not initialised in this test path, by design — we are not
testing CRUD here, BS.2.4 owns that). Auth-layer rejection happens
before any DB read, so timing is moot.

Production status
─────────────────
dev-only. The matrix encodes the spec already shipped in catalog.py +
installer.py and adds a contract gate; no production code changes.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Iterator, Literal

import pytest

from backend import auth as _au


# ════════════════════════════════════════════════════════════════════
#  Matrix — BS.2.3 RBAC spec, frozen
# ════════════════════════════════════════════════════════════════════


MinRole = Literal["operator", "admin"]


@dataclass(frozen=True)
class RbacRow:
    """One endpoint in the BS.2.3 RBAC matrix."""

    method: str          # HTTP verb (uppercase)
    path: str            # router-relative path (no /api/v1 prefix)
    min_role: MinRole    # the dep that must be wired
    pep_gated: bool      # True if the handler additionally goes
                         # through pep_gateway.evaluate(...) HOLD
    label: str           # short human-readable summary, used in
                         # parametrize ids


# Catalog router — 9 endpoints (per BS.2.1 ``test_route_registration_full_set``)
_CATALOG_ROWS: tuple[RbacRow, ...] = (
    RbacRow("GET",    "/catalog/entries",            "operator", False, "read entries list"),
    RbacRow("GET",    "/catalog/entries/{entry_id}", "operator", False, "read entry by id"),
    RbacRow("POST",   "/catalog/entries",            "admin",    False, "write entry create"),
    RbacRow("PATCH",  "/catalog/entries/{entry_id}", "admin",    False, "write entry patch"),
    RbacRow("DELETE", "/catalog/entries/{entry_id}", "admin",    False, "write entry delete"),
    RbacRow("GET",    "/catalog/sources",            "admin",    False, "source CRUD list"),
    RbacRow("POST",   "/catalog/sources",            "admin",    False, "source CRUD create"),
    RbacRow("PATCH",  "/catalog/sources/{sub_id}",   "admin",    False, "source CRUD patch"),
    RbacRow("DELETE", "/catalog/sources/{sub_id}",   "admin",    False, "source CRUD delete"),
)

# Installer router — 6 endpoints (per BS.2.2 ``test_route_registration_full_set``)
_INSTALLER_ROWS: tuple[RbacRow, ...] = (
    RbacRow("POST",   "/installer/jobs",                  "operator", True,  "install create (PEP HOLD)"),
    RbacRow("GET",    "/installer/jobs",                  "operator", False, "read jobs list"),
    RbacRow("GET",    "/installer/jobs/{job_id}",         "operator", False, "read job by id"),
    RbacRow("POST",   "/installer/jobs/{job_id}/cancel",  "operator", False, "install cancel"),
    RbacRow("POST",   "/installer/jobs/{job_id}/retry",   "operator", True,  "install retry (PEP HOLD)"),
    RbacRow("GET",    "/installer/jobs/poll",             "admin",    False, "sidecar long-poll (admin until BS.4.1)"),
)

BS23_RBAC_MATRIX: tuple[RbacRow, ...] = _CATALOG_ROWS + _INSTALLER_ROWS


def _path_with_params(path: str) -> str:
    """Replace ``{name}`` placeholders with concrete values that pass
    each router's pydantic path-param regex.

    Catalog uses kebab-case ``entry_id`` + ``sub-`` prefixed
    ``sub_id``; installer uses ``ij-`` + 12 hex for ``job_id``.
    """
    return (
        path
        .replace("{entry_id}",  "nodejs-lts-20")
        .replace("{sub_id}",    "sub-0123456789ab")
        .replace("{job_id}",    "ij-0123456789ab")
    )


# ════════════════════════════════════════════════════════════════════
#  Spec lock — BS.2.3 literal counts and groupings
# ════════════════════════════════════════════════════════════════════


def test_matrix_size_and_split_by_router():
    """The BS.2.3 surface is exactly 15 endpoints today: 9 catalog
    (BS.2.1) + 6 installer (BS.2.2). Drift here means a new endpoint
    landed without RBAC review."""
    assert len(_CATALOG_ROWS) == 9
    assert len(_INSTALLER_ROWS) == 6
    assert len(BS23_RBAC_MATRIX) == 15


def test_matrix_role_distribution_matches_bs23_spec():
    """Per the BS.2.3 row literal:

      read=operator authenticated  → 4 reads (2 catalog entries + 2
                                    installer jobs reads = 4)
      install=operator+PEP         → 2 (POST jobs + POST retry; both
                                    PEP-gated)
      write=admin                  → 3 (POST/PATCH/DELETE entries)
      source CRUD=admin            → 4 (GET/POST/PATCH/DELETE sources)
      cancel=operator              → 1 (POST cancel; non-PEP because
                                    state-machine guards the action)
      poll=admin                   → 1 (sidecar long-poll, until BS.4.1)
    """
    by_role: dict[str, int] = {"operator": 0, "admin": 0}
    pep_gated_count = 0
    for row in BS23_RBAC_MATRIX:
        by_role[row.min_role] += 1
        if row.pep_gated:
            pep_gated_count += 1

    # 4 catalog reads (entries × 2) + 4 installer jobs reads/writes
    # under operator (POST jobs / GET jobs / GET job_id / POST cancel
    # / POST retry = 5) → operator = 2 + 5 = 7
    assert by_role["operator"] == 7, (
        f"operator count drifted: {by_role['operator']} (expected 7 = 2 "
        f"catalog reads + 5 installer ops)"
    )
    # 3 catalog entry writes + 4 source CRUD + 1 sidecar poll = 8
    assert by_role["admin"] == 8, (
        f"admin count drifted: {by_role['admin']} (expected 8 = 3 entry "
        f"writes + 4 source CRUD + 1 sidecar poll)"
    )
    # PEP HOLD applies on POST /installer/jobs and POST /installer/jobs/
    # {id}/retry (both create-or-restart paths). Cancel is operator-only
    # but NOT PEP-gated — see BS.2.2 docstring.
    assert pep_gated_count == 2, (
        f"PEP-gated count drifted: {pep_gated_count} (expected 2)"
    )


def test_matrix_pep_gate_attached_only_to_install_create_and_retry():
    """Spec lock: only ``install=operator+PEP`` paths carry the gate."""
    pep_paths = sorted(
        (row.method, row.path) for row in BS23_RBAC_MATRIX if row.pep_gated
    )
    assert pep_paths == [
        ("POST", "/installer/jobs"),
        ("POST", "/installer/jobs/{job_id}/retry"),
    ]


def test_matrix_source_crud_is_uniformly_admin():
    """Sources are admin across every method (BS.2.3 'source CRUD=admin').

    Sources carry vendor credentials (``auth_secret_ref``) and feed-fan
    parameters that materially change what auto-installs land in the
    tenant — operator-write would let a phishing-grade vector through.
    """
    src_rows = [r for r in BS23_RBAC_MATRIX if r.path.startswith("/catalog/sources")]
    assert len(src_rows) == 4
    assert all(r.min_role == "admin" for r in src_rows), (
        "BS.2.3 spec violation: source CRUD must be admin-only across "
        f"the board; got {[(r.method, r.min_role) for r in src_rows]}"
    )


def test_matrix_entry_writes_are_admin_reads_are_operator():
    """Spec lock: ``/catalog/entries`` reads = operator, writes = admin."""
    for row in _CATALOG_ROWS:
        if row.path.startswith("/catalog/entries"):
            if row.method == "GET":
                assert row.min_role == "operator", (
                    f"{row.method} {row.path}: BS.2.3 says reads = operator"
                )
            else:
                assert row.min_role == "admin", (
                    f"{row.method} {row.path}: BS.2.3 says writes = admin"
                )


def test_matrix_install_create_and_retry_are_operator_plus_pep():
    """Spec lock: install = operator role + PEP HOLD gate.

    The PEP gate is what catches "operator can install ANY entry,
    including dangerous-looking ones" — admin doesn't get a free
    bypass either; the gate fires on both. RBAC is the door, PEP is
    the bouncer.
    """
    for row in _INSTALLER_ROWS:
        if row.method == "POST" and row.path in (
            "/installer/jobs", "/installer/jobs/{job_id}/retry",
        ):
            assert row.min_role == "operator", (
                f"{row.method} {row.path} must be operator-gated (BS.2.3 "
                "'install=operator+PEP') — admin-only would lock the "
                "common-case install action away from the role that "
                "actually does the work"
            )
            assert row.pep_gated, (
                f"{row.method} {row.path} must carry PEP HOLD"
            )


def test_matrix_sidecar_poll_admin_is_documented_stop_gap():
    """``GET /installer/jobs/poll`` is admin until BS.4.1 swaps in
    sidecar bearer-token auth (BS.4.1-followup-sidecar-token).

    Pinning here ensures a future refactor that drops auth on /poll
    (eg. "the sidecar handles its own auth") trips a red gate.
    """
    poll_rows = [r for r in BS23_RBAC_MATRIX if r.path == "/installer/jobs/poll"]
    assert len(poll_rows) == 1
    assert poll_rows[0].min_role == "admin"


# ════════════════════════════════════════════════════════════════════
#  Drift guards — matrix vs router routes
# ════════════════════════════════════════════════════════════════════


def _registered_routes(router) -> set[tuple[str, str]]:
    """Return ``{(method, path), ...}`` for every route on *router*.

    Excludes HEAD/OPTIONS — FastAPI auto-generates those alongside GET
    and they aren't part of the BS.2.3 surface.
    """
    out: set[tuple[str, str]] = set()
    for r in router.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            out.add((m, r.path))
    return out


def test_matrix_covers_every_catalog_route():
    """Every route registered on ``catalog.router`` must appear in the
    BS.2.3 matrix. A new endpoint added to catalog.py without an RBAC
    decision trips here."""
    from backend.routers import catalog

    registered = _registered_routes(catalog.router)
    matrix = {(r.method, r.path) for r in _CATALOG_ROWS}
    missing = registered - matrix
    extra = matrix - registered
    assert not missing, (
        f"Catalog routes missing from BS.2.3 matrix: {sorted(missing)}"
    )
    assert not extra, (
        f"Matrix references catalog routes that no longer exist: {sorted(extra)}"
    )


def test_matrix_covers_every_installer_route():
    """Every route registered on ``installer.router`` must appear in
    the BS.2.3 matrix."""
    from backend.routers import installer

    registered = _registered_routes(installer.router)
    matrix = {(r.method, r.path) for r in _INSTALLER_ROWS}
    missing = registered - matrix
    extra = matrix - registered
    assert not missing, (
        f"Installer routes missing from BS.2.3 matrix: {sorted(missing)}"
    )
    assert not extra, (
        f"Matrix references installer routes that no longer exist: "
        f"{sorted(extra)}"
    )


def _route_dependencies(router, method: str, path: str) -> list:
    """Return the list of dependency callables wired on a route."""
    for r in router.routes:
        if (getattr(r, "path", None) == path
                and method in (r.methods or set())):
            return [d.call for d in r.dependant.dependencies]
    return []


@pytest.mark.parametrize(
    "row",
    BS23_RBAC_MATRIX,
    ids=[f"{r.method} {r.path}".strip() for r in BS23_RBAC_MATRIX],
)
def test_matrix_dep_alignment_per_endpoint(row: RbacRow):
    """The dep callable wired on the route must match the matrix's
    ``min_role``. ``require_operator`` for operator rows,
    ``require_admin`` for admin rows."""
    if row.path.startswith("/catalog/"):
        from backend.routers import catalog as _r
    elif row.path.startswith("/installer/"):
        from backend.routers import installer as _r
    else:
        pytest.fail(f"unknown router prefix for matrix row {row}")

    deps = _route_dependencies(_r.router, row.method, row.path)
    expected = (
        _au.require_operator if row.min_role == "operator"
        else _au.require_admin
    )
    assert expected in deps, (
        f"{row.method} {row.path}: BS.2.3 matrix says min_role="
        f"{row.min_role!r} (dep={expected.__name__}); wired deps were "
        f"{[getattr(d, '__name__', repr(d)) for d in deps]}"
    )


# ════════════════════════════════════════════════════════════════════
#  Behavioural enforcement — ASGI TestClient + dep override
# ════════════════════════════════════════════════════════════════════
#
# Why this exists in addition to the dep-identity check above:
# ``require_operator in deps`` proves the right *object* is referenced.
# It does not prove the inner ``role_at_least`` actually returns False
# for a wrong-role caller at runtime, nor that the role hierarchy
# (``viewer < operator < admin < super_admin``) is honoured. The block
# below sends actual HTTP requests through the ASGI stack, with each
# role injected via ``app.dependency_overrides``, and reads status
# codes — same shape prod sees.
#
# Status semantics:
#   * 403 → role gate rejected (the BS.2.3 contract)
#   * any other status (200/404/422/500/...) → auth permitted; what
#     the handler does past auth is BS.2.4's concern, not ours
#
# We rely on the FastAPI ``TestClient`` rather than the async
# ``httpx.AsyncClient`` shared ``client`` fixture because (a) we don't
# need a per-test sqlite or PG pool — auth-layer rejection happens
# before any DB read; and (b) TestClient's sync API plays nicer with
# parametrize.


@pytest.fixture()
def app_for_rbac(monkeypatch):
    """Return the FastAPI app with bootstrap pinned to "finalized".

    We DON'T use the shared ``client`` fixture here because it forces
    a fresh sqlite + PG pool init that we don't need (auth gate fires
    before any DB call). But the bootstrap-gate middleware short-
    circuits *every* non-exempt request to 503 ``bootstrap_required``
    on a fresh install (no pool → admin-password probe fails closed),
    which would beat the auth layer to the punch and mask the real
    RBAC behaviour. Pin the gate to all-green so requests reach the
    auth dep, mirroring the shared ``client`` fixture's pattern.
    """
    from backend.main import app
    from backend import bootstrap as _boot

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    yield app
    # Cleanup — defensive double-pop in case a test forgot the
    # context manager (unlikely but cheap insurance).
    app.dependency_overrides.pop(_au.current_user, None)
    _boot._gate_cache_reset()


@contextlib.contextmanager
def _override_current_user(app, role: str | None) -> Iterator[None]:
    """Inject a fake ``current_user`` returning a User of *role*.

    Pass ``role=None`` to simulate "anonymous" by raising 401 from
    inside the override (mirrors what ``current_user`` does in
    session/strict mode without a cookie).
    """
    from fastapi import HTTPException

    if role is None:
        async def _fake():
            raise HTTPException(status_code=401, detail="Authentication required")
    else:
        fake_user = _au.User(
            id=f"u-rbac-{role}",
            email=f"{role}@rbac.test",
            name=f"BS23 {role}",
            role=role,
            enabled=True,
            tenant_id="t-rbac-bs23",
        )

        async def _fake():
            return fake_user

    app.dependency_overrides[_au.current_user] = _fake
    try:
        yield
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


def _client(app):
    """Return a fresh ``starlette.TestClient`` against *app*.

    Each call gives a clean client so cookies / state from one test
    don't leak to the next — TestClient is cheap to construct.
    """
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


def _full_path(row: RbacRow) -> str:
    """Return the API-prefixed path with placeholders substituted."""
    from backend.config import settings

    return settings.api_prefix + _path_with_params(row.path)


def _send(client, row: RbacRow):
    """Issue ``row.method`` against the appropriate path with a minimal
    body for write paths.

    The body shape doesn't have to be valid — auth runs first; if
    auth permits, we don't care whether the handler returns 422 / 500
    / etc. (BS.2.4 owns happy-path body validation).
    """
    full = _full_path(row)
    if row.method == "GET":
        return client.get(full)
    if row.method == "DELETE":
        return client.delete(full)
    # POST / PATCH — send an empty body. Routers that require a body
    # will 422 (still NOT 403) which is the signal we want.
    if row.method == "POST":
        return client.post(full, json={})
    if row.method == "PATCH":
        return client.patch(full, json={})
    pytest.fail(f"unhandled method {row.method!r} for {row.path!r}")


@pytest.mark.parametrize(
    "row",
    BS23_RBAC_MATRIX,
    ids=[f"{r.method} {r.path}".strip() for r in BS23_RBAC_MATRIX],
)
def test_viewer_is_denied_on_every_endpoint(app_for_rbac, row: RbacRow):
    """``viewer`` is below the BS.2.3 floor (operator) for every
    endpoint — every call must 403."""
    with _override_current_user(app_for_rbac, "viewer"):
        client = _client(app_for_rbac)
        res = _send(client, row)
    assert res.status_code == 403, (
        f"{row.method} {row.path}: viewer should hit 403 (BS.2.3: "
        f"min_role>=operator everywhere); got {res.status_code} "
        f"body={res.text[:200]!r}"
    )


@pytest.mark.parametrize(
    "row",
    [r for r in BS23_RBAC_MATRIX if r.min_role == "operator"],
    ids=lambda r: f"{r.method} {r.path}",
)
def test_operator_is_permitted_on_operator_endpoints(app_for_rbac, row: RbacRow):
    """For every endpoint marked ``min_role='operator'`` in the matrix,
    the operator role must NOT 403.

    The handler may return any other code (200 / 404 / 422 / 500
    depending on whether asyncpg is available) — only 401/403 would
    indicate the auth layer rejected the operator, which is the
    contract violation we are asserting against."""
    with _override_current_user(app_for_rbac, "operator"):
        client = _client(app_for_rbac)
        res = _send(client, row)
    assert res.status_code not in (401, 403), (
        f"{row.method} {row.path}: operator was rejected by the auth "
        f"layer (status={res.status_code}); BS.2.3 says operator must "
        f"be permitted on this endpoint. Body: {res.text[:200]!r}"
    )


@pytest.mark.parametrize(
    "row",
    [r for r in BS23_RBAC_MATRIX if r.min_role == "admin"],
    ids=lambda r: f"{r.method} {r.path}",
)
def test_operator_is_denied_on_admin_endpoints(app_for_rbac, row: RbacRow):
    """For every endpoint marked ``min_role='admin'``, the operator
    role must 403 — BS.2.3 says admin-only for entry writes, source
    CRUD, and sidecar poll."""
    with _override_current_user(app_for_rbac, "operator"):
        client = _client(app_for_rbac)
        res = _send(client, row)
    assert res.status_code == 403, (
        f"{row.method} {row.path}: operator should hit 403 (BS.2.3 "
        f"admin-only); got {res.status_code} body={res.text[:200]!r}"
    )


@pytest.mark.parametrize(
    "row",
    BS23_RBAC_MATRIX,
    ids=[f"{r.method} {r.path}".strip() for r in BS23_RBAC_MATRIX],
)
def test_admin_is_permitted_on_every_endpoint(app_for_rbac, row: RbacRow):
    """``admin`` is at the BS.2.3 ceiling — must be permitted across
    the board (operator endpoints by hierarchy ``admin > operator``,
    admin endpoints by direct match)."""
    with _override_current_user(app_for_rbac, "admin"):
        client = _client(app_for_rbac)
        res = _send(client, row)
    assert res.status_code not in (401, 403), (
        f"{row.method} {row.path}: admin was rejected by the auth "
        f"layer (status={res.status_code}); BS.2.3 hierarchy says "
        f"admin>=operator>=viewer must be permitted everywhere. "
        f"Body: {res.text[:200]!r}"
    )


@pytest.mark.parametrize(
    "row",
    BS23_RBAC_MATRIX,
    ids=[f"{r.method} {r.path}".strip() for r in BS23_RBAC_MATRIX],
)
def test_super_admin_is_permitted_on_every_endpoint(app_for_rbac, row: RbacRow):
    """``super_admin`` is the platform tier (Y2 #278) — strictly above
    admin. Must be permitted on every BS.2.3 endpoint."""
    with _override_current_user(app_for_rbac, "super_admin"):
        client = _client(app_for_rbac)
        res = _send(client, row)
    assert res.status_code not in (401, 403), (
        f"{row.method} {row.path}: super_admin was rejected by the "
        f"auth layer (status={res.status_code}); BS.2.3 hierarchy "
        f"says super_admin>=admin must be permitted. "
        f"Body: {res.text[:200]!r}"
    )


# ════════════════════════════════════════════════════════════════════
#  Self-fingerprint guard — pre-commit pattern
# ════════════════════════════════════════════════════════════════════


def test_self_fingerprint_clean():
    """SOP Step 3 pattern: this file MUST NOT contain any of the four
    compat-era SQL fingerprints. Catches accidental copy-paste from
    legacy router code."""
    import pathlib
    import re

    path = pathlib.Path(__file__)
    text = path.read_text(encoding="utf-8")
    # The regex itself is allowed to mention the fingerprints — strip
    # this very test out of the body before scanning.
    body = text.split("def test_self_fingerprint_clean")[0]
    forbidden = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    hits = forbidden.findall(body)
    assert not hits, f"Compat fingerprint(s) found in test body: {hits}"
