"""BS.2.1 — smoke tests for ``backend/routers/catalog.py``.

Scope (per the BS phase split — full ~25 case integration suite is
BS.2.4's ``test_catalog_api.py``):

* Router import + route registration shape
* Pydantic schema validation (positive + negative)
* Auth gate wiring — read = ``require_operator``, write = ``require_admin``
* Module-level constants align with alembic 0051 / _schema.yaml

These tests do NOT require a live PG. They exercise the import surface,
the FastAPI route layer's deps registration, and the Pydantic body
validators. The full PG-backed CRUD / filter / pagination / tenant-
isolation matrix lands in BS.2.4.
"""

from __future__ import annotations

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module-level surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_prefix_and_tags():
    from backend.routers import catalog
    assert catalog.router.prefix == "/catalog"
    assert "catalog" in catalog.router.tags


def test_route_registration_full_set():
    """Every endpoint in the BS.2.1 spec is registered exactly once."""
    from backend.routers import catalog
    pairs = sorted(
        (sorted(r.methods)[0], r.path)
        for r in catalog.router.routes
        if hasattr(r, "methods") and r.methods
    )
    expected = [
        ("DELETE", "/catalog/entries/{entry_id}"),
        ("DELETE", "/catalog/sources/{sub_id}"),
        ("GET", "/catalog/entries"),
        ("GET", "/catalog/entries/{entry_id}"),
        ("GET", "/catalog/sources"),
        ("PATCH", "/catalog/entries/{entry_id}"),
        ("PATCH", "/catalog/sources/{sub_id}"),
        ("POST", "/catalog/entries"),
        ("POST", "/catalog/sources"),
    ]
    assert pairs == expected


