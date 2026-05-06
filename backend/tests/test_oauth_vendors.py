"""AS.1.3 — `backend.security.oauth_vendors` catalog contract tests.

Exercises every public surface of the vendor catalog:

    1. Catalog completeness     11 vendors with the canonical slug set,
                                in canonical declaration order.
    2. Per-vendor invariants    URLs are HTTPS, scope tuples + extra
                                params are immutable, OIDC vendors mint
                                nonces on `begin_authorization_for_vendor`.
    3. VendorConfig hashability frozen dataclass + tuple inner state →
                                works as dict key / set member.
    4. Lookup contract          `get_vendor` round-trips, unknown raises
                                `VendorNotFoundError`.
    5. Catalog-aware shims      `build_authorize_url_for_vendor` /
                                `begin_authorization_for_vendor` thread
                                vendor data into the AS.1.1 core lib;
                                caller overrides win over catalog
                                defaults.
    6. Module-global state audit (per SOP §1) — `VENDORS` is a
                                MappingProxyType (not `dict`); reload
                                yields stable canonical ID tuple;
                                no module-level mutable containers.
    7. Cross-twin drift guard   For every vendor, every catalog field
                                MUST byte-match the TS twin at
                                `templates/_shared/oauth-client/vendors.ts`
                                — extracted via regex from the source.
                                SHA-256 oracle over `ALL_VENDOR_IDS`
                                pins canonical order.
"""

from __future__ import annotations

import hashlib
import importlib
import pathlib
import re
import types
from urllib.parse import parse_qs, urlparse

import pytest

from backend.security import oauth_client as oc
from backend.security import oauth_vendors as ov


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Catalog completeness
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CANONICAL_VENDOR_IDS = (
    "github",
    "google",
    "microsoft",
    "apple",
    "gitlab",
    "bitbucket",
    "slack",
    "notion",
    "salesforce",
    "hubspot",
    "discord",
)


def test_catalog_has_eleven_vendors():
    assert len(ov.ALL_VENDORS) == 11


def test_catalog_canonical_ids_in_declaration_order():
    assert ov.ALL_VENDOR_IDS == CANONICAL_VENDOR_IDS


def test_catalog_individual_constants_exported():
    """The 11 module-level constants (`GITHUB`, `GOOGLE`, …) MUST be
    the same objects as the entries in `ALL_VENDORS` — guards against
    accidental copy-construction that would let drift sneak in
    between `GITHUB.tokenEndpoint` and `ALL_VENDORS[0].tokenEndpoint`."""
    name_to_const = {
        "github": ov.GITHUB,
        "google": ov.GOOGLE,
        "microsoft": ov.MICROSOFT,
        "apple": ov.APPLE,
        "gitlab": ov.GITLAB,
        "bitbucket": ov.BITBUCKET,
        "slack": ov.SLACK,
        "notion": ov.NOTION,
        "salesforce": ov.SALESFORCE,
        "hubspot": ov.HUBSPOT,
        "discord": ov.DISCORD,
    }
    for vendor in ov.ALL_VENDORS:
        assert name_to_const[vendor.provider_id] is vendor


def test_vendors_lookup_round_trip():
    for slug in CANONICAL_VENDOR_IDS:
        v = ov.VENDORS[slug]
        assert v.provider_id == slug
        # MappingProxyType supports `in`.
        assert slug in ov.VENDORS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Per-vendor invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("vendor", ov.ALL_VENDORS, ids=lambda v: v.provider_id)
def test_vendor_endpoints_are_https(vendor: ov.VendorConfig):
    assert vendor.authorize_endpoint.startswith("https://"), vendor
    assert vendor.token_endpoint.startswith("https://"), vendor
    if vendor.userinfo_endpoint is not None:
        assert vendor.userinfo_endpoint.startswith("https://"), vendor
    if vendor.revocation_endpoint is not None:
        assert vendor.revocation_endpoint.startswith("https://"), vendor


@pytest.mark.parametrize("vendor", ov.ALL_VENDORS, ids=lambda v: v.provider_id)
def test_vendor_provider_id_kebab_or_word_case(vendor: ov.VendorConfig):
    """Slugs MUST be lowercase letters / digits — no spaces, no
    capitals, no underscores. The 11 shipped slugs are word-only;
    future entries are encouraged to follow the same shape (the slug
    doubles as a clean URL path segment)."""
    assert re.fullmatch(r"[a-z][a-z0-9]*", vendor.provider_id), vendor


