"""FS.2b.1 -- Outbound integration OAuth flow scaffolds.

FS.2b connects generated apps to third-party APIs (GitHub, Slack,
Notion, and similar outbound integrations). This module renders the
minimal generated-app scaffold for the authorization-code flow:
authorize route, callback route, token exchange, token-vault storage
bridge, refresh middleware, and scope upgrades for already-connected
providers. Disconnect/revoke endpoints erase the generated app's local
token record after a best-effort provider revocation call.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable constants/classes/functions only. Each
render derives files/env metadata from explicit provider plans and the
AS.1 vendor catalog; there is no cache, singleton, env read, network
IO, or shared mutable state across uvicorn workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from backend.auth_provisioning.self_hosted import (
    AuthScaffoldEnvVar,
    AuthScaffoldFile,
)
from backend.auth_provisioning.vendor_oauth import VendorOAuthAppConfigPlan
from backend.security import token_vault
from backend.security.oauth_vendors import VendorNotFoundError, get_vendor


OUTBOUND_OAUTH_VENDOR_IDS: tuple[str, ...] = (
    "github",
    "slack",
    "google_workspace",
    "microsoft_365",
    "notion",
    "salesforce",
    "hubspot",
    "zoom",
    "stripe_connect",
    "discord",
)


@dataclass(frozen=True)
class OutboundOAuthVendorCatalogItem:
    """One FS.2b.6 outbound integration vendor catalog entry."""

    provider: str
    display_name: str
    scope: tuple[str, ...]
    authorize_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None
    extra_authorize_params: tuple[tuple[str, str], ...] = ()
    is_oidc: bool = False
    token_vault_provider: str | None = None

    def to_plan(
        self,
        *,
        app_name: str,
        app_base_url: str,
        callback_path: str,
    ) -> VendorOAuthAppConfigPlan:
        callback_url = _callback_url(
            base_url=app_base_url,
            callback_path=callback_path,
            provider=self.provider,
        )
        return VendorOAuthAppConfigPlan(
            provider=self.provider,
            display_name=self.display_name,
            app_name=app_name,
            callback_url=callback_url,
            automation="manual",
            console_url=_outbound_console_urls()[self.provider],
            instructions=(),
            required_env=("AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "AUTH_PROVIDER"),
            warnings=(),
            metadata={
                "scope": list(self.scope),
                "authorize_endpoint": self.authorize_endpoint,
                "token_endpoint": self.token_endpoint,
                "revocation_endpoint": self.revocation_endpoint,
                "extra_authorize_params": [
                    list(p) for p in self.extra_authorize_params
                ],
                "is_oidc": self.is_oidc,
                "token_vault_provider": self.token_vault_provider,
            },
        )


@dataclass(frozen=True)
class OutboundOAuthVendorCatalogOptions:
    """Inputs for rendering the FS.2b.6 outbound integration vendor catalog."""

    app_name: str
    app_base_url: str
    callback_path: str = "/api/integrations/{provider}/callback"

    def validate(self) -> None:
        if not self.app_name or not self.app_name.strip():
            raise ValueError("app_name is required")
        if not self.app_base_url or not self.app_base_url.strip():
            raise ValueError("app_base_url is required")
        if not self.callback_path or not self.callback_path.strip():
            raise ValueError("callback_path is required")


OUTBOUND_OAUTH_VENDOR_ITEMS: tuple[OutboundOAuthVendorCatalogItem, ...] = (
    OutboundOAuthVendorCatalogItem(
        provider="github",
        display_name="GitHub",
        scope=("read:user", "user:email", "repo"),
        authorize_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        revocation_endpoint=None,
        extra_authorize_params=(("allow_signup", "true"),),
        token_vault_provider="github",
    ),
    OutboundOAuthVendorCatalogItem(
        provider="slack",
        display_name="Slack",
        scope=("users:read", "users:read.email", "channels:read", "chat:write"),
        authorize_endpoint="https://slack.com/oauth/v2/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.access",
        revocation_endpoint="https://slack.com/api/auth.revoke",
    ),
    OutboundOAuthVendorCatalogItem(
        provider="google_workspace",
        display_name="Google Workspace",
        scope=(
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/admin.directory.user.readonly",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        ),
        authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        revocation_endpoint="https://oauth2.googleapis.com/revoke",
        extra_authorize_params=(
            ("access_type", "offline"),
            ("prompt", "consent"),
        ),
        is_oidc=True,
        token_vault_provider="google",
    ),
    OutboundOAuthVendorCatalogItem(
        provider="microsoft_365",
        display_name="Microsoft 365",
        scope=(
            "openid",
            "email",
            "profile",
            "offline_access",
            "User.Read",
            "Files.Read",
        ),
        authorize_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        revocation_endpoint=None,
        is_oidc=True,
        token_vault_provider="microsoft",
    ),
    OutboundOAuthVendorCatalogItem(
        provider="notion",
        display_name="Notion",
        scope=(),
        authorize_endpoint="https://api.notion.com/v1/oauth/authorize",
        token_endpoint="https://api.notion.com/v1/oauth/token",
        revocation_endpoint=None,
        extra_authorize_params=(("owner", "user"),),
    ),
    OutboundOAuthVendorCatalogItem(
        provider="salesforce",
        display_name="Salesforce",
        scope=("openid", "email", "profile", "api", "refresh_token"),
        authorize_endpoint="https://login.salesforce.com/services/oauth2/authorize",
        token_endpoint="https://login.salesforce.com/services/oauth2/token",
        revocation_endpoint="https://login.salesforce.com/services/oauth2/revoke",
        is_oidc=True,
    ),
    OutboundOAuthVendorCatalogItem(
        provider="hubspot",
        display_name="HubSpot",
        scope=("oauth", "crm.objects.contacts.read", "crm.objects.companies.read"),
        authorize_endpoint="https://app.hubspot.com/oauth/authorize",
        token_endpoint="https://api.hubapi.com/oauth/v1/token",
        revocation_endpoint=None,
    ),
    OutboundOAuthVendorCatalogItem(
        provider="zoom",
        display_name="Zoom",
        scope=("user:read", "meeting:read", "meeting:write"),
        authorize_endpoint="https://zoom.us/oauth/authorize",
        token_endpoint="https://zoom.us/oauth/token",
        revocation_endpoint="https://zoom.us/oauth/revoke",
    ),
    OutboundOAuthVendorCatalogItem(
        provider="stripe_connect",
        display_name="Stripe Connect",
        scope=("read_write",),
        authorize_endpoint="https://connect.stripe.com/oauth/authorize",
        token_endpoint="https://connect.stripe.com/oauth/token",
        revocation_endpoint=None,
    ),
    OutboundOAuthVendorCatalogItem(
        provider="discord",
        display_name="Discord",
        scope=("identify", "email", "guilds"),
        authorize_endpoint="https://discord.com/oauth2/authorize",
        token_endpoint="https://discord.com/api/oauth2/token",
        revocation_endpoint="https://discord.com/api/oauth2/token/revoke",
    ),
)


OUTBOUND_OAUTH_VENDORS: Mapping[str, OutboundOAuthVendorCatalogItem] = MappingProxyType(
    {item.provider: item for item in OUTBOUND_OAUTH_VENDOR_ITEMS}
)


def _outbound_console_urls() -> Mapping[str, str]:
    return MappingProxyType({
        "github": "https://github.com/settings/developers",
        "slack": "https://api.slack.com/apps",
        "google_workspace": "https://console.cloud.google.com/apis/credentials",
        "microsoft_365": (
            "https://entra.microsoft.com/#view/"
            "Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
        ),
        "notion": "https://www.notion.so/my-integrations",
        "salesforce": "https://login.salesforce.com/setup",
        "hubspot": "https://developers.hubspot.com/",
        "zoom": "https://marketplace.zoom.us/develop/create",
        "stripe_connect": "https://dashboard.stripe.com/settings/connect",
        "discord": "https://discord.com/developers/applications",
    })


@dataclass(frozen=True)
class OutboundOAuthFlowProviderItem:
    """One outbound OAuth provider exposed by the generated app."""

    provider: str
    display_name: str
    callback_url: str
    scope: tuple[str, ...]
    authorize_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None
    extra_authorize_params: tuple[tuple[str, str], ...]
    is_oidc: bool
    client_id_env: str
    client_secret_env: str
    token_vault_supported: bool
    token_vault_provider: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "callback_url": self.callback_url,
            "scope": list(self.scope),
            "authorize_endpoint": self.authorize_endpoint,
            "token_endpoint": self.token_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "extra_authorize_params": [list(p) for p in self.extra_authorize_params],
            "is_oidc": self.is_oidc,
            "client_id_env": self.client_id_env,
            "client_secret_env": self.client_secret_env,
            "token_vault_supported": self.token_vault_supported,
            "token_vault_provider": self.token_vault_provider,
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
    disconnect_path: str = "auth/outbound-disconnect.ts"
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
        if not self.disconnect_path or not self.disconnect_path.strip():
            raise ValueError("disconnect_path is required")


def get_outbound_oauth_vendor(provider: str) -> OutboundOAuthVendorCatalogItem:
    """Return the FS.2b.6 outbound integration catalog entry."""
    key = provider.strip().lower()
    try:
        return OUTBOUND_OAUTH_VENDORS[key]
    except KeyError:
        raise KeyError(
            f"unknown outbound OAuth vendor {provider!r}; "
            f"known: {', '.join(OUTBOUND_OAUTH_VENDOR_IDS)}"
        ) from None


def render_outbound_oauth_vendor_catalog(
    options: OutboundOAuthVendorCatalogOptions,
) -> tuple[VendorOAuthAppConfigPlan, ...]:
    """Render the FS.2b.6 ten-vendor outbound OAuth setup catalog."""
    options.validate()
    return tuple(
        item.to_plan(
            app_name=options.app_name,
            app_base_url=options.app_base_url,
            callback_path=options.callback_path,
        )
        for item in OUTBOUND_OAUTH_VENDOR_ITEMS
    )


def list_outbound_oauth_flow_providers() -> list[str]:
    """Return FS.2b.6 outbound integration vendor ids."""
    return list(OUTBOUND_OAUTH_VENDOR_IDS)


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
            AuthScaffoldFile(
                options.disconnect_path.strip("/"),
                _disconnect_file(),
            ),
        ) + _route_files(options.route_prefix, providers),
        env=_env_vars(providers) + tuple(options.extra_env),
        notes=(
            "FS.2b.2 encrypts callback token sets with the AS.2 token vault",
            "FS.2b.3 refresh middleware rotates refresh tokens before provider calls",
            "FS.2b.4 upgrades missing scopes from an existing connection without disconnect/reconnect",
            "FS.2b.5 disconnect deletes local vault records after best-effort IdP revocation",
            "FS.2b.6 outbound vendor catalog pins GitHub/Slack/Google Workspace/Microsoft 365/Notion/Salesforce/HubSpot/Zoom/Stripe Connect/Discord",
            "client secrets are declared as env metadata only",
        ),
    )


def _provider_item(plan: VendorOAuthAppConfigPlan) -> OutboundOAuthFlowProviderItem:
    provider = plan.provider.strip().lower()
    metadata = dict(plan.metadata)
    try:
        vendor = get_vendor(provider)
        display_name = plan.display_name
        scope = tuple(str(s) for s in metadata.get("scope", vendor.default_scopes))
        authorize_endpoint = str(
            metadata.get("authorize_endpoint") or vendor.authorize_endpoint
        )
        token_endpoint = str(metadata.get("token_endpoint") or vendor.token_endpoint)
        revocation_endpoint = (
            str(metadata["revocation_endpoint"])
            if metadata.get("revocation_endpoint")
            else vendor.revocation_endpoint
        )
        extra_authorize_params = tuple(
            (str(k), str(v))
            for k, v in metadata.get(
                "extra_authorize_params",
                vendor.extra_authorize_params,
            )
        )
        is_oidc = bool(metadata.get("is_oidc", vendor.is_oidc))
    except VendorNotFoundError:
        required = ("authorize_endpoint", "token_endpoint", "scope", "is_oidc")
        missing = [name for name in required if name not in metadata]
        if missing:
            raise KeyError(
                f"outbound provider {provider!r} is not in AS.1 catalog "
                f"and missing metadata: {', '.join(missing)}"
            ) from None
        display_name = plan.display_name
        scope = tuple(str(s) for s in metadata["scope"])
        authorize_endpoint = str(metadata["authorize_endpoint"])
        token_endpoint = str(metadata["token_endpoint"])
        revocation_endpoint = (
            str(metadata["revocation_endpoint"])
            if metadata.get("revocation_endpoint")
            else None
        )
        extra_authorize_params = tuple(
            (str(k), str(v)) for k, v in metadata.get("extra_authorize_params", ())
        )
        is_oidc = bool(metadata["is_oidc"])
    token_vault_provider = metadata.get("token_vault_provider") or provider
    token_vault_supported = str(token_vault_provider) in token_vault.SUPPORTED_PROVIDERS
    return OutboundOAuthFlowProviderItem(
        provider=provider,
        display_name=display_name,
        callback_url=plan.callback_url,
        scope=scope,
        authorize_endpoint=authorize_endpoint,
        token_endpoint=token_endpoint,
        revocation_endpoint=revocation_endpoint,
        extra_authorize_params=extra_authorize_params,
        is_oidc=is_oidc,
        client_id_env=_env_name(provider, "CLIENT_ID"),
        client_secret_env=_env_name(provider, "CLIENT_SECRET"),
        token_vault_supported=token_vault_supported,
        token_vault_provider=str(token_vault_provider) if token_vault_supported else None,
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
  revocationEndpoint: string | null
  extraAuthorizeParams: Readonly<Record<string, string>>
  isOidc: boolean
  clientIdEnv: string
  clientSecretEnv: string
  tokenVaultSupported: boolean
  tokenVaultProvider: string | null
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


def _disconnect_file() -> str:
    return """// FS.2b.5 outbound OAuth disconnect + revoke helper.
