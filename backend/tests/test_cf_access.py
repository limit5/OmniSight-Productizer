"""W14.4 — Contract tests for :mod:`backend.cf_access`.

Pinned: pure helpers (application-name / domain / payload builders;
JWT decode + alignment), :class:`CFAccessConfig` validation, the
:class:`HttpxCFAccessClient` HTTP shape (via ``httpx.MockTransport``),
and :class:`CFAccessManager` lifecycle (create / delete / idempotent /
failure modes).
"""

from __future__ import annotations

import base64
import json
import threading
from types import SimpleNamespace
from typing import Any, Mapping

import httpx
import pytest

from backend import cf_access as ca
from backend.cf_access import (
    CF_ACCESS_SCHEMA_VERSION,
    CF_API_BASE,
    DEFAULT_APP_TYPE,
    DEFAULT_HTTP_TIMEOUT_S,
    DEFAULT_SESSION_DURATION,
    JWT_HEADER_NAME,
    PREVIEW_APP_NAME_PREFIX,
    CFAccessAPIError,
    CFAccessApplicationRecord,
    CFAccessConfig,
    CFAccessError,
    CFAccessManager,
    CFAccessMisconfigured,
    CFAccessNotFound,
    HttpxCFAccessClient,
    build_application_domain,
    build_application_name,
    build_application_payload,
    build_policy_payload,
    compute_effective_emails,
    extract_jwt_claims,
    jwt_claims_align_with_session,
    token_fingerprint,
    validate_email,
    validate_session_duration,
    validate_team_domain,
)


# ──────────────────────────────────────────────────────────────────
#  Module surface
# ──────────────────────────────────────────────────────────────────


EXPECTED_ALL = {
    "CF_ACCESS_SCHEMA_VERSION",
    "CF_API_BASE",
    "DEFAULT_HTTP_TIMEOUT_S",
    "DEFAULT_SESSION_DURATION",
    "PREVIEW_APP_NAME_PREFIX",
    "JWT_HEADER_NAME",
    "DEFAULT_APP_TYPE",
    "CFAccessError",
    "CFAccessAPIError",
    "CFAccessNotFound",
    "CFAccessMisconfigured",
    "CFAccessConfig",
    "CFAccessClient",
    "HttpxCFAccessClient",
    "CFAccessManager",
    "CFAccessApplicationRecord",
    "build_application_name",
    "build_application_domain",
    "build_application_payload",
    "build_policy_payload",
    "compute_effective_emails",
    "validate_email",
    "validate_session_duration",
    "validate_team_domain",
    "extract_jwt_claims",
    "jwt_claims_align_with_session",
    "token_fingerprint",
}


def test_all_exports_match_expected() -> None:
    assert set(ca.__all__) == EXPECTED_ALL


def test_all_exports_unique() -> None:
    assert len(ca.__all__) == len(set(ca.__all__))


def test_schema_version_semver() -> None:
    parts = CF_ACCESS_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_cf_api_base_pinned() -> None:
    # B12 + W14.3 siblings use the same base — drift guard.
    assert CF_API_BASE == "https://api.cloudflare.com/client/v4"


def test_default_timeout_positive() -> None:
    assert DEFAULT_HTTP_TIMEOUT_S > 0


def test_default_session_duration_shape() -> None:
    # CF Access accepts "Ns/Nm/Nh/Nd"; we ship "30m" by default.
    assert DEFAULT_SESSION_DURATION == "30m"


def test_preview_app_name_prefix() -> None:
    # Operators ``cf-access list-apps`` to spot W14 apps; pin the
    # naming so the trail is grep-able.
    assert PREVIEW_APP_NAME_PREFIX == "omnisight-preview-"


def test_jwt_header_name_pinned() -> None:
    assert JWT_HEADER_NAME == "Cf-Access-Jwt-Assertion"


def test_default_app_type_self_hosted() -> None:
    assert DEFAULT_APP_TYPE == "self_hosted"


# ──────────────────────────────────────────────────────────────────
#  Error hierarchy
# ──────────────────────────────────────────────────────────────────


def test_error_hierarchy() -> None:
    assert issubclass(CFAccessAPIError, CFAccessError)
    assert issubclass(CFAccessNotFound, CFAccessError)
    assert issubclass(CFAccessMisconfigured, CFAccessError)
    assert issubclass(CFAccessError, RuntimeError)


def test_api_error_carries_status() -> None:
    exc = CFAccessAPIError("boom", status=502)
    assert exc.status == 502
    assert "boom" in str(exc)


def test_api_error_default_status() -> None:
    exc = CFAccessAPIError("nope")
    assert exc.status == 0


# ──────────────────────────────────────────────────────────────────
#  Validation helpers
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "user@example.com",
        "ops+oncall@team.example.com",
        "first.last@sub.example.io",
        "a.b-c_d@example.com",
    ],
)
def test_validate_email_accepts(value: str) -> None:
    validate_email(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "no-at-sign",
        "@example.com",
        "user@",
        "user@no-tld",
        "user @example.com",
        "user\nuser@example.com",
        " leading@example.com",
        "trailing@example.com ",
    ],
)
def test_validate_email_rejects(value: str) -> None:
    with pytest.raises(CFAccessError):
        validate_email(value)