@pytest.mark.parametrize("vendor", ov.ALL_VENDORS, ids=lambda v: v.provider_id)
def test_vendor_default_scopes_are_immutable_tuple(vendor: ov.VendorConfig):
    assert isinstance(vendor.default_scopes, tuple)
    for scope in vendor.default_scopes:
        assert isinstance(scope, str) and scope, vendor


@pytest.mark.parametrize("vendor", ov.ALL_VENDORS, ids=lambda v: v.provider_id)
def test_vendor_extra_params_are_tuple_of_tuples(vendor: ov.VendorConfig):
    assert isinstance(vendor.extra_authorize_params, tuple)
    for pair in vendor.extra_authorize_params:
        assert isinstance(pair, tuple) and len(pair) == 2
        assert isinstance(pair[0], str) and isinstance(pair[1], str)


@pytest.mark.parametrize("vendor", ov.ALL_VENDORS, ids=lambda v: v.provider_id)
def test_vendor_extra_params_keys_unique(vendor: ov.VendorConfig):
    keys = [k for k, _ in vendor.extra_authorize_params]
    assert len(keys) == len(set(keys)), vendor


def test_vendor_oidc_vendors_match_design_intent():
    """Pin the OIDC subset against the AS.1.3 row design — Google,
    Microsoft, Apple, GitLab, Salesforce ship as OIDC; the rest ship
    as plain OAuth 2.0. Adding a new OIDC vendor (or flipping an
    existing one) must update this assertion."""
    oidc = {v.provider_id for v in ov.ALL_VENDORS if v.is_oidc}
    assert oidc == {"google", "microsoft", "apple", "gitlab", "salesforce"}


def test_vendor_refresh_support_matches_design_intent():
    no_refresh = {v.provider_id for v in ov.ALL_VENDORS if not v.supports_refresh_token}
    # Notion is the only vendor without refresh_token (long-lived
    # access_token + no refresh grant).
    assert no_refresh == {"notion"}


def test_vendor_pkce_support_matches_design_intent():
    no_pkce = {v.provider_id for v in ov.ALL_VENDORS if not v.supports_pkce}
    # Notion + HubSpot do not document PKCE support.
    assert no_pkce == {"notion", "hubspot"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — VendorConfig hashability + extra_params_mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_vendor_config_is_hashable():
    """Frozen dataclass + tuple inner state → instances usable as
    dict keys / set members. Future audit code may key per-vendor
    metrics by `VendorConfig` instance directly."""
    cache = {ov.GITHUB: 1, ov.GOOGLE: 2}
    assert cache[ov.GITHUB] == 1
    assert cache[ov.GOOGLE] == 2
    assert len({ov.GITHUB, ov.GOOGLE, ov.GITHUB}) == 2


def test_vendor_config_is_frozen():
    with pytest.raises((AttributeError, Exception)):
        ov.GITHUB.token_endpoint = "https://evil.example/token"  # type: ignore[misc]


def test_extra_params_mapping_is_fresh_dict_each_call():
    """`extra_params_mapping` returns a fresh dict per call — caller
    can mutate freely without bleeding into the catalog."""
    a = ov.GOOGLE.extra_params_mapping
    b = ov.GOOGLE.extra_params_mapping
    assert a is not b
    a["prompt"] = "login"  # mutate caller copy
    assert b["prompt"] == "consent"  # second copy still pristine


def test_extra_params_mapping_round_trips_keys():
    assert dict(ov.GOOGLE.extra_authorize_params) == ov.GOOGLE.extra_params_mapping


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — Lookup contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_get_vendor_returns_canonical_constant():
    assert ov.get_vendor("github") is ov.GITHUB
    assert ov.get_vendor("salesforce") is ov.SALESFORCE


def test_get_vendor_raises_typed_error_on_unknown_slug():
    with pytest.raises(ov.VendorNotFoundError) as exc:
        ov.get_vendor("myspace")
    assert "myspace" in str(exc.value)
    # Helpful hint — error message names the known set.
    for known in CANONICAL_VENDOR_IDS:
        assert known in str(exc.value)


def test_vendor_not_found_error_is_keyerror_subclass():
    """`VendorNotFoundError(KeyError)` so callers using bare
    `except KeyError:` for catalog lookup also catch us."""
    assert issubclass(ov.VendorNotFoundError, KeyError)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — Catalog-aware shims (build / begin)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def test_build_authorize_url_pulls_endpoint_from_vendor():
    url = ov.build_authorize_url_for_vendor(
        ov.GITHUB,
        client_id="Iv1.test",
        redirect_uri="https://app.example/cb",
        state="S",
        code_challenge="C",
    )
    assert url.startswith("https://github.com/login/oauth/authorize?")


def test_build_authorize_url_uses_default_scopes_when_omitted():
    url = ov.build_authorize_url_for_vendor(
        ov.GOOGLE,
        client_id="goog.test",
        redirect_uri="https://app.example/cb",
        state="S",
        code_challenge="C",
    )
    q = _query(url)
    assert q["scope"] == ["openid email profile"]


def test_build_authorize_url_caller_scope_overrides_default():
    url = ov.build_authorize_url_for_vendor(
        ov.GITHUB,
        client_id="Iv1.test",
        redirect_uri="https://app.example/cb",
        state="S",
        code_challenge="C",
        scope=["read:org"],
    )
    q = _query(url)
    assert q["scope"] == ["read:org"]


def test_build_authorize_url_threads_vendor_extra_params():
    url = ov.build_authorize_url_for_vendor(
        ov.GOOGLE,
        client_id="goog.test",
        redirect_uri="https://app.example/cb",
        state="S",
        code_challenge="C",
    )
    q = _query(url)
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]


