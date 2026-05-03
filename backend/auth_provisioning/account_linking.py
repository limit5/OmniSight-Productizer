"""FS.2.4 -- Account-linking + multi-provider auth stack scaffolds.

FS.2.3 renders per-vendor OAuth app setup plans. This module composes
those plans into generated-app files for a multi-provider self-hosted
auth stack while preserving the AS.0.3 takeover-prevention contract:
linking a new OAuth provider to an existing password account must ask
the app to verify the password before the provider method is bound.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable constants/classes/functions only. Each
render derives files/env metadata from explicit options and the AS.1
vendor catalog; there is no cache, singleton, env read, network IO, or
shared mutable state across uvicorn workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from backend import account_linking as _linking
from backend.auth_provisioning.self_hosted import (
    AuthScaffoldEnvVar,
    AuthScaffoldFile,
    normalize_self_hosted_framework,
)
from backend.auth_provisioning.vendor_oauth import VendorOAuthAppConfigPlan
from backend.security.oauth_vendors import ALL_VENDOR_IDS, get_vendor


_FRAMEWORK_DEPENDENCIES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "nextauth": ("next-auth",),
    "lucia": ("lucia",),
})


class UnsupportedAccountLinkingProviderError(ValueError):
    """Requested OAuth provider is not supported by AS.0.3 account linking."""

    def __init__(self, provider: str):
        supported = ", ".join(list_account_linking_stack_providers())
        super().__init__(
            f"Unsupported account-linking provider '{provider}'. "
            f"Expected one of: {supported}"
        )
        self.provider = provider


@dataclass(frozen=True)
class AccountLinkingProviderStackItem:
    """One provider entry in the generated multi-provider stack."""

    provider: str
    display_name: str
    callback_url: str
    scope: tuple[str, ...]
    authorize_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str | None
    client_id_env: str
    client_secret_env: str
    auth_method: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "callback_url": self.callback_url,
            "scope": list(self.scope),
            "authorize_endpoint": self.authorize_endpoint,
            "token_endpoint": self.token_endpoint,
            "userinfo_endpoint": self.userinfo_endpoint,
            "client_id_env": self.client_id_env,
            "client_secret_env": self.client_secret_env,
            "auth_method": self.auth_method,
        }


@dataclass(frozen=True)
class AccountLinkingStackResult:
    """Manifest for FS.2.4 account-linking stack files."""

    framework: str
    providers: tuple[AccountLinkingProviderStackItem, ...]
    files: tuple[AuthScaffoldFile, ...]
    env: tuple[AuthScaffoldEnvVar, ...]
    dependencies: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "providers": [p.to_dict() for p in self.providers],
            "files": [f.to_dict() for f in self.files],
            "env": [v.to_dict() for v in self.env],
            "dependencies": list(self.dependencies),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class AccountLinkingStackOptions:
    """Inputs for rendering an account-linking multi-provider stack."""

    framework: str
    provider_plans: tuple[VendorOAuthAppConfigPlan, ...]
    provider_stack_path: str = "auth/oauth-provider-stack.ts"
    account_linking_path: str = "auth/account-linking.ts"
    route_prefix: str = "app/api/auth"
    extra_env: tuple[AuthScaffoldEnvVar, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        normalize_self_hosted_framework(self.framework)
        if len(self.provider_plans) < 2:
            raise ValueError("provider_plans must contain at least two providers")
        if not self.provider_stack_path or not self.provider_stack_path.strip():
            raise ValueError("provider_stack_path is required")
        if not self.account_linking_path or not self.account_linking_path.strip():
            raise ValueError("account_linking_path is required")
        if not self.route_prefix or not self.route_prefix.strip():
            raise ValueError("route_prefix is required")


def list_account_linking_stack_providers() -> list[str]:
    """Return AS.1 providers whose auth-method tags pass AS.0.3 linking."""
    return [
        provider
        for provider in ALL_VENDOR_IDS
        if _linking.is_valid_method(_auth_method(provider))
    ]


def render_account_linking_stack(
    options: AccountLinkingStackOptions,
) -> AccountLinkingStackResult:
    """Render account-linking + multi-provider stack scaffold files."""
    options.validate()
    framework = normalize_self_hosted_framework(options.framework)
    providers = tuple(_provider_item(plan) for plan in options.provider_plans)
    _assert_unique_providers(providers)
    env = _env_vars(providers) + tuple(options.extra_env)
    return AccountLinkingStackResult(
        framework=framework,
        providers=providers,
        files=(
            AuthScaffoldFile(
                options.provider_stack_path.strip("/"),
                _provider_stack_file(providers),
            ),
            AuthScaffoldFile(
                options.account_linking_path.strip("/"),
                _account_linking_file(framework, providers),
            ),
        ) + _route_files(options, framework, providers),
        env=env,
        dependencies=_FRAMEWORK_DEPENDENCIES[framework],
        notes=(
            "account linking requires password confirmation before adding "
            "a provider to an existing password account",
            "client secrets are declared as env metadata only",
        ),
    )


def _provider_item(plan: VendorOAuthAppConfigPlan) -> AccountLinkingProviderStackItem:
    provider = plan.provider.strip().lower()
    method = _auth_method(provider)
    if not _linking.is_valid_method(method):
        raise UnsupportedAccountLinkingProviderError(plan.provider)
    metadata = dict(plan.metadata)
    vendor = get_vendor(provider)
    return AccountLinkingProviderStackItem(
        provider=provider,
        display_name=plan.display_name,
        callback_url=plan.callback_url,
        scope=tuple(str(s) for s in metadata.get("scope", ())),
        authorize_endpoint=str(
            metadata.get("authorize_endpoint") or vendor.authorize_endpoint
        ),
        token_endpoint=str(metadata.get("token_endpoint") or vendor.token_endpoint),
        userinfo_endpoint=(
            str(metadata["userinfo_endpoint"])
            if metadata.get("userinfo_endpoint")
            else vendor.userinfo_endpoint
        ),
        client_id_env=_env_name(provider, "CLIENT_ID"),
        client_secret_env=_env_name(provider, "CLIENT_SECRET"),
        auth_method=method,
    )


def _assert_unique_providers(
    providers: tuple[AccountLinkingProviderStackItem, ...],
) -> None:
    seen: set[str] = set()
    for item in providers:
        if item.provider in seen:
            raise ValueError(f"duplicate provider in provider_plans: {item.provider}")
        seen.add(item.provider)


def _env_vars(
    providers: tuple[AccountLinkingProviderStackItem, ...],
) -> tuple[AuthScaffoldEnvVar, ...]:
    env: tuple[AuthScaffoldEnvVar, ...] = ()
    for item in providers:
        env += (
            AuthScaffoldEnvVar(item.client_id_env, True, source="fs.2.3"),
            AuthScaffoldEnvVar(
                item.client_secret_env,
                True,
                sensitive=True,
                source="fs.2.3",
            ),
        )
    return env + (
        AuthScaffoldEnvVar("AUTH_LINK_PASSWORD_CONFIRMATION", True),
    )


def _provider_stack_file(
    providers: tuple[AccountLinkingProviderStackItem, ...],
) -> str:
    entries = ",\n".join(_provider_entry(item) for item in providers)
    return f"""// FS.2.4 multi-provider OAuth stack.