def test_validate_email_rejects_non_string() -> None:
    with pytest.raises(CFAccessError):
        validate_email(123)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "acme.cloudflareaccess.com",
        "team-x.cloudflareaccess.com",
        "alpha9.cloudflareaccess.com",
    ],
)
def test_validate_team_domain_accepts(value: str) -> None:
    validate_team_domain(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "https://acme.cloudflareaccess.com",
        "acme.cloudflareaccess.com/path",
        "acme.example.com",  # wrong suffix
        " acme.cloudflareaccess.com ",
    ],
)
def test_validate_team_domain_rejects(value: str) -> None:
    with pytest.raises(CFAccessMisconfigured):
        validate_team_domain(value)


@pytest.mark.parametrize("value", ["30s", "5m", "24h", "1d", "120m"])
def test_validate_session_duration_accepts(value: str) -> None:
    validate_session_duration(value)


@pytest.mark.parametrize(
    "value", ["", " ", "30 minutes", "5", "ten m", "30M", "1.5h"]
)
def test_validate_session_duration_rejects(value: str) -> None:
    with pytest.raises(CFAccessMisconfigured):
        validate_session_duration(value)


def test_token_fingerprint_short() -> None:
    assert token_fingerprint("abc") == "****"


def test_token_fingerprint_long_shows_last_four() -> None:
    fp = token_fingerprint("abcdefghijklmnop")
    assert fp.endswith("mnop")
    assert fp.startswith("…")


def test_token_fingerprint_non_string() -> None:
    assert token_fingerprint(None) == "****"  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────
#  Application name / domain / payload builders
# ──────────────────────────────────────────────────────────────────


def test_build_application_name_happy() -> None:
    name = build_application_name("ws-deadbeef")
    assert name == "omnisight-preview-ws-deadbeef"


def test_build_application_name_rejects_bad_sandbox() -> None:
    with pytest.raises(CFAccessError):
        build_application_name("not-a-sandbox-id")


def test_build_application_domain_happy() -> None:
    domain = build_application_domain("ws-deadbeef", "ai.sora-dev.app")
    # MUST agree byte-for-byte with cf_ingress.build_ingress_hostname.
    assert domain == "preview-ws-deadbeef.ai.sora-dev.app"


def test_build_application_domain_rejects_bad_sandbox() -> None:
    with pytest.raises(CFAccessError):
        build_application_domain("nope", "ai.sora-dev.app")


def test_build_application_domain_rejects_bad_host() -> None:
    with pytest.raises(CFAccessMisconfigured):
        build_application_domain("ws-deadbeef", "not-a-host")


def test_build_application_domain_matches_cf_ingress_byte_for_byte() -> None:
    # Drift guard — the W14.3 ingress hostname and the W14.4 access
    # domain MUST be identical, otherwise an Access app gates a
    # hostname that has no tunnel route (or vice versa).
    from backend.cf_ingress import build_ingress_hostname

    a = build_application_domain("ws-deadbeef", "ai.sora-dev.app")
    b = build_ingress_hostname("ws-deadbeef", "ai.sora-dev.app")
    assert a == b


# ──────────────────────────────────────────────────────────────────
#  compute_effective_emails
# ──────────────────────────────────────────────────────────────────


def test_compute_effective_emails_happy() -> None:
    out = compute_effective_emails(["a@x.com"], defaults=["b@x.com"])
    assert out == ("a@x.com", "b@x.com")


def test_compute_effective_emails_dedups_case_insensitive() -> None:
    out = compute_effective_emails(["A@x.com"], defaults=["a@x.com"])
    assert len(out) == 1


def test_compute_effective_emails_strips_whitespace() -> None:
    out = compute_effective_emails(["  a@x.com  "], defaults=[" b@x.com "])
    assert out == ("a@x.com", "b@x.com")


def test_compute_effective_emails_skips_blank_entries() -> None:
    out = compute_effective_emails(["", " ", "a@x.com"], defaults=[])
    assert out == ("a@x.com",)


def test_compute_effective_emails_rejects_csv_string() -> None:
    with pytest.raises(CFAccessError):
        compute_effective_emails("a@x.com,b@x.com")  # type: ignore[arg-type]


def test_compute_effective_emails_rejects_non_string_entry() -> None:
    with pytest.raises(CFAccessError):
        compute_effective_emails([123, "a@x.com"])  # type: ignore[list-item]


def test_compute_effective_emails_rejects_invalid_email() -> None:
    with pytest.raises(CFAccessError):
        compute_effective_emails(["nope"], defaults=[])


def test_compute_effective_emails_returns_empty_when_all_blank() -> None:
    out = compute_effective_emails([], defaults=[])
    assert out == ()


