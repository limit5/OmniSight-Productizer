"""L3 Step 2 — LLM provider secrets, at-rest encrypted.

Stores API keys and auxiliary credentials (Azure endpoint, Ollama
base URL, model override) for each LLM provider in a KS.1 envelope
JSON marker under ``data/.llm_secrets.enc``. Values never land on disk
in plaintext; only the last four chars of a key are ever shown back to
callers (via :func:`fingerprint`). The KS.1.11 legacy Fernet marker
compatibility window is complete; non-envelope files are treated as
deprecated data and read as empty until an operator backfills them.

``load_into_settings`` mirrors the decrypted values into
:mod:`backend.config.settings` so the existing agent factory
(``backend.agents.llm.get_llm``) picks them up without an env-var
reload. The wizard's Step 3 (``POST /api/v1/bootstrap/llm-provision``)
is the single writer — everything else just reads via
:func:`get_provider_credentials` or inspects :func:`list_providers`.

Ping classification lives here too so the route can translate
provider errors into wizard-friendly messages without reaching into
LangChain:

    key_invalid        — HTTP 401/403 from hosted provider
    quota_exceeded     — HTTP 429
    network_unreachable — connection / timeout error
    bad_request        — HTTP 4xx that isn't auth/quota
    provider_error     — HTTP 5xx or unexpected response shape
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from backend import secret_store
from backend.security import envelope as tenant_envelope

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SECRETS_PATH = _PROJECT_ROOT / "data" / ".llm_secrets.enc"
_LLM_SECRET_TENANT_ID = "t-default"
_LLM_SECRET_ENVELOPE_FORMAT_VERSION = 1

# The four providers the bootstrap wizard offers. Keep in sync with
# ``app/bootstrap/page.tsx``: the wizard menu only ships these four.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "ollama", "azure")

_PING_TIMEOUT_S = 10.0


# ─────────────────────────────────────────────────────────────────
#  Errors
# ─────────────────────────────────────────────────────────────────


# Operator-friendly prefixes mapped per kind. The route passes these
# straight through to the wizard so ``detail`` is actionable without
# the UI having to classify the message on its own.
KIND_PREFIX: dict[str, str] = {
    "key_invalid": "Invalid API key",
    "quota_exceeded": "Quota exceeded",
    "network_unreachable": "Cannot reach provider",
    "bad_request": "Bad request",
    "provider_error": "Provider error",
}


def clear_message(kind: str, provider: str, reason: str, *, status: int | None = None) -> str:
    """Compose an operator-friendly error message for ``kind``.

    Output shape: ``"{Prefix} — {provider}: {reason} (HTTP {status})"``.
    The prefix is stable per kind so the UI can string-match in tests
    without duplicating the same mapping table.
    """
    prefix = KIND_PREFIX.get(kind, "Provider error")
    tail = f" (HTTP {status})" if status is not None else ""
    return f"{prefix} — {provider}: {reason}{tail}"


class ProviderPingError(Exception):
    """Raised when :func:`ping_provider` cannot verify a provider.

    ``kind`` is one of the classified strings above — callers map it
    to an HTTP status + user-facing message. ``message`` is always
    prefixed with :data:`KIND_PREFIX` so the wizard can show it to the
    operator verbatim — the ``kind`` field still carries the machine
    label for any UI-side branching (icon, hint, retry affordance).
    """

    def __init__(self, kind: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "status": self.status}


# ─────────────────────────────────────────────────────────────────
#  Encrypted store
# ─────────────────────────────────────────────────────────────────


def _reset_for_tests(path: Optional[Path] = None) -> None:
    """Point the encrypted store at a fresh file (or wipe the default)."""
    global _SECRETS_PATH
    if path is not None:
        _SECRETS_PATH = path
    elif _SECRETS_PATH.exists():
        try:
            _SECRETS_PATH.unlink()
        except OSError:
            pass


def _read_raw() -> dict[str, dict[str, str]]:
    if not _SECRETS_PATH.exists():
        return {}
    try:
        ciphertext = _SECRETS_PATH.read_text(encoding="ascii").strip()
        if not ciphertext:
            return {}
        plaintext = _decrypt_store_payload(ciphertext)
        data = json.loads(plaintext)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("secrets: failed to decrypt %s (%s) — treating as empty",
                       _SECRETS_PATH, exc)
        return {}


def _store_carrier(ciphertext: str, dek_ref: tenant_envelope.TenantDEKRef) -> str:
    payload = {
        "fmt": _LLM_SECRET_ENVELOPE_FORMAT_VERSION,
        "ciphertext": ciphertext,
        "dek_ref": dek_ref.to_dict(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _load_store_carrier(
    ciphertext: str,
) -> tuple[str, tenant_envelope.TenantDEKRef]:
    payload = json.loads(ciphertext)
    if not isinstance(payload, dict):
        raise ValueError("llm secret envelope must be an object")
    if payload.get("fmt") != _LLM_SECRET_ENVELOPE_FORMAT_VERSION:
        raise ValueError("unknown llm secret envelope format")
    inner = payload.get("ciphertext")
    if not isinstance(inner, str) or not inner:
        raise ValueError("llm secret envelope missing ciphertext")
    dek_ref_raw = payload.get("dek_ref")
    if not isinstance(dek_ref_raw, dict):
        raise ValueError("llm secret envelope missing dek_ref")
    return inner, tenant_envelope.TenantDEKRef.from_dict(dek_ref_raw)


def _encrypt_store_payload(payload: str) -> str:
    ciphertext, dek_ref = tenant_envelope.encrypt(
        payload,
        _LLM_SECRET_TENANT_ID,
        purpose="llm-provider-secrets",
    )
    return _store_carrier(ciphertext, dek_ref)


def _decrypt_store_payload(ciphertext: str) -> str:
    try:
        inner, dek_ref = _load_store_carrier(ciphertext)
    except (TypeError, ValueError, tenant_envelope.EnvelopeEncryptionError):
        if isinstance(ciphertext, str) and not ciphertext.lstrip().startswith("{"):
            raise ValueError("legacy Fernet provider credential path is deprecated")
        raise
    return tenant_envelope.decrypt(inner, dek_ref)


def _write_raw(data: dict[str, dict[str, str]]) -> None:
    _SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, sort_keys=True)
    ciphertext = _encrypt_store_payload(payload)
    _SECRETS_PATH.write_text(ciphertext, encoding="ascii")
    try:
        _SECRETS_PATH.chmod(0o600)
    except OSError:
        pass


def set_provider_credentials(
    provider: str,
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    azure_deployment: str = "",
) -> dict[str, str]:
    """Persist credentials for *provider* and update process settings.

    Returns the stored record with the key redacted to its fingerprint.
    Raises ``ValueError`` for unknown providers.
    """
    p = (provider or "").strip().lower()
    if p not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")

    record: dict[str, str] = {}
    if api_key:
        record["api_key"] = api_key.strip()
    if model:
        record["model"] = model.strip()
    if base_url:
        record["base_url"] = base_url.strip()
    if azure_deployment:
        record["azure_deployment"] = azure_deployment.strip()

    data = _read_raw()
    data[p] = record
    _write_raw(data)

    load_into_settings()
    return {
        "provider": p,
        "model": record.get("model", ""),
        "base_url": record.get("base_url", ""),
        "azure_deployment": record.get("azure_deployment", ""),
        "fingerprint": fingerprint(record.get("api_key", "")),
    }


def get_provider_credentials(provider: str) -> dict[str, str]:
    """Return the decrypted credential record for *provider* (may be empty)."""
    p = (provider or "").strip().lower()
    return _read_raw().get(p, {})


def list_provider_fingerprints() -> dict[str, dict[str, str]]:
    """Return every stored provider with the API key redacted to its tail."""
    out: dict[str, dict[str, str]] = {}
    for provider, rec in _read_raw().items():
        out[provider] = {
            "has_key": bool(rec.get("api_key")),
            "fingerprint": fingerprint(rec.get("api_key", "")),
            "model": rec.get("model", ""),
            "base_url": rec.get("base_url", ""),
            "azure_deployment": rec.get("azure_deployment", ""),
        }
    return out


def fingerprint(api_key: str) -> str:
    """Redact an API key down to its tail — safe to log or show in UI."""
    return secret_store.fingerprint(api_key or "")


# ─────────────────────────────────────────────────────────────────
#  Settings bridge
# ─────────────────────────────────────────────────────────────────


def load_into_settings() -> None:
    """Copy every stored credential into :mod:`backend.config.settings`.

    Called once at import time and again after every write so the rest
    of the stack (``backend.agents.llm``) picks up the new values
    without an env-var reload. The agent cache is cleared so the next
    ``get_llm()`` call rebuilds with the new credentials.
    """
    try:
        from backend.config import settings
    except Exception as exc:
        logger.debug("secrets: cannot import settings (%s)", exc)
        return

    data = _read_raw()
    for provider, rec in data.items():
        api_key = rec.get("api_key", "")
        model = rec.get("model", "")
        base_url = rec.get("base_url", "")
        field = f"{provider}_api_key"
        if api_key and hasattr(settings, field):
            setattr(settings, field, api_key)
        if model:
            # Only set llm_model when this provider is the active one so
            # switching providers doesn't leak a stale model name.
            if (settings.llm_provider or "").strip().lower() == provider:
                settings.llm_model = model
        if provider == "ollama" and base_url:
            settings.ollama_base_url = base_url

    # Bust the LLM cache so next call uses the new credentials.
    try:
        from backend.agents.llm import _cache
        _cache.clear()
    except Exception:
        pass


# Load encrypted credentials into settings on module import so a
# restart picks up what the wizard wrote last time.
try:
    load_into_settings()
except Exception as exc:
    logger.debug("secrets: initial load_into_settings failed (%s)", exc)


# ─────────────────────────────────────────────────────────────────
#  Provider ping
# ─────────────────────────────────────────────────────────────────


def _classify_http_status(status: int) -> str:
    if status in (401, 403):
        return "key_invalid"
    if status == 429:
        return "quota_exceeded"
    if 400 <= status < 500:
        return "bad_request"
    return "provider_error"


_KIND_DEFAULT_REASON: dict[str, str] = {
    "key_invalid": "the API key was rejected — double-check for typos or an expired credential",
    "quota_exceeded": "rate limit or monthly quota exhausted — try again later or upgrade the account",
    "bad_request": "the provider rejected the request shape",
    "provider_error": "the provider responded with an internal error",
}


def _extract_reason(resp: "httpx.Response", kind: str, *, default: str | None = None) -> str:
    """Pull a short, user-meaningful reason out of a provider response.

    Preference order: ``error.message`` → ``error.type`` → ``error`` (raw string)
    → :data:`_KIND_DEFAULT_REASON` → truncated response body. Keeps the
    result short so it renders in a wizard banner without scrolling.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            for key in ("message", "type", "code"):
                v = err.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:200]
        elif isinstance(err, str) and err.strip():
            return err.strip()[:200]
        for key in ("message", "detail"):
            v = body.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
    fallback = default or _KIND_DEFAULT_REASON.get(kind) or "unexpected reply"
    text = (resp.text or "").strip()
    if text and kind in ("bad_request", "provider_error") and len(text) <= 200:
        return text
    return fallback