def test_build_authorize_url_caller_extra_overrides_vendor_extra():
    """Caller-supplied extra_params override vendor catalog defaults
    (last-write-wins on key collisions during the merge)."""
    url = ov.build_authorize_url_for_vendor(
        ov.GOOGLE,
        client_id="goog.test",
        redirect_uri="https://app.example/cb",
        state="S",
        code_challenge="C",
        extra_params={"prompt": "login"},
    )
    q = _query(url)
    assert q["prompt"] == ["login"]
    # Vendor's other params still apply.
    assert q["access_type"] == ["offline"]


def test_gitlab_default_scopes_match_oidc_login_contract():
    """FX2.D9.7.6 pins GitLab login to read_user + OIDC profile claims."""
    url = ov.build_authorize_url_for_vendor(
        ov.GITLAB,
        client_id="gitlab.test",
        redirect_uri="https://app.example/cb",
        state="S",
        code_challenge="C",
    )
    q = _query(url)
    assert q["scope"] == ["read_user openid email profile"]


def test_bitbucket_default_scopes_match_login_contract():
    """FX2.D9.7.7 pins Bitbucket login to account + email scopes."""
    url = ov.build_authorize_url_for_vendor(
        ov.BITBUCKET,
        client_id="bitbucket.test",
        redirect_uri="https://app.example/cb",
        state="S",
        code_challenge="C",
    )
    q = _query(url)
    assert q["scope"] == ["account email"]


def test_begin_authorization_for_vendor_mints_nonce_for_oidc_only():
    _, github_flow = ov.begin_authorization_for_vendor(
        ov.GITHUB, client_id="cid", redirect_uri="https://app/cb"
    )
    _, google_flow = ov.begin_authorization_for_vendor(
        ov.GOOGLE, client_id="cid", redirect_uri="https://app/cb"
    )
    assert github_flow.nonce is None  # not OIDC
    assert google_flow.nonce is not None  # OIDC


def test_begin_authorization_for_vendor_round_trips_provider():
    _, flow = ov.begin_authorization_for_vendor(
        ov.MICROSOFT, client_id="cid", redirect_uri="https://app/cb"
    )
    assert flow.provider == "microsoft"
    assert flow.scope == ov.MICROSOFT.default_scopes


def test_begin_authorization_for_vendor_threads_extra_authorize_params():
    url, _ = ov.begin_authorization_for_vendor(
        ov.APPLE, client_id="cid", redirect_uri="https://app/cb"
    )
    assert "response_mode=form_post" in url


