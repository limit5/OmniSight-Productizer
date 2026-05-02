"""FS.2b.1 -- Outbound integration OAuth flow scaffolds.

FS.2b connects generated apps to third-party APIs (GitHub, Slack,
Notion, and similar outbound integrations). This module renders the
minimal generated-app scaffold for the authorization-code flow:
authorize route, callback route, token exchange, token-vault storage
bridge, refresh middleware, and scope upgrades for already-connected
providers. Disconnect/revoke endpoints are separate FS.2b follow-up rows.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable constants/classes/functions only. Each
render derives files/env metadata from explicit provider plans and the
AS.1 vendor catalog; there is no cache, singleton, env read, network
IO, or shared mutable state across uvicorn workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.auth_provisioning.self_hosted import (
    AuthScaffoldEnvVar,
    AuthScaffoldFile,
)
from backend.auth_provisioning.vendor_oauth import VendorOAuthAppConfigPlan
from backend.security import token_vault
from backend.security.oauth_vendors import ALL_VENDOR_IDS, get_vendor


@dataclass(frozen=True)
class OutboundOAuthFlowProviderItem:
    """One outbound OAuth provider exposed by the generated app."""

    provider: str
    display_name: str
    callback_url: str
    scope: tuple[str, ...]
    authorize_endpoint: str
    token_endpoint: str
    extra_authorize_params: tuple[tuple[str, str], ...]
    is_oidc: bool
    client_id_env: str
    client_secret_env: str
    token_vault_supported: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "callback_url": self.callback_url,
            "scope": list(self.scope),
            "authorize_endpoint": self.authorize_endpoint,
            "token_endpoint": self.token_endpoint,
            "extra_authorize_params": [list(p) for p in self.extra_authorize_params],
            "is_oidc": self.is_oidc,
            "client_id_env": self.client_id_env,
            "client_secret_env": self.client_secret_env,
            "token_vault_supported": self.token_vault_supported,
        }


@dataclass(frozen=True)
class OutboundOAuthFlowScaffoldResult:
    """Manifest for an FS.2b.1 outbound OAuth flow scaffold."""

    providers: tuple[OutboundOAuthFlowProviderItem, ...]
    files: tuple[AuthScaffoldFile, ...]
    env: tuple[AuthScaffoldEnvVar, ...]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": [p.to_dict() for p in self.providers],
            "files": [f.to_dict() for f in self.files],
            "env": [v.to_dict() for v in self.env],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class OutboundOAuthFlowScaffoldOptions:
    """Inputs for rendering outbound integration OAuth flow files."""

    provider_plans: tuple[VendorOAuthAppConfigPlan, ...]
    flow_path: str = "auth/outbound-oauth-flow.ts"
    route_prefix: str = "app/api/integrations"
    oauth_client_import: str = "@/shared/oauth-client"
    token_vault_import: str = "@/shared/token-vault"
    token_vault_path: str = "auth/outbound-token-vault.ts"
    refresh_middleware_path: str = "auth/outbound-refresh-middleware.ts"
    scope_upgrade_path: str = "auth/outbound-scope-upgrade.ts"
    extra_env: tuple[AuthScaffoldEnvVar, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        if not self.provider_plans:
            raise ValueError("provider_plans must contain at least one provider")
        if not self.flow_path or not self.flow_path.strip():
            raise ValueError("flow_path is required")
        if not self.route_prefix or not self.route_prefix.strip():
            raise ValueError("route_prefix is required")
        if not self.oauth_client_import or not self.oauth_client_import.strip():
            raise ValueError("oauth_client_import is required")
        if not self.token_vault_import or not self.token_vault_import.strip():
            raise ValueError("token_vault_import is required")
        if not self.token_vault_path or not self.token_vault_path.strip():
            raise ValueError("token_vault_path is required")
        if (
            not self.refresh_middleware_path
            or not self.refresh_middleware_path.strip()
        ):
            raise ValueError("refresh_middleware_path is required")
        if not self.scope_upgrade_path or not self.scope_upgrade_path.strip():
            raise ValueError("scope_upgrade_path is required")


def list_outbound_oauth_flow_providers() -> list[str]:
    """Return AS.1 vendor ids that can render FS.2b.1 flow scaffolds."""
    return list(ALL_VENDOR_IDS)


def render_outbound_oauth_flow_scaffold(
    options: OutboundOAuthFlowScaffoldOptions,
) -> OutboundOAuthFlowScaffoldResult:
    """Render authorize/callback/token-exchange scaffold files."""
    options.validate()
    providers = tuple(_provider_item(plan) for plan in options.provider_plans)
    _assert_unique_providers(providers)
    return OutboundOAuthFlowScaffoldResult(
        providers=providers,
        files=(
            AuthScaffoldFile(
                options.flow_path.strip("/"),
                _flow_file(providers, options.oauth_client_import),
            ),
            AuthScaffoldFile(
                options.token_vault_path.strip("/"),
                _token_vault_file(options.token_vault_import),
            ),
            AuthScaffoldFile(
                options.refresh_middleware_path.strip("/"),
                _refresh_middleware_file(
                    options.oauth_client_import,
                    options.token_vault_import,
                ),
            ),
            AuthScaffoldFile(
                options.scope_upgrade_path.strip("/"),
                _scope_upgrade_file(options.oauth_client_import),
            ),
        ) + _route_files(options.route_prefix, providers),
        env=_env_vars(providers) + tuple(options.extra_env),
        notes=(
            "FS.2b.2 encrypts callback token sets with the AS.2 token vault",
            "FS.2b.3 refresh middleware rotates refresh tokens before provider calls",
            "FS.2b.4 upgrades missing scopes from an existing connection without disconnect/reconnect",
            "client secrets are declared as env metadata only",
        ),
    )


def _provider_item(plan: VendorOAuthAppConfigPlan) -> OutboundOAuthFlowProviderItem:
    provider = plan.provider.strip().lower()
    vendor = get_vendor(provider)
    metadata = dict(plan.metadata)
    return OutboundOAuthFlowProviderItem(
        provider=provider,
        display_name=plan.display_name,
        callback_url=plan.callback_url,
        scope=tuple(str(s) for s in metadata.get("scope", vendor.default_scopes)),
        authorize_endpoint=str(
            metadata.get("authorize_endpoint") or vendor.authorize_endpoint
        ),
        token_endpoint=str(metadata.get("token_endpoint") or vendor.token_endpoint),
        extra_authorize_params=tuple(
            (str(k), str(v)) for k, v in vendor.extra_authorize_params
        ),
        is_oidc=bool(metadata.get("is_oidc", vendor.is_oidc)),
        client_id_env=_env_name(provider, "CLIENT_ID"),
        client_secret_env=_env_name(provider, "CLIENT_SECRET"),
        token_vault_supported=provider in token_vault.SUPPORTED_PROVIDERS,
    )


def _assert_unique_providers(
    providers: tuple[OutboundOAuthFlowProviderItem, ...],
) -> None:
    seen: set[str] = set()
    for item in providers:
        if item.provider in seen:
            raise ValueError(f"duplicate provider in provider_plans: {item.provider}")
        seen.add(item.provider)


def _env_vars(
    providers: tuple[OutboundOAuthFlowProviderItem, ...],
) -> tuple[AuthScaffoldEnvVar, ...]:
    env: tuple[AuthScaffoldEnvVar, ...] = ()
    for item in providers:
        env += (
            AuthScaffoldEnvVar(item.client_id_env, True, source="fs.2b.1"),
            AuthScaffoldEnvVar(
                item.client_secret_env,
                True,
                sensitive=True,
                source="fs.2b.1",
            ),
        )
    return env + (
        AuthScaffoldEnvVar(
            "OAUTH_TOKEN_VAULT_MASTER_KEY",
            True,
            sensitive=True,
            source="fs.2b.2",
        ),
    )


def _flow_file(
    providers: tuple[OutboundOAuthFlowProviderItem, ...],
    oauth_client_import: str,
) -> str:
    entries = ",\n".join(_provider_entry(item) for item in providers)
    imported = _ts_string(oauth_client_import)
    return f"""// FS.2b.1 outbound OAuth flow scaffold.
