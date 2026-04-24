"""Phase 5b-3 (#llm-credentials) — llm_credentials CRUD REST endpoints.

Surface
───────
* ``GET    /llm-credentials``              — list (masked, tenant-scoped)
* ``POST   /llm-credentials``              — create (body → Fernet → DB)
* ``GET    /llm-credentials/{id}``         — detail (masked)
* ``PATCH  /llm-credentials/{id}``         — partial update / rotate key
* ``DELETE /llm-credentials/{id}``         — delete (auto-elect new default)
* ``POST   /llm-credentials/{id}/test``    — live probe the provider's API

All mutations write to :mod:`backend.audit` (the service layer in
``backend.llm_credentials`` takes care of that — this router stays
thin). All reads return the API key only as
:func:`backend.secret_store.fingerprint` strings (``…abc4``) — the
plaintext secret never leaves the server.

Tenant RLS
──────────
The ``require_admin`` dependency resolves the authenticated user; we
forward their ``tenant_id`` to :func:`db_context.set_tenant_id` so
every :mod:`backend.llm_credentials` call path filters through
``tenant_where_pg``. A caller with admin on tenant A cannot list /
update / delete any row in tenant B (regression-guarded by
``test_llm_credentials_crud.py::test_tenant_isolation``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import llm_credentials as _lc
from backend.db_context import set_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-credentials", tags=["llm-credentials"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_PROVIDER_PATTERN = (
    r"^(anthropic|google|openai|xai|groq|"
    r"deepseek|together|openrouter|ollama)$"
)


class LLMCredentialCreate(BaseModel):
    provider: str = Field(..., pattern=_PROVIDER_PATTERN)
    label: str = Field("", max_length=256)
    value: str = Field("", max_length=8192)
    auth_type: str = Field("pat", pattern=r"^(pat|oauth)$")
    is_default: bool = False
    enabled: bool = True
    metadata: dict = Field(default_factory=dict)


class LLMCredentialUpdate(BaseModel):
    # All fields optional — partial PATCH. ``None`` means "don't
    # touch"; explicit empty string on ``value`` clears it.
    label: str | None = None
    auth_type: str | None = Field(None, pattern=r"^(pat|oauth)$")
    is_default: bool | None = None
    enabled: bool | None = None
    metadata: dict | None = None
    value: str | None = None


def _ensure_tenant(user: _au.User) -> None:
    """Pin the request-scoped tenant context from the authenticated user."""
    set_tenant_id(user.tenant_id or "t-default")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Read endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("")
async def list_llm_credentials(
    provider: str | None = Query(None, pattern=_PROVIDER_PATTERN),
    enabled_only: bool = Query(False),
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    items = await _lc.list_credentials(
        provider=provider, enabled_only=enabled_only,
    )
    return {"items": items, "count": len(items)}


@router.get("/{credential_id}")
async def get_llm_credential_endpoint(
    credential_id: str,
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    row = await _lc.get_credential(credential_id)
    if row is None:
        raise HTTPException(404, "llm_credential not found")
    return row


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Write endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("", status_code=201)
async def create_llm_credential(
    body: LLMCredentialCreate,
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    try:
        out = await _lc.create_credential(
            provider=body.provider,
            label=body.label,
            value=body.value,
            auth_type=body.auth_type,
            is_default=body.is_default,
            enabled=body.enabled,
            metadata=body.metadata,
        )
    except _lc.LLMCredentialConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return out


@router.patch("/{credential_id}")
async def update_llm_credential(
    credential_id: str,
    body: LLMCredentialUpdate,
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    updates = {
        k: v for k, v in body.model_dump(exclude_unset=True).items()
    }
    try:
        out = await _lc.update_credential(credential_id, updates=updates)
    except _lc.LLMCredentialNotFound:
        raise HTTPException(404, "llm_credential not found")
    except _lc.LLMCredentialConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return out


@router.delete("/{credential_id}")
async def delete_llm_credential(
    credential_id: str,
    auto_elect_new_default: bool = Query(True),
    user: _au.User = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    try:
        out = await _lc.delete_credential(
            credential_id,
            auto_elect_new_default=auto_elect_new_default,
        )
    except _lc.LLMCredentialNotFound:
        raise HTTPException(404, "llm_credential not found")
    except _lc.LLMCredentialConflict as exc:
        raise HTTPException(409, str(exc))
    return {"status": "deleted", **out}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Live probe — ping each provider's list-models endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Per-provider probe configuration. Each entry is a "cheap read" —
# list models or equivalent — which is fastest and cheapest way to
# validate an API key without running inference / charging tokens.
# Keyed by provider; ``auth`` encodes where the key goes (Bearer
# header, custom header, or query param). Ollama is special-cased
# below because it's keyless + needs ``base_url`` from ``metadata``.
_PROBE_SPECS: dict[str, dict[str, str]] = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/models",
        "auth": "header",
        "header": "x-api-key",
        "extra": "anthropic-version: 2023-06-01",
    },
    "openai": {
        "url": "https://api.openai.com/v1/models",
        "auth": "bearer",
    },
    "google": {
        # Gemini API uses query-param auth for the public v1beta surface.
        "url": "https://generativelanguage.googleapis.com/v1beta/models",
        "auth": "query",
        "query": "key",
    },
    "xai": {
        "url": "https://api.x.ai/v1/models",
        "auth": "bearer",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/models",
        "auth": "bearer",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/v1/models",
        "auth": "bearer",
    },
    "together": {
        "url": "https://api.together.xyz/v1/models",
        "auth": "bearer",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/models",
        "auth": "bearer",
    },
}


async def _curl_json(args: list[str]) -> tuple[int, dict | list | None, str]:
    """Run ``curl`` with ``-w '%{http_code}'`` and return parsed JSON.

    Returns ``(status_code, parsed_or_None, raw_tail)``. ``parsed_or_None``
    is the decoded JSON body when parse succeeds, else ``None``. The
    raw tail helps the router surface a helpful error message when the
    provider returned HTML / plain-text.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    raw = stdout.decode(errors="replace")
    # ``-w '\n%{http_code}'`` — last line is the status code.
    body = raw
    status = 0
    nl = raw.rfind("\n")
    if nl != -1:
        tail = raw[nl + 1:].strip()
        if tail.isdigit():
            status = int(tail)
            body = raw[:nl]
    parsed: dict | list | None = None
    try:
        parsed = json.loads(body)
    except Exception:
        parsed = None
    return status, parsed, body[:500]


