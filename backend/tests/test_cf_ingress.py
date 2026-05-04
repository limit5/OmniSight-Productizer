"""W14.3 — Contract tests for :mod:`backend.cf_ingress`.

Pinned: pure helpers (hostname / service-url builder; ingress-rule
add/remove splice), :class:`CFIngressConfig` validation, the
:class:`HttpxCFIngressClient` HTTP shape (via ``httpx.MockTransport``),
and :class:`CFIngressManager` lifecycle (create / delete / idempotent /
failure modes).
"""

from __future__ import annotations

import json
import threading
from typing import Any, Mapping
from types import SimpleNamespace

import httpx
import pytest

from backend import cf_ingress as ci
from backend.cf_ingress import (
    CF_INGRESS_SCHEMA_VERSION,
    CF_API_BASE,
    DEFAULT_HTTP_TIMEOUT_S,
    DEFAULT_INGRESS_FALLBACK,
    PREVIEW_HOSTNAME_PREFIX,
    CFIngressAPIError,
    CFIngressConfig,
    CFIngressError,
    CFIngressManager,
    CFIngressMisconfigured,
    CFIngressNotFound,
    HttpxCFIngressClient,
    build_ingress_hostname,
    build_ingress_service_url,
    compute_ingress_rules_add,
    compute_ingress_rules_remove,
    extract_fallback_rule,
    find_ingress_rule,
    is_fallback_rule,
    token_fingerprint,
    validate_account_id,
    validate_sandbox_id,
    validate_tunnel_host,
    validate_tunnel_id,
)


# ──────────────────────────────────────────────────────────────────
#  Module surface
# ──────────────────────────────────────────────────────────────────


EXPECTED_ALL = {
    "CF_INGRESS_SCHEMA_VERSION",
    "CF_API_BASE",
    "DEFAULT_HTTP_TIMEOUT_S",
    "PREVIEW_HOSTNAME_PREFIX",
    "DEFAULT_INGRESS_FALLBACK",
    "DEFAULT_HMR_ORIGIN_REQUEST",
    "CFIngressError",
    "CFIngressAPIError",
    "CFIngressNotFound",
    "CFIngressMisconfigured",
    "CFIngressConfig",
    "CFIngressClient",
    "HttpxCFIngressClient",
    "CFIngressManager",
    "build_ingress_hostname",
    "build_ingress_service_url",
    "build_hmr_origin_request",
    "compute_ingress_rules_add",
    "compute_ingress_rules_remove",
    "find_ingress_rule",
    "extract_fallback_rule",
    "is_fallback_rule",
    "validate_tunnel_host",
    "validate_account_id",
    "validate_tunnel_id",
    "validate_sandbox_id",
    "token_fingerprint",
}


def test_all_exports_match_expected() -> None:
    assert set(ci.__all__) == EXPECTED_ALL


def test_all_exports_unique() -> None:
    assert len(ci.__all__) == len(set(ci.__all__))


def test_schema_version_semver() -> None:
    parts = CF_INGRESS_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_cf_api_base_pinned() -> None:
    # B12 cloudflare_client uses the same base — drift guard.
    assert CF_API_BASE == "https://api.cloudflare.com/client/v4"


def test_default_timeout_positive() -> None:
    assert DEFAULT_HTTP_TIMEOUT_S > 0


def test_preview_hostname_prefix() -> None:
    # The W14 epic header pins this naming scheme.
    assert PREVIEW_HOSTNAME_PREFIX == "preview-"


def test_default_fallback_shape() -> None:
    assert DEFAULT_INGRESS_FALLBACK["service"] == "http_status:404"
    assert "hostname" not in DEFAULT_INGRESS_FALLBACK


def test_default_fallback_immutable() -> None:
    with pytest.raises(TypeError):
        DEFAULT_INGRESS_FALLBACK["service"] = "tampered"  # type: ignore[index]


# ──────────────────────────────────────────────────────────────────
#  Error hierarchy
# ──────────────────────────────────────────────────────────────────


def test_error_hierarchy() -> None:
    assert issubclass(CFIngressAPIError, CFIngressError)
    assert issubclass(CFIngressNotFound, CFIngressError)
    assert issubclass(CFIngressMisconfigured, CFIngressError)
    assert issubclass(CFIngressError, RuntimeError)


def test_api_error_carries_status() -> None:
    exc = CFIngressAPIError("boom", status=502)
    assert exc.status == 502
    assert "boom" in str(exc)


def test_api_error_default_status() -> None:
    exc = CFIngressAPIError("nope")
    assert exc.status == 0


# ──────────────────────────────────────────────────────────────────
#  Validation helpers
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "ai.sora-dev.app",
        "preview.example.com",
        "sub.tenant.platform.io",
    ],
)
def test_validate_tunnel_host_accepts(value: str) -> None:
    validate_tunnel_host(value)  # no raise


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "no-dot",
        ".leading.dot",
        "trailing.dot.",
        " whitespace.dom ",
        "https://prefix.example.com",
        "with/slash.example.com",
        "with space.example.com",
    ],
)
def test_validate_tunnel_host_rejects(value: str) -> None:
    with pytest.raises(CFIngressMisconfigured):
        validate_tunnel_host(value)


def test_validate_tunnel_host_rejects_non_string() -> None:
    with pytest.raises(CFIngressMisconfigured):
        validate_tunnel_host(123)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "0123456789abcdef0123456789abcdef",
        "abcdef0123456789abcdef0123456789",
    ],
)
def test_validate_account_id_accepts(value: str) -> None:
    validate_account_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        "deadbeef" * 5,
        "uppercase-ABC0123456789ABC0123456789AB",
        "X" * 32,  # non-hex
    ],
)
def test_validate_account_id_rejects(value: str) -> None:
    with pytest.raises(CFIngressMisconfigured):
        validate_account_id(value)


def test_validate_tunnel_id_accepts() -> None:
    validate_tunnel_id("0123456789abcdef0123456789abcdef")