def test_compute_effective_emails_sorted() -> None:
    # Sort means two workers building the same policy POST identical
    # bytes — CF's dedup catches the loser cleanly.
    out = compute_effective_emails(["c@x.com", "a@x.com"], defaults=["b@x.com"])
    assert out == ("a@x.com", "b@x.com", "c@x.com")


# ──────────────────────────────────────────────────────────────────
#  build_policy_payload
# ──────────────────────────────────────────────────────────────────


def test_build_policy_payload_happy() -> None:
    p = build_policy_payload(name="ops", emails=["a@x.com", "b@x.com"])
    assert p["name"] == "ops"
    assert p["decision"] == "allow"
    assert p["precedence"] == 1
    assert p["include"] == [
        {"email": {"email": "a@x.com"}},
        {"email": {"email": "b@x.com"}},
    ]


def test_build_policy_payload_rejects_empty_emails() -> None:
    with pytest.raises(CFAccessError):
        build_policy_payload(name="ops", emails=[])


def test_build_policy_payload_rejects_empty_name() -> None:
    with pytest.raises(CFAccessError):
        build_policy_payload(name="", emails=["a@x.com"])


def test_build_policy_payload_rejects_bad_decision() -> None:
    with pytest.raises(CFAccessError):
        build_policy_payload(name="ops", emails=["a@x.com"], decision="huh")


def test_build_policy_payload_rejects_bad_precedence() -> None:
    with pytest.raises(CFAccessError):
        build_policy_payload(name="ops", emails=["a@x.com"], precedence=0)


def test_build_policy_payload_rejects_invalid_email() -> None:
    with pytest.raises(CFAccessError):
        build_policy_payload(name="ops", emails=["not-an-email"])


# ──────────────────────────────────────────────────────────────────
#  build_application_payload
# ──────────────────────────────────────────────────────────────────


def test_build_application_payload_happy() -> None:
    payload = build_application_payload(
        name="omnisight-preview-ws-deadbeef",
        domain="preview-ws-deadbeef.ai.sora-dev.app",
        emails=["a@x.com"],
    )
    assert payload["name"] == "omnisight-preview-ws-deadbeef"
    assert payload["domain"] == "preview-ws-deadbeef.ai.sora-dev.app"
    assert payload["type"] == "self_hosted"
    assert payload["session_duration"] == DEFAULT_SESSION_DURATION
    assert payload["auto_redirect_to_identity"] is True
    assert payload["app_launcher_visible"] is False
    assert len(payload["policies"]) == 1
    assert payload["policies"][0]["include"] == [{"email": {"email": "a@x.com"}}]


def test_build_application_payload_custom_session_duration() -> None:
    payload = build_application_payload(
        name="x", domain="x.example.com", emails=["a@x.com"], session_duration="2h"
    )
    assert payload["session_duration"] == "2h"


def test_build_application_payload_rejects_bad_session_duration() -> None:
    with pytest.raises(CFAccessMisconfigured):
        build_application_payload(
            name="x",
            domain="x.example.com",
            emails=["a@x.com"],
            session_duration="not a duration",
        )


def test_build_application_payload_rejects_bad_app_type() -> None:
    with pytest.raises(CFAccessError):
        build_application_payload(
            name="x",
            domain="x.example.com",
            emails=["a@x.com"],
            app_type="weird",
        )


def test_build_application_payload_rejects_empty_name() -> None:
    with pytest.raises(CFAccessError):
        build_application_payload(name="", domain="x.example.com", emails=["a@x.com"])


def test_build_application_payload_rejects_empty_domain() -> None:
    with pytest.raises(CFAccessError):
        build_application_payload(name="x", domain="", emails=["a@x.com"])


def test_build_application_payload_rejects_extra_with_conflict() -> None:
    with pytest.raises(CFAccessError):
        build_application_payload(
            name="x",
            domain="x.example.com",
            emails=["a@x.com"],
            extra={"name": "tampered"},
        )


def test_build_application_payload_accepts_extra_non_conflict() -> None:
    payload = build_application_payload(
        name="x",
        domain="x.example.com",
        emails=["a@x.com"],
        extra={"logo_url": "https://example.com/logo.png"},
    )
    assert payload["logo_url"] == "https://example.com/logo.png"


# ──────────────────────────────────────────────────────────────────
#  JWT helpers
# ──────────────────────────────────────────────────────────────────