async def _probe_llm_credential(
    provider: str, value: str, metadata: dict,
) -> dict:
    """Dispatch probe to the correct provider.

    On 2xx + parseable JSON, returns ``{"status": "ok", ...}`` with
    a best-effort ``model_count`` hint. On non-2xx or parse failure,
    returns ``{"status": "error", "message": "..."}`` — never raises,
    since the caller's router surface is a user-facing diagnostic.
    """
    if provider == "ollama":
        base_url = (metadata or {}).get("base_url") or ""
        base_url = str(base_url).strip().rstrip("/")
        if not base_url:
            return {
                "status": "error",
                "message": (
                    "Ollama probe requires metadata.base_url "
                    "(e.g. http://ai_engine:11434)."
                ),
            }
        status, parsed, tail = await _curl_json([
            "curl", "-s", "-w", "\n%{http_code}",
            f"{base_url}/api/tags",
        ])
        if status < 200 or status >= 300:
            return {
                "status": "error",
                "message": f"Ollama returned HTTP {status}: {tail[:200]}",
            }
        models = (parsed or {}).get("models") if isinstance(parsed, dict) else None
        return {
            "status": "ok",
            "provider": "ollama",
            "base_url": base_url,
            "model_count": len(models) if isinstance(models, list) else 0,
        }

    spec = _PROBE_SPECS.get(provider)
    if spec is None:
        return {
            "status": "error",
            "message": f"Unknown provider {provider!r}",
        }
    if not value:
        return {"status": "error", "message": "API key is required"}

    url = spec["url"]
    auth = spec["auth"]
    args: list[str] = ["curl", "-s", "-w", "\n%{http_code}"]

    if auth == "bearer":
        args += ["-H", f"Authorization: Bearer {value}"]
    elif auth == "header":
        args += ["-H", f"{spec['header']}: {value}"]
        extra = spec.get("extra")
        if extra:
            args += ["-H", extra]
    elif auth == "query":
        qp = spec.get("query", "key")
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{qp}={urllib.parse.quote(value, safe='')}"

    args += ["-H", "Accept: application/json", url]
    status, parsed, tail = await _curl_json(args)

    if status < 200 or status >= 300:
        # Try to lift a useful message out of the provider's error body.
        message = f"HTTP {status}"
        if isinstance(parsed, dict):
            err = parsed.get("error") or parsed.get("message")
            if isinstance(err, dict):
                message = err.get("message") or err.get("type") or message
            elif isinstance(err, str):
                message = err
        elif tail:
            message = f"HTTP {status}: {tail[:200]}"
        return {
            "status": "error",
            "provider": provider,
            "http_status": status,
            "message": message,
        }

    # Best-effort count — every OpenAI-compatible surface returns
    # ``{"data": [...]}`` or similar; Anthropic returns
    # ``{"data": [...], ...}``; Google returns ``{"models": [...]}``.
    model_count = 0
    if isinstance(parsed, dict):
        for key in ("data", "models"):
            v = parsed.get(key)
            if isinstance(v, list):
                model_count = len(v)
                break
    return {
        "status": "ok",
        "provider": provider,
        "model_count": model_count,
    }


@router.post("/{credential_id}/test")
async def test_llm_credential(
    credential_id: str,
    user: _au.User = Depends(_au.require_admin),
):
    """Live-probe the provider's API with this credential's stored key.

    The plaintext key is decrypted server-side, threaded into the probe
    (curl, 15s timeout), and is never echoed back in the response.
    Ollama credentials use ``metadata.base_url`` instead of an API key.
    """
    _ensure_tenant(user)
    row = await _lc.get_credential(credential_id)
    if row is None:
        raise HTTPException(404, "llm_credential not found")
    value = await _lc.get_plaintext_value(credential_id)
    try:
        probe = await asyncio.wait_for(
            _probe_llm_credential(
                row["provider"],
                value or "",
                row.get("metadata") or {},
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return {
            "credential_id": credential_id,
            "provider": row["provider"],
            "status": "error",
            "message": "Connection timed out (15s)",
        }
    return {
        "credential_id": credential_id,
        "provider": row["provider"],
        **probe,
    }