def test_validate_tunnel_id_rejects_empty() -> None:
    with pytest.raises(CFIngressMisconfigured):
        validate_tunnel_id("")


@pytest.mark.parametrize(
    "value",
    [
        "ws-deadbeef",
        "ws-46c080f82adb",
        "ws-0123456789ab",
    ],
)
def test_validate_sandbox_id_accepts(value: str) -> None:
    validate_sandbox_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "deadbeef",  # missing prefix
        "ws-",  # too short
        "ws-abc",  # 3 chars, below 6 floor
        "ws-DEADBEEF",  # uppercase
        "ws-../etc",  # traversal attempt
        "ws-foo bar",  # whitespace
        "ws-zzz123",  # non-hex
    ],
)
def test_validate_sandbox_id_rejects(value: str) -> None:
    with pytest.raises(CFIngressError):
        validate_sandbox_id(value)


def test_validate_sandbox_id_rejects_non_string() -> None:
    with pytest.raises(CFIngressError):
        validate_sandbox_id(None)  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────
#  Token fingerprint
# ──────────────────────────────────────────────────────────────────


def test_token_fingerprint_short_token_is_redacted() -> None:
    assert token_fingerprint("short") == "****"


def test_token_fingerprint_long_token_shows_last4() -> None:
    assert token_fingerprint("eyJhbGciOiJIUzI1NiwidGV4dCJ9.AB12") == "…AB12"


def test_token_fingerprint_non_string() -> None:
    assert token_fingerprint(None) == "****"  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────
#  build_ingress_hostname / build_ingress_service_url
# ──────────────────────────────────────────────────────────────────


def test_build_ingress_hostname_happy_path() -> None:
    h = build_ingress_hostname("ws-deadbeef", "ai.sora-dev.app")
    assert h == "preview-ws-deadbeef.ai.sora-dev.app"


def test_build_ingress_hostname_within_dns_label_cap() -> None:
    h = build_ingress_hostname("ws-46c080f82adb", "ai.sora-dev.app")
    # Each label must be ≤63 chars.
    for label in h.split("."):
        assert len(label) <= 63


def test_build_ingress_hostname_rejects_bad_sandbox_id() -> None:
    with pytest.raises(CFIngressError):
        build_ingress_hostname("not-a-ws-id", "ai.sora-dev.app")


def test_build_ingress_hostname_rejects_bad_tunnel_host() -> None:
    with pytest.raises(CFIngressMisconfigured):
        build_ingress_hostname("ws-deadbeef", "")


def test_build_ingress_service_url_happy_path() -> None:
    assert build_ingress_service_url(41001) == "http://127.0.0.1:41001"


def test_build_ingress_service_url_custom_host() -> None:
    assert (
        build_ingress_service_url(41001, host="host.docker.internal")
        == "http://host.docker.internal:41001"
    )


@pytest.mark.parametrize("port", [0, -1, 65536, 999_999])
def test_build_ingress_service_url_rejects_bad_port(port: int) -> None:
    with pytest.raises(CFIngressError):
        build_ingress_service_url(port)


def test_build_ingress_service_url_rejects_empty_host() -> None:
    with pytest.raises(CFIngressError):
        build_ingress_service_url(41001, host="")


def test_build_ingress_service_url_rejects_non_int() -> None:
    with pytest.raises(CFIngressError):
        build_ingress_service_url("41001")  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────
#  is_fallback_rule / extract_fallback_rule / find_ingress_rule
# ──────────────────────────────────────────────────────────────────


def test_is_fallback_rule_default() -> None:
    assert is_fallback_rule({"service": "http_status:404"})


def test_is_fallback_rule_with_path() -> None:
    # Some operators add path-based fallbacks.
    assert is_fallback_rule({"service": "http_status:503"})


def test_is_fallback_rule_rejects_with_hostname() -> None:
    assert not is_fallback_rule(
        {"hostname": "x.example.com", "service": "http://localhost:80"}
    )


def test_is_fallback_rule_rejects_non_mapping() -> None:
    assert not is_fallback_rule("not a mapping")  # type: ignore[arg-type]


def test_is_fallback_rule_rejects_empty_dict() -> None:
    assert not is_fallback_rule({})


def test_extract_fallback_rule_picks_last_when_valid() -> None:
    rules = [
        {"hostname": "a.example.com", "service": "http://localhost:1"},
        {"service": "http_status:503"},
    ]
    assert extract_fallback_rule(rules)["service"] == "http_status:503"


def test_extract_fallback_rule_synthesises_when_missing() -> None:
    rules = [
        {"hostname": "a.example.com", "service": "http://localhost:1"},
    ]
    fb = extract_fallback_rule(rules)
    assert fb is DEFAULT_INGRESS_FALLBACK


def test_extract_fallback_rule_synthesises_when_empty() -> None:
    assert extract_fallback_rule([]) is DEFAULT_INGRESS_FALLBACK


def test_find_ingress_rule_match() -> None:
    rules = [
        {"hostname": "a.example.com", "service": "http://1"},
        {"hostname": "b.example.com", "service": "http://2"},
    ]
    found = find_ingress_rule(rules, "b.example.com")
    assert found is not None and found["service"] == "http://2"


def test_find_ingress_rule_no_match() -> None:
    rules = [{"hostname": "a.example.com", "service": "http://1"}]
    assert find_ingress_rule(rules, "z.example.com") is None


def test_find_ingress_rule_skips_non_mapping() -> None:
    rules = [
        "garbage",  # type: ignore[list-item]
        {"hostname": "good.example.com", "service": "http://1"},
    ]
    found = find_ingress_rule(rules, "good.example.com")  # type: ignore[arg-type]
    assert found is not None


# ──────────────────────────────────────────────────────────────────
#  compute_ingress_rules_add
# ──────────────────────────────────────────────────────────────────


