"""BS.2.2 — smoke tests for ``backend/routers/installer.py``.

Scope (per the BS phase split — full ~18 case integration suite is
BS.2.4's ``test_installer_api.py``):

* Router import + route registration shape (route ordering matters —
  ``/jobs/poll`` must precede ``/jobs/{job_id}`` so the literal path
  beats the path-parameter capture)
* Pydantic schema validation (positive + negative)
* Auth gate wiring — read = ``require_operator`` / sidecar poll =
  ``require_admin`` / write = ``require_operator``
* Module-level constants align with alembic 0051 install_jobs CHECK
  + the design ADR §4.3 sidecar protocol version contract

These tests do NOT require a live PG. They exercise the import surface,
the FastAPI route layer's deps registration, and the Pydantic body
validators. The full PG-backed CRUD / state-machine / PEP-integration /
long-poll-claim / tenant-isolation matrix lands in BS.2.4.
"""

from __future__ import annotations

import re

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module-level surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_prefix_and_tags():
    from backend.routers import installer
    assert installer.router.prefix == "/installer"
    assert "installer" in installer.router.tags


def test_route_registration_full_set():
    """Every endpoint in the BS.2.2 + BS.8.2 spec is registered once."""
    from backend.routers import installer
    pairs = sorted(
        (sorted(r.methods)[0], r.path)
        for r in installer.router.routes
        if hasattr(r, "methods") and r.methods
    )
    expected = sorted([
        ("POST", "/installer/jobs"),
        ("GET", "/installer/jobs"),
        ("GET", "/installer/jobs/{job_id}"),
        ("POST", "/installer/jobs/{job_id}/cancel"),
        ("POST", "/installer/jobs/{job_id}/retry"),
        ("POST", "/installer/jobs/{job_id}/progress"),
        ("GET", "/installer/jobs/poll"),
        # BS.8.2 — Cleanup-unused list + bulk uninstall.
        ("GET", "/installer/installed"),
        ("POST", "/installer/uninstall"),
    ])
    assert pairs == expected


def test_poll_route_precedes_param_route():
    """``GET /installer/jobs/poll`` must be registered BEFORE
    ``GET /installer/jobs/{job_id}`` — FastAPI matches in registration
    order, so the literal must win against the path-param capture or
    a sidecar's ``GET /installer/jobs/poll?sidecar_id=…`` would route
    into ``get_job(job_id="poll")`` and 422 on the id regex."""
    from backend.routers import installer
    paths_in_order = [
        r.path for r in installer.router.routes
        if hasattr(r, "methods") and "GET" in (r.methods or set())
    ]
    poll_idx = paths_in_order.index("/installer/jobs/poll")
    param_idx = paths_in_order.index("/installer/jobs/{job_id}")
    assert poll_idx < param_idx, (
        f"poll route at idx {poll_idx} must precede {{job_id}} at idx {param_idx}"
    )


def test_constants_mirror_alembic_0051_check_constraints():
    """Every closed enum in the router matches the alembic 0051 CHECK.

    Drift here means a 422 from the router that PG would have accepted
    (or worse: a body the router accepted that PG rejects with 500).
    """
    from backend.routers import installer
    assert installer.INSTALL_JOB_STATES == (
        "queued", "running", "completed", "failed", "cancelled",
    )
    assert installer.TERMINAL_STATES == ("completed", "failed", "cancelled")
    assert installer.ACTIVE_STATES == ("queued", "running")


def test_constants_default_protocol_version_is_supported():
    from backend.routers import installer
    assert installer.DEFAULT_SIDECAR_PROTOCOL_VERSION in (
        installer.SUPPORTED_SIDECAR_PROTOCOL_VERSIONS
    )
    # Today only v1 ships — when v2 lands the tuple grows but the
    # default should always live inside it.
    assert installer.SUPPORTED_SIDECAR_PROTOCOL_VERSIONS == (1,)


def test_constants_pep_tool_name():
    """The PEP tool string must stay stable — BS.7 coaching card lookup
    keys off this exact string in ``pep_gateway._TOOL_COACHING``."""
    from backend.routers import installer
    assert installer.INSTALL_PEP_TOOL == "install_entry"


def test_constants_install_job_id_pattern():
    """``ij-`` + 12 hex chars per alembic 0051 PK convention. Generator
    must produce ids that match the regex used by the GET / cancel /
    retry handlers."""
    from backend.routers import installer
    new_id = installer._new_install_job_id()
    assert installer._INSTALL_JOB_ID_RE.match(new_id), (
        f"generated id {new_id!r} doesn't match {installer.INSTALL_JOB_ID_PATTERN}"
    )
    assert new_id.startswith("ij-")
    assert len(new_id) == len("ij-") + 12