// Reuses AS.1 OAuth helpers for authorize, callback state, and token parsing.

import {{
  beginAuthorization,
  parseTokenResponse,
  verifyStateAndConsume,
  type FlowSession,
  type TokenSet,
}} from "{imported}"

export type OutboundOAuthProvider = {{
  provider: string
  displayName: string
  callbackUrl: string
  scope: readonly string[]
  authorizeEndpoint: string
  tokenEndpoint: string
  extraAuthorizeParams: Readonly<Record<string, string>>
  isOidc: boolean
  clientIdEnv: string
  clientSecretEnv: string
  tokenVaultSupported: boolean
}}

export const outboundOAuthProviders = [
{entries},
] as const satisfies readonly OutboundOAuthProvider[]

export type OutboundOAuthFlowRecord = FlowSession
export type OutboundOAuthTokenSet = TokenSet

export function outboundProviderById(provider: string) {{
  return outboundOAuthProviders.find((item) => item.provider === provider) || null
}}

export async function beginOutboundAuthorization(provider: string) {{
  const item = outboundProviderById(provider)
  if (!item) throw new Error(`unsupported outbound OAuth provider: ${{provider}}`)
  return beginAuthorization({{
    provider: item.provider,
    authorizeEndpoint: item.authorizeEndpoint,
    clientId: process.env[item.clientIdEnv]!,
    redirectUri: item.callbackUrl,
    scope: [...item.scope],
    useOidcNonce: item.isOidc,
    extraAuthorizeParams: item.extraAuthorizeParams,
  }})
}}