def _jwt(claims: Mapping[str, Any]) -> str:
    """Build a fake CF Access JWT (header.payload.signature) — signature
    is bogus because :func:`extract_jwt_claims` does not verify it."""

    def _seg(d: Mapping[str, Any]) -> str:
        raw = json.dumps(d, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _seg({"alg": "RS256", "typ": "JWT"})
    payload = _seg(claims)
    return f"{header}.{payload}.bogus-signature"


def test_extract_jwt_claims_happy() -> None:
    token = _jwt({"email": "a@x.com", "aud": ["abc123"], "iss": "https://x"})
    claims = extract_jwt_claims(token)
    assert claims["email"] == "a@x.com"
    assert claims["aud"] == ["abc123"]


def test_extract_jwt_claims_rejects_empty() -> None:
    with pytest.raises(CFAccessError):
        extract_jwt_claims("")


def test_extract_jwt_claims_rejects_non_string() -> None:
    with pytest.raises(CFAccessError):
        extract_jwt_claims(None)  # type: ignore[arg-type]


def test_extract_jwt_claims_rejects_two_segments() -> None:
    with pytest.raises(CFAccessError):
        extract_jwt_claims("hdr.payload")


def test_extract_jwt_claims_rejects_bad_base64() -> None:
    with pytest.raises(CFAccessError):
        extract_jwt_claims("hdr.@@@.sig")


def test_extract_jwt_claims_rejects_non_json_payload() -> None:
    payload_b64 = base64.urlsafe_b64encode(b"not json").rstrip(b"=").decode()
    with pytest.raises(CFAccessError):
        extract_jwt_claims(f"hdr.{payload_b64}.sig")


def test_extract_jwt_claims_rejects_non_object_payload() -> None:
    payload_b64 = (
        base64.urlsafe_b64encode(b'"a string"').rstrip(b"=").decode("ascii")
    )
    with pytest.raises(CFAccessError):
        extract_jwt_claims(f"hdr.{payload_b64}.sig")


def test_jwt_claims_align_with_session_happy() -> None:
    claims = {"email": "a@x.com", "aud": ["abc"], "iss": "https://team.x"}
    assert jwt_claims_align_with_session(
        claims,
        session_email="a@x.com",
        expected_aud="abc",
        expected_iss="https://team.x",
    )


def test_jwt_claims_align_with_session_case_insensitive_email() -> None:
    claims = {"email": "A@X.com"}
    assert jwt_claims_align_with_session(claims, session_email="a@x.com")


def test_jwt_claims_align_with_session_rejects_mismatched_email() -> None:
    claims = {"email": "b@x.com"}
    assert not jwt_claims_align_with_session(claims, session_email="a@x.com")


def test_jwt_claims_align_with_session_rejects_missing_email() -> None:
    assert not jwt_claims_align_with_session({}, session_email="a@x.com")


def test_jwt_claims_align_with_session_rejects_non_mapping() -> None:
    assert not jwt_claims_align_with_session(
        "not a mapping",  # type: ignore[arg-type]
        session_email="a@x.com",
    )


def test_jwt_claims_align_with_session_rejects_empty_session_email() -> None:
    assert not jwt_claims_align_with_session({"email": "a@x.com"}, session_email="")


def test_jwt_claims_align_with_session_aud_in_list() -> None:
    claims = {"email": "a@x.com", "aud": ["abc", "xyz"]}
    assert jwt_claims_align_with_session(
        claims, session_email="a@x.com", expected_aud="xyz"
    )


def test_jwt_claims_align_with_session_aud_string() -> None:
    claims = {"email": "a@x.com", "aud": "abc"}
    assert jwt_claims_align_with_session(
        claims, session_email="a@x.com", expected_aud="abc"
    )


def test_jwt_claims_align_with_session_aud_missing() -> None:
    claims = {"email": "a@x.com"}
    assert not jwt_claims_align_with_session(
        claims, session_email="a@x.com", expected_aud="abc"
    )


def test_jwt_claims_align_with_session_aud_wrong() -> None:
    claims = {"email": "a@x.com", "aud": ["xyz"]}
    assert not jwt_claims_align_with_session(
        claims, session_email="a@x.com", expected_aud="abc"
    )


def test_jwt_claims_align_with_session_iss_match() -> None:
    claims = {"email": "a@x.com", "iss": "https://team.x"}
    assert jwt_claims_align_with_session(
        claims, session_email="a@x.com", expected_iss="https://team.x"
    )


def test_jwt_claims_align_with_session_iss_mismatch() -> None:
    claims = {"email": "a@x.com", "iss": "https://other.x"}
    assert not jwt_claims_align_with_session(
        claims, session_email="a@x.com", expected_iss="https://team.x"
    )


# ──────────────────────────────────────────────────────────────────
#  CFAccessConfig
# ──────────────────────────────────────────────────────────────────


def _ok_config_kwargs() -> dict[str, Any]:
    return {
        "tunnel_host": "ai.sora-dev.app",
        "api_token": "deadbeef" * 8,
        "account_id": "0" * 32,
        "team_domain": "acme.cloudflareaccess.com",
    }


def test_cfaccess_config_happy_path() -> None:
    c = CFAccessConfig(**_ok_config_kwargs())
    assert c.session_duration == DEFAULT_SESSION_DURATION
    assert c.auto_redirect_to_identity is True
    assert c.aud_tag == ""
    assert c.default_emails == ()
    assert c.issuer_url == "https://acme.cloudflareaccess.com"


def test_cfaccess_config_to_dict_redacts_token() -> None:
    c = CFAccessConfig(**_ok_config_kwargs())
    d = c.to_dict()
    assert d["api_token_fingerprint"].startswith("…")
    assert "api_token" not in d
    assert d["schema_version"] == CF_ACCESS_SCHEMA_VERSION
    assert d["issuer_url"] == "https://acme.cloudflareaccess.com"


def test_cfaccess_config_rejects_empty_token() -> None:
    kwargs = _ok_config_kwargs()
    kwargs["api_token"] = ""
    with pytest.raises(CFAccessMisconfigured):
        CFAccessConfig(**kwargs)


def test_cfaccess_config_rejects_zero_timeout() -> None:
    kwargs = _ok_config_kwargs()
    with pytest.raises(CFAccessMisconfigured):
        CFAccessConfig(**kwargs, http_timeout_s=0)


def test_cfaccess_config_rejects_bad_session_duration() -> None:
    kwargs = _ok_config_kwargs()
    with pytest.raises(CFAccessMisconfigured):
        CFAccessConfig(**kwargs, session_duration="thirty minutes")


def test_cfaccess_config_rejects_bad_team_domain() -> None:
    kwargs = _ok_config_kwargs()
    kwargs["team_domain"] = "acme.example.com"
    with pytest.raises(CFAccessMisconfigured):
        CFAccessConfig(**kwargs)


def test_cfaccess_config_rejects_bad_account_id() -> None:
    kwargs = _ok_config_kwargs()
    kwargs["account_id"] = "not-a-uuid"
    with pytest.raises(CFAccessMisconfigured):
        CFAccessConfig(**kwargs)


def test_cfaccess_config_rejects_csv_default_emails() -> None:
    kwargs = _ok_config_kwargs()
    with pytest.raises(CFAccessMisconfigured):
        CFAccessConfig(**kwargs, default_emails="a@x.com,b@x.com")  # type: ignore[arg-type]


def test_cfaccess_config_rejects_invalid_default_email() -> None:
    kwargs = _ok_config_kwargs()
    with pytest.raises(CFAccessError):
        CFAccessConfig(**kwargs, default_emails=("not-an-email",))


def test_cfaccess_config_dedups_default_emails() -> None:
    c = CFAccessConfig(
        **_ok_config_kwargs(), default_emails=("A@X.com", "a@x.com", "b@x.com")
    )
    # Case-insensitive dedup.
    assert len(c.default_emails) == 2


def test_cfaccess_config_default_emails_strips_whitespace() -> None:
    c = CFAccessConfig(
        **_ok_config_kwargs(), default_emails=(" a@x.com ", "", "b@x.com")
    )
    assert "a@x.com" in c.default_emails
    assert "b@x.com" in c.default_emails


def test_cfaccess_config_from_settings_happy() -> None:
    settings = SimpleNamespace(
        tunnel_host="ai.sora-dev.app",
        cf_api_token="ghi" * 4,
        cf_account_id="a" * 32,
        cf_access_team_domain="acme.cloudflareaccess.com",
        cf_access_default_emails="ops@x.com, oncall@x.com",
        cf_access_session_duration="2h",
        cf_access_aud_tag="abc",
    )
    c = CFAccessConfig.from_settings(settings)
    assert c.tunnel_host == "ai.sora-dev.app"
    assert c.session_duration == "2h"
    assert c.aud_tag == "abc"
    assert "ops@x.com" in c.default_emails
    assert "oncall@x.com" in c.default_emails


@pytest.mark.parametrize(
    "drop",
    [
        "tunnel_host",
        "cf_api_token",
        "cf_account_id",
        "cf_access_team_domain",
    ],
)
def test_cfaccess_config_from_settings_rejects_partial(drop: str) -> None:
    base = {
        "tunnel_host": "ai.sora-dev.app",
        "cf_api_token": "ghi" * 4,
        "cf_account_id": "a" * 32,
        "cf_access_team_domain": "acme.cloudflareaccess.com",
        "cf_access_default_emails": "",
        "cf_access_session_duration": "",
        "cf_access_aud_tag": "",
    }
    base[drop] = ""
    settings = SimpleNamespace(**base)
    with pytest.raises(CFAccessMisconfigured):
        CFAccessConfig.from_settings(settings)


def test_cfaccess_config_from_settings_uses_default_session_duration() -> None:
    settings = SimpleNamespace(
        tunnel_host="ai.sora-dev.app",
        cf_api_token="ghi" * 4,
        cf_account_id="a" * 32,
        cf_access_team_domain="acme.cloudflareaccess.com",
        cf_access_default_emails="",
        cf_access_session_duration="",
        cf_access_aud_tag="",
    )
    c = CFAccessConfig.from_settings(settings)
    assert c.session_duration == DEFAULT_SESSION_DURATION


def test_cfaccess_config_frozen() -> None:
    c = CFAccessConfig(**_ok_config_kwargs())
    with pytest.raises(Exception):
        c.tunnel_host = "tampered"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────
#  HttpxCFAccessClient (uses httpx.MockTransport)
# ──────────────────────────────────────────────────────────────────


def _api_path(config: CFAccessConfig) -> str:
    return f"/accounts/{config.account_id}/access/apps"


def test_httpx_client_stores_config_reference() -> None:
    config = CFAccessConfig(**_ok_config_kwargs())
    client = HttpxCFAccessClient(config)
    assert client.config is config


def test_httpx_client_rejects_non_config() -> None:
    with pytest.raises(TypeError):
        HttpxCFAccessClient("not a config")  # type: ignore[arg-type]


def test_httpx_client_list_happy(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith(_api_path(config))
        assert request.headers["Authorization"] == f"Bearer {config.api_token}"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {"id": "app-1", "name": "omnisight-preview-ws-1234abcd"},
                    {"id": "app-2", "name": "other-app"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    out = client.list_applications()
    assert len(out) == 2
    assert out[0]["id"] == "app-1"


def test_httpx_client_list_returns_empty_when_null_result(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": None})

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    assert client.list_applications() == []


def test_httpx_client_list_raises_on_4xx(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"message": "missing scope: access edit"}],
            },
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    with pytest.raises(CFAccessAPIError) as excinfo:
        client.list_applications()
    assert excinfo.value.status == 403
    assert "missing scope" in str(excinfo.value)


def test_httpx_client_create_happy(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"success": True, "result": {"id": "app-deadbeef", "name": "x"}},
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    out = client.create_application({"name": "x", "domain": "x.example.com"})
    assert captured["body"]["name"] == "x"
    assert out["id"] == "app-deadbeef"


def test_httpx_client_create_rejects_non_mapping() -> None:
    config = CFAccessConfig(**_ok_config_kwargs())
    client = HttpxCFAccessClient(config)
    with pytest.raises(TypeError):
        client.create_application("not a mapping")  # type: ignore[arg-type]


def test_httpx_client_create_raises_on_4xx(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"success": False, "errors": [{"message": "domain in use"}]},
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    with pytest.raises(CFAccessAPIError):
        client.create_application({"name": "x", "domain": "x.example.com"})


def test_httpx_client_create_raises_when_missing_result(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": None})

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    with pytest.raises(CFAccessAPIError):
        client.create_application({"name": "x", "domain": "x.example.com"})


def test_httpx_client_delete_happy(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        captured["path"] = request.url.path
        return httpx.Response(
            200, json={"success": True, "result": {"id": "app-deadbeef"}}
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    out = client.delete_application("app-deadbeef")
    assert captured["path"].endswith("/access/apps/app-deadbeef")
    assert out["id"] == "app-deadbeef"


def test_httpx_client_delete_returns_empty_when_result_null(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": None})

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    assert client.delete_application("app-deadbeef") == {}


def test_httpx_client_delete_rejects_empty_app_id() -> None:
    config = CFAccessConfig(**_ok_config_kwargs())
    client = HttpxCFAccessClient(config)
    with pytest.raises(CFAccessError):
        client.delete_application("")


def test_httpx_client_delete_raises_on_4xx_non_404(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"success": False, "errors": [{"message": "bad id"}]}
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    with pytest.raises(CFAccessAPIError):
        client.delete_application("app-deadbeef")


def test_httpx_client_get_raises_on_non_json_body(monkeypatch) -> None:
    config = CFAccessConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFAccessClient(config)
    with pytest.raises(CFAccessAPIError):
        client.list_applications()


# ──────────────────────────────────────────────────────────────────
#  CFAccessManager — fakes
# ──────────────────────────────────────────────────────────────────


class FakeCFAccessClient:
    """Test fake — records all CRUD calls, lets the test pre-seed the
    apps list, and emulates failure modes via raise_on_* hooks."""

    def __init__(self, apps: list[Mapping[str, Any]] | None = None) -> None:
        self._apps: list[dict[str, Any]] = [dict(a) for a in (apps or [])]
        self.list_calls: int = 0
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []
        self.raise_on_list: Exception | None = None
        self.raise_on_create: Exception | None = None
        self.raise_on_delete: Exception | None = None
        self._next_id = 100

    def list_applications(self) -> list[dict[str, Any]]:
        if self.raise_on_list is not None:
            raise self.raise_on_list
        self.list_calls += 1
        return [dict(a) for a in self._apps]

    def create_application(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        self.create_calls.append(dict(payload))
        app_id = f"app-{self._next_id:06d}"
        self._next_id += 1
        record = {"id": app_id, "name": payload.get("name"), "domain": payload.get("domain")}
        self._apps.append(record)
        return record

    def delete_application(self, app_id: str) -> dict[str, Any]:
        if self.raise_on_delete is not None:
            raise self.raise_on_delete
        self.delete_calls.append(app_id)
        before = len(self._apps)
        self._apps = [a for a in self._apps if a.get("id") != app_id]
        if len(self._apps) == before:
            return {}
        return {"id": app_id}

    def current_apps(self) -> list[dict[str, Any]]:
        return [dict(a) for a in self._apps]


def _make_manager(
    *, apps: list[Mapping[str, Any]] | None = None
) -> tuple[CFAccessManager, FakeCFAccessClient]:
    config = CFAccessConfig(**_ok_config_kwargs())
    client = FakeCFAccessClient(apps=apps)
    return CFAccessManager(config=config, client=client), client


# ──────────────────────────────────────────────────────────────────
#  CFAccessManager — construction
# ──────────────────────────────────────────────────────────────────


def test_manager_rejects_non_config() -> None:
    with pytest.raises(TypeError):
        CFAccessManager(config="not a config")  # type: ignore[arg-type]


def test_manager_uses_default_httpx_client_when_none() -> None:
    config = CFAccessConfig(**_ok_config_kwargs())
    manager = CFAccessManager(config=config)
    assert isinstance(manager.client, HttpxCFAccessClient)


def test_manager_exposes_config_property() -> None:
    manager, _ = _make_manager()
    assert isinstance(manager.config, CFAccessConfig)


def test_manager_exposes_client_property() -> None:
    manager, fake = _make_manager()
    assert manager.client is fake


# ──────────────────────────────────────────────────────────────────
#  CFAccessManager — create_application
# ──────────────────────────────────────────────────────────────────


def test_manager_create_app_happy() -> None:
    manager, fake = _make_manager()
    record = manager.create_application(
        sandbox_id="ws-deadbeef", emails=["a@x.com"]
    )
    assert isinstance(record, CFAccessApplicationRecord)
    assert record.sandbox_id == "ws-deadbeef"
    assert record.app_id.startswith("app-")
    assert record.name == "omnisight-preview-ws-deadbeef"
    assert record.domain == "preview-ws-deadbeef.ai.sora-dev.app"
    assert record.emails == ("a@x.com",)
    assert fake.list_calls == 1
    assert len(fake.create_calls) == 1
    body = fake.create_calls[0]
    assert body["name"] == "omnisight-preview-ws-deadbeef"
    assert body["domain"] == "preview-ws-deadbeef.ai.sora-dev.app"
    assert body["type"] == "self_hosted"
    assert body["session_duration"] == DEFAULT_SESSION_DURATION
    assert body["policies"][0]["include"] == [{"email": {"email": "a@x.com"}}]


def test_manager_create_app_unions_default_emails() -> None:
    config = CFAccessConfig(
        **_ok_config_kwargs(), default_emails=("admin@x.com",)
    )
    fake = FakeCFAccessClient()
    manager = CFAccessManager(config=config, client=fake)
    record = manager.create_application(
        sandbox_id="ws-deadbeef", emails=["op@x.com"]
    )
    # Both emails on the policy.
    assert "admin@x.com" in record.emails
    assert "op@x.com" in record.emails


def test_manager_create_app_rejects_empty_emails() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFAccessError):
        manager.create_application(sandbox_id="ws-deadbeef", emails=[])


def test_manager_create_app_idempotent_when_exists() -> None:
    seed = [
        {"id": "app-existing", "name": "omnisight-preview-ws-deadbeef"},
    ]
    manager, fake = _make_manager(apps=seed)
    record = manager.create_application(
        sandbox_id="ws-deadbeef", emails=["a@x.com"]
    )
    # No POST — the existing app was reused.
    assert fake.create_calls == []
    assert record.app_id == "app-existing"


def test_manager_create_app_records_in_local_cache() -> None:
    manager, _ = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    record = manager.get_application("ws-deadbeef")
    assert record is not None
    assert record.name == "omnisight-preview-ws-deadbeef"


def test_manager_create_app_propagates_list_error() -> None:
    manager, fake = _make_manager()
    fake.raise_on_list = CFAccessAPIError("upstream 502", status=502)
    with pytest.raises(CFAccessAPIError):
        manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])


def test_manager_create_app_propagates_create_error() -> None:
    manager, fake = _make_manager()
    fake.raise_on_create = CFAccessAPIError("upstream 500", status=500)
    with pytest.raises(CFAccessAPIError):
        manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])


def test_manager_create_app_rejects_invalid_sandbox_id() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFAccessError):
        manager.create_application(sandbox_id="nope", emails=["a@x.com"])


def test_manager_create_app_rejects_invalid_email() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFAccessError):
        manager.create_application(sandbox_id="ws-deadbeef", emails=["nope"])