def test_constants_poll_timeout_bounds():
    from backend.routers import installer
    assert installer.POLL_TIMEOUT_S_DEFAULT <= installer.POLL_TIMEOUT_S_MAX
    assert installer.POLL_TIMEOUT_S_MAX <= 60  # uvicorn worker safety


def test_constants_pep_hold_timeout_capped():
    """PEP hold for install caps below the gateway default of 1800s.
    A 30-min HTTP block per install request is too long to hold a
    uvicorn conn — frontend should re-submit (idempotency_key dedupes)."""
    from backend.routers import installer
    assert installer.INSTALL_PEP_HOLD_TIMEOUT_S <= 600.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Install job id regex
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("good_id", [
    "ij-0123456789ab",
    "ij-abcdef012345",
    "ij-000000000000",
    "ij-ffffffffffff",
])
def test_install_job_id_regex_accepts_valid(good_id):
    from backend.routers.installer import _INSTALL_JOB_ID_RE
    assert _INSTALL_JOB_ID_RE.match(good_id), f"should accept {good_id!r}"


@pytest.mark.parametrize("bad_id", [
    "",
    "ij-",
    "ij-short",
    "ij-0123456789abc",       # 13 chars (too long)
    "ij-ABCDEF012345",         # uppercase hex disallowed
    "ij-0123456789ab ",        # trailing whitespace
    "u-0123456789ab",          # wrong prefix
    "0123456789ab",            # missing prefix
    "ij_0123456789ab",         # underscore not hyphen
    "ij-0123456789xy",         # non-hex chars
])
def test_install_job_id_regex_rejects_invalid(bad_id):
    from backend.routers.installer import _INSTALL_JOB_ID_RE
    assert not _INSTALL_JOB_ID_RE.match(bad_id), f"should reject {bad_id!r}"


