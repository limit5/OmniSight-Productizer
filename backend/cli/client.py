"""HTTP client wrapper used by the ``omnisight`` CLI.

Kept as its own module so the Click command layer stays free of
``httpx`` imports at call-time — the tests substitute a fake client
via the Click context without ever opening a network connection.

Design notes:

* Base URL + bearer resolved from ``--base-url`` / ``--token`` flags
  first, then ``OMNISIGHT_BASE_URL`` / ``OMNISIGHT_TOKEN`` env vars,
  then the documented default (``http://localhost:8000`` + no token).
* ``run_stream`` yields decoded SSE frames as ``(event, data)`` tuples
  so the caller can format them however it wants (terminal pretty
  print, JSON tail, silent).
* Every request raises :class:`OmniSightCliError` with a short message
  on non-2xx — the Click layer maps that to ``ClickException`` and a
  ``UsageError``-compatible exit code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterator

import httpx


DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 30.0
STREAM_TIMEOUT = 600.0


class OmniSightCliError(RuntimeError):
    """Surfaced back to Click as a user-visible failure."""


@dataclass(frozen=True)
class CliConfig:
    """Resolved configuration for one CLI invocation."""

    base_url: str
    token: str
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def resolve(
        cls,
        base_url: str | None,
        token: str | None,
        timeout: float | None = None,
    ) -> "CliConfig":
        url = (base_url or os.environ.get("OMNISIGHT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        bearer = token if token is not None else os.environ.get("OMNISIGHT_TOKEN", "")
        to = timeout if timeout is not None else DEFAULT_TIMEOUT
        return cls(base_url=url, token=bearer or "", timeout=to)


class OmniSightClient:
    """Thin ``httpx``-backed adapter around the endpoints the CLI drives.

    The class exposes one public method per command (status / list
    workspaces / inspect / inject / run_stream). Each method returns a
    plain dict or iterator of dicts so the Click layer can format
    without caring about HTTP.
    """

    def __init__(self, config: CliConfig):
        self.config = config

    # ─── internals ──────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.config.token:
            h["Authorization"] = f"Bearer {self.config.token}"
        return h

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.config.base_url}{path}"

    def _raise_for_status(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail") or body.get("error") or body)
                else:
                    detail = str(body)
            except Exception:
                detail = r.text[:200]
            raise OmniSightCliError(
                f"{r.request.method} {r.request.url.path} → HTTP {r.status_code}: {detail}"
            )

    def _get(self, path: str) -> Any:
        try:
            with httpx.Client(timeout=self.config.timeout) as c:
                r = c.get(self._url(path), headers=self._headers())
        except httpx.HTTPError as exc:
            raise OmniSightCliError(f"connection failed: {exc}") from exc
        self._raise_for_status(r)
        try:
            return r.json()
        except Exception as exc:
            raise OmniSightCliError(f"invalid JSON from {path}: {exc}") from exc

    def _post(self, path: str, json_body: dict[str, Any] | None = None) -> Any:
        try:
            with httpx.Client(timeout=self.config.timeout) as c:
                r = c.post(
                    self._url(path),
                    headers=self._headers(),
                    json=json_body or {},
                )
        except httpx.HTTPError as exc:
            raise OmniSightCliError(f"connection failed: {exc}") from exc
        self._raise_for_status(r)
        try:
            return r.json()
        except Exception:
            return {"ok": True, "raw": r.text}

    # ─── public API ─────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """GET /status — system KPI snapshot."""
        return self._get("/status")

    def list_workspaces(self) -> list[dict[str, Any]]:
        """GET /workspaces — active workspace list."""
        data = self._get("/workspaces")
        if not isinstance(data, list):
            raise OmniSightCliError(f"unexpected /workspaces payload type: {type(data).__name__}")
        return data

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        """GET /agents/{id} — agent detail."""
        if not agent_id:
            raise OmniSightCliError("agent_id is required")
        return self._get(f"/agents/{agent_id}")

    def get_workspace(self, agent_id: str) -> dict[str, Any] | None:
        """GET /workspaces/{agent_id} — workspace pointer (None on 404)."""
        if not agent_id:
            raise OmniSightCliError("agent_id is required")
        try:
            return self._get(f"/workspaces/{agent_id}")
        except OmniSightCliError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def inject_hint(self, agent_id: str, text: str, author: str = "cli") -> dict[str, Any]:
        """POST /chatops/inject — blackboard hint."""
        if not agent_id:
            raise OmniSightCliError("agent_id is required")
        if not text or not text.strip():
            raise OmniSightCliError("hint text is required")
        return self._post(
            "/chatops/inject",
            json_body={"agent_id": agent_id, "text": text, "author": author or "cli"},
        )

    def run_stream(self, command: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """POST /invoke/stream?command=<NL> — yield ``(event, data-dict)``.

        The backend emits text/event-stream; each frame contains an
        ``event:`` line and a JSON-encoded ``data:`` line. We decode
        both so the CLI formatter can keep its logic JSON-native.
        """
        if not command or not command.strip():
            raise OmniSightCliError("command is required")
        url = self._url("/invoke/stream")
        try:
            with httpx.Client(timeout=STREAM_TIMEOUT) as c:
                with c.stream(
                    "POST",
                    url,
                    headers={**self._headers(), "Accept": "text/event-stream"},
                    params={"command": command},
                ) as r:
                    if r.status_code >= 400:
                        body = r.read().decode("utf-8", "replace")[:500]
                        raise OmniSightCliError(
                            f"POST /invoke/stream → HTTP {r.status_code}: {body}"
                        )
                    yield from _iter_sse_frames(r.iter_lines())
        except httpx.HTTPError as exc:
            raise OmniSightCliError(f"stream failed: {exc}") from exc


def _iter_sse_frames(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Decode ``text/event-stream`` frames into ``(event, data-dict)``.

    Robust to comment lines (``:keep-alive``), empty frames, and
    ``data:`` payloads that arrive as plain strings rather than JSON
    (fallback: wrap into ``{"message": <text>}``).
    """
    event = "message"
    data_buf: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_buf:
                payload = "\n".join(data_buf)
                try:
                    parsed = json.loads(payload)
                    if not isinstance(parsed, dict):
                        parsed = {"value": parsed}
                except Exception:
                    parsed = {"message": payload}
                yield event, parsed
            event = "message"
            data_buf = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip() or "message"
        elif line.startswith("data:"):
            data_buf.append(line[len("data:"):].lstrip())
    if data_buf:
        payload = "\n".join(data_buf)
        try:
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
        except Exception:
            parsed = {"message": payload}
        yield event, parsed