def test_compute_add_into_empty_list() -> None:
    out = compute_ingress_rules_add(
        [], hostname="preview-ws-1.ai.sora-dev.app", service_url="http://127.0.0.1:41001"
    )
    assert len(out) == 2  # new rule + default fallback
    assert out[0] == {
        "hostname": "preview-ws-1.ai.sora-dev.app",
        "service": "http://127.0.0.1:41001",
    }
    assert out[1]["service"] == "http_status:404"
    assert "hostname" not in out[1]


def test_compute_add_preserves_existing_fallback() -> None:
    existing = [{"service": "http_status:503"}]
    out = compute_ingress_rules_add(
        existing, hostname="preview-ws-1.x.com", service_url="http://127.0.0.1:41001"
    )
    assert out[-1]["service"] == "http_status:503"


def test_compute_add_preserves_unrelated_rules() -> None:
    existing = [
        {"hostname": "ai.sora-dev.app", "service": "http://caddy:8080"},
        {"service": "http_status:404"},
    ]
    out = compute_ingress_rules_add(
        existing,
        hostname="preview-ws-1.ai.sora-dev.app",
        service_url="http://127.0.0.1:41001",
    )
    # Unrelated rule still present, new rule before fallback
    hostnames = [r.get("hostname") for r in out]
    assert "ai.sora-dev.app" in hostnames
    assert "preview-ws-1.ai.sora-dev.app" in hostnames
    assert out[-1].get("hostname") is None  # fallback last


def test_compute_add_idempotent_same_target() -> None:
    existing = [
        {"hostname": "preview-ws-1.x.com", "service": "http://127.0.0.1:41001"},
        {"service": "http_status:404"},
    ]
    out = compute_ingress_rules_add(
        existing,
        hostname="preview-ws-1.x.com",
        service_url="http://127.0.0.1:41001",
    )
    # Still exactly one rule + fallback (no duplicate)
    rules_for_h = [r for r in out if r.get("hostname") == "preview-ws-1.x.com"]
    assert len(rules_for_h) == 1


def test_compute_add_replaces_when_target_drifts() -> None:
    existing = [
        {"hostname": "preview-ws-1.x.com", "service": "http://127.0.0.1:41001"},
        {"service": "http_status:404"},
    ]
    out = compute_ingress_rules_add(
        existing,
        hostname="preview-ws-1.x.com",
        service_url="http://127.0.0.1:42999",  # new port
    )
    rules_for_h = [r for r in out if r.get("hostname") == "preview-ws-1.x.com"]
    assert len(rules_for_h) == 1
    assert rules_for_h[0]["service"] == "http://127.0.0.1:42999"


def test_compute_add_returns_deep_copy() -> None:
    rule = {"hostname": "a.x.com", "service": "http://1"}
    existing: list[Mapping[str, Any]] = [rule, {"service": "http_status:404"}]
    out = compute_ingress_rules_add(
        existing, hostname="b.x.com", service_url="http://2"
    )
    # Mutating the returned list does not affect the input.
    out[0]["service"] = "tampered"
    assert rule["service"] == "http://1"


def test_compute_add_skips_non_mapping_entries() -> None:
    existing = [
        "garbage",  # type: ignore[list-item]
        {"hostname": "a.x.com", "service": "http://1"},
        {"service": "http_status:404"},
    ]
    out = compute_ingress_rules_add(
        existing, hostname="b.x.com", service_url="http://2"  # type: ignore[arg-type]
    )
    # The string is dropped; rules + new + fallback
    assert all(isinstance(r, dict) for r in out)


def test_compute_add_rejects_empty_hostname() -> None:
    with pytest.raises(CFIngressError):
        compute_ingress_rules_add([], hostname="", service_url="http://1")


def test_compute_add_rejects_empty_service_url() -> None:
    with pytest.raises(CFIngressError):
        compute_ingress_rules_add([], hostname="a.x.com", service_url="")


def test_compute_add_rejects_non_list_existing() -> None:
    with pytest.raises(CFIngressError):
        compute_ingress_rules_add(
            ("not", "a", "list"),  # type: ignore[arg-type]
            hostname="a.x.com",
            service_url="http://1",
        )


# ──────────────────────────────────────────────────────────────────
#  compute_ingress_rules_remove
# ──────────────────────────────────────────────────────────────────


def test_compute_remove_happy_path() -> None:
    existing = [
        {"hostname": "a.x.com", "service": "http://1"},
        {"hostname": "b.x.com", "service": "http://2"},
        {"service": "http_status:404"},
    ]
    out = compute_ingress_rules_remove(existing, hostname="a.x.com")
    assert all(r.get("hostname") != "a.x.com" for r in out)
    assert out[-1]["service"] == "http_status:404"


def test_compute_remove_idempotent_when_absent() -> None:
    existing = [
        {"hostname": "a.x.com", "service": "http://1"},
        {"service": "http_status:404"},
    ]
    out = compute_ingress_rules_remove(existing, hostname="z.x.com")
    # Still has a.x.com + fallback, length 2
    assert len(out) == 2
    assert out[0]["hostname"] == "a.x.com"


def test_compute_remove_preserves_fallback() -> None:
    existing = [
        {"hostname": "a.x.com", "service": "http://1"},
        {"service": "http_status:503"},
    ]
    out = compute_ingress_rules_remove(existing, hostname="a.x.com")
    assert out[-1]["service"] == "http_status:503"


def test_compute_remove_dedups_duplicate_rules() -> None:
    # Some operators may have duplicate rules — both should disappear.
    existing = [
        {"hostname": "a.x.com", "service": "http://1"},
        {"hostname": "a.x.com", "service": "http://2"},
        {"service": "http_status:404"},
    ]
    out = compute_ingress_rules_remove(existing, hostname="a.x.com")
    assert all(r.get("hostname") != "a.x.com" for r in out)


def test_compute_remove_rejects_empty_hostname() -> None:
    with pytest.raises(CFIngressError):
        compute_ingress_rules_remove([], hostname="")