export function verifyOutboundCallback(flow: OutboundOAuthFlowRecord, state: string) {{
  verifyStateAndConsume(flow, state)
}}

export async function exchangeOutboundCode(
  item: OutboundOAuthProvider,
  flow: OutboundOAuthFlowRecord,
  code: string,
): Promise<OutboundOAuthTokenSet> {{
  const tokenRes = await fetch(item.tokenEndpoint, {{
    method: "POST",
    headers: {{
      "Accept": "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
    }},
    body: new URLSearchParams({{
      grant_type: "authorization_code",
      code,
      redirect_uri: flow.redirectUri,
      client_id: process.env[item.clientIdEnv]!,
      client_secret: process.env[item.clientSecretEnv] || "",
      code_verifier: flow.codeVerifier,
    }}),
  }})
  return parseTokenResponse(await tokenRes.json())
}}
"""


def _token_vault_file(token_vault_import: str) -> str:
    imported = _ts_string(token_vault_import)
    return f"""// FS.2b.2 outbound OAuth token vault bridge.
// Reuses the AS.2 token-vault TS twin; the generated app owns storage.

import {{
  KEY_VERSION_CURRENT,
  SUPPORTED_PROVIDERS,
  TokenVault,
  importMasterKey,
  type EncryptedToken,
}} from "{imported}"
import type {{ OutboundOAuthTokenSet }} from "./outbound-oauth-flow"

export type OutboundOAuthVaultRecord = {{
  userId: string
  provider: string
  accessTokenEnc: EncryptedToken
  refreshTokenEnc: EncryptedToken | null
  tokenType: string | null
  scope: string | null
  expiresAt: string | null
  keyVersion: number
}}

export function assertTokenVaultProvider(provider: string) {{
  if (!SUPPORTED_PROVIDERS.has(provider)) {{
    throw new Error(`unsupported token-vault provider: ${{provider}}`)
  }}
}}

export async function encryptOutboundTokenSet(
  userId: string,
  provider: string,
  token: OutboundOAuthTokenSet,
  masterKeyRaw: string,
): Promise<OutboundOAuthVaultRecord> {{
  assertTokenVaultProvider(provider)
  const vault = new TokenVault(await importMasterKey(base64urlDecode(masterKeyRaw)))
  const accessTokenEnc = await vault.encryptForUser(userId, provider, token.accessToken)
  const refreshTokenEnc = token.refreshToken
    ? await vault.encryptForUser(userId, provider, token.refreshToken)
    : null
  return {{
    userId,
    provider,
    accessTokenEnc,
    refreshTokenEnc,
    tokenType: token.tokenType || null,
    scope: token.scope.length ? token.scope.join(" ") : null,
    expiresAt: token.expiresAt ? new Date(token.expiresAt * 1000).toISOString() : null,
    keyVersion: KEY_VERSION_CURRENT,
  }}
}}