async def _ping_anthropic(api_key: str) -> dict[str, Any]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    async with httpx.AsyncClient(timeout=_PING_TIMEOUT_S) as client:
        resp = await client.get("https://api.anthropic.com/v1/models", headers=headers)
    if resp.status_code >= 400:
        kind = _classify_http_status(resp.status_code)
        raise ProviderPingError(
            kind,
            clear_message(
                kind,
                "Anthropic",
                _extract_reason(resp, kind),
                status=resp.status_code,
            ),
            status=resp.status_code,
        )
    models: list[str] = []
    try:
        body = resp.json()
        for item in (body.get("data") or [])[:25]:
            mid = item.get("id")
            if mid:
                models.append(str(mid))
    except ValueError:
        pass
    return {"models": models}


async def _ping_openai(api_key: str, base_url: str = "") -> dict[str, Any]:
    url = (base_url.rstrip("/") if base_url else "https://api.openai.com/v1") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=_PING_TIMEOUT_S) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        kind = _classify_http_status(resp.status_code)
        raise ProviderPingError(
            kind,
            clear_message(
                kind,
                "OpenAI",
                _extract_reason(resp, kind),
                status=resp.status_code,
            ),
            status=resp.status_code,
        )
    models: list[str] = []
    try:
        body = resp.json()
        for item in (body.get("data") or [])[:25]:
            mid = item.get("id")
            if mid:
                models.append(str(mid))
    except ValueError:
        pass
    return {"models": models}


