"""FS.2.5 -- Email, magic-link, TOTP MFA, and WebAuthn baseline.

FS.2.2/FS.2.4 render OAuth-oriented self-hosted auth scaffolds. This
module renders the companion baseline for email auth surfaces: password
login, passwordless magic links, TOTP enrollment/challenge, and WebAuthn
registration/challenge. The output is a generated-app manifest only;
secret values remain env metadata and crypto/session persistence stays
with the caller's app/backend.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable constants/classes/functions only. Each
render derives files/env metadata from explicit options; there is no
cache, singleton, env read, network IO, or shared mutable state across
uvicorn workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from backend.auth_provisioning.self_hosted import (
    AuthScaffoldEnvVar,
    AuthScaffoldFile,
    normalize_self_hosted_framework,
)


_BASELINE_METHODS: tuple[str, ...] = (
    "email_password",
    "magic_link",
    "totp",
    "webauthn",
)
_FRAMEWORK_DEPENDENCIES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "nextauth": ("next-auth",),
    "lucia": ("lucia",),
})


@dataclass(frozen=True)
class EmailMfaBaselineResult:
    """Manifest for FS.2.5 email + MFA baseline files."""

    framework: str
    methods: tuple[str, ...]
    files: tuple[AuthScaffoldFile, ...]
    env: tuple[AuthScaffoldEnvVar, ...]
    dependencies: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "methods": list(self.methods),
            "files": [f.to_dict() for f in self.files],
            "env": [v.to_dict() for v in self.env],
            "dependencies": list(self.dependencies),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class EmailMfaBaselineOptions:
    """Inputs for rendering an email + MFA self-hosted auth baseline."""

    framework: str
    auth_dir: str = "auth"
    route_prefix: str = "app/api/auth"
    api_base_url_env: str = "NEXT_PUBLIC_API_BASE_URL"
    extra_env: tuple[AuthScaffoldEnvVar, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        normalize_self_hosted_framework(self.framework)
        if not self.auth_dir or not self.auth_dir.strip():
            raise ValueError("auth_dir is required")
        if not self.route_prefix or not self.route_prefix.strip():
            raise ValueError("route_prefix is required")
        if not self.api_base_url_env or not self.api_base_url_env.strip():
            raise ValueError("api_base_url_env is required")


def list_email_mfa_baseline_methods() -> list[str]:
    """Return the FS.2.5 baseline auth method ids in UI order."""
    return list(_BASELINE_METHODS)


def render_email_mfa_baseline(
    options: EmailMfaBaselineOptions,
) -> EmailMfaBaselineResult:
    """Render email/password, magic-link, TOTP, and WebAuthn scaffold files."""
    options.validate()
    framework = normalize_self_hosted_framework(options.framework)
    files = (
        AuthScaffoldFile(
            f"{options.auth_dir.strip('/')}/email-mfa-baseline.ts",
            _baseline_client_file(options),
        ),
    ) + _framework_files(options, framework)
    return EmailMfaBaselineResult(
        framework=framework,
        methods=_BASELINE_METHODS,
        files=files,
        env=_env_vars(options) + tuple(options.extra_env),
        dependencies=_FRAMEWORK_DEPENDENCIES[framework],
        notes=(
            "magic-link secret is declared as env metadata only",
            "TOTP and WebAuthn routes proxy the existing backend MFA contract",
        ),
    )


def _env_vars(options: EmailMfaBaselineOptions) -> tuple[AuthScaffoldEnvVar, ...]:
    return (
        AuthScaffoldEnvVar(options.api_base_url_env.strip(), True),
        AuthScaffoldEnvVar("AUTH_EMAIL_FROM", True),
        AuthScaffoldEnvVar("AUTH_MAGIC_LINK_SECRET", True, True),
        AuthScaffoldEnvVar("AUTH_MAGIC_LINK_TTL_SECONDS", True),
        AuthScaffoldEnvVar("AUTH_MFA_REQUIRED", True),
        AuthScaffoldEnvVar("WEBAUTHN_RP_ID", True),
        AuthScaffoldEnvVar("WEBAUTHN_ORIGIN", True),
    )


def _baseline_client_file(options: EmailMfaBaselineOptions) -> str:
    api_base = _ts_string(options.api_base_url_env.strip())
    return f"""// FS.2.5 email + MFA baseline client.