def test_compute_remove_rejects_non_list() -> None:
    with pytest.raises(CFIngressError):
        compute_ingress_rules_remove(
            "not a list",  # type: ignore[arg-type]
            hostname="a.x.com",
        )


# ──────────────────────────────────────────────────────────────────
#  CFIngressConfig
# ──────────────────────────────────────────────────────────────────


def _ok_config_kwargs() -> dict[str, str]:
    return {
        "tunnel_host": "ai.sora-dev.app",
        "api_token": "deadbeef" * 8,  # ≥ 8 chars
        "account_id": "0" * 32,
        "tunnel_id": "1" * 32,
    }


def test_cfingress_config_happy_path() -> None:
    c = CFIngressConfig(**_ok_config_kwargs())
    assert c.service_host == "127.0.0.1"
    assert c.http_timeout_s == DEFAULT_HTTP_TIMEOUT_S


def test_cfingress_config_to_dict_redacts_token() -> None:
    c = CFIngressConfig(**_ok_config_kwargs())
    d = c.to_dict()
    assert d["api_token_fingerprint"].startswith("…")
    assert "api_token" not in d
    assert d["schema_version"] == CF_INGRESS_SCHEMA_VERSION


def test_cfingress_config_rejects_empty_token() -> None:
    kwargs = _ok_config_kwargs()
    kwargs["api_token"] = ""
    with pytest.raises(CFIngressMisconfigured):
        CFIngressConfig(**kwargs)


def test_cfingress_config_rejects_zero_timeout() -> None:
    kwargs = _ok_config_kwargs()
    with pytest.raises(CFIngressMisconfigured):
        CFIngressConfig(**kwargs, http_timeout_s=0)


def test_cfingress_config_rejects_empty_service_host() -> None:
    kwargs = _ok_config_kwargs()
    with pytest.raises(CFIngressMisconfigured):
        CFIngressConfig(**kwargs, service_host="")


def test_cfingress_config_from_settings_happy_path() -> None:
    settings = SimpleNamespace(
        tunnel_host="ai.sora-dev.app",
        cf_api_token="ghi" * 4,
        cf_account_id="a" * 32,
        cf_tunnel_id="b" * 32,
    )
    c = CFIngressConfig.from_settings(settings)
    assert c.tunnel_host == "ai.sora-dev.app"
    assert c.account_id == "a" * 32


@pytest.mark.parametrize(
    "drop",
    ["tunnel_host", "cf_api_token", "cf_account_id", "cf_tunnel_id"],
)
def test_cfingress_config_from_settings_rejects_partial(drop: str) -> None:
    base = {
        "tunnel_host": "ai.sora-dev.app",
        "cf_api_token": "ghi" * 4,
        "cf_account_id": "a" * 32,
        "cf_tunnel_id": "b" * 32,
    }
    base[drop] = ""
    settings = SimpleNamespace(**base)
    with pytest.raises(CFIngressMisconfigured) as excinfo:
        CFIngressConfig.from_settings(settings)
    # Error message names the missing knob.
    upper = drop.upper().replace("CF_", "CF_") if drop != "tunnel_host" else "TUNNEL_HOST"
    assert "OMNISIGHT_" in str(excinfo.value)


def test_cfingress_config_from_settings_strips_whitespace() -> None:
    settings = SimpleNamespace(
        tunnel_host="  ai.sora-dev.app  ",
        cf_api_token="  ghi" + "ghi" * 3 + "  ",
        cf_account_id="  " + "a" * 32 + "  ",
        cf_tunnel_id="  " + "b" * 32 + "  ",
    )
    c = CFIngressConfig.from_settings(settings)
    assert c.tunnel_host == "ai.sora-dev.app"


def test_cfingress_config_frozen() -> None:
    c = CFIngressConfig(**_ok_config_kwargs())
    with pytest.raises(Exception):
        c.tunnel_host = "tampered"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────
#  HttpxCFIngressClient (uses httpx.MockTransport)
# ──────────────────────────────────────────────────────────────────