async def _ping_azure(api_key: str, base_url: str, deployment: str = "") -> dict[str, Any]:
    if not base_url:
        raise ProviderPingError(
            "bad_request",
            clear_message(
                "bad_request",
                "Azure OpenAI",
                "endpoint (base_url) is required — e.g. "
                "https://<resource>.openai.azure.com",
            ),
        )
    url = base_url.rstrip("/") + "/openai/deployments?api-version=2023-05-15"
    headers = {"api-key": api_key}
    async with httpx.AsyncClient(timeout=_PING_TIMEOUT_S) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        kind = _classify_http_status(resp.status_code)
        raise ProviderPingError(
            kind,
            clear_message(
                kind,
                "Azure OpenAI",
                _extract_reason(resp, kind),
                status=resp.status_code,
            ),
            status=resp.status_code,
        )
    deployments: list[str] = []
    try:
        body = resp.json()
        for item in (body.get("data") or [])[:25]:
            name = item.get("id") or item.get("model")
            if name:
                deployments.append(str(name))
    except ValueError:
        pass
    if deployment and deployments and deployment not in deployments:
        logger.info(
            "azure ping: deployment %r not in returned list %s (may still exist)",
            deployment, deployments,
        )
    return {"models": deployments}


async def _ping_ollama(base_url: str = "") -> dict[str, Any]:
    url = (base_url.rstrip("/") if base_url else "http://localhost:11434") + "/api/tags"
    async with httpx.AsyncClient(timeout=_PING_TIMEOUT_S) as client:
        resp = await client.get(url)
    if resp.status_code >= 400:
        kind = _classify_http_status(resp.status_code)
        raise ProviderPingError(
            kind,
            clear_message(
                kind,
                "Ollama",
                _extract_reason(resp, kind, default=f"unexpected reply from {url}"),
                status=resp.status_code,
            ),
            status=resp.status_code,
        )
    models: list[str] = []
    try:
        body = resp.json()
        for item in body.get("models", []) or []:
            name = item.get("name") or item.get("model")
            if name:
                models.append(str(name))
    except ValueError:
        pass
    return {"models": models}