// Password, magic-link, TOTP, and WebAuthn stay on one backend API surface.

const apiBase = process.env.{api_base} || ""

export const emailMfaBaselineMethods = [
  "email_password",
  "magic_link",
  "totp",
  "webauthn",
] as const

export type EmailMfaBaselineMethod = (typeof emailMfaBaselineMethods)[number]

export type MagicLinkPurpose = "login" | "signup" | "verify_email"

async function postJson<T>(path: string, body: unknown): Promise<T> {{
  const res = await fetch(`${{apiBase}}${{path}}`, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(body),
    credentials: "include",
  }})
  if (!res.ok) throw new Error(`auth baseline request failed: ${{res.status}}`)
  return (await res.json()) as T
}}

export function passwordLogin(email: string, password: string) {{
  return postJson("/api/v1/auth/login", {{ email, password }})
}}

export function requestMagicLink(email: string, purpose: MagicLinkPurpose = "login") {{
  return postJson("/api/v1/auth/magic-link/request", {{ email, purpose }})
}}

export function verifyMagicLink(token: string) {{
  return postJson("/api/v1/auth/magic-link/confirm", {{ token }})
}}

export function beginTotpEnroll() {{
  return postJson("/api/v1/auth/mfa/totp/enroll", {{}})
}}

export function confirmTotpEnroll(code: string) {{
  return postJson("/api/v1/auth/mfa/totp/confirm", {{ code }})
}}

export function verifyTotpChallenge(mfaToken: string, code: string) {{
  return postJson("/api/v1/auth/mfa/challenge", {{ mfa_token: mfaToken, code }})
}}

export function beginWebAuthnRegister(name = "") {{
  return postJson("/api/v1/auth/mfa/webauthn/register/begin", {{ name }})
}}

export function completeWebAuthnRegister(credential: unknown, name = "") {{
  return postJson("/api/v1/auth/mfa/webauthn/register/complete", {{ credential, name }})
}}

export function beginWebAuthnChallenge(mfaToken: string) {{
  return postJson("/api/v1/auth/mfa/webauthn/challenge/begin", {{ mfa_token: mfaToken }})
}}

export function completeWebAuthnChallenge(mfaToken: string, credential: unknown) {{
  return postJson("/api/v1/auth/mfa/webauthn/challenge/complete", {{
    mfa_token: mfaToken,
    credential,
  }})
}}
"""


def _framework_files(
    options: EmailMfaBaselineOptions,
    framework: str,
) -> tuple[AuthScaffoldFile, ...]:
    if framework == "nextauth":
        return (
            AuthScaffoldFile(
                f"{options.auth_dir.strip('/')}/nextauth.email-mfa.ts",
                _nextauth_email_mfa_file(),
            ),
        )
    route_prefix = options.route_prefix.strip("/")
    return (
        AuthScaffoldFile(
            f"{route_prefix}/magic-link/route.ts",
            _lucia_magic_link_request_route(),
        ),
        AuthScaffoldFile(
            f"{route_prefix}/magic-link/verify/route.ts",
            _lucia_magic_link_verify_route(),
        ),
        AuthScaffoldFile(
            f"{route_prefix}/mfa/totp/route.ts",
            _lucia_totp_route(),
        ),
        AuthScaffoldFile(
            f"{route_prefix}/mfa/webauthn/route.ts",
            _lucia_webauthn_route(),
        ),
    )


def _nextauth_email_mfa_file() -> str:
    return """// FS.2.5 Auth.js email + MFA baseline adapter.
