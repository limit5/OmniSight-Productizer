"""Phase 54 — GitHub App scaffold (Open Agents borrow #3).

Replaces the OMNISIGHT_GITHUB_TOKEN PAT path with a proper App
installation token flow:

  1. Sign a 6-min JWT with the App private key (RSA-PEM in env).
  2. Exchange the JWT at GET /app/installations/{id}/access_tokens
     for a 1h installation token (cached 50 min on our side).
  3. Use the installation token for all subsequent gh API calls
     scoped to that installation's repos.

Implementation notes:
  * Pure stdlib JWT — no PyJWT dep. RS256 signing uses
    cryptography.hazmat (already a transitive of httpx). We hash +
    sign + base64url; format is identical to PyJWT output.
  * Installation token cache: in-process dict, keyed by
    installation_id, TTL 50 min so we always swap before GitHub's 1h
    expiry.
  * Webhook handler (PUSH/INSTALLATION) is out of scope for this
    MVP — schema is in place; backend/routers/webhooks.py extension
    lands in v1.

Env contract:
  OMNISIGHT_GITHUB_APP_ID         numeric App ID
  OMNISIGHT_GITHUB_APP_PRIVATE_KEY  PEM (newlines or literal `\\n`)
  OMNISIGHT_GITHUB_API_BASE_URL   default https://api.github.com
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_JWT_TTL_S = 6 * 60       # 6 min — GitHub max is 10
_INST_TOKEN_TTL_S = 50 * 60  # 50 min cache; GitHub expires at 60

# in-process cache: installation_id → (token, expires_at)
_inst_token_cache: dict[int, tuple[str, float]] = {}


@dataclass
class InstallationToken:
    token: str
    expires_at: float
    installation_id: int


class GitHubAppNotConfigured(RuntimeError):
    """Raised when env doesn't carry App credentials."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JWT (RS256) — stdlib + cryptography
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _normalize_pem(raw: str) -> bytes:
    """Accept either real newlines or literal `\\n` in env."""
    return raw.replace("\\n", "\n").encode("utf-8")


def _sign_app_jwt(app_id: str, private_key_pem: str, now: float | None = None) -> str:
    """Mint a GitHub-App JWT. Returns header.payload.signature."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover
        raise GitHubAppNotConfigured(
            "cryptography library required for GitHub App JWT signing"
        ) from exc

    iat = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": iat - 30,            # 30s clock-skew tolerance
        "exp": iat + _JWT_TTL_S,
        "iss": str(app_id),
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(
        _normalize_pem(private_key_pem), password=None,
    )
    sig = key.sign(
        signing_input.encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{signing_input}.{_b64url(sig)}"


def app_jwt() -> str:
    """Return a fresh App JWT using env-provided credentials."""
    app_id = (os.environ.get("OMNISIGHT_GITHUB_APP_ID") or "").strip()
    pem = (os.environ.get("OMNISIGHT_GITHUB_APP_PRIVATE_KEY") or "").strip()
    if not app_id or not pem:
        raise GitHubAppNotConfigured(
            "set OMNISIGHT_GITHUB_APP_ID + OMNISIGHT_GITHUB_APP_PRIVATE_KEY"
        )
    return _sign_app_jwt(app_id, pem)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Installation token exchange + cache
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _api_base() -> str:
    return (os.environ.get("OMNISIGHT_GITHUB_API_BASE_URL")
            or "https://api.github.com").rstrip("/")


async def get_installation_token(installation_id: int) -> InstallationToken:
    """Return a valid installation token, hitting cache when possible."""
    cached = _inst_token_cache.get(installation_id)
    if cached and cached[1] > time.time() + 60:  # 60s safety margin
        return InstallationToken(token=cached[0], expires_at=cached[1],
                                  installation_id=installation_id)
    jwt = app_jwt()
    url = f"{_api_base()}/app/installations/{installation_id}/access_tokens"
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code != 201:
        raise RuntimeError(
            f"installation token request failed: {resp.status_code} {resp.text[:200]}"
        )
    data = resp.json()
    token = data["token"]
    # GitHub returns ISO timestamp; we just track the cache TTL ourselves.
    expires_at = time.time() + _INST_TOKEN_TTL_S
    _inst_token_cache[installation_id] = (token, expires_at)
    return InstallationToken(token=token, expires_at=expires_at,
                              installation_id=installation_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DB helpers (installations table from migration 0005)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def upsert_installation(installation_id: int, account_login: str,
                              account_type: str = "User",
                              repos: Optional[list[str]] = None,
                              permissions: Optional[dict] = None) -> None:
    """SP-5.7c (2026-04-21): ported to pool. ON CONFLICT already
    makes it atomic against concurrent same-id upserts."""
    repos_json = json.dumps(repos or [])
    perms_json = json.dumps(permissions or {})
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO github_installations "
            "(installation_id, account_login, account_type, "
            " repos_json, permissions_json) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (installation_id) DO UPDATE SET "
            "  account_login = EXCLUDED.account_login, "
            "  account_type = EXCLUDED.account_type, "
            "  repos_json = EXCLUDED.repos_json, "
            "  permissions_json = EXCLUDED.permissions_json",
            installation_id, account_login, account_type,
            repos_json, perms_json,
        )


async def list_installations() -> list[dict]:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT installation_id, account_login, account_type, "
            "repos_json, permissions_json, created_at, suspended_at "
            "FROM github_installations ORDER BY installation_id"
        )
    return [
        {
            "installation_id": r["installation_id"],
            "account_login": r["account_login"],
            "account_type": r["account_type"],
            "repos": json.loads(r["repos_json"] or "[]"),
            "permissions": json.loads(r["permissions_json"] or "{}"),
            "created_at": r["created_at"],
            "suspended_at": r["suspended_at"],
        }
        for r in rows
    ]


def _reset_cache_for_tests() -> None:
    _inst_token_cache.clear()