# ──────────────────────────────────────────────────────────────────
#  CFAccessManager — delete_application
# ──────────────────────────────────────────────────────────────────


def test_manager_delete_app_happy() -> None:
    manager, fake = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    fake.delete_calls.clear()

    removed = manager.delete_application("ws-deadbeef")
    assert removed is True
    assert len(fake.delete_calls) == 1
    apps = fake.current_apps()
    assert all(a.get("name") != "omnisight-preview-ws-deadbeef" for a in apps)


def test_manager_delete_app_idempotent_when_absent() -> None:
    manager, fake = _make_manager()
    removed = manager.delete_application("ws-deadbeef")
    assert removed is False
    assert fake.delete_calls == []


def test_manager_delete_app_recovers_after_cache_miss() -> None:
    """If the cache is empty (worker restart) but the CF account still
    has the app, delete_application should look it up by name and
    delete it."""

    seed = [
        {"id": "app-orphan", "name": "omnisight-preview-ws-deadbeef"},
    ]
    manager, fake = _make_manager(apps=seed)
    removed = manager.delete_application("ws-deadbeef")
    assert removed is True
    assert "app-orphan" in fake.delete_calls


def test_manager_delete_app_clears_local_cache() -> None:
    manager, _ = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    manager.delete_application("ws-deadbeef")
    assert manager.get_application("ws-deadbeef") is None