def test_constants_mirror_alembic_0051_check_constraints():
    """Every closed enum in the router matches the alembic 0051 CHECK.

    Drift here means a 422 from the router that PG would have accepted
    (or worse: a body the router accepted that PG rejects with 500).
    """
    from backend.routers import catalog
    assert catalog.ENTRY_FAMILIES == (
        "mobile", "embedded", "web", "software",
        "rtos", "cross-toolchain", "custom",
    )
    assert catalog.ENTRY_INSTALL_METHODS == (
        "noop", "docker_pull", "shell_script", "vendor_installer",
    )
    assert catalog.ALL_SOURCES == (
        "shipped", "operator", "override", "subscription",
    )
    # POST may only ever set operator / override — shipped is the
    # alembic 0052 seed migration's job, subscription is the BS.8.5
    # feed worker's job.
    assert catalog.WRITABLE_SOURCES == ("operator", "override")
    assert catalog.SOURCE_AUTH_METHODS == (
        "none", "basic", "bearer", "signed_url",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Entry id regex
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("good_id", [
    "nxp-mcuxpresso-imxrt1170",
    "zephyr-rtos-3-7",
    "arm-gnu-toolchain-13",
    "a",                 # single-char minimum
    "ab",
    "node-20",
    "rust",
])
def test_entry_id_regex_accepts_valid(good_id):
    from backend.routers.catalog import _ENTRY_ID_RE, ENTRY_ID_MAX_LEN
    assert _ENTRY_ID_RE.match(good_id), f"should accept {good_id!r}"
    assert len(good_id) <= ENTRY_ID_MAX_LEN


@pytest.mark.parametrize("bad_id", [
    "",                  # empty
    "-leading",          # leading hyphen
    "trailing-",         # trailing hyphen
    "double--hyphen",    # consecutive hyphens
    "Upper",             # uppercase
    "with_underscore",
    "white space",
    "punct.dot",
])
def test_entry_id_regex_rejects_invalid(bad_id):
    from backend.routers.catalog import _ENTRY_ID_RE
    assert not _ENTRY_ID_RE.match(bad_id), f"should reject {bad_id!r}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas — positive & negative
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_entry_create_full_operator_body_passes():
    from backend.routers.catalog import EntryCreate
    body = EntryCreate(
        id="custom-tool",
        source="operator",
        vendor="acme",
        family="software",
        display_name="Acme Tool",
        version="1.0.0",
        install_method="shell_script",
    )
    assert body.id == "custom-tool"
    assert body.source == "operator"


def test_entry_create_override_body_passes_with_partial_fields():
    from backend.routers.catalog import EntryCreate
    body = EntryCreate(
        id="nxp-mcuxpresso-imxrt1170",
        source="override",
        version="11.10.1",  # only the field we want to overlay
    )
    assert body.source == "override"
    assert body.vendor is None  # inherits from shipped base


def test_entry_create_rejects_shipped_source():
    """Source='shipped' must be rejected — only the alembic 0052 seed
    migration writes shipped rows."""
    import pydantic
    from backend.routers.catalog import EntryCreate
    with pytest.raises(pydantic.ValidationError):
        EntryCreate(
            id="ok-id",
            source="shipped",  # type: ignore[arg-type]
            vendor="x", family="software", display_name="x",
            version="1", install_method="noop",
        )


def test_entry_create_rejects_subscription_source():
    import pydantic
    from backend.routers.catalog import EntryCreate
    with pytest.raises(pydantic.ValidationError):
        EntryCreate(
            id="ok-id",
            source="subscription",  # type: ignore[arg-type]
        )


def test_entry_create_rejects_invalid_id_pattern():
    import pydantic
    from backend.routers.catalog import EntryCreate
    with pytest.raises(pydantic.ValidationError):
        EntryCreate(id="UPPER", source="operator")


def test_entry_create_rejects_unknown_family():
    import pydantic
    from backend.routers.catalog import EntryCreate
    with pytest.raises(pydantic.ValidationError):
        EntryCreate(
            id="ok-id", source="operator",
            family="not-a-family",  # type: ignore[arg-type]
        )


def test_entry_create_rejects_unknown_install_method():
    import pydantic
    from backend.routers.catalog import EntryCreate
    with pytest.raises(pydantic.ValidationError):
        EntryCreate(
            id="ok-id", source="operator",
            install_method="rsync",  # type: ignore[arg-type]
        )


def test_entry_create_rejects_oversized_size_bytes():
    """size_bytes ceiling = 1 TiB; rejecting 2 TiB."""
    import pydantic
    from backend.routers.catalog import EntryCreate, SIZE_BYTES_MAX
    with pytest.raises(pydantic.ValidationError):
        EntryCreate(
            id="ok-id", source="operator",
            size_bytes=SIZE_BYTES_MAX + 1,
        )


def test_entry_create_rejects_negative_size_bytes():
    import pydantic
    from backend.routers.catalog import EntryCreate
    with pytest.raises(pydantic.ValidationError):
        EntryCreate(
            id="ok-id", source="operator",
            size_bytes=-1,
        )


def test_entry_create_rejects_invalid_sha256():
    import pydantic
    from backend.routers.catalog import EntryCreate
    with pytest.raises(pydantic.ValidationError):
        EntryCreate(
            id="ok-id", source="operator",
            sha256="not-hex",
        )


def test_entry_patch_has_any_field():
    from backend.routers.catalog import EntryPatch
    assert not EntryPatch().has_any_field()
    assert EntryPatch(version="2").has_any_field()
    assert EntryPatch(hidden=False).has_any_field()
    # depends_on=[] is a deliberate empty-list write, NOT "leave alone"
    assert EntryPatch(depends_on=[]).has_any_field()


def test_subscription_create_minimal_body():
    from backend.routers.catalog import SubscriptionCreate
    body = SubscriptionCreate(feed_url="https://example.com/feed.json")
    assert body.auth_method == "none"
    assert body.refresh_interval_s == 86400
    assert body.enabled is True


def test_subscription_create_rejects_whitespace_in_secret_ref():
    """auth_secret_ref must be a secret-store key, never a plaintext
    secret. A literal token (which always contains whitespace, base64
    padding, or both) must 422 — keeps secrets out of the catalog DB."""
    import pydantic
    from backend.routers.catalog import SubscriptionCreate
    with pytest.raises(pydantic.ValidationError):
        SubscriptionCreate(
            feed_url="https://example.com/feed.json",
            auth_method="bearer",
            auth_secret_ref="literal token with space",
        )


def test_subscription_create_rejects_unknown_auth_method():
    import pydantic
    from backend.routers.catalog import SubscriptionCreate
    with pytest.raises(pydantic.ValidationError):
        SubscriptionCreate(
            feed_url="https://example.com/feed.json",
            auth_method="oauth2",  # type: ignore[arg-type]
        )


def test_subscription_create_clamps_refresh_interval():
    """Lower bound 60s, upper bound 30 days. Anything outside 422s."""
    import pydantic
    from backend.routers.catalog import SubscriptionCreate
    with pytest.raises(pydantic.ValidationError):
        SubscriptionCreate(
            feed_url="https://example.com/feed.json",
            refresh_interval_s=10,
        )
    with pytest.raises(pydantic.ValidationError):
        SubscriptionCreate(
            feed_url="https://example.com/feed.json",
            refresh_interval_s=60 * 86400,
        )


def test_subscription_patch_has_any_field():
    from backend.routers.catalog import SubscriptionPatch
    assert not SubscriptionPatch().has_any_field()
    assert SubscriptionPatch(enabled=False).has_any_field()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auth dependency wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _route_dependencies(router, method: str, path: str) -> list:
    """Return the list of dependency-callable references registered on
    a route — used to verify the right ``require_*`` is wired."""
    for r in router.routes:
        if (getattr(r, "path", None) == path
                and method in (r.methods or set())):
            return [d.call for d in r.dependant.dependencies]
    return []


def test_get_entries_uses_require_operator():
    from backend import auth as _au
    from backend.routers import catalog
    deps = _route_dependencies(catalog.router, "GET", "/catalog/entries")
    assert _au.require_operator in deps


def test_get_entry_uses_require_operator():
    from backend import auth as _au
    from backend.routers import catalog
    deps = _route_dependencies(
        catalog.router, "GET", "/catalog/entries/{entry_id}",
    )
    assert _au.require_operator in deps


def test_post_entries_uses_require_admin():
    from backend import auth as _au
    from backend.routers import catalog
    deps = _route_dependencies(catalog.router, "POST", "/catalog/entries")
    assert _au.require_admin in deps


def test_patch_entry_uses_require_admin():
    from backend import auth as _au
    from backend.routers import catalog
    deps = _route_dependencies(
        catalog.router, "PATCH", "/catalog/entries/{entry_id}",
    )
    assert _au.require_admin in deps


def test_delete_entry_uses_require_admin():
    from backend import auth as _au
    from backend.routers import catalog
    deps = _route_dependencies(
        catalog.router, "DELETE", "/catalog/entries/{entry_id}",
    )
    assert _au.require_admin in deps


@pytest.mark.parametrize("method,path", [
    ("GET", "/catalog/sources"),
    ("POST", "/catalog/sources"),
    ("PATCH", "/catalog/sources/{sub_id}"),
    ("DELETE", "/catalog/sources/{sub_id}"),
])
def test_sources_endpoints_all_admin_only(method, path):
    """Source CRUD is admin-only across the board (BS.2.3 spec)."""
    from backend import auth as _au
    from backend.routers import catalog
    deps = _route_dependencies(catalog.router, method, path)
    assert _au.require_admin in deps, (
        f"{method} {path} must be admin-only"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Resolver — override > operator > shipped
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _shipped_row(**kw):
    base = {
        "id": "x", "source": "shipped", "schema_version": 1,
        "tenant_id": None, "vendor": "v", "family": "software",
        "display_name": "X", "version": "1", "install_method": "noop",
        "install_url": None, "sha256": None, "size_bytes": None,
        "depends_on": [], "metadata": {}, "hidden": False,
        "created_at": None, "updated_at": None,
    }
    base.update(kw)
    return base


def test_resolve_returns_shipped_when_only_shipped():
    from backend.routers.catalog import _resolve
    rows = [_shipped_row()]
    out = _resolve(rows)
    assert out["source"] == "shipped"
    assert out["version"] == "1"


def test_resolve_operator_shadows_shipped():
    from backend.routers.catalog import _resolve
    rows = [
        _shipped_row(),
        _shipped_row(source="operator", tenant_id="t-x", version="9"),
    ]
    out = _resolve(rows)
    assert out["source"] == "operator"
    assert out["version"] == "9"


def test_resolve_override_overlays_only_set_columns():
    """Override row is sparse — only its non-NULL columns replace the
    shipped base. Unset columns inherit from shipped."""
    from backend.routers.catalog import _resolve
    rows = [
        _shipped_row(version="1.0.0", display_name="Shipped"),
        _shipped_row(
            source="override", tenant_id="t-x",
            version="2.0.0",       # admin overrode the version
            display_name=None,     # admin did not touch display_name
            vendor=None,            # ditto vendor
        ),
    ]
    out = _resolve(rows)
    assert out["source"] == "override"
    assert out["version"] == "2.0.0"          # from override
    assert out["display_name"] == "Shipped"   # inherited from shipped
    assert out["vendor"] == "v"               # inherited from shipped


def test_resolve_override_hidden_propagates():
    """An override row with hidden=TRUE tombstones the shipped row for
    the tenant — caller filters hidden=True out of visible results."""
    from backend.routers.catalog import _resolve
    rows = [
        _shipped_row(),
        _shipped_row(source="override", tenant_id="t-x", hidden=True),
    ]
    out = _resolve(rows)
    assert out["hidden"] is True
