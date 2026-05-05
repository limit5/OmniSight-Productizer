"""BP.A2A.5 -- outbound A2A HTTP/SSE client.

The client mirrors existing project HTTP helpers: small frozen request/result
models, injected ``httpx.AsyncClient`` factory for tests, explicit timeout
handling, and no hidden module-level client singleton.

Module-global state audit (SOP Step 1): this module defines constants,
dataclasses, and helpers only. AgentCard cache state is per ``A2AClient``
instance and per worker by design; the retry breaker delegates to
``backend.circuit_breaker``, matching the existing per-worker provider-breaker
infra unless a future shared breaker backend is introduced upstream.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote, urljoin

import httpx

from backend import circuit_breaker
from backend.a2a.agent_card import AgentCard


AGENT_CARD_CACHE_TTL_SECONDS = 3600.0
A2A_DEFAULT_TIMEOUT_SECONDS = 60.0
A2A_DEFAULT_MAX_ATTEMPTS = 3
A2A_PROVIDER = "a2a"


class A2AClientError(RuntimeError):
    """Base class for outbound A2A client failures."""


class A2ACircuitOpen(A2AClientError):
    """Raised when the per-tenant A2A circuit is open."""


class A2ARequestFailed(A2AClientError):
    """Raised when the remote A2A server returns an error or bad payload."""


class A2ATimeout(A2AClientError):
    """Raised when the remote A2A request times out."""


@dataclass(frozen=True)
class A2AInvocationResult:
    """Synchronous A2A invocation result."""

    status_code: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class A2AStreamEvent:
    """One parsed SSE event from a remote A2A stream."""

    event: str
    data: Any
    id: str = ""
    retry: int | None = None


HttpClientFactory = Callable[..., httpx.AsyncClient]
SleepFn = Callable[[float], Any]


class A2AClient:
    """Tenant-scoped outbound A2A client with AgentCard cache and breaker."""

    def __init__(
        self,
        base_url: str,
        *,
        tenant_id: str,
        bearer_token: str = "",
        timeout_s: float = A2A_DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = A2A_DEFAULT_MAX_ATTEMPTS,
        cache_ttl_s: float = AGENT_CARD_CACHE_TTL_SECONDS,
        client_factory: HttpClientFactory | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self.base_url = _normalise_base_url(base_url)
        self.tenant_id = _required("tenant_id", tenant_id)
        self.bearer_token = bearer_token.strip()
        self.timeout_s = timeout_s
        self.max_attempts = max(1, int(max_attempts))
        self.cache_ttl_s = cache_ttl_s
        self.client_factory = client_factory or httpx.AsyncClient
        self._sleep = sleep or asyncio.sleep
        self._card_cache: tuple[float, AgentCard] | None = None

    @property
    def circuit_fingerprint(self) -> str:
        return sha256(self.base_url.encode()).hexdigest()[:16]

    async def fetch_agent_card(self, *, force_refresh: bool = False) -> AgentCard:
        """Fetch and cache the remote AgentCard for one hour by default."""

        cached = self._card_cache
        now = time.time()
        if (
            not force_refresh
            and cached is not None
            and now - cached[0] < self.cache_ttl_s
        ):
            return cached[1]

        payload = await self._request_json(
            "GET",
            "/.well-known/agent.json",
            retry_reason="agent_card",
        )
        try:
            card = AgentCard.model_validate(payload)
        except Exception as exc:
            raise A2ARequestFailed("remote AgentCard did not match schema") from exc
        self._card_cache = (now, card)
        return card

    async def invoke(
        self,
        agent_name: str,
        payload: dict[str, Any],
        *,
        stream: bool = False,
    ) -> A2AInvocationResult:
        """Invoke a remote A2A agent in JSON mode."""

        path = _invoke_path(agent_name)
        params = {"stream": "true"} if stream else None
        result = await self._request_json(
            "POST",
            path,
            json_body=payload,
            params=params,
            retry_reason="invoke",
        )
        return A2AInvocationResult(status_code=200, payload=result)

    async def stream_invoke(
        self,
        agent_name: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[A2AStreamEvent]:
        """Invoke a remote A2A agent and yield parsed SSE events."""

        self._raise_if_circuit_open()
        last_exc: BaseException | None = None
        path = _invoke_path(agent_name)
        for attempt in range(1, self.max_attempts + 1):
            try:
                async for event in self._stream_once(path, payload):
                    yield event
                circuit_breaker.record_success(
                    self.tenant_id,
                    A2A_PROVIDER,
                    self.circuit_fingerprint,
                )
                return
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, A2ARequestFailed, A2ATimeout) as exc:
                last_exc = exc
                if attempt >= self.max_attempts:
                    break
                await self._sleep(_retry_delay(attempt))

        circuit_breaker.record_failure(
            self.tenant_id,
            A2A_PROVIDER,
            self.circuit_fingerprint,
            reason="stream_invoke",
        )
        if isinstance(last_exc, A2ATimeout):
            raise A2ATimeout("remote A2A stream timed out") from last_exc
        raise A2ARequestFailed("remote A2A stream failed") from last_exc

    async def _stream_once(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[A2AStreamEvent]:
        url = _url(self.base_url, path)
        timeout = httpx.Timeout(self.timeout_s)
        try:
            async with self.client_factory(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=self._headers(accept="text/event-stream"),
                    json=payload,
                    params={"stream": "true"},
                ) as resp:
                    if resp.status_code >= 500 or resp.status_code == 429:
                        raise A2ARequestFailed(
                            f"remote A2A stream returned {resp.status_code}"
                        )
                    if resp.status_code >= 400:
                        raise A2ARequestFailed(
                            f"remote A2A stream rejected request with {resp.status_code}"
                        )
                    event = "message"
                    event_id = ""
                    retry: int | None = None
                    data_lines: list[str] = []
                    async for line in resp.aiter_lines():
                        parsed = _parse_sse_line(
                            line,
                            event=event,
                            event_id=event_id,
                            retry=retry,
                            data_lines=data_lines,
                        )
                        if parsed is None:
                            continue
                        if isinstance(parsed, A2AStreamEvent):
                            yield parsed
                            event = "message"
                            event_id = ""
                            retry = None
                            data_lines = []
                        else:
                            event, event_id, retry, data_lines = parsed
                    if data_lines:
                        yield _build_sse_event(event, event_id, retry, data_lines)
        except httpx.TimeoutException as exc:
            raise A2ATimeout("remote A2A stream timed out") from exc

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        retry_reason: str,
    ) -> dict[str, Any]:
        self._raise_if_circuit_open()
        last_exc: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                payload = await self._request_json_once(
                    method,
                    path,
                    json_body=json_body,
                    params=params,
                )
                circuit_breaker.record_success(
                    self.tenant_id,
                    A2A_PROVIDER,
                    self.circuit_fingerprint,
                )
                return payload
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, A2ARequestFailed, A2ATimeout) as exc:
                last_exc = exc
                if attempt >= self.max_attempts:
                    break
                await self._sleep(_retry_delay(attempt))

        circuit_breaker.record_failure(
            self.tenant_id,
            A2A_PROVIDER,
            self.circuit_fingerprint,
            reason=retry_reason,
        )
        if isinstance(last_exc, A2ATimeout):
            raise A2ATimeout("remote A2A request timed out") from last_exc
        raise A2ARequestFailed("remote A2A request failed") from last_exc

    async def _request_json_once(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None,
        params: dict[str, str] | None,
    ) -> dict[str, Any]:
        url = _url(self.base_url, path)
        timeout = httpx.Timeout(self.timeout_s)
        try:
            async with self.client_factory(timeout=timeout) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                )
        except httpx.TimeoutException as exc:
            raise A2ATimeout("remote A2A request timed out") from exc

        if resp.status_code >= 500 or resp.status_code == 429:
            raise A2ARequestFailed(f"remote A2A returned {resp.status_code}")
        if resp.status_code >= 400:
            raise A2ARequestFailed(f"remote A2A rejected request with {resp.status_code}")
        try:
            data = resp.json() if resp.content else {}
        except ValueError as exc:
            raise A2ARequestFailed("remote A2A returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise A2ARequestFailed("remote A2A JSON response must be an object")
        return data

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-Omnisight-Tenant-Id": self.tenant_id,
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _raise_if_circuit_open(self) -> None:
        if circuit_breaker.is_open(
            self.tenant_id,
            A2A_PROVIDER,
            self.circuit_fingerprint,
        ):
            raise A2ACircuitOpen("remote A2A circuit is open")


def _normalise_base_url(base_url: str) -> str:
    base = _required("base_url", base_url).rstrip("/")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise ValueError("base_url must start with http:// or https://")
    return base


def _required(name: str, value: str) -> str:
    clean = (value or "").strip()
    if not clean:
        raise ValueError(f"{name} is required")
    return clean


def _invoke_path(agent_name: str) -> str:
    return "/a2a/invoke/" + quote(_required("agent_name", agent_name), safe="")


def _url(base_url: str, path: str) -> str:
    return urljoin(base_url + "/", path.lstrip("/"))


def _retry_delay(attempt: int) -> float:
    return min(2.0, 0.1 * (2 ** max(0, attempt - 1)))


def _parse_sse_line(
    line: str,
    *,
    event: str,
    event_id: str,
    retry: int | None,
    data_lines: list[str],
) -> A2AStreamEvent | tuple[str, str, int | None, list[str]] | None:
    if line == "":
        if not data_lines:
            return None
        return _build_sse_event(event, event_id, retry, data_lines)
    if line.startswith(":"):
        return None
    field, _, value = line.partition(":")
    if value.startswith(" "):
        value = value[1:]
    if field == "event":
        event = value or "message"
    elif field == "data":
        data_lines.append(value)
    elif field == "id":
        event_id = value
    elif field == "retry":
        try:
            retry = int(value)
        except ValueError:
            retry = None
    return event, event_id, retry, data_lines


def _build_sse_event(
    event: str,
    event_id: str,
    retry: int | None,
    data_lines: list[str],
) -> A2AStreamEvent:
    raw = "\n".join(data_lines)
    try:
        data: Any = json.loads(raw)
    except ValueError:
        data = raw
    return A2AStreamEvent(event=event, data=data, id=event_id, retry=retry)


__all__ = [
    "A2AClient",
    "A2AClientError",
    "A2ACircuitOpen",
    "A2AInvocationResult",
    "A2ARequestFailed",
    "A2AStreamEvent",
    "A2ATimeout",
    "AGENT_CARD_CACHE_TTL_SECONDS",
]