def test_manager_delete_app_404_treated_as_already_gone() -> None:
    manager, fake = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    fake.raise_on_delete = CFAccessAPIError("not found", status=404)
    removed = manager.delete_application("ws-deadbeef")
    assert removed is False
    assert manager.get_application("ws-deadbeef") is None


def test_manager_delete_app_propagates_non_404_error() -> None:
    manager, fake = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    fake.raise_on_delete = CFAccessAPIError("upstream 502", status=502)
    with pytest.raises(CFAccessAPIError):
        manager.delete_application("ws-deadbeef")


def test_manager_delete_app_rejects_invalid_sandbox_id() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFAccessError):
        manager.delete_application("nope")


# ──────────────────────────────────────────────────────────────────
#  CFAccessManager — list / snapshot / public_url / cleanup
# ──────────────────────────────────────────────────────────────────


def test_manager_list_apps_empty() -> None:
    manager, _ = _make_manager()
    assert manager.list_applications() == ()


def test_manager_list_apps_two() -> None:
    manager, _ = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    manager.create_application(sandbox_id="ws-1234abcd", emails=["b@x.com"])
    assert len(manager.list_applications()) == 2


def test_manager_public_url_for() -> None:
    manager, _ = _make_manager()
    url = manager.public_url_for("ws-deadbeef")
    assert url == "https://preview-ws-deadbeef.ai.sora-dev.app"


