"""KS.3.11 -- fail-fast SaaS client for Tier 3 BYOG proxy forwarding.

Tier 3 tenants choose BYOG so provider keys never leave customer
infrastructure. When the customer proxy is unreachable, mTLS fails, or
proxy auth rejects the caller, the SaaS request must stop at that
boundary. It must not fall back to OmniSight-hosted direct provider
egress.

Module-global state audit: this module defines constants, dataclasses,
and pure helpers only. Each call creates its own ``httpx.AsyncClient``
unless a test injects one, so there is no cross-worker mutable state.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable
from urllib.parse import quote, urlencode, urljoin

import httpx


BYOG_PROXY_FORWARD_PREFIX = "/v1/llm/"
BYOG_PROXY_DEFAULT_TIMEOUT_SECONDS = 60.0


class BYOGProxyError(RuntimeError):
    """Base class for fail-fast BYOG proxy forwarding failures."""


class BYOGProxyUnavailable(BYOGProxyError):
    """Raised when the customer proxy cannot be reached or mTLS fails."""


class BYOGProxyRejected(BYOGProxyError):
    """Raised when the proxy auth envelope rejects the SaaS caller."""


@dataclass(frozen=True)
class BYOGProxyTarget:
    proxy_url: str
    tenant_id: str
    client_cert_file: str = ""
    client_key_file: str = ""
    client_ca_file: str = ""
    nonce_hmac_key: str = ""


def build_proxy_llm_url(
    target: BYOGProxyTarget,
    provider: str,
    upstream_path: str,
    *,
    query: dict[str, str] | None = None,
) -> str:
    base = target.proxy_url.rstrip("/") + "/"
    path = (
        BYOG_PROXY_FORWARD_PREFIX.lstrip("/")
        + quote(provider.strip(), safe="")
        + "/"
        + upstream_path.lstrip("/")
    )
    url = urljoin(base, path)
    if query:
        url += "?" + urlencode(query)
    return url


async def forward_llm_request_via_proxy(
    target: BYOGProxyTarget,
    provider: str,
    upstream_path: str,
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    content: bytes | str | None = None,
    query: dict[str, str] | None = None,
    timeout_s: float = BYOG_PROXY_DEFAULT_TIMEOUT_SECONDS,
    client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> httpx.Response:
    """Forward one LLM request through a Tier 3 customer proxy.

    Transport/auth failures raise BYOG-specific exceptions and stop the
    call. This helper intentionally has no direct-provider fallback hook;
    callers that catch :class:`BYOGProxyError` should return an error to
    the user rather than calling ``backend.agents.llm.get_llm``.
    """
    url = build_proxy_llm_url(target, provider, upstream_path, query=query)
    outbound_headers = dict(headers or {})
    outbound_headers.update(_signed_proxy_headers(target, method, url))
    cert = None
    if target.client_cert_file and target.client_key_file:
        cert = (target.client_cert_file, target.client_key_file)
    verify: bool | str = target.client_ca_file or True
    factory = client_factory or httpx.AsyncClient

    try:
        async with factory(timeout=timeout_s, cert=cert, verify=verify) as client:
            response = await client.request(
                method,
                url,
                headers=outbound_headers,
                content=content,
            )
    except httpx.TransportError as exc:
        raise BYOGProxyUnavailable(
            "BYOG proxy unavailable or mTLS handshake failed"
        ) from exc

    if response.status_code in {401, 403}:
        raise BYOGProxyRejected("BYOG proxy authentication rejected the request")
    return response


def _signed_proxy_headers(target: BYOGProxyTarget, method: str, url: str) -> dict[str, str]:
    headers = {
        "X-Omnisight-Tenant-Id": target.tenant_id,
        "X-Omnisight-Nonce": secrets.token_urlsafe(24),
        "X-Omnisight-Timestamp": str(int(time.time())),
    }
    key = target.nonce_hmac_key.strip()
    if not key:
        return headers
    request_uri = httpx.URL(url).raw_path.decode()
    signed = "\n".join([
        method.upper(),
        request_uri,
        target.tenant_id,
        headers["X-Omnisight-Nonce"],
        headers["X-Omnisight-Timestamp"],
    ])
    digest = hmac.new(key.encode(), signed.encode(), sha256).hexdigest()
    headers["X-Omnisight-Signature"] = "sha256=" + digest
    return headers