// Generated from FS.2.3 vendor app config plans; secrets stay in env.

export type OAuthProviderStackItem = {{
  provider: string
  displayName: string
  authMethod: string
  callbackUrl: string
  scope: readonly string[]
  authorizeEndpoint: string
  tokenEndpoint: string
  userinfoEndpoint: string | null
  clientIdEnv: string
  clientSecretEnv: string
}}

export const oauthProviderStack = [
{entries},
] as const satisfies readonly OAuthProviderStackItem[]

export const oauthProviderIds = oauthProviderStack.map((item) => item.provider)

export function providerById(provider: string) {{
  return oauthProviderStack.find((item) => item.provider === provider) || null
}}
"""


def _provider_entry(item: AccountLinkingProviderStackItem) -> str:
    scope = ", ".join(f'"{_ts_string(s)}"' for s in item.scope)
    userinfo = (
        f'"{_ts_string(item.userinfo_endpoint)}"'
        if item.userinfo_endpoint
        else "null"
    )
    return f"""  {{
    provider: "{_ts_string(item.provider)}",
    displayName: "{_ts_string(item.display_name)}",
    authMethod: "{_ts_string(item.auth_method)}",
    callbackUrl: "{_ts_string(item.callback_url)}",
    scope: [{scope}],
    authorizeEndpoint: "{_ts_string(item.authorize_endpoint)}",
    tokenEndpoint: "{_ts_string(item.token_endpoint)}",
    userinfoEndpoint: {userinfo},
    clientIdEnv: "{_ts_string(item.client_id_env)}",
    clientSecretEnv: "{_ts_string(item.client_secret_env)}",
  }}"""


def _account_linking_file(
    framework: str,
    providers: tuple[AccountLinkingProviderStackItem, ...],
) -> str:
    methods = ", ".join(f'"{_ts_string(p.auth_method)}"' for p in providers)
    return f"""// FS.2.4 account-linking policy bridge for {framework}.