def test_manager_public_url_for_rejects_invalid() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFAccessError):
        manager.public_url_for("nope")


def test_manager_snapshot_shape() -> None:
    manager, _ = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    snap = manager.snapshot()
    assert snap["schema_version"] == CF_ACCESS_SCHEMA_VERSION
    assert snap["count"] == 1
    assert snap["applications"][0]["sandbox_id"] == "ws-deadbeef"


def test_manager_cleanup_removes_all() -> None:
    manager, fake = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    manager.create_application(sandbox_id="ws-1234abcd", emails=["a@x.com"])
    removed = manager.cleanup()
    assert removed == 2
    assert manager.list_applications() == ()


def test_manager_cleanup_idempotent() -> None:
    manager, _ = _make_manager()
    assert manager.cleanup() == 0


def test_manager_cleanup_swallows_individual_errors() -> None:
    manager, fake = _make_manager()
    manager.create_application(sandbox_id="ws-deadbeef", emails=["a@x.com"])
    fake.raise_on_delete = CFAccessAPIError("flaky", status=502)
    # Cleanup must not raise even when individual deletes fail.
    removed = manager.cleanup()
    assert removed == 0


# ──────────────────────────────────────────────────────────────────
#  Concurrency
# ──────────────────────────────────────────────────────────────────