async def ping_provider(
    provider: str,
    *,
    api_key: str = "",
    base_url: str = "",
    azure_deployment: str = "",
) -> dict[str, Any]:
    """Verify *provider* is reachable and the supplied credentials work.

    Returns ``{"latency_ms": int, "models": list[str]}`` on success.
    Raises :class:`ProviderPingError` on auth / quota / network failure.
    """
    p = (provider or "").strip().lower()
    if p not in SUPPORTED_PROVIDERS:
        raise ProviderPingError(
            "bad_request",
            clear_message(
                "bad_request",
                provider or "<empty>",
                f"unsupported provider — valid: {list(SUPPORTED_PROVIDERS)}",
            ),
        )

    if p != "ollama" and not (api_key or "").strip():
        raise ProviderPingError(
            "key_invalid",
            clear_message(
                "key_invalid",
                p,
                "no API key provided — paste the key from the provider dashboard",
            ),
        )

    started = time.monotonic()
    try:
        if p == "anthropic":
            info = await _ping_anthropic(api_key.strip())
        elif p == "openai":
            info = await _ping_openai(api_key.strip(), base_url=base_url.strip())
        elif p == "azure":
            info = await _ping_azure(
                api_key.strip(),
                base_url=base_url.strip(),
                deployment=azure_deployment.strip(),
            )
        elif p == "ollama":
            info = await _ping_ollama(base_url=base_url.strip())
        else:  # pragma: no cover — guarded above
            raise ProviderPingError(
                "bad_request",
                clear_message("bad_request", p, "unsupported provider"),
            )
    except ProviderPingError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        raise ProviderPingError(
            "network_unreachable",
            clear_message(
                "network_unreachable",
                p,
                f"no response within {int(_PING_TIMEOUT_S)}s — check DNS, firewall, or proxy ({exc})",
            ),
        ) from exc
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        raise ProviderPingError(
            "network_unreachable",
            clear_message(
                "network_unreachable",
                p,
                f"transport error — {exc}",
            ),
        ) from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    return {"latency_ms": latency_ms, **info}
