"""FS.2.2 -- NextAuth.js / Lucia self-hosted auth scaffolds.

FS.2.1 provisions provider-side OAuth/OIDC applications and returns an
``AuthProviderSetupResult``. This module turns that result into a small
generated-app scaffold manifest for self-hosted auth frameworks while
reusing the AS.1 TypeScript OAuth client twin already shipped under
``templates/_shared/oauth-client``.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable constants/classes/functions only. No
module-level cache, singleton, or mutable registry is read or written;
each scaffold render is derived entirely from explicit inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

from backend.auth_provisioning.base import AuthProviderSetupResult


_FRAMEWORK_ALIASES: Mapping[str, str] = MappingProxyType({
    "nextauth": "nextauth",
    "next-auth": "nextauth",
    "authjs": "nextauth",
    "auth-js": "nextauth",
    "lucia": "lucia",
})
_FRAMEWORKS: tuple[str, ...] = ("nextauth", "lucia")


class UnsupportedSelfHostedAuthFrameworkError(ValueError):
    """Requested self-hosted auth scaffold is not supported by FS.2.2."""

    def __init__(self, framework: str):
        super().__init__(
            f"Unsupported self-hosted auth framework '{framework}'. "
            f"Expected one of: {', '.join(_FRAMEWORKS)}"
        )
        self.framework = framework


@dataclass(frozen=True)
class AuthScaffoldEnvVar:
    """Environment variable declaration needed by a rendered scaffold."""

    name: str
    required: bool
    sensitive: bool = False
    source: str = "operator"

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "required": self.required,
            "sensitive": self.sensitive,
            "source": self.source,
        }


@dataclass(frozen=True)
class AuthScaffoldFile:
    """One file the caller should write into the generated app."""

    path: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "content": self.content}


@dataclass(frozen=True)
class SelfHostedAuthScaffoldResult:
    """Manifest for a self-hosted auth scaffold render."""

    framework: str
    provider: str
    files: tuple[AuthScaffoldFile, ...]
    env: tuple[AuthScaffoldEnvVar, ...]
    dependencies: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "provider": self.provider,
            "files": [f.to_dict() for f in self.files],
            "env": [v.to_dict() for v in self.env],
            "dependencies": list(self.dependencies),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SelfHostedAuthScaffoldOptions:
    """Inputs for rendering a NextAuth.js or Lucia self-hosted scaffold."""

    framework: str
    provider_setup: AuthProviderSetupResult
    app_base_url: str
    oauth_client_import: str = "@/shared/oauth-client"
    route_prefix: str = "app/api/auth"
    auth_dir: str = "auth"
    extra_env: tuple[AuthScaffoldEnvVar, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        normalize_self_hosted_framework(self.framework)
        if not self.provider_setup.provider:
            raise ValueError("provider_setup.provider is required")
        if not self.provider_setup.client_id:
            raise ValueError("provider_setup.client_id is required")
        if not self.provider_setup.issuer_url:
            raise ValueError("provider_setup.issuer_url is required")
        if not self.app_base_url or not self.app_base_url.strip():
            raise ValueError("app_base_url is required")
        if not self.oauth_client_import or not self.oauth_client_import.strip():
            raise ValueError("oauth_client_import is required")


def list_self_hosted_frameworks() -> list[str]:
    """Return canonical FS.2.2 self-hosted auth scaffold ids."""
    return list(_FRAMEWORKS)


def normalize_self_hosted_framework(framework: str) -> str:
    """Normalize framework aliases while preserving a narrow public set."""
    key = framework.strip().lower().replace("_", "-")
    try:
        return _FRAMEWORK_ALIASES[key]
    except KeyError as exc:
        raise UnsupportedSelfHostedAuthFrameworkError(framework) from exc


def render_self_hosted_auth_scaffold(
    options: SelfHostedAuthScaffoldOptions,
) -> SelfHostedAuthScaffoldResult:
    """Render a NextAuth.js or Lucia scaffold manifest.

    The manifest deliberately carries env var names and sensitivity
    metadata, not secret values from ``AuthProviderSetupResult``.
    """
    options.validate()
    framework = normalize_self_hosted_framework(options.framework)
    setup = options.provider_setup
    env = _env_vars(setup, framework) + tuple(options.extra_env)
    if framework == "nextauth":
        return SelfHostedAuthScaffoldResult(
            framework=framework,
            provider=setup.provider,
            files=_nextauth_files(options),
            env=env,
            dependencies=("next-auth",),
            notes=("client_secret is declared as env metadata only",),
        )
    return SelfHostedAuthScaffoldResult(
        framework=framework,
        provider=setup.provider,
        files=_lucia_files(options),
        env=env,
        dependencies=("lucia",),
        notes=("caller supplies the Lucia session adapter for its database",),
    )


def _env_vars(
    setup: AuthProviderSetupResult,
    framework: str,
) -> tuple[AuthScaffoldEnvVar, ...]:
    common = (
        AuthScaffoldEnvVar("AUTH_PROVIDER", True, source="fs.2.1"),
        AuthScaffoldEnvVar("AUTH_ISSUER_URL", True, source="fs.2.1"),
        AuthScaffoldEnvVar("AUTH_CLIENT_ID", True, source="fs.2.1"),
        AuthScaffoldEnvVar(
            "AUTH_CLIENT_SECRET",
            bool(setup.client_secret),
            sensitive=True,
            source="fs.2.1",
        ),
        AuthScaffoldEnvVar("AUTH_REDIRECT_URI", True, source="fs.2.1"),
    )
    if framework == "nextauth":
        return common + (
            AuthScaffoldEnvVar("AUTH_SECRET", True, True),
            AuthScaffoldEnvVar("NEXTAUTH_URL", True),
            AuthScaffoldEnvVar(
                "AUTH_MFA_REQUIRED",
                bool(setup.require_mfa),
                source="sc.8.2",
            ),
        )
    return common + (
        AuthScaffoldEnvVar("AUTH_AUTHORIZE_ENDPOINT", True),
        AuthScaffoldEnvVar("AUTH_TOKEN_ENDPOINT", True),
        AuthScaffoldEnvVar("LUCIA_SESSION_SECRET", True, True),
    )


def _nextauth_files(options: SelfHostedAuthScaffoldOptions) -> tuple[AuthScaffoldFile, ...]:
    setup = options.provider_setup
    auth_dir = options.auth_dir.strip("/")
    route_prefix = options.route_prefix.strip("/")
    provider = _ts_string(setup.provider)
    app_base_url = _ts_string(options.app_base_url.rstrip("/"))
    scope = " ".join(setup.scopes)
    files = (
        AuthScaffoldFile(
            f"{auth_dir}/oauth-client.ts",
            _oauth_client_reexport(options.oauth_client_import),
        ),
        AuthScaffoldFile(
            f"{auth_dir}/nextauth.mfa.ts",
            _nextauth_mfa_file(setup.require_mfa),
        ),
        AuthScaffoldFile(
            f"{auth_dir}/nextauth.config.ts",
            f"""// FS.2.2 self-hosted Auth.js scaffold.