def test_begin_authorization_for_vendor_caller_extra_authorize_params_merge():
    url, _ = ov.begin_authorization_for_vendor(
        ov.GOOGLE,
        client_id="cid",
        redirect_uri="https://app/cb",
        extra_authorize_params={"hd": "tenant.example"},
    )
    q = _query(url)
    assert q["hd"] == ["tenant.example"]
    assert q["access_type"] == ["offline"]  # vendor still applies


def test_begin_authorization_for_vendor_state_ttl_default():
    """When `state_ttl_seconds` is omitted the catalog shim must use
    the AS.1.1 lib default — guards against a future copy-paste
    regression that hardcodes a different TTL."""
    url, flow = ov.begin_authorization_for_vendor(
        ov.GITHUB, client_id="cid", redirect_uri="https://app/cb"
    )
    assert (flow.expires_at - flow.created_at) == oc.DEFAULT_STATE_TTL_SECONDS


def test_begin_authorization_for_vendor_state_ttl_override():
    url, flow = ov.begin_authorization_for_vendor(
        ov.GITHUB,
        client_id="cid",
        redirect_uri="https://app/cb",
        state_ttl_seconds=120,
    )
    assert (flow.expires_at - flow.created_at) == 120


def test_get_vendor_then_begin_authorization_round_trip():
    """End-to-end: caller validates a slug from a URL path against
    the catalog, then drives the flow — the AS.6.1 endpoint shape."""
    vendor = ov.get_vendor("discord")
    url, flow = ov.begin_authorization_for_vendor(
        vendor, client_id="cid", redirect_uri="https://app/cb"
    )
    assert "discord.com" in url
    assert flow.provider == "discord"
    assert flow.scope == ("identify", "email")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — Module-global state audit (SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_vendors_index_is_mappingproxy_not_dict():
    """`VENDORS` MUST be a `types.MappingProxyType` (frozen view) —
    not a `dict`. SOP §1 module-state audit forbids mutable
    containers at module scope; mappingproxy is the canonical
    immutable-mapping shape and survives the AS.1.1 sibling test
    `test_no_module_level_mutable_state` if it ever gets copied
    onto `oauth_vendors`."""
    assert isinstance(ov.VENDORS, types.MappingProxyType)


def test_vendors_module_has_no_mutable_module_level_containers():
    forbidden = (list, dict, set, bytearray)
    offenders = []
    for name, val in vars(ov).items():
        if name.startswith("__") and name.endswith("__"):
            continue
        if isinstance(val, forbidden):
            offenders.append((name, type(val).__name__))
    assert offenders == [], (
        f"module-level mutable containers detected: {offenders}"
    )


def test_canonical_vendor_ids_stable_across_reload():
    """Reload yields byte-identical canonical id tuple — every
    uvicorn worker reads the same catalog. SOP §1 audit answer #1
    (deterministic-by-construction across workers)."""
    before = ov.ALL_VENDOR_IDS
    importlib.reload(ov)
    after = ov.ALL_VENDOR_IDS
    assert before == after


def test_public_surface_matches_all():
    for name in ov.__all__:
        assert hasattr(ov, name), f"__all__ promises {name!r} but it's absent"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — Cross-twin drift guard (Python ↔ TS)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# The AS.1.5 design will land additional vendor-shape parity (token
# response parser round-trips identically against the same fixture).
# This file already covers the **catalog-side** drift: every vendor's
# every catalog field MUST match the TS twin byte-for-byte. Drift on
# either side breaks one of these tests, CI red is the canary.

_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "oauth-client"
    / "vendors.ts"
)


def _read_ts_twin() -> str:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    return _TS_TWIN_PATH.read_text(encoding="utf-8")


