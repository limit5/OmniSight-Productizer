"""KS.3.11/KS.3.14 -- Tier 3 BYOG proxy fail-fast contract tests."""

from __future__ import annotations

import httpx
import pytest

from backend.byog_proxy_client import (
    BYOGProxyRejected,
    BYOGProxyTarget,
    BYOGProxyUnavailable,
    build_proxy_llm_url,
    forward_llm_request_via_proxy,
)


def test_build_proxy_llm_url_routes_to_customer_proxy() -> None:
    target = BYOGProxyTarget(
        proxy_url="https://proxy.customer.example.com/base",
        tenant_id="tenant-a",
    )

    url = build_proxy_llm_url(
        target,
        "openai",
        "/v1/chat/completions",
        query={"stream": "true"},
    )

    assert (
        url
        == "https://proxy.customer.example.com/base/v1/llm/openai/v1/chat/completions?stream=true"
    )


@pytest.mark.asyncio
async def test_proxy_transport_error_fails_fast_without_direct_provider_fallback(
    monkeypatch,
) -> None:
    def _explode_direct_provider_call(*_args, **_kwargs):  # noqa: ANN002
        raise AssertionError("direct provider fallback must not be called")

    monkeypatch.setattr("backend.agents.llm.get_llm", _explode_direct_provider_call)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def client_factory(**kwargs) -> httpx.AsyncClient:  # noqa: ANN003
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    target = BYOGProxyTarget(
        proxy_url="https://proxy.customer.example.com",
        tenant_id="tenant-a",
        nonce_hmac_key="0123456789abcdef0123456789abcdef",
    )

    with pytest.raises(BYOGProxyUnavailable, match="mTLS handshake failed"):
        await forward_llm_request_via_proxy(
            target,
            "openai",
            "/v1/chat/completions",
            content=b"{}",
            client_factory=client_factory,
        )


@pytest.mark.asyncio
async def test_proxy_unreachable_closes_gracefully_without_response_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("proxy network unreachable", request=request)

    def client_factory(**kwargs) -> httpx.AsyncClient:  # noqa: ANN003
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    target = BYOGProxyTarget(
        proxy_url="https://proxy.customer.example.com",
        tenant_id="tenant-a",
        nonce_hmac_key="0123456789abcdef0123456789abcdef",
    )

    with pytest.raises(BYOGProxyUnavailable) as excinfo:
        await forward_llm_request_via_proxy(
            target,
            "openai",
            "/v1/chat/completions",
            content=b'{"stream":true}',
            client_factory=client_factory,
        )

    assert "mTLS handshake failed" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


@pytest.mark.asyncio
async def test_proxy_auth_rejection_fails_fast_without_direct_provider_fallback(
    monkeypatch,
) -> None:
    def _explode_direct_provider_call(*_args, **_kwargs):  # noqa: ANN002
        raise AssertionError("direct provider fallback must not be called")

    monkeypatch.setattr("backend.agents.llm.get_llm", _explode_direct_provider_call)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="client certificate pin mismatch")

    def client_factory(**kwargs) -> httpx.AsyncClient:  # noqa: ANN003
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    target = BYOGProxyTarget(
        proxy_url="https://proxy.customer.example.com",
        tenant_id="tenant-a",
        nonce_hmac_key="0123456789abcdef0123456789abcdef",
    )

    with pytest.raises(BYOGProxyRejected, match="authentication rejected"):
        await forward_llm_request_via_proxy(
            target,
            "openai",
            "/v1/chat/completions",
            content=b"{}",
            client_factory=client_factory,
        )


@pytest.mark.asyncio
async def test_successful_proxy_call_preserves_signed_zero_trust_boundary() -> None:
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["tenant"] = request.headers["X-Omnisight-Tenant-Id"]
        seen["nonce"] = request.headers["X-Omnisight-Nonce"]
        seen["signature"] = request.headers["X-Omnisight-Signature"]
        return httpx.Response(200, json={"ok": True})

    def client_factory(**kwargs) -> httpx.AsyncClient:  # noqa: ANN003
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    target = BYOGProxyTarget(
        proxy_url="https://proxy.customer.example.com",
        tenant_id="tenant-a",
        nonce_hmac_key="0123456789abcdef0123456789abcdef",
    )

    response = await forward_llm_request_via_proxy(
        target,
        "openai",
        "/v1/chat/completions",
        content=b"{}",
        client_factory=client_factory,
    )

    assert response.status_code == 200
    assert seen["url"] == (
        "https://proxy.customer.example.com/v1/llm/openai/v1/chat/completions"
    )
    assert seen["tenant"] == "tenant-a"
    assert seen["nonce"]
    assert seen["signature"].startswith("sha256=")


@pytest.mark.asyncio
async def test_mtls_client_cert_and_ca_are_passed_to_httpx() -> None:
    factory_kwargs: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": str(request.url)})

    def client_factory(**kwargs) -> httpx.AsyncClient:  # noqa: ANN003
        factory_kwargs.update(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    target = BYOGProxyTarget(
        proxy_url="https://proxy.customer.example.com",
        tenant_id="tenant-a",
        client_cert_file="/run/omnisight/client.crt",
        client_key_file="/run/omnisight/client.key",
        client_ca_file="/run/omnisight/proxy-ca.crt",
        nonce_hmac_key="0123456789abcdef0123456789abcdef",
    )

    response = await forward_llm_request_via_proxy(
        target,
        "openai",
        "/v1/chat/completions",
        content=b"{}",
        client_factory=client_factory,
    )

    assert response.status_code == 200
    assert factory_kwargs["cert"] == (
        "/run/omnisight/client.crt",
        "/run/omnisight/client.key",
    )
    assert factory_kwargs["verify"] == "/run/omnisight/proxy-ca.crt"


@pytest.mark.asyncio
async def test_signed_nonce_changes_per_request_under_fixed_timestamp(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []
    nonces = iter(["nonce-a", "nonce-b"])
    monkeypatch.setattr("backend.byog_proxy_client.secrets.token_urlsafe", lambda _n: next(nonces))
    monkeypatch.setattr("backend.byog_proxy_client.time.time", lambda: 1_700_000_000)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((
            request.headers["X-Omnisight-Nonce"],
            request.headers["X-Omnisight-Signature"],
        ))
        return httpx.Response(200, json={"ok": True})

    def client_factory(**kwargs) -> httpx.AsyncClient:  # noqa: ANN003
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    target = BYOGProxyTarget(
        proxy_url="https://proxy.customer.example.com",
        tenant_id="tenant-a",
        nonce_hmac_key="0123456789abcdef0123456789abcdef",
    )

    for _ in range(2):
        response = await forward_llm_request_via_proxy(
            target,
            "openai",
            "/v1/chat/completions",
            content=b"{}",
            client_factory=client_factory,
        )
        assert response.status_code == 200

    assert seen[0][0] == "nonce-a"
    assert seen[1][0] == "nonce-b"
    assert seen[0][1] != seen[1][1]