// Mirrors backend.account_linking: password accounts require confirmation
// before a new OAuth authMethod is linked.

import {{ providerById }} from "./oauth-provider-stack"

export const supportedOAuthAuthMethods = [{methods}] as const

export type ExistingAuthMethod = "password" | (typeof supportedOAuthAuthMethods)[number]

export function authMethodForProvider(provider: string): ExistingAuthMethod {{
  const item = providerById(provider)
  if (!item) throw new Error(`unsupported OAuth provider: ${{provider}}`)
  return item.authMethod as ExistingAuthMethod
}}

export function requiresPasswordConfirmation(
  existingMethods: readonly string[],
  provider: string,
): boolean {{
  const method = authMethodForProvider(provider)
  return existingMethods.includes("password") && !existingMethods.includes(method)
}}

export function canUnlinkProvider(
  existingMethods: readonly string[],
  provider: string,
): boolean {{
  const method = authMethodForProvider(provider)
  return existingMethods.includes(method) && existingMethods.length > 1
}}
"""


def _route_files(
    options: AccountLinkingStackOptions,
    framework: str,
    providers: tuple[AccountLinkingProviderStackItem, ...],
) -> tuple[AuthScaffoldFile, ...]:
    route_prefix = options.route_prefix.strip("/")
    if framework == "nextauth":
        return (
            AuthScaffoldFile(
                "auth/nextauth.providers.ts",
                _nextauth_provider_file(),
            ),
        )
    return tuple(
        AuthScaffoldFile(
            f"{route_prefix}/{item.provider}/link/route.ts",
            _lucia_link_route(item.provider),
        )
        for item in providers
    )


def _nextauth_provider_file() -> str:
    return """// FS.2.4 Auth.js provider stack adapter.

import type { OAuthConfig } from "next-auth/providers"
import { oauthProviderStack } from "./oauth-provider-stack"

export const providers = oauthProviderStack.map((item) => ({
  id: item.provider,
  name: item.displayName,
  type: "oauth",
  authorization: {
    url: item.authorizeEndpoint,
    params: { scope: item.scope.join(" ") },
  },
  token: item.tokenEndpoint,
  userinfo: item.userinfoEndpoint || undefined,
  clientId: process.env[item.clientIdEnv],
  clientSecret: process.env[item.clientSecretEnv],
  checks: ["pkce", "state"],
})) satisfies OAuthConfig<unknown>[]
"""


def _lucia_link_route(provider: str) -> str:
    provider_literal = _ts_string(provider)
    return f"""// FS.2.4 Lucia account-link start route for {provider_literal}.

import {{
  authMethodForProvider,
  requiresPasswordConfirmation,
}} from "@/auth/account-linking"

export async function POST(req: Request) {{
  const body = await req.json()
  const existingMethods = Array.isArray(body.existingMethods) ? body.existingMethods : []
  if (requiresPasswordConfirmation(existingMethods, "{provider_literal}") && !body.password) {{
    return Response.json({{ error: "password_confirmation_required" }}, {{ status: 401 }})
  }}
  return Response.json({{
    provider: "{provider_literal}",
    authMethod: authMethodForProvider("{provider_literal}"),
    status: "ready_to_authorize",
  }})
}}
"""


def _env_name(provider: str, suffix: str) -> str:
    return f"OAUTH_{provider.upper()}_{suffix}"


def _auth_method(provider: str) -> str:
    return f"{_linking.OAUTH_METHOD_PREFIX}{provider}"


def _ts_string(value: str | None) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "AccountLinkingProviderStackItem",
    "AccountLinkingStackOptions",
    "AccountLinkingStackResult",
    "UnsupportedAccountLinkingProviderError",
    "list_account_linking_stack_providers",
    "render_account_linking_stack",
]