def _ts_object_block(src: str, const_name: str) -> str:
    """Pull the literal `{ ... }` body of `export const <NAME>: VendorConfig = makeVendor({ ... })`.

    Returns the raw text between the outermost braces — caller parses
    individual fields with field-specific regex.
    """
    # Locate the makeVendor( opening for this const.
    head = re.search(
        rf"export\s+const\s+{const_name}\s*:\s*VendorConfig\s*=\s*makeVendor\(\s*\{{",
        src,
    )
    assert head, f"could not find `export const {const_name}: VendorConfig = makeVendor({{...}})`"
    start = head.end() - 1  # position of opening `{`
    depth = 0
    for i in range(start, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unterminated object literal for {const_name}")


def _extract_string_field(block: str, field: str) -> str | None:
    """Pull `field: "value"` or `field: null`. Returns Python None
    for null. Returns raw string for a string literal."""
    m = re.search(
        rf'(?<![A-Za-z0-9_]){field}\s*:\s*(null|"((?:[^"\\]|\\.)*)")',
        block,
    )
    assert m, f"field {field!r} not found in TS object block"
    if m.group(1) == "null":
        return None
    raw = m.group(2)
    # Apply minimal JS-string unescape — only \" \\ \/ \n \r \t \uXXXX
    # appear in our catalog values (none actually use these, but be
    # safe for future entries).
    return bytes(raw, "utf-8").decode("unicode_escape")


def _extract_bool_field(block: str, field: str) -> bool:
    m = re.search(rf'(?<![A-Za-z0-9_]){field}\s*:\s*(true|false)\b', block)
    assert m, f"bool field {field!r} not found"
    return m.group(1) == "true"


def _extract_string_array(block: str, field: str) -> list[str]:
    """Pull `field: [ "a", "b", ... ]`."""
    m = re.search(
        rf'(?<![A-Za-z0-9_]){field}\s*:\s*\[(.*?)\]',
        block,
        re.DOTALL,
    )
    assert m, f"array field {field!r} not found"
    inner = m.group(1).strip()
    if not inner:
        return []
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', inner)
    return [bytes(s, "utf-8").decode("unicode_escape") for s in items]


def _extract_balanced_bracket_body(block: str, field: str) -> str:
    """Locate `field: [` and return the substring between the matched
    pair of outer `[` `]`, doing depth counting so nested arrays
    (e.g. `[ [k, v], [k, v] ]`) are captured correctly. The simple
    non-greedy regex `\\[(.*?)\\]` fails on nested arrays."""
    head = re.search(rf'(?<![A-Za-z0-9_]){field}\s*:\s*\[', block)
    assert head, f"array field {field!r} not found"
    start = head.end() - 1  # position of opening `[`
    depth = 0
    i = start
    in_string = False
    string_char = ""
    while i < len(block):
        c = block[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == string_char:
                in_string = False
        else:
            if c == '"' or c == "'":
                in_string = True
                string_char = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return block[start + 1 : i]
        i += 1
    raise AssertionError(f"unterminated array literal for {field!r}")


def _extract_pair_array(block: str, field: str) -> list[tuple[str, str]]:
    """Pull `field: [ ["k", "v"], ["k", "v"] ]` — depth-aware so the
    inner `[k, v]` brackets don't terminate the outer match."""
    inner = _extract_balanced_bracket_body(block, field).strip()
    if not inner:
        return []
    pairs = re.findall(
        r'\[\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]',
        inner,
    )
    return [
        (
            bytes(k, "utf-8").decode("unicode_escape"),
            bytes(v, "utf-8").decode("unicode_escape"),
        )
        for k, v in pairs
    ]


_PROVIDER_ID_TO_TS_CONST = {
    "github": "GITHUB",
    "google": "GOOGLE",
    "microsoft": "MICROSOFT",
    "apple": "APPLE",
    "gitlab": "GITLAB",
    "bitbucket": "BITBUCKET",
    "slack": "SLACK",
    "notion": "NOTION",
    "salesforce": "SALESFORCE",
    "hubspot": "HUBSPOT",
    "discord": "DISCORD",
}


@pytest.mark.parametrize(
    "vendor", ov.ALL_VENDORS, ids=lambda v: v.provider_id
)
def test_vendor_catalog_field_parity_python_ts(vendor: ov.VendorConfig):
    """Per-vendor field-by-field equality between the two twins.
    A change to any of the 11 catalog fields on either side breaks
    its parametrized test for the affected vendor only — fast triage.
    """
    ts_src = _read_ts_twin()
    const_name = _PROVIDER_ID_TO_TS_CONST[vendor.provider_id]
    block = _ts_object_block(ts_src, const_name)

    assert _extract_string_field(block, "providerId") == vendor.provider_id
    assert _extract_string_field(block, "displayName") == vendor.display_name
    assert _extract_string_field(block, "authorizeEndpoint") == vendor.authorize_endpoint
    assert _extract_string_field(block, "tokenEndpoint") == vendor.token_endpoint
    assert _extract_string_field(block, "userinfoEndpoint") == vendor.userinfo_endpoint
    assert _extract_string_field(block, "revocationEndpoint") == vendor.revocation_endpoint
    assert tuple(_extract_string_array(block, "defaultScopes")) == vendor.default_scopes
    assert _extract_bool_field(block, "isOidc") == vendor.is_oidc
    ts_pairs = _extract_pair_array(block, "extraAuthorizeParams")
    assert tuple(ts_pairs) == vendor.extra_authorize_params
    assert _extract_bool_field(block, "supportsRefreshToken") == vendor.supports_refresh_token
    assert _extract_bool_field(block, "supportsPkce") == vendor.supports_pkce


def test_canonical_vendor_id_order_sha256_parity_python_ts():
    """SHA-256 of `\\n`.join(ALL_VENDOR_IDS) MUST match between sides.
    Catches a reorder of the catalog (e.g. swap apple+gitlab) — the
    per-vendor field tests above are ID-keyed and would silently pass
    for a reorder, so this hash is the order-pinning oracle.
    """
    ts_src = _read_ts_twin()
    # Pull the inner content of the ALL_VENDORS array body.
    m = re.search(
        r"export\s+const\s+ALL_VENDORS\s*:\s*[^=]*=\s*Object\.freeze\(\s*\[(.*?)\]\s*\)",
        ts_src,
        re.DOTALL,
    )
    assert m, "could not find ALL_VENDORS array body in TS twin"
    inner = m.group(1)
    # Each line should reference one of the 11 vendor const names.
    ts_consts = [
        c for c in re.findall(r"\b([A-Z][A-Z]+)\b", inner)
        if c in set(_PROVIDER_ID_TO_TS_CONST.values())
    ]
    # Map each TS const back to its provider_id slug.
    ts_to_slug = {c: s for s, c in _PROVIDER_ID_TO_TS_CONST.items()}
    ts_slugs = [ts_to_slug[c] for c in ts_consts]
    py_slugs = list(ov.ALL_VENDOR_IDS)

    py_hash = hashlib.sha256("\n".join(py_slugs).encode("utf-8")).hexdigest()
    ts_hash = hashlib.sha256("\n".join(ts_slugs).encode("utf-8")).hexdigest()
    assert py_hash == ts_hash, (
        f"vendor catalog order drift between Python and TS twin\n"
        f"  Python: {py_slugs}\n"
        f"  TS    : {ts_slugs}\n"
        f"  Python SHA-256: {py_hash}\n"
        f"  TS     SHA-256: {ts_hash}"
    )


def test_ts_twin_declares_eleven_export_const_vendors():
    """Sanity — every Python vendor has a matching `export const NAME:
    VendorConfig = makeVendor({...})` in the TS twin. Catches a
    partial port that adds a vendor on Python only or vice versa."""
    ts_src = _read_ts_twin()
    declared: set[str] = set()
    for ts_const in _PROVIDER_ID_TO_TS_CONST.values():
        if re.search(
            rf"export\s+const\s+{ts_const}\s*:\s*VendorConfig\s*=\s*makeVendor\(",
            ts_src,
        ):
            declared.add(ts_const)
    assert declared == set(_PROVIDER_ID_TO_TS_CONST.values()), (
        f"missing TS-side vendor declarations: "
        f"{set(_PROVIDER_ID_TO_TS_CONST.values()) - declared}"
    )


def test_ts_twin_freezes_all_catalog_collections():
    """Provenance grep — TS twin must Object.freeze the outer arrays
    + map. Mirrors the Python `MappingProxyType` invariant for
    `VENDORS`. Loosely matches but each Object.freeze call is
    semantically necessary."""
    ts_src = _read_ts_twin()
    # ALL_VENDORS / ALL_VENDOR_IDS / VENDORS each call Object.freeze
    # at the export site. Three matches expected at minimum.
    freeze_count = len(re.findall(r"Object\.freeze\(", ts_src))
    assert freeze_count >= 3, (
        f"expected >=3 Object.freeze() calls (ALL_VENDORS / "
        f"ALL_VENDOR_IDS / VENDORS), found {freeze_count}"
    )