function base64urlDecode(value: string): Uint8Array {{
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(
    Math.ceil(value.length / 4) * 4,
    "=",
  )
  const bin = atob(padded)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}}
"""


def _refresh_middleware_file(
    oauth_client_import: str,
    token_vault_import: str,
) -> str:
    imported_oauth = _ts_string(oauth_client_import)
    imported_vault = _ts_string(token_vault_import)
    return f"""// FS.2b.3 outbound OAuth refresh middleware.
// Reuses AS.1 AutoRefreshFetch + AS.2 token vault records.

import {{
  AutoRefreshFetch,
  TokenRefreshError,
  autoRefresh,
  needsRefresh,
  type RefreshFn,
  type TokenSet,
}} from "{imported_oauth}"
import {{
  TokenVault,
  importMasterKey,
}} from "{imported_vault}"
import {{ outboundProviderById, type OutboundOAuthProvider }} from "./outbound-oauth-flow"
import {{ encryptOutboundTokenSet, type OutboundOAuthVaultRecord }} from "./outbound-token-vault"

export type OutboundOAuthRefreshStore = {{
  load(userId: string, provider: string): Promise<OutboundOAuthVaultRecord | null>
  save(record: OutboundOAuthVaultRecord): Promise<void>
  markExpired?(userId: string, provider: string, reason: string): Promise<void>
}}

export type OutboundOAuthRefreshResult = {{
  token: TokenSet
  vaultRecord: OutboundOAuthVaultRecord
  refreshed: boolean
  rotated: boolean
}}

export async function decryptOutboundVaultRecord(
  record: OutboundOAuthVaultRecord,
  masterKeyRaw: string,
): Promise<TokenSet> {{
  const vault = new TokenVault(await importMasterKey(base64urlDecode(masterKeyRaw)))
  const accessToken = await vault.decryptForUser(
    record.userId,
    record.provider,
    record.accessTokenEnc,
  )
  const refreshToken = record.refreshTokenEnc
    ? await vault.decryptForUser(record.userId, record.provider, record.refreshTokenEnc)
    : null
  return Object.freeze({{
    accessToken,
    refreshToken,
    tokenType: record.tokenType || "Bearer",
    expiresAt: record.expiresAt ? Date.parse(record.expiresAt) / 1000 : null,
    scope: record.scope ? record.scope.split(/[\\s,]+/).filter(Boolean) : [],
    idToken: null,
    raw: {{}},
  }}) as TokenSet
}}

export async function refreshOutboundVaultRecord(
  record: OutboundOAuthVaultRecord,
  provider: OutboundOAuthProvider,
  masterKeyRaw: string,
  opts: {{ skewSeconds?: number; now?: number }} = {{}},
): Promise<OutboundOAuthRefreshResult> {{
  const current = await decryptOutboundVaultRecord(record, masterKeyRaw)
  const due = needsRefresh(current, {{
    skewSeconds: opts.skewSeconds,
    now: opts.now,
  }})
  if (due && !current.refreshToken) {{
    throw new TokenRefreshError(
      "stored outbound OAuth token is expired and has no refresh_token",
    )
  }}

  let rotated = false
  const token = await autoRefresh(current, buildRefreshFn(provider), {{
    skewSeconds: opts.skewSeconds,
    onRotated: (_oldToken, _newToken, didRotate) => {{
      rotated = didRotate
    }},
  }})
  const vaultRecord = due
    ? await encryptOutboundTokenSet(
        record.userId,
        record.provider,
        token,
        masterKeyRaw,
      )
    : record
  return {{ token, vaultRecord, refreshed: due, rotated }}
}}

export async function createOutboundAutoRefreshFetch(
  userId: string,
  providerId: string,
  store: OutboundOAuthRefreshStore,
  masterKeyRaw: string,
  fetchImpl: typeof fetch = fetch,
): Promise<AutoRefreshFetch> {{
  const provider = outboundProviderById(providerId)
  if (!provider) throw new Error(`unsupported outbound OAuth provider: ${{providerId}}`)
  const record = await store.load(userId, provider.provider)
  if (!record) throw new Error(`missing outbound OAuth token for ${{provider.provider}}`)

  const current = await decryptOutboundVaultRecord(record, masterKeyRaw)
  return new AutoRefreshFetch(current, buildRefreshFn(provider), {{
    fetchImpl,
    onRotated: async (_oldToken, newToken) => {{
      const next = await encryptOutboundTokenSet(
        record.userId,
        record.provider,
        newToken,
        masterKeyRaw,
      )
      await store.save(next)
    }},
  }})
}}