class _RecordingTransport(httpx.MockTransport):
    """httpx MockTransport that records requests for assertions."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx.Request] = []

        def wrapping_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(wrapping_handler)


def _api_path(config: CFIngressConfig) -> str:
    return (
        f"/accounts/{config.account_id}"
        f"/cfd_tunnel/{config.tunnel_id}/configurations"
    )


def _make_client(handler) -> tuple[HttpxCFIngressClient, CFIngressConfig, _RecordingTransport]:
    """Build an HttpxCFIngressClient backed by a MockTransport."""

    config = CFIngressConfig(**_ok_config_kwargs())
    transport = _RecordingTransport(handler)

    # Monkey-patch httpx.Client to use our transport whenever instantiated.
    client = HttpxCFIngressClient(config)
    return client, config, transport


def test_httpx_client_stores_config_reference() -> None:
    config = CFIngressConfig(**_ok_config_kwargs())
    client = HttpxCFIngressClient(config)
    assert client.config is config


def test_httpx_client_rejects_non_config() -> None:
    with pytest.raises(TypeError):
        HttpxCFIngressClient("not a config")  # type: ignore[arg-type]


def test_httpx_client_get_happy_path(monkeypatch) -> None:
    config = CFIngressConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith(_api_path(config))
        assert request.url.path.startswith("/client/v4")
        assert request.headers["Authorization"] == f"Bearer {config.api_token}"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "config": {
                        "ingress": [
                            {"hostname": "a.x.com", "service": "http://1"},
                            {"service": "http_status:404"},
                        ]
                    }
                },
            },
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFIngressClient(config)
    out = client.get_tunnel_config()
    assert isinstance(out, dict)
    assert out["ingress"][0]["hostname"] == "a.x.com"


def test_httpx_client_get_returns_empty_when_unprovisioned(monkeypatch) -> None:
    config = CFIngressConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": None})

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFIngressClient(config)
    out = client.get_tunnel_config()
    assert out == {"ingress": []}


def test_httpx_client_get_returns_empty_when_no_config_field(monkeypatch) -> None:
    config = CFIngressConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": {"foo": "bar"}})

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFIngressClient(config)
    assert client.get_tunnel_config() == {"ingress": []}


def test_httpx_client_get_raises_on_4xx(monkeypatch) -> None:
    config = CFIngressConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"message": "missing scope: tunnel edit"}],
            },
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFIngressClient(config)
    with pytest.raises(CFIngressAPIError) as excinfo:
        client.get_tunnel_config()
    assert excinfo.value.status == 403
    assert "missing scope" in str(excinfo.value)


def test_httpx_client_get_raises_on_500_no_json(monkeypatch) -> None:
    config = CFIngressConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream gateway broke")

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFIngressClient(config)
    with pytest.raises(CFIngressAPIError) as excinfo:
        client.get_tunnel_config()
    assert excinfo.value.status == 500


def test_httpx_client_put_happy_path(monkeypatch) -> None:
    config = CFIngressConfig(**_ok_config_kwargs())
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(200, json={"success": True, "result": {"applied": True}})

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFIngressClient(config)
    payload = {"ingress": [{"service": "http_status:404"}]}
    out = client.put_tunnel_config(payload)
    assert captured["body"] == {"config": payload}
    assert out["result"]["applied"] is True


def test_httpx_client_put_rejects_non_mapping() -> None:
    config = CFIngressConfig(**_ok_config_kwargs())
    client = HttpxCFIngressClient(config)
    with pytest.raises(TypeError):
        client.put_tunnel_config("not a mapping")  # type: ignore[arg-type]


def test_httpx_client_put_raises_on_4xx(monkeypatch) -> None:
    config = CFIngressConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"success": False, "errors": [{"message": "bad ingress shape"}]}
        )

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFIngressClient(config)
    with pytest.raises(CFIngressAPIError):
        client.put_tunnel_config({"ingress": []})


def test_httpx_client_get_raises_on_non_json_body(monkeypatch) -> None:
    config = CFIngressConfig(**_ok_config_kwargs())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    transport = httpx.MockTransport(handler)
    _real_Client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _real_Client(transport=transport, **kw)
    )
    client = HttpxCFIngressClient(config)
    with pytest.raises(CFIngressAPIError):
        client.get_tunnel_config()


# ──────────────────────────────────────────────────────────────────
#  CFIngressManager — fakes
# ──────────────────────────────────────────────────────────────────


class FakeCFIngressClient:
    """Test fake — records all calls, lets the test pre-seed the
    tunnel state, and emulates failure modes via raise_on_get /
    raise_on_put hooks."""

    def __init__(self, ingress: list[Mapping[str, Any]] | None = None) -> None:
        self._state: dict[str, Any] = {"ingress": list(ingress or [])}
        self.gets: int = 0
        self.puts: list[dict[str, Any]] = []
        self.raise_on_get: Exception | None = None
        self.raise_on_put: Exception | None = None

    def get_tunnel_config(self) -> dict[str, Any]:
        if self.raise_on_get is not None:
            raise self.raise_on_get
        self.gets += 1
        return {"ingress": [dict(r) for r in self._state["ingress"]]}

    def put_tunnel_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        if self.raise_on_put is not None:
            raise self.raise_on_put
        self.puts.append({k: v for k, v in dict(config).items()})
        self._state["ingress"] = [dict(r) for r in config.get("ingress", [])]
        return {"success": True}

    def current_ingress(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._state["ingress"]]


def _make_manager(
    *, ingress: list[Mapping[str, Any]] | None = None
) -> tuple[CFIngressManager, FakeCFIngressClient]:
    config = CFIngressConfig(**_ok_config_kwargs())
    client = FakeCFIngressClient(ingress=ingress)
    return CFIngressManager(config=config, client=client), client


# ──────────────────────────────────────────────────────────────────
#  CFIngressManager — construction
# ──────────────────────────────────────────────────────────────────


def test_manager_rejects_non_config() -> None:
    with pytest.raises(TypeError):
        CFIngressManager(config="not a config")  # type: ignore[arg-type]


def test_manager_uses_default_httpx_client_when_none() -> None:
    config = CFIngressConfig(**_ok_config_kwargs())
    manager = CFIngressManager(config=config)
    assert isinstance(manager.client, HttpxCFIngressClient)


def test_manager_exposes_config_property() -> None:
    manager, _ = _make_manager()
    assert isinstance(manager.config, CFIngressConfig)


def test_manager_exposes_client_property() -> None:
    manager, fake = _make_manager()
    assert manager.client is fake


# ──────────────────────────────────────────────────────────────────
#  CFIngressManager — create_rule
# ──────────────────────────────────────────────────────────────────


def test_manager_create_rule_happy_path() -> None:
    manager, fake = _make_manager()
    url = manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    assert url == "https://preview-ws-deadbeef.ai.sora-dev.app"
    assert fake.gets == 1
    assert len(fake.puts) == 1
    rules = fake.current_ingress()
    assert any(
        r.get("hostname") == "preview-ws-deadbeef.ai.sora-dev.app" for r in rules
    )
    assert rules[-1]["service"] == "http_status:404"


def test_manager_create_rule_idempotent_no_put_when_already_present() -> None:
    # W14.7: "already in target shape" now includes the HMR-friendly
    # originRequest block. Seeding a rule without originRequest is the
    # legacy → upgraded migration case; that path is covered by
    # test_manager_create_rule_upgrades_legacy_rule_missing_origin_request.
    seed = [
        {
            "hostname": "preview-ws-1234abcd.ai.sora-dev.app",
            "service": "http://127.0.0.1:41001",
            "originRequest": dict(ci.DEFAULT_HMR_ORIGIN_REQUEST),
        },
        {"service": "http_status:404"},
    ]
    manager, fake = _make_manager(ingress=seed)
    url = manager.create_rule(sandbox_id="ws-1234abcd", host_port=41001)
    assert url == "https://preview-ws-1234abcd.ai.sora-dev.app"
    # GET was called, but no PUT (already in target shape).
    assert fake.gets == 1
    assert fake.puts == []


def test_manager_create_rule_replaces_when_port_drifts() -> None:
    seed = [
        {
            "hostname": "preview-ws-deadbeef.ai.sora-dev.app",
            "service": "http://127.0.0.1:41001",
        },
        {"service": "http_status:404"},
    ]
    manager, fake = _make_manager(ingress=seed)
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=42999)
    assert len(fake.puts) == 1
    rules = fake.current_ingress()
    target = next(
        r
        for r in rules
        if r.get("hostname") == "preview-ws-deadbeef.ai.sora-dev.app"
    )
    assert target["service"] == "http://127.0.0.1:42999"


def test_manager_create_rule_records_in_local_cache() -> None:
    manager, _ = _make_manager()
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    record = manager.get_rule("ws-deadbeef")
    assert record is not None
    assert record.hostname == "preview-ws-deadbeef.ai.sora-dev.app"
    assert record.service_url == "http://127.0.0.1:41001"


def test_manager_create_rule_propagates_get_api_error() -> None:
    manager, fake = _make_manager()
    fake.raise_on_get = CFIngressAPIError("upstream 502", status=502)
    with pytest.raises(CFIngressAPIError):
        manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)


def test_manager_create_rule_propagates_put_api_error() -> None:
    manager, fake = _make_manager()
    fake.raise_on_put = CFIngressAPIError("upstream 500", status=500)
    with pytest.raises(CFIngressAPIError):
        manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)


def test_manager_create_rule_rejects_invalid_sandbox_id() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFIngressError):
        manager.create_rule(sandbox_id="nope", host_port=41001)


def test_manager_create_rule_rejects_invalid_host_port() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFIngressError):
        manager.create_rule(sandbox_id="ws-deadbeef", host_port=999_999)


def test_manager_create_rule_preserves_unrelated_rules() -> None:
    seed = [
        {"hostname": "ai.sora-dev.app", "service": "http://caddy:8080"},
        {"hostname": "api.sora-dev.app", "service": "http://api:8000"},
        {"service": "http_status:404"},
    ]
    manager, fake = _make_manager(ingress=seed)
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    rules = fake.current_ingress()
    hostnames = {r.get("hostname") for r in rules}
    assert "ai.sora-dev.app" in hostnames
    assert "api.sora-dev.app" in hostnames
    assert "preview-ws-deadbeef.ai.sora-dev.app" in hostnames


# ──────────────────────────────────────────────────────────────────
#  CFIngressManager — delete_rule
# ──────────────────────────────────────────────────────────────────


def test_manager_delete_rule_happy_path() -> None:
    manager, fake = _make_manager()
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    fake.puts.clear()  # reset
    fake.gets = 0

    removed = manager.delete_rule("ws-deadbeef")
    assert removed is True
    assert fake.gets == 1
    assert len(fake.puts) == 1
    rules = fake.current_ingress()
    assert all(
        r.get("hostname") != "preview-ws-deadbeef.ai.sora-dev.app" for r in rules
    )


def test_manager_delete_rule_idempotent_when_absent() -> None:
    manager, fake = _make_manager()
    removed = manager.delete_rule("ws-deadbeef")
    assert removed is False
    # GET happened, PUT did not (nothing to splice).
    assert fake.gets == 1
    assert fake.puts == []


def test_manager_delete_rule_clears_local_cache() -> None:
    manager, _ = _make_manager()
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    manager.delete_rule("ws-deadbeef")
    assert manager.get_rule("ws-deadbeef") is None


def test_manager_delete_rule_clears_cache_even_when_absent() -> None:
    # If the rule was already cleaned up server-side but the cache
    # still has a stale record, delete_rule should clear it.
    manager, fake = _make_manager()
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    # External cleanup: drop the rule on the server side.
    fake._state["ingress"] = [{"service": "http_status:404"}]
    removed = manager.delete_rule("ws-deadbeef")
    assert removed is False
    assert manager.get_rule("ws-deadbeef") is None


def test_manager_delete_rule_propagates_get_error() -> None:
    manager, fake = _make_manager()
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    fake.raise_on_get = CFIngressAPIError("upstream 502", status=502)
    with pytest.raises(CFIngressAPIError):
        manager.delete_rule("ws-deadbeef")


def test_manager_delete_rule_rejects_invalid_sandbox_id() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFIngressError):
        manager.delete_rule("nope")


# ──────────────────────────────────────────────────────────────────
#  CFIngressManager — list / snapshot / public_url_for / cleanup
# ──────────────────────────────────────────────────────────────────


def test_manager_list_rules_empty() -> None:
    manager, _ = _make_manager()
    assert manager.list_rules() == ()


def test_manager_list_rules_returns_records() -> None:
    manager, _ = _make_manager()
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    manager.create_rule(sandbox_id="ws-feedface", host_port=41002)
    records = manager.list_rules()
    assert len(records) == 2
    sandboxes = {r.sandbox_id for r in records}
    assert sandboxes == {"ws-deadbeef", "ws-feedface"}


def test_manager_public_url_for_pure() -> None:
    manager, fake = _make_manager()
    url = manager.public_url_for("ws-deadbeef")
    assert url == "https://preview-ws-deadbeef.ai.sora-dev.app"
    # Pure helper — does not GET CF.
    assert fake.gets == 0


def test_manager_public_url_for_rejects_invalid() -> None:
    manager, _ = _make_manager()
    with pytest.raises(CFIngressError):
        manager.public_url_for("nope")


def test_manager_snapshot_shape() -> None:
    manager, _ = _make_manager()
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    snap = manager.snapshot()
    assert snap["schema_version"] == CF_INGRESS_SCHEMA_VERSION
    assert snap["count"] == 1
    assert snap["config"]["api_token_fingerprint"].startswith("…")
    assert snap["rules"][0]["sandbox_id"] == "ws-deadbeef"


def test_manager_cleanup_removes_all() -> None:
    manager, fake = _make_manager()
    manager.create_rule(sandbox_id="ws-aaaaaaaa", host_port=41001)
    manager.create_rule(sandbox_id="ws-bbbbbbbb", host_port=41002)
    fake.puts.clear()

    n = manager.cleanup()
    assert n == 2
    assert manager.list_rules() == ()
    rules = fake.current_ingress()
    # Only the fallback should remain.
    assert len(rules) == 1
    assert rules[0]["service"] == "http_status:404"


def test_manager_cleanup_idempotent() -> None:
    manager, _ = _make_manager()
    manager.create_rule(sandbox_id="ws-aaaaaaaa", host_port=41001)
    manager.cleanup()
    # Second cleanup: nothing to do.
    assert manager.cleanup() == 0


def test_manager_cleanup_swallows_individual_errors() -> None:
    manager, fake = _make_manager()
    manager.create_rule(sandbox_id="ws-aaaaaaaa", host_port=41001)
    manager.create_rule(sandbox_id="ws-bbbbbbbb", host_port=41002)
    # Cause delete_rule to fail on every call.
    fake.raise_on_put = CFIngressAPIError("flaky", status=500)
    n = manager.cleanup()
    # We swallow the per-rule failure but the count is 0 (no successes).
    assert n == 0


# ──────────────────────────────────────────────────────────────────
#  Concurrency
# ──────────────────────────────────────────────────────────────────


def test_manager_concurrent_create_distinct_sandboxes() -> None:
    """Sixteen workers concurrently creating rules for distinct
    sandbox_ids should all succeed without lost mutations."""

    manager, fake = _make_manager()
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            sid = f"ws-{idx:08x}"
            manager.create_rule(sandbox_id=sid, host_port=41000 + idx)
        except Exception as exc:  # pragma: no cover - we assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(manager.list_rules()) == 16
    rules = fake.current_ingress()
    # 16 workspaces + 1 fallback
    assert len(rules) == 17


def test_manager_concurrent_create_then_delete_round_trip() -> None:
    manager, _ = _make_manager()

    def worker(idx: int) -> None:
        sid = f"ws-{idx:08x}"
        manager.create_rule(sandbox_id=sid, host_port=41000 + idx)
        manager.delete_rule(sid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert manager.list_rules() == ()


# ──────────────────────────────────────────────────────────────────
#  Cross-worker state contract
# ──────────────────────────────────────────────────────────────────


def test_eight_workers_compute_same_hostname() -> None:
    """W14.3 cross-worker contract: every uvicorn worker's manager
    must compute the same public hostname for the same sandbox_id —
    that's how the canonical CF state stays unique."""

    hosts = {build_ingress_hostname("ws-deadbeef", "ai.sora-dev.app") for _ in range(8)}
    assert len(hosts) == 1