// Reuses the AS.1 OAuth client twin for PKCE/state TTL constants.

import NextAuth, {{ type NextAuthConfig }} from "next-auth"
import {{ DEFAULT_STATE_TTL_SECONDS }} from "./oauth-client"
import {{ nextAuthMfaCallbacks }} from "./nextauth.mfa"

const providerId = process.env.AUTH_PROVIDER || "{provider}"

export const authConfig = {{
  providers: [
    {{
      id: providerId,
      name: providerId,
      type: "oidc",
      issuer: process.env.AUTH_ISSUER_URL!,
      clientId: process.env.AUTH_CLIENT_ID!,
      clientSecret: process.env.AUTH_CLIENT_SECRET,
      authorization: {{ params: {{ scope: "{_ts_string(scope)}" }} }},
      checks: ["pkce", "state"],
      profile(profile) {{
        return {{
          id: String(profile.sub || profile.id),
          name: profile.name,
          email: profile.email,
        }}
      }},
    }},
  ],
  session: {{ strategy: "jwt" }},
  callbacks: nextAuthMfaCallbacks,
  cookies: {{
    pkceCodeVerifier: {{
      name: "authjs.pkce.code_verifier",
      options: {{
        httpOnly: true,
        sameSite: "lax",
        path: "/",
        secure: {app_base_url}.startsWith("https://"),
        maxAge: DEFAULT_STATE_TTL_SECONDS,
      }},
    }},
  }},
}} satisfies NextAuthConfig