def test_manager_concurrent_creates_distinct_sandboxes() -> None:
    """16 worker threads create 16 distinct sandboxes — no corruption,
    all 16 records are tracked."""

    manager, _ = _make_manager()
    sandbox_ids = [f"ws-{i:06x}aaa" for i in range(16)]
    errors: list[Exception] = []
    barrier = threading.Barrier(len(sandbox_ids))

    def create(sid: str) -> None:
        try:
            barrier.wait(timeout=5)
            manager.create_application(sandbox_id=sid, emails=["a@x.com"])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=create, args=(s,)) for s in sandbox_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(manager.list_applications()) == 16


def test_manager_concurrent_create_then_delete_round_trip() -> None:
    """8 worker threads each create + delete their own sandbox — final
    state is empty."""

    manager, _ = _make_manager()
    sandbox_ids = [f"ws-{i:06x}bbb" for i in range(8)]
    errors: list[Exception] = []
    barrier = threading.Barrier(len(sandbox_ids))

    def round_trip(sid: str) -> None:
        try:
            barrier.wait(timeout=5)
            manager.create_application(sandbox_id=sid, emails=["a@x.com"])
            manager.delete_application(sid)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=round_trip, args=(s,)) for s in sandbox_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert manager.list_applications() == ()


# ──────────────────────────────────────────────────────────────────
#  Cross-worker contract
# ──────────────────────────────────────────────────────────────────


def test_eight_workers_compute_same_application_name() -> None:
    """SOP §1 cross-worker contract: same input → same output, no
    matter which uvicorn worker runs the helper."""

    results = [build_application_name("ws-deadbeef") for _ in range(8)]
    assert len(set(results)) == 1


def test_eight_workers_compute_same_application_domain() -> None:
    results = [
        build_application_domain("ws-deadbeef", "ai.sora-dev.app")
        for _ in range(8)
    ]
    assert len(set(results)) == 1