function buildRefreshFn(provider: OutboundOAuthProvider): RefreshFn {{
  return async (refreshToken: string) => {{
    const tokenRes = await fetch(provider.tokenEndpoint, {{
      method: "POST",
      headers: {{
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
      }},
      body: new URLSearchParams({{
        grant_type: "refresh_token",
        refresh_token: refreshToken,
        client_id: process.env[provider.clientIdEnv]!,
        client_secret: process.env[provider.clientSecretEnv] || "",
      }}),
    }})
    return tokenRes.json()
  }}
}}

function base64urlDecode(value: string): Uint8Array {{
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(
    Math.ceil(value.length / 4) * 4,
    "=",
  )
  const bin = atob(padded)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}}
"""


def _scope_upgrade_file(oauth_client_import: str) -> str:
    imported_oauth = _ts_string(oauth_client_import)
    return f"""// FS.2b.4 outbound OAuth scope upgrade flow.
// Requires an existing vault record; missing scopes are authorized without disconnect/reconnect.

import {{
  beginAuthorization,
  type TokenSet,
}} from "{imported_oauth}"
import {{ outboundProviderById, type OutboundOAuthProvider }} from "./outbound-oauth-flow"
import {{ encryptOutboundTokenSet, type OutboundOAuthVaultRecord }} from "./outbound-token-vault"
import {{ decryptOutboundVaultRecord }} from "./outbound-refresh-middleware"

export type OutboundOAuthScopeUpgradeStore = {{
  load(userId: string, provider: string): Promise<OutboundOAuthVaultRecord | null>
  save(record: OutboundOAuthVaultRecord): Promise<void>
}}

export type OutboundOAuthScopeUpgradeAuthorization = {{
  url: string
  flow: Awaited<ReturnType<typeof beginAuthorization>>["flow"]
  missingScopes: readonly string[]
  mergedScopes: readonly string[]
}}

export type OutboundOAuthScopeUpgradeResult = {{
  status: "already_granted" | "authorization_required"
  authorization: OutboundOAuthScopeUpgradeAuthorization | null
}}

export async function beginOutboundScopeUpgrade(
  userId: string,
  providerId: string,
  requestedScopes: readonly string[],
  store: OutboundOAuthScopeUpgradeStore,
  masterKeyRaw: string,
): Promise<OutboundOAuthScopeUpgradeResult> {{
  const provider = outboundProviderById(providerId)
  if (!provider) throw new Error(`unsupported outbound OAuth provider: ${{providerId}}`)

  const record = await store.load(userId, provider.provider)
  if (!record) {{
    throw new Error("scope upgrade requires an existing outbound OAuth connection")
  }}

  const current = await decryptOutboundVaultRecord(record, masterKeyRaw)
  const missingScopes = missingScopeValues(current.scope, requestedScopes)
  if (missingScopes.length === 0) {{
    return {{ status: "already_granted", authorization: null }}
  }}

  const mergedScopes = mergeScopes(current.scope, requestedScopes)
  const {{ url, flow }} = await beginAuthorization({{
    provider: provider.provider,
    authorizeEndpoint: provider.authorizeEndpoint,
    clientId: process.env[provider.clientIdEnv]!,
    redirectUri: provider.callbackUrl,
    scope: mergedScopes,
    useOidcNonce: provider.isOidc,
    extraAuthorizeParams: provider.extraAuthorizeParams,
    extra: {{
      scope_upgrade: "true",
      current_scope: current.scope.join(" "),
      requested_scope: requestedScopes.join(" "),
      missing_scope: missingScopes.join(" "),
    }},
  }})
  return {{
    status: "authorization_required",
    authorization: {{
      url,
      flow,
      missingScopes,
      mergedScopes,
    }},
  }}
}}