export const {{ handlers, auth, signIn, signOut }} = NextAuth(authConfig)
""",
        ),
        AuthScaffoldFile(
            f"{route_prefix}/[...nextauth]/route.ts",
            """// FS.2.2 self-hosted Auth.js route handler.

import { handlers } from "@/auth/nextauth.config"

export const { GET, POST } = handlers
""",
        ),
    )
    return files


def _nextauth_mfa_file(require_mfa: bool) -> str:
    default_required = "true" if require_mfa else "false"
    return f"""// SC.8.2 Auth.js self-hosted MFA scaffold.
// Reuses the AS.1 OAuth client TTL so MFA step-up and OAuth state expire together.

import type {{ NextAuthConfig }} from "next-auth"
import {{ DEFAULT_STATE_TTL_SECONDS }} from "./oauth-client"

export const nextAuthMfaPosture = {{
  requireMfa: process.env.AUTH_MFA_REQUIRED === "true" || {default_required},
  challengeTtlSeconds: DEFAULT_STATE_TTL_SECONDS,
  challengePath: "/api/v1/auth/mfa/challenge",
  totpEnrollPath: "/api/v1/auth/mfa/totp/enroll",
  webauthnChallengePath: "/api/v1/auth/mfa/webauthn/challenge/complete",
}} as const

export type NextAuthMfaSession = {{
  readonly mfaVerified?: boolean | null
}}

export function requiresNextAuthMfaStepUp(
  session: NextAuthMfaSession | null | undefined,
): boolean {{
  return nextAuthMfaPosture.requireMfa && session?.mfaVerified !== true
}}

export function nextAuthMfaRedirectUrl(next = "/"): string {{
  return `/mfa-challenge?next=${{encodeURIComponent(next)}}`
}}

export const nextAuthMfaCallbacks = {{
  async jwt({{ token, trigger, session }}) {{
    const nextToken = token as typeof token & {{ mfaVerified?: boolean }}
    const nextSession = session as NextAuthMfaSession | undefined
    if (trigger === "update" && nextSession?.mfaVerified === true) {{
      nextToken.mfaVerified = true
    }}
    return nextToken
  }},
  async session({{ session, token }}) {{
    const enriched = session as typeof session & {{
      mfaRequired?: boolean
      mfaVerified?: boolean
    }}
    const mfaToken = token as typeof token & {{ mfaVerified?: boolean }}
    enriched.mfaRequired = nextAuthMfaPosture.requireMfa
    enriched.mfaVerified = mfaToken.mfaVerified === true
    return enriched
  }},
  async signIn({{ user }}) {{
    if (!nextAuthMfaPosture.requireMfa) return true
    return Boolean(user?.email)
  }},
}} satisfies Pick<NextAuthConfig, "callbacks">["callbacks"]
"""


def _lucia_files(options: SelfHostedAuthScaffoldOptions) -> tuple[AuthScaffoldFile, ...]:
    setup = options.provider_setup
    auth_dir = options.auth_dir.strip("/")
    route_prefix = options.route_prefix.strip("/")
    provider = _ts_string(setup.provider)
    scope = ", ".join(f'"{_ts_string(s)}"' for s in setup.scopes)
    return (
        AuthScaffoldFile(
            f"{auth_dir}/oauth-client.ts",
            _oauth_client_reexport(options.oauth_client_import),
        ),
        AuthScaffoldFile(
            f"{auth_dir}/lucia.ts",
            f"""// FS.2.2 self-hosted Lucia scaffold.
// The app supplies the DB adapter; OAuth protocol work stays in AS.1.