// The app stores users/sessions; this file declares the method posture
// that callbacks can enforce after password or magic-link sign-in.

import type { NextAuthConfig } from "next-auth"
import { emailMfaBaselineMethods } from "./email-mfa-baseline"

export const emailMfaMethodPosture = {
  methods: emailMfaBaselineMethods,
  magicLinkTtlSeconds: Number(process.env.AUTH_MAGIC_LINK_TTL_SECONDS || "900"),
  requireMfa: process.env.AUTH_MFA_REQUIRED === "true",
  webauthn: {
    rpId: process.env.WEBAUTHN_RP_ID!,
    origin: process.env.WEBAUTHN_ORIGIN!,
  },
} as const

export const emailMfaCallbacks = {
  async signIn({ user }) {
    if (!user?.email) return false
    return true
  },
} satisfies Pick<NextAuthConfig, "callbacks">["callbacks"]
"""


def _lucia_magic_link_request_route() -> str:
    return """// FS.2.5 Lucia magic-link request route.

import { requestMagicLink } from "@/auth/email-mfa-baseline"

export async function POST(req: Request) {
  const body = await req.json()
  const email = typeof body.email === "string" ? body.email : ""
  if (!email) return Response.json({ error: "email_required" }, { status: 400 })
  return Response.json(await requestMagicLink(email, body.purpose || "login"))
}
"""


def _lucia_magic_link_verify_route() -> str:
    return """// FS.2.5 Lucia magic-link verification route.

import { verifyMagicLink } from "@/auth/email-mfa-baseline"

export async function POST(req: Request) {
  const body = await req.json()
  const token = typeof body.token === "string" ? body.token : ""
  if (!token) return Response.json({ error: "token_required" }, { status: 400 })
  return Response.json(await verifyMagicLink(token))
}
"""


def _lucia_totp_route() -> str:
    return """// FS.2.5 Lucia TOTP baseline route.

import {
  beginTotpEnroll,
  confirmTotpEnroll,
  verifyTotpChallenge,
} from "@/auth/email-mfa-baseline"

export async function POST(req: Request) {
  const body = await req.json()
  if (body.action === "enroll") return Response.json(await beginTotpEnroll())
  if (body.action === "confirm") {
    return Response.json(await confirmTotpEnroll(String(body.code || "")))
  }
  if (body.action === "challenge") {
    return Response.json(await verifyTotpChallenge(
      String(body.mfaToken || ""),
      String(body.code || ""),
    ))
  }
  return Response.json({ error: "unsupported_totp_action" }, { status: 400 })
}
"""


def _lucia_webauthn_route() -> str:
    return """// FS.2.5 Lucia WebAuthn baseline route.

import {
  beginWebAuthnChallenge,
  beginWebAuthnRegister,
  completeWebAuthnChallenge,
  completeWebAuthnRegister,
} from "@/auth/email-mfa-baseline"

export async function POST(req: Request) {
  const body = await req.json()
  if (body.action === "register_begin") {
    return Response.json(await beginWebAuthnRegister(String(body.name || "")))
  }
  if (body.action === "register_complete") {
    return Response.json(await completeWebAuthnRegister(body.credential, String(body.name || "")))
  }
  if (body.action === "challenge_begin") {
    return Response.json(await beginWebAuthnChallenge(String(body.mfaToken || "")))
  }
  if (body.action === "challenge_complete") {
    return Response.json(await completeWebAuthnChallenge(
      String(body.mfaToken || ""),
      body.credential,
    ))
  }
  return Response.json({ error: "unsupported_webauthn_action" }, { status: 400 })
}
"""


def _ts_string(value: str | None) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "EmailMfaBaselineOptions",
    "EmailMfaBaselineResult",
    "list_email_mfa_baseline_methods",
    "render_email_mfa_baseline",
]