export async function mergeOutboundScopeUpgradeToken(
  previousRecord: OutboundOAuthVaultRecord,
  upgradedToken: TokenSet,
  masterKeyRaw: string,
): Promise<OutboundOAuthVaultRecord> {{
  const previous = await decryptOutboundVaultRecord(previousRecord, masterKeyRaw)
  const mergedToken = Object.freeze({{
    accessToken: upgradedToken.accessToken,
    refreshToken: upgradedToken.refreshToken || previous.refreshToken,
    tokenType: upgradedToken.tokenType || previous.tokenType,
    expiresAt: upgradedToken.expiresAt ?? previous.expiresAt,
    scope: mergeScopes(previous.scope, upgradedToken.scope),
    idToken: upgradedToken.idToken || previous.idToken,
    raw: upgradedToken.raw,
  }}) as TokenSet
  return encryptOutboundTokenSet(
    previousRecord.userId,
    previousRecord.provider,
    mergedToken,
    masterKeyRaw,
  )
}}

export function missingScopeValues(
  currentScopes: readonly string[],
  requestedScopes: readonly string[],
): readonly string[] {{
  const current = new Set(currentScopes.map(normalizeScope).filter(Boolean))
  return requestedScopes
    .map(normalizeScope)
    .filter((scope, index, all) => scope && all.indexOf(scope) === index)
    .filter((scope) => !current.has(scope))
}}

function mergeScopes(
  currentScopes: readonly string[],
  requestedScopes: readonly string[],
): readonly string[] {{
  const out: string[] = []
  for (const scope of [...currentScopes, ...requestedScopes].map(normalizeScope)) {{
    if (scope && !out.includes(scope)) out.push(scope)
  }}
  return Object.freeze(out) as readonly string[]
}}

function normalizeScope(scope: string): string {{
  return String(scope).trim()
}}
"""


def _provider_entry(item: OutboundOAuthFlowProviderItem) -> str:
    scope = ", ".join(f'"{_ts_string(s)}"' for s in item.scope)
    extra = ", ".join(
        f'"{_ts_string(k)}": "{_ts_string(v)}"'
        for k, v in item.extra_authorize_params
    )
    return f"""  {{
    provider: "{_ts_string(item.provider)}",
    displayName: "{_ts_string(item.display_name)}",
    callbackUrl: "{_ts_string(item.callback_url)}",
    scope: [{scope}],
    authorizeEndpoint: "{_ts_string(item.authorize_endpoint)}",
    tokenEndpoint: "{_ts_string(item.token_endpoint)}",
    extraAuthorizeParams: {{{extra}}},
    isOidc: {_ts_bool(item.is_oidc)},
    clientIdEnv: "{_ts_string(item.client_id_env)}",
    clientSecretEnv: "{_ts_string(item.client_secret_env)}",
    tokenVaultSupported: {_ts_bool(item.token_vault_supported)},
  }}"""


def _route_files(
    route_prefix: str,
    providers: tuple[OutboundOAuthFlowProviderItem, ...],
) -> tuple[AuthScaffoldFile, ...]:
    prefix = route_prefix.strip("/")
    files: tuple[AuthScaffoldFile, ...] = ()
    for item in providers:
        files += (
            AuthScaffoldFile(
                f"{prefix}/{item.provider}/authorize/route.ts",
                _authorize_route(item.provider),
            ),
            AuthScaffoldFile(
                f"{prefix}/{item.provider}/callback/route.ts",
                _callback_route(item.provider),
            ),
            AuthScaffoldFile(
                f"{prefix}/{item.provider}/scope-upgrade/route.ts",
                _scope_upgrade_route(item.provider),
            ),
        )
    return files


def _authorize_route(provider: str) -> str:
    provider_literal = _ts_string(provider)
    cookie_name = _cookie_name(provider)
    return f"""// FS.2b.1 outbound OAuth authorize route for {provider_literal}.

import {{ beginOutboundAuthorization }} from "@/auth/outbound-oauth-flow"

export async function GET() {{
  const {{ url, flow }} = await beginOutboundAuthorization("{provider_literal}")
  const res = Response.redirect(url)
  res.headers.append(
    "Set-Cookie",
    `{cookie_name}=${{encodeURIComponent(JSON.stringify(flow))}}; HttpOnly; Path=/; SameSite=Lax`,
  )
  return res
}}
"""


def _callback_route(provider: str) -> str:
    provider_literal = _ts_string(provider)
    cookie_name = _cookie_name(provider)
    return f"""// FS.2b.1 outbound OAuth callback route for {provider_literal}.