import {{ Lucia }} from "lucia"
import {{
  beginAuthorization,
  parseTokenResponse,
  verifyStateAndConsume,
  type FlowSession,
  type TokenSet,
}} from "./oauth-client"

export const providerId = process.env.AUTH_PROVIDER || "{provider}"
export const defaultScope = [{scope}] as const

export type OAuthFlowRecord = FlowSession
export type OAuthTokenRecord = TokenSet

export function createLucia(adapter: ConstructorParameters<typeof Lucia>[0]) {{
  return new Lucia(adapter, {{
    sessionCookie: {{
      attributes: {{
        secure: process.env.NODE_ENV === "production",
      }},
    }},
  }})
}}

export {{
  beginAuthorization,
  parseTokenResponse,
  verifyStateAndConsume,
}}
""",
        ),
        AuthScaffoldFile(
            f"{route_prefix}/{setup.provider}/route.ts",
            """// FS.2.2 Lucia OAuth start route.

import { beginAuthorization, defaultScope, providerId } from "@/auth/lucia"

export async function GET() {
  const redirectUri = process.env.AUTH_REDIRECT_URI!
  const { url, flow } = await beginAuthorization({
    provider: providerId,
    authorizeEndpoint: process.env.AUTH_AUTHORIZE_ENDPOINT!,
    clientId: process.env.AUTH_CLIENT_ID!,
    redirectUri,
    scope: [...defaultScope],
  })

  const res = Response.redirect(url)
  res.headers.append(
    "Set-Cookie",
    `oauth_flow=${encodeURIComponent(JSON.stringify(flow))}; HttpOnly; Path=/; SameSite=Lax`,
  )
  return res
}
""",
        ),
        AuthScaffoldFile(
            f"{route_prefix}/{setup.provider}/callback/route.ts",
            """// FS.2.2 Lucia OAuth callback route.

import {
  parseTokenResponse,
  verifyStateAndConsume,
  type OAuthFlowRecord,
} from "@/auth/lucia"

function readFlow(req: Request): OAuthFlowRecord {
  const cookie = req.headers.get("cookie") || ""
  const found = cookie.split(";").find((part) => part.trim().startsWith("oauth_flow="))
  if (!found) throw new Error("missing oauth flow cookie")
  return JSON.parse(decodeURIComponent(found.split("=", 2)[1])) as OAuthFlowRecord
}

export async function GET(req: Request) {
  const url = new URL(req.url)
  const code = url.searchParams.get("code")
  const state = url.searchParams.get("state")
  if (!code || !state) return new Response("missing OAuth callback params", { status: 400 })

  const flow = readFlow(req)
  verifyStateAndConsume(flow, state)

  const tokenRes = await fetch(process.env.AUTH_TOKEN_ENDPOINT!, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: process.env.AUTH_REDIRECT_URI!,
      client_id: process.env.AUTH_CLIENT_ID!,
      client_secret: process.env.AUTH_CLIENT_SECRET || "",
      code_verifier: flow.codeVerifier,
    }),
  })

  const token = parseTokenResponse(await tokenRes.json())
  return Response.json({ ok: true, provider: flow.provider, token })
}
""",
        ),
    )


def _oauth_client_reexport(oauth_client_import: str) -> str:
    imported = _ts_string(oauth_client_import)
    return f"""// FS.2.2 bridge to the AS.1 generated-app OAuth client.

export {{
  DEFAULT_STATE_TTL_SECONDS,
  beginAuthorization,
  parseTokenResponse,
  verifyStateAndConsume,
  type FlowSession,
  type TokenSet,
}} from "{imported}"
"""


def _ts_string(value: Optional[str]) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "AuthScaffoldEnvVar",
    "AuthScaffoldFile",
    "SelfHostedAuthScaffoldOptions",
    "SelfHostedAuthScaffoldResult",
    "UnsupportedSelfHostedAuthFrameworkError",
    "list_self_hosted_frameworks",
    "normalize_self_hosted_framework",
    "render_self_hosted_auth_scaffold",
]