// Best-effort IdP revocation; local token erasure always proceeds for DSAR.

import { outboundProviderById } from "./outbound-oauth-flow"
import {
  decryptOutboundVaultRecord,
} from "./outbound-refresh-middleware"
import type { OutboundOAuthVaultRecord } from "./outbound-token-vault"

export type OutboundOAuthDisconnectStore = {
  load(userId: string, provider: string): Promise<OutboundOAuthVaultRecord | null>
  delete(userId: string, provider: string): Promise<void>
}

export type OutboundOAuthDisconnectTrigger = "user_unlink" | "dsar_erasure"

export type OutboundOAuthDisconnectResult = {
  ok: true
  provider: string
  status: "revoked" | "not_linked"
  trigger: OutboundOAuthDisconnectTrigger
  revocationAttempted: boolean
  revocationOutcome: "success" | "revocation_failed" | null
  localDeleted: boolean
  error: string | null
}

export async function disconnectOutboundOAuth(
  userId: string,
  providerId: string,
  store: OutboundOAuthDisconnectStore,
  masterKeyRaw: string,
  trigger: OutboundOAuthDisconnectTrigger = "user_unlink",
): Promise<OutboundOAuthDisconnectResult> {
  const provider = outboundProviderById(providerId)
  if (!provider) throw new Error(`unsupported outbound OAuth provider: ${providerId}`)

  const record = await store.load(userId, provider.provider)
  if (!record) {
    return {
      ok: true,
      provider: provider.provider,
      status: "not_linked",
      trigger,
      revocationAttempted: false,
      revocationOutcome: null,
      localDeleted: false,
      error: null,
    }
  }

  let revocationAttempted = false
  let revocationOutcome: "success" | "revocation_failed" | null = null
  let error: string | null = null

  if (provider.revocationEndpoint) {
    try {
      const token = await decryptOutboundVaultRecord(record, masterKeyRaw)
      const tokenToRevoke = token.refreshToken || token.accessToken
      const hint = token.refreshToken ? "refresh_token" : "access_token"
      if (tokenToRevoke) {
        revocationAttempted = true
        const res = await fetch(provider.revocationEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            token: tokenToRevoke,
            token_type_hint: hint,
          }),
        })
        if (!res.ok) throw new Error(`revocation_http_${res.status}`)
        revocationOutcome = "success"
      }
    } catch (err) {
      revocationOutcome = "revocation_failed"
      error = err instanceof Error ? err.message : "revocation_failed"
    }
  }

  await store.delete(userId, provider.provider)
  return {
    ok: true,
    provider: provider.provider,
    status: "revoked",
    trigger,
    revocationAttempted,
    revocationOutcome,
    localDeleted: true,
    error,
  }
}
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
  vaultProvider: string
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
  vaultProvider: string = provider,
): Promise<OutboundOAuthVaultRecord> {{
  assertTokenVaultProvider(vaultProvider)
  const vault = new TokenVault(await importMasterKey(base64urlDecode(masterKeyRaw)))
  const accessTokenEnc = await vault.encryptForUser(userId, vaultProvider, token.accessToken)
  const refreshTokenEnc = token.refreshToken
    ? await vault.encryptForUser(userId, vaultProvider, token.refreshToken)
    : null
  return {{
    userId,
    provider,
    vaultProvider,
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
  const vaultProvider = record.vaultProvider || record.provider
  const accessToken = await vault.decryptForUser(
    record.userId,
    vaultProvider,
    record.accessTokenEnc,
  )
  const refreshToken = record.refreshTokenEnc
    ? await vault.decryptForUser(record.userId, vaultProvider, record.refreshTokenEnc)
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
        record.vaultProvider || record.provider,
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
        record.vaultProvider || record.provider,
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
    previousRecord.vaultProvider || previousRecord.provider,
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
    revocationEndpoint: {_ts_nullable_string(item.revocation_endpoint)},
    extraAuthorizeParams: {{{extra}}},
    isOidc: {_ts_bool(item.is_oidc)},
    clientIdEnv: "{_ts_string(item.client_id_env)}",
    clientSecretEnv: "{_ts_string(item.client_secret_env)}",
    tokenVaultSupported: {_ts_bool(item.token_vault_supported)},
    tokenVaultProvider: {_ts_nullable_string(item.token_vault_provider)},
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
            AuthScaffoldFile(
                f"{prefix}/{item.provider}/disconnect/route.ts",
                _disconnect_route(item.provider),
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


def _disconnect_route(provider: str) -> str:
    provider_literal = _ts_string(provider)
    return f"""// FS.2b.5 outbound OAuth disconnect + revoke route for {provider_literal}.

import {{ disconnectOutboundOAuth }} from "@/auth/outbound-disconnect"
import {{ outboundTokenStore }} from "@/auth/outbound-token-store"

export async function DELETE(req: Request) {{
  const userId = req.headers.get("x-omnisight-user-id")
  if (!userId) return Response.json({{ error: "missing_user_id" }}, {{ status: 401 }})

  const url = new URL(req.url)
  const triggerParam = url.searchParams.get("trigger")
  const trigger = triggerParam === "dsar_erasure" ? "dsar_erasure" : "user_unlink"
  const result = await disconnectOutboundOAuth(
    userId,
    "{provider_literal}",
    outboundTokenStore,
    process.env.OAUTH_TOKEN_VAULT_MASTER_KEY!,
    trigger,
  )
  return Response.json(result)
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
    provider.tokenVaultProvider || provider.provider,
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


def _callback_url(*, base_url: str, callback_path: str, provider: str) -> str:
    base = base_url.strip().rstrip("/")
    path = callback_path.strip()
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path.format(provider=provider)}"


def _cookie_name(provider: str) -> str:
    return f"outbound_oauth_flow_{provider}"


def _ts_bool(value: bool) -> str:
    return "true" if value else "false"


def _ts_nullable_string(value: str | None) -> str:
    if value is None:
        return "null"
    return f'"{_ts_string(value)}"'


def _ts_string(value: str | None) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "OUTBOUND_OAUTH_VENDOR_IDS",
    "OUTBOUND_OAUTH_VENDOR_ITEMS",
    "OUTBOUND_OAUTH_VENDORS",
    "OutboundOAuthVendorCatalogItem",
    "OutboundOAuthVendorCatalogOptions",
    "OutboundOAuthFlowProviderItem",
    "OutboundOAuthFlowScaffoldOptions",
    "OutboundOAuthFlowScaffoldResult",
    "get_outbound_oauth_vendor",
    "list_outbound_oauth_flow_providers",
    "render_outbound_oauth_vendor_catalog",
    "render_outbound_oauth_flow_scaffold",
]