# ──────────────────────────────────────────────────────────────────
#  W14.7 — HMR-friendly originRequest + WebSocket passthrough
# ──────────────────────────────────────────────────────────────────


def test_w14_7_default_origin_request_pins_required_keys() -> None:
    """Drift guard: the default originRequest must carry the six
    cloudflared knobs the W14.7 spec promises."""

    body = dict(ci.DEFAULT_HMR_ORIGIN_REQUEST)
    assert set(body.keys()) == {
        "connectTimeout",
        "tcpKeepAlive",
        "keepAliveTimeout",
        "keepAliveConnections",
        "noTLSVerify",
        "disableChunkedEncoding",
    }


def test_w14_7_default_origin_request_values() -> None:
    body = dict(ci.DEFAULT_HMR_ORIGIN_REQUEST)
    assert body["connectTimeout"] == "30s"
    assert body["tcpKeepAlive"] == "30s"
    assert body["keepAliveTimeout"] == "900s"
    assert body["keepAliveConnections"] == 100
    assert body["noTLSVerify"] is False
    assert body["disableChunkedEncoding"] is False


def test_w14_7_default_origin_request_is_immutable() -> None:
    """``MappingProxyType`` rejects mutation; this is the contract for
    "callers may read but never mutate the canonical default"."""

    with pytest.raises(TypeError):
        ci.DEFAULT_HMR_ORIGIN_REQUEST["connectTimeout"] = "1s"  # type: ignore[index]