// FS.2b.2 stores the returned token set in the generated app's token vault.

import {{
  exchangeOutboundCode,
  outboundProviderById,
  verifyOutboundCallback,
  type OutboundOAuthFlowRecord,
}} from "@/auth/outbound-oauth-flow"
import {{ encryptOutboundTokenSet }} from "@/auth/outbound-token-vault"

function readFlow(req: Request): OutboundOAuthFlowRecord {{
  const cookie = req.headers.get("cookie") || ""
  const found = cookie.split(";").find((part) => part.trim().startsWith("{cookie_name}="))
  if (!found) throw new Error("missing outbound OAuth flow cookie")
  return JSON.parse(decodeURIComponent(found.split("=", 2)[1])) as OutboundOAuthFlowRecord
}}

export async function GET(req: Request) {{
  const provider = outboundProviderById("{provider_literal}")
  if (!provider) return Response.json({{ error: "unsupported_provider" }}, {{ status: 404 }})
  if (!provider.tokenVaultSupported) {{
    return Response.json({{ error: "unsupported_token_vault_provider" }}, {{ status: 501 }})
  }}

  const url = new URL(req.url)
  const code = url.searchParams.get("code")
  const state = url.searchParams.get("state")
  if (!code || !state) return Response.json({{ error: "missing_oauth_callback_params" }}, {{ status: 400 }})

  const flow = readFlow(req)
  verifyOutboundCallback(flow, state)

  const token = await exchangeOutboundCode(provider, flow, code)
  const userId = req.headers.get("x-omnisight-user-id")
  if (!userId) return Response.json({{ error: "missing_user_id" }}, {{ status: 401 }})

  const vaultRecord = await encryptOutboundTokenSet(
    userId,
    provider.provider,
    token,
    process.env.OAUTH_TOKEN_VAULT_MASTER_KEY!,
  )
  return Response.json({{
    ok: true,
    provider: provider.provider,
    vaultRecord,
  }})
}}
"""


def _scope_upgrade_route(provider: str) -> str:
    provider_literal = _ts_string(provider)
    cookie_name = _cookie_name(provider)
    return f"""// FS.2b.4 outbound OAuth scope-upgrade route for {provider_literal}.

import {{ beginOutboundScopeUpgrade }} from "@/auth/outbound-scope-upgrade"
import {{ outboundTokenStore }} from "@/auth/outbound-token-store"

export async function POST(req: Request) {{
  const userId = req.headers.get("x-omnisight-user-id")
  if (!userId) return Response.json({{ error: "missing_user_id" }}, {{ status: 401 }})

  const body = await req.json()
  const requestedScopes = Array.isArray(body.scopes)
    ? body.scopes.map((scope: unknown) => String(scope))
    : []
  if (requestedScopes.length === 0) {{
    return Response.json({{ error: "missing_requested_scopes" }}, {{ status: 400 }})
  }}

  const result = await beginOutboundScopeUpgrade(
    userId,
    "{provider_literal}",
    requestedScopes,
    outboundTokenStore,
    process.env.OAUTH_TOKEN_VAULT_MASTER_KEY!,
  )
  if (result.status === "already_granted") return Response.json({{ ok: true, status: result.status }})

  const res = Response.json({{
    ok: true,
    status: result.status,
    url: result.authorization!.url,
    missingScopes: result.authorization!.missingScopes,
  }})
  res.headers.append(
    "Set-Cookie",
    `{cookie_name}=${{encodeURIComponent(JSON.stringify(result.authorization!.flow))}}; HttpOnly; Path=/; SameSite=Lax`,
  )
  return res
}}
"""


def _env_name(provider: str, suffix: str) -> str:
    return f"OAUTH_{provider.upper()}_{suffix}"


def _cookie_name(provider: str) -> str:
    return f"outbound_oauth_flow_{provider}"


def _ts_bool(value: bool) -> str:
    return "true" if value else "false"


def _ts_string(value: str | None) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "OutboundOAuthFlowProviderItem",
    "OutboundOAuthFlowScaffoldOptions",
    "OutboundOAuthFlowScaffoldResult",
    "list_outbound_oauth_flow_providers",
    "render_outbound_oauth_flow_scaffold",
]