def test_install_job_id_generator_collision_floor():
    """Sample 1000 generated ids; expect zero collisions and full regex
    compliance. 12 hex = 48 bits, so 1000 samples should never collide."""
    from backend.routers.installer import _new_install_job_id, _INSTALL_JOB_ID_RE
    ids = {_new_install_job_id() for _ in range(1000)}
    assert len(ids) == 1000, "id generator collided in 1000 samples"
    for jid in ids:
        assert _INSTALL_JOB_ID_RE.match(jid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas — InstallJobCreate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_install_job_create_minimal_body_passes():
    from backend.routers.installer import InstallJobCreate
    body = InstallJobCreate(
        entry_id="nxp-mcuxpresso-imxrt1170",
        idempotency_key="abcdef0123456789",  # 16 chars OK
    )
    assert body.entry_id == "nxp-mcuxpresso-imxrt1170"
    assert body.idempotency_key == "abcdef0123456789"
    assert body.bytes_total is None
    assert body.metadata == {}


def test_install_job_create_with_uuid_idempotency_key():
    """A typical frontend uuid.uuid4().hex (32 chars) must validate."""
    import uuid
    from backend.routers.installer import InstallJobCreate
    body = InstallJobCreate(
        entry_id="nodejs-lts-20",
        idempotency_key=uuid.uuid4().hex,
    )
    assert len(body.idempotency_key) == 32


def test_install_job_create_with_full_body():
    from backend.routers.installer import InstallJobCreate
    body = InstallJobCreate(
        entry_id="zephyr-rtos-3-7",
        idempotency_key="ABCDEF0123456789abcdef",
        bytes_total=1024 * 1024 * 200,  # 200 MiB
        metadata={"channel": "stable", "vendor_token_ref": "secret-store-key-7"},
    )
    assert body.bytes_total == 200 * 1024 * 1024
    assert body.metadata["channel"] == "stable"


def test_install_job_create_rejects_invalid_entry_id():
    import pydantic
    from backend.routers.installer import InstallJobCreate
    with pytest.raises(pydantic.ValidationError):
        InstallJobCreate(
            entry_id="UPPER-CASE",
            idempotency_key="abcdef0123456789",
        )


def test_install_job_create_rejects_oversized_entry_id():
    import pydantic
    from backend.routers.installer import InstallJobCreate, ENTRY_ID_MAX_LEN
    with pytest.raises(pydantic.ValidationError):
        InstallJobCreate(
            entry_id="a" + "-b" * ENTRY_ID_MAX_LEN,  # > max
            idempotency_key="abcdef0123456789",
        )


def test_install_job_create_rejects_short_idempotency_key():
    """Idempotency key min length 16 — anything shorter is too easy
    to collide on a busy tenant."""
    import pydantic
    from backend.routers.installer import InstallJobCreate
    with pytest.raises(pydantic.ValidationError):
        InstallJobCreate(
            entry_id="nodejs-lts-20",
            idempotency_key="too-short",  # 9 chars
        )


def test_install_job_create_rejects_oversized_idempotency_key():
    import pydantic
    from backend.routers.installer import InstallJobCreate
    with pytest.raises(pydantic.ValidationError):
        InstallJobCreate(
            entry_id="nodejs-lts-20",
            idempotency_key="x" * 65,  # > 64 cap
        )


def test_install_job_create_rejects_idempotency_key_with_whitespace():
    """Whitespace in the key is almost always a paste-token mistake."""
    import pydantic
    from backend.routers.installer import InstallJobCreate
    with pytest.raises(pydantic.ValidationError):
        InstallJobCreate(
            entry_id="nodejs-lts-20",
            idempotency_key="key with space xx",
        )


def test_install_job_create_rejects_negative_bytes_total():
    import pydantic
    from backend.routers.installer import InstallJobCreate
    with pytest.raises(pydantic.ValidationError):
        InstallJobCreate(
            entry_id="nodejs-lts-20",
            idempotency_key="abcdef0123456789",
            bytes_total=-1,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas — InstallJobCancelBody
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_install_job_cancel_body_empty_ok():
    """Cancel may be a bare POST — empty body is fine."""
    from backend.routers.installer import InstallJobCancelBody
    body = InstallJobCancelBody()
    assert body.reason is None


def test_install_job_cancel_body_with_reason():
    from backend.routers.installer import InstallJobCancelBody
    body = InstallJobCancelBody(reason="vendor URL turned out to be wrong")
    assert "vendor URL" in body.reason


def test_install_job_cancel_body_rejects_oversized_reason():
    import pydantic
    from backend.routers.installer import InstallJobCancelBody, CANCEL_REASON_MAX_LEN
    with pytest.raises(pydantic.ValidationError):
        InstallJobCancelBody(reason="x" * (CANCEL_REASON_MAX_LEN + 1))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas — InstallJobRetryBody
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_install_job_retry_body_requires_idempotency_key():
    """Retry must dedupe on its own — caller supplies a fresh key."""
    import pydantic
    from backend.routers.installer import InstallJobRetryBody
    with pytest.raises(pydantic.ValidationError):
        InstallJobRetryBody()


def test_install_job_retry_body_validates_idempotency_key_pattern():
    import pydantic
    from backend.routers.installer import InstallJobRetryBody
    InstallJobRetryBody(idempotency_key="abcdef0123456789")  # OK
    with pytest.raises(pydantic.ValidationError):
        InstallJobRetryBody(idempotency_key="bad key")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas — BulkUninstallBody (BS.8.2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bulk_uninstall_body_accepts_minimal_list():
    from backend.routers.installer import BulkUninstallBody
    body = BulkUninstallBody(entry_ids=["foo-bar"])
    assert body.entry_ids == ["foo-bar"]
    assert body.reason is None


def test_bulk_uninstall_body_rejects_empty_list():
    import pydantic
    from backend.routers.installer import BulkUninstallBody
    with pytest.raises(pydantic.ValidationError):
        BulkUninstallBody(entry_ids=[])


def test_bulk_uninstall_body_rejects_oversized_list():
    import pydantic
    from backend.routers.installer import (
        BULK_UNINSTALL_MAX_ENTRIES, BulkUninstallBody,
    )
    with pytest.raises(pydantic.ValidationError):
        BulkUninstallBody(
            entry_ids=[f"id-{i}" for i in range(BULK_UNINSTALL_MAX_ENTRIES + 1)],
        )


def test_bulk_uninstall_body_rejects_oversized_reason():
    import pydantic
    from backend.routers.installer import (
        BulkUninstallBody, CANCEL_REASON_MAX_LEN,
    )
    with pytest.raises(pydantic.ValidationError):
        BulkUninstallBody(
            entry_ids=["foo"],
            reason="x" * (CANCEL_REASON_MAX_LEN + 1),
        )


def test_uninstall_pep_tool_constant_is_stable():
    """The PEP tool string must stay stable — pep_gateway's `tier_unlisted`
    branch + a future `uninstall_intercept` coaching card both key off
    this exact string."""
    from backend.routers import installer
    assert installer.UNINSTALL_PEP_TOOL == "uninstall_entry"
    assert installer.INSTALL_KIND_UNINSTALL == "uninstall"


def test_is_uninstall_record_recognises_kind_field():
    from backend.routers.installer import _is_uninstall_record

    # dict with kind=uninstall → True
    assert _is_uninstall_record({"kind": "uninstall"}) is True
    # dict but kind is something else → False
    assert _is_uninstall_record({"kind": "install"}) is False
    # dict but no kind key → False
    assert _is_uninstall_record({"entry_id": "foo"}) is False
    # None / empty / wrong type → False
    assert _is_uninstall_record(None) is False
    assert _is_uninstall_record("") is False
    assert _is_uninstall_record([]) is False
    # JSON string also recognised (defence-in-depth for asyncpg without codec)
    assert _is_uninstall_record('{"kind": "uninstall"}') is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auth dependency wiring (mirrors test_catalog_router_smoke pattern)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _route_dependencies(router, method: str, path: str) -> list:
    for r in router.routes:
        if (getattr(r, "path", None) == path
                and method in (r.methods or set())):
            return [d.call for d in r.dependant.dependencies]
    return []


def test_post_jobs_uses_require_operator():
    from backend import auth as _au
    from backend.routers import installer
    deps = _route_dependencies(installer.router, "POST", "/installer/jobs")
    assert _au.require_operator in deps


def test_get_jobs_uses_require_operator():
    from backend import auth as _au
    from backend.routers import installer
    deps = _route_dependencies(installer.router, "GET", "/installer/jobs")
    assert _au.require_operator in deps


def test_get_job_by_id_uses_require_operator():
    from backend import auth as _au
    from backend.routers import installer
    deps = _route_dependencies(
        installer.router, "GET", "/installer/jobs/{job_id}",
    )
    assert _au.require_operator in deps


def test_post_cancel_uses_require_operator():
    from backend import auth as _au
    from backend.routers import installer
    deps = _route_dependencies(
        installer.router, "POST", "/installer/jobs/{job_id}/cancel",
    )
    assert _au.require_operator in deps


def test_post_retry_uses_require_operator():
    from backend import auth as _au
    from backend.routers import installer
    deps = _route_dependencies(
        installer.router, "POST", "/installer/jobs/{job_id}/retry",
    )
    assert _au.require_operator in deps


def test_poll_uses_require_admin_until_sidecar_token_lands():
    """Sidecar token auth lands with BS.4.1; until then admin role is
    the safe stand-in. This test pins that decision so a future
    refactor that drops auth on /poll trips a red gate."""
    from backend import auth as _au
    from backend.routers import installer
    deps = _route_dependencies(
        installer.router, "GET", "/installer/jobs/poll",
    )
    assert _au.require_admin in deps


def test_get_installed_uses_require_operator():
    """BS.8.2 — the cleanup-unused list is operator-only (no admin gate
    needed because every row is already tenant-scoped + read-only)."""
    from backend import auth as _au
    from backend.routers import installer
    deps = _route_dependencies(installer.router, "GET", "/installer/installed")
    assert _au.require_operator in deps


def test_post_uninstall_uses_require_operator():
    """BS.8.2 — bulk uninstall must run through the operator role + the
    PEP HOLD path. This test pins the operator gate; the PEP HOLD lives
    inside the handler body, exercised by the PG-live integration suite."""
    from backend import auth as _au
    from backend.routers import installer
    deps = _route_dependencies(installer.router, "POST", "/installer/uninstall")
    assert _au.require_operator in deps


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Row marshalling
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _job_row(**kw):
    base = {
        "id": "ij-0123456789ab",
        "tenant_id": "t-test",
        "entry_id": "nodejs-lts-20",
        "state": "queued",
        "idempotency_key": "abcdef0123456789",
        "sidecar_id": None,
        "protocol_version": 1,
        "bytes_done": 0,
        "bytes_total": None,
        "eta_seconds": None,
        "log_tail": "",
        "result_json": None,
        "error_reason": None,
        "pep_decision_id": None,
        "requested_by": "u-test",
        "queued_at": None,
        "claimed_at": None,
        "started_at": None,
        "completed_at": None,
    }
    base.update(kw)
    return base


def test_row_to_install_job_passes_through_all_columns():
    from backend.routers.installer import _row_to_install_job
    out = _row_to_install_job(_job_row(
        bytes_done=42, bytes_total=1024, eta_seconds=7,
    ))
    assert out["id"] == "ij-0123456789ab"
    assert out["state"] == "queued"
    assert out["bytes_done"] == 42
    assert out["bytes_total"] == 1024
    assert out["eta_seconds"] == 7


def test_row_to_install_job_handles_jsonb_as_string():
    """JSONB cols come back as Python objects via the asyncpg codec, but
    in dev-without-codec they may arrive as raw JSON strings — defence-
    in-depth parses string back into a dict."""
    from backend.routers.installer import _row_to_install_job
    out = _row_to_install_job(_job_row(result_json='{"ok": true, "n": 3}'))
    assert out["result_json"] == {"ok": True, "n": 3}


def test_row_to_install_job_handles_jsonb_as_dict():
    from backend.routers.installer import _row_to_install_job
    out = _row_to_install_job(_job_row(result_json={"already": "decoded"}))
    assert out["result_json"] == {"already": "decoded"}


def test_row_to_install_job_handles_invalid_jsonb_string():
    """Garbage JSON column shouldn't crash the marshaller — fall back
    to None so the API still returns the row."""
    from backend.routers.installer import _row_to_install_job
    out = _row_to_install_job(_job_row(result_json="not json {{{"))
    assert out["result_json"] is None


def test_row_to_install_job_optional_int_columns():
    from backend.routers.installer import _row_to_install_job
    out = _row_to_install_job(_job_row(bytes_total=None, eta_seconds=None))
    assert out["bytes_total"] is None
    assert out["eta_seconds"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sidecar-id pattern (long-poll claim auth side)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("good_id", [
    "sidecar-1",
    "sidecar.replica.0",
    "host-7:omnisight-installer:abc",
    "a",
    "x" * 128,
])
def test_sidecar_id_pattern_accepts_valid(good_id):
    from backend.routers.installer import SIDECAR_ID_PATTERN
    assert re.match(SIDECAR_ID_PATTERN, good_id), f"should accept {good_id!r}"


@pytest.mark.parametrize("bad_id", [
    "",
    " ",
    "with space",
    "x" * 129,           # too long
    "tab\there",
    "newline\nhere",
    "shell;injection",
])
def test_sidecar_id_pattern_rejects_invalid(bad_id):
    from backend.routers.installer import SIDECAR_ID_PATTERN
    assert not re.match(SIDECAR_ID_PATTERN, bad_id), f"should reject {bad_id!r}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Wiring sanity — installer router lands under /api/v1 in the app
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_installer_router_wired_into_main_app():
    """``backend/main.py`` must include the installer router with the
    standard ``/api/v1`` prefix — drift here means the endpoints exist
    in the module but aren't reachable via HTTP."""
    from backend.main import app
    paths = {
        (sorted(r.methods)[0], r.path)
        for r in app.routes
        if hasattr(r, "methods") and r.methods
    }
    expected = {
        ("POST", "/api/v1/installer/jobs"),
        ("GET", "/api/v1/installer/jobs"),
        ("GET", "/api/v1/installer/jobs/{job_id}"),
        ("POST", "/api/v1/installer/jobs/{job_id}/cancel"),
        ("POST", "/api/v1/installer/jobs/{job_id}/retry"),
        ("GET", "/api/v1/installer/jobs/poll"),
    }
    missing = expected - paths
    assert not missing, f"installer routes not wired into app: {missing}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PEP gateway integration shape — tool name + classify behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_pep_classify_returns_hold_for_install_entry():
    """The PEP tool name we use must classify as HOLD across every tier
    — installs are inherently destructive (write to host disk / pull
    images / run vendor scripts), so the tier whitelist must NEVER
    contain ``install_entry``. If a future PR slips ``install_entry``
    onto a tier whitelist this test goes red."""
    from backend import pep_gateway
    from backend.routers.installer import INSTALL_PEP_TOOL
    for tier in ("t1", "t2", "t3"):
        action, rule, reason, scope = pep_gateway.classify(
            INSTALL_PEP_TOOL, {"entry_id": "x"}, tier,
        )
        assert action == pep_gateway.PepAction.hold, (
            f"install must HOLD on tier {tier} — got {action} ({rule})"
        )


def test_install_pep_tool_not_in_any_tier_whitelist():
    from backend import pep_gateway
    from backend.routers.installer import INSTALL_PEP_TOOL
    for tier in ("t1", "t2", "t3"):
        wl = pep_gateway.tier_whitelist(tier)
        assert INSTALL_PEP_TOOL not in wl, (
            f"install_entry leaked into {tier} whitelist — would skip PEP HOLD"
        )