def test_w14_7_build_hmr_origin_request_returns_fresh_copy() -> None:
    a = ci.build_hmr_origin_request()
    b = ci.build_hmr_origin_request()
    assert a == b
    a["connectTimeout"] = "1s"
    # The default constant + the second call must not have been mutated.
    assert ci.DEFAULT_HMR_ORIGIN_REQUEST["connectTimeout"] == "30s"
    assert b["connectTimeout"] == "30s"


def test_w14_7_build_hmr_origin_request_layers_overrides() -> None:
    body = ci.build_hmr_origin_request({"connectTimeout": "45s"})
    assert body["connectTimeout"] == "45s"
    # Untouched keys still carry the defaults.
    assert body["keepAliveTimeout"] == "900s"
    assert body["disableChunkedEncoding"] is False


def test_w14_7_build_hmr_origin_request_rejects_non_mapping() -> None:
    with pytest.raises(CFIngressError):
        ci.build_hmr_origin_request("not a mapping")  # type: ignore[arg-type]


def test_w14_7_build_hmr_origin_request_rejects_empty_key() -> None:
    with pytest.raises(CFIngressError):
        ci.build_hmr_origin_request({"": "30s"})


def test_w14_7_build_hmr_origin_request_rejects_non_scalar_value() -> None:
    with pytest.raises(CFIngressError):
        ci.build_hmr_origin_request({"connectTimeout": ["30s"]})  # type: ignore[dict-item]


def test_w14_7_build_hmr_origin_request_accepts_bool_int_float() -> None:
    body = ci.build_hmr_origin_request(
        {"noTLSVerify": True, "keepAliveConnections": 200, "extra_float": 1.5}
    )
    assert body["noTLSVerify"] is True
    assert body["keepAliveConnections"] == 200
    assert body["extra_float"] == 1.5


def test_w14_7_compute_add_includes_origin_request_when_supplied() -> None:
    out = compute_ingress_rules_add(
        [],
        hostname="preview-ws-deadbeef.x.com",
        service_url="http://127.0.0.1:41001",
        origin_request=ci.build_hmr_origin_request(),
    )
    new_rule = out[0]
    assert "originRequest" in new_rule
    assert new_rule["originRequest"]["disableChunkedEncoding"] is False


def test_w14_7_compute_add_omits_origin_request_when_none() -> None:
    """Backward compat: passing ``origin_request=None`` keeps the
    legacy W14.3 shape (no originRequest field), so callers that have
    not yet upgraded continue to work."""

    out = compute_ingress_rules_add(
        [],
        hostname="preview-ws-deadbeef.x.com",
        service_url="http://127.0.0.1:41001",
        origin_request=None,
    )
    assert "originRequest" not in out[0]


def test_w14_7_compute_add_replaces_when_origin_request_drifts() -> None:
    """If a legacy rule (no originRequest) already exists for the
    hostname, supplying an origin_request triggers a replacement so
    every per-sandbox rule converges on the HMR-friendly shape."""

    existing: list[Mapping[str, Any]] = [
        {
            "hostname": "preview-ws-deadbeef.x.com",
            "service": "http://127.0.0.1:41001",
        },
        {"service": "http_status:404"},
    ]
    out = compute_ingress_rules_add(
        existing,
        hostname="preview-ws-deadbeef.x.com",
        service_url="http://127.0.0.1:41001",
        origin_request=ci.build_hmr_origin_request(),
    )
    target = next(r for r in out if r.get("hostname") == "preview-ws-deadbeef.x.com")
    assert "originRequest" in target


def test_w14_7_compute_add_rejects_non_mapping_origin_request() -> None:
    with pytest.raises(CFIngressError):
        compute_ingress_rules_add(
            [],
            hostname="preview-ws-deadbeef.x.com",
            service_url="http://127.0.0.1:41001",
            origin_request="not a mapping",  # type: ignore[arg-type]
        )


def test_w14_7_manager_create_rule_default_stamps_hmr_origin_request() -> None:
    """Production behaviour: callers who don't pass origin_request get
    the HMR-friendly defaults stamped onto the new rule automatically."""

    manager, fake = _make_manager()
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    rules = fake.current_ingress()
    target = next(
        r for r in rules if r.get("hostname") == "preview-ws-deadbeef.ai.sora-dev.app"
    )
    assert target["originRequest"] == dict(ci.DEFAULT_HMR_ORIGIN_REQUEST)


def test_w14_7_manager_create_rule_caller_overrides_layer_on_top() -> None:
    manager, fake = _make_manager()
    manager.create_rule(
        sandbox_id="ws-deadbeef",
        host_port=41001,
        origin_request={"connectTimeout": "45s"},
    )
    rules = fake.current_ingress()
    target = next(
        r for r in rules if r.get("hostname") == "preview-ws-deadbeef.ai.sora-dev.app"
    )
    assert target["originRequest"]["connectTimeout"] == "45s"
    # Other defaults still present.
    assert target["originRequest"]["keepAliveTimeout"] == "900s"


def test_w14_7_manager_create_rule_empty_dict_disables_origin_request() -> None:
    """Caller may explicitly opt-out by passing an empty dict — the
    rule then has the legacy W14.3 shape (no originRequest). Only
    used for triage of misbehaving connectors."""

    manager, fake = _make_manager()
    manager.create_rule(
        sandbox_id="ws-deadbeef", host_port=41001, origin_request={}
    )
    rules = fake.current_ingress()
    target = next(
        r for r in rules if r.get("hostname") == "preview-ws-deadbeef.ai.sora-dev.app"
    )
    assert "originRequest" not in target


def test_w14_7_manager_create_rule_upgrades_legacy_rule_missing_origin_request() -> None:
    """Migration path: a legacy rule (no originRequest) gets replaced
    with the HMR-friendly shape on the next ``create_rule`` call.
    This is the W14.7 first-rollout behaviour — every restart re-stamps
    rules so an existing fleet converges on WebSocket-friendly defaults
    without requiring an explicit migration script."""

    seed = [
        {
            "hostname": "preview-ws-deadbeef.ai.sora-dev.app",
            "service": "http://127.0.0.1:41001",
        },
        {"service": "http_status:404"},
    ]
    manager, fake = _make_manager(ingress=seed)
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    # PUT must have been issued because the rule needed upgrading.
    assert len(fake.puts) == 1
    rules = fake.current_ingress()
    target = next(
        r for r in rules if r.get("hostname") == "preview-ws-deadbeef.ai.sora-dev.app"
    )
    assert target["originRequest"]["disableChunkedEncoding"] is False


def test_w14_7_manager_create_rule_idempotent_when_origin_request_already_canonical() -> None:
    """Once a rule has the canonical originRequest, a re-launch with
    the same host_port produces no PUT (idempotent)."""

    seed = [
        {
            "hostname": "preview-ws-deadbeef.ai.sora-dev.app",
            "service": "http://127.0.0.1:41001",
            "originRequest": dict(ci.DEFAULT_HMR_ORIGIN_REQUEST),
        },
        {"service": "http_status:404"},
    ]
    manager, fake = _make_manager(ingress=seed)
    manager.create_rule(sandbox_id="ws-deadbeef", host_port=41001)
    assert fake.gets == 1
    assert fake.puts == []


def test_w14_7_eight_workers_stamp_identical_origin_request() -> None:
    """Cross-worker contract: every uvicorn worker stamps a byte-equal
    originRequest body so concurrent re-launches converge on the same
    canonical rule."""

    bodies = [ci.build_hmr_origin_request() for _ in range(8)]
    first = bodies[0]
    assert all(b == first for b in bodies)
