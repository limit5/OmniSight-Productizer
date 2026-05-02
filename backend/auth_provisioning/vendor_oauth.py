"""FS.2.3 -- Vendor OAuth app configuration plans.

FS.2.1/FS.2.2 can prepare OmniSight-side auth metadata and generated
app scaffolds, but most consumer OAuth vendors intentionally require a
human-owned console step before a new client id/secret exists. This
module renders a deterministic setup plan for the AS.1 vendor catalog:
GitHub uses its App Manifest conversion API where available; every
other shipped vendor gets step-by-step operator instructions plus
callback drift detection.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
All tables are immutable tuples / MappingProxyType views. Each render
derives the same plan from explicit inputs in every worker; there is no
cache, singleton, network IO, env read, or cross-worker shared mutable
state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

from backend.security.oauth_vendors import VendorConfig, get_vendor


_MANUAL = "manual"
_API_ASSISTED = "api-assisted"


@dataclass(frozen=True)
class VendorOAuthApiRequest:
    """One vendor API request the wizard can execute after user consent."""

    method: str
    url: str
    description: str
    headers: tuple[tuple[str, str], ...] = ()
    body: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "method": self.method,
            "url": self.url,
            "description": self.description,
            "headers": [list(h) for h in self.headers],
        }
        if self.body is not None:
            data["body"] = dict(self.body)
        return data


@dataclass(frozen=True)
class VendorOAuthInstruction:
    """One human-facing console step for a vendor OAuth app setup."""

    title: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "detail": self.detail}


@dataclass(frozen=True)
class VendorOAuthAppConfigPlan:
    """Semi-automated OAuth app setup plan for one AS.1 vendor."""

    provider: str
    display_name: str
    app_name: str
    callback_url: str
    automation: str
    console_url: str
    instructions: tuple[VendorOAuthInstruction, ...]
    required_env: tuple[str, ...]
    api_requests: tuple[VendorOAuthApiRequest, ...] = ()
    callback_changed: bool = False
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "app_name": self.app_name,
            "callback_url": self.callback_url,
            "automation": self.automation,
            "console_url": self.console_url,
            "instructions": [i.to_dict() for i in self.instructions],
            "required_env": list(self.required_env),
            "api_requests": [r.to_dict() for r in self.api_requests],
            "callback_changed": self.callback_changed,
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VendorOAuthAppConfigOptions:
    """Inputs for rendering an FS.2.3 vendor OAuth setup plan."""

    provider: str
    app_name: str
    app_base_url: str
    callback_path: str = "/api/auth/callback/{provider}"
    existing_callback_urls: tuple[str, ...] = ()
    github_org: Optional[str] = None
    homepage_url: Optional[str] = None
    privacy_policy_url: Optional[str] = None
    terms_url: Optional[str] = None

    def validate(self) -> None:
        if not self.provider or not self.provider.strip():
            raise ValueError("provider is required")
        if not self.app_name or not self.app_name.strip():
            raise ValueError("app_name is required")
        if not self.app_base_url or not self.app_base_url.strip():
            raise ValueError("app_base_url is required")
        if not self.callback_path or not self.callback_path.strip():
            raise ValueError("callback_path is required")


def list_vendor_oauth_plan_providers() -> list[str]:
    """Return AS.1 vendor ids supported by FS.2.3 plan rendering."""
    from backend.security.oauth_vendors import ALL_VENDOR_IDS

    return list(ALL_VENDOR_IDS)


def render_vendor_oauth_app_config_plan(
    options: VendorOAuthAppConfigOptions,
) -> VendorOAuthAppConfigPlan:
    """Render a vendor OAuth setup plan without performing network IO."""
    options.validate()
    vendor = get_vendor(options.provider.strip().lower())
    callback_url = _callback_url(
        base_url=options.app_base_url,
        callback_path=options.callback_path,
        provider=vendor.provider_id,
    )
    callback_changed = (
        bool(options.existing_callback_urls)
        and callback_url not in options.existing_callback_urls
    )
    if vendor.provider_id == "github":
        return _github_plan(options, vendor, callback_url, callback_changed)
    return _manual_plan(options, vendor, callback_url, callback_changed)


def _github_plan(
    options: VendorOAuthAppConfigOptions,
    vendor: VendorConfig,
    callback_url: str,
    callback_changed: bool,
) -> VendorOAuthAppConfigPlan:
    manifest = {
        "name": options.app_name,
        "url": options.homepage_url or options.app_base_url.rstrip("/"),
        "hook_attributes": {"url": callback_url},
        "redirect_url": callback_url,
        "callback_urls": [callback_url],
        "public": False,
        "default_permissions": {},
        "default_events": [],
    }
    encoded = urlencode({"manifest": json.dumps(manifest, separators=(",", ":"))})
    base = (
        f"https://github.com/organizations/{options.github_org}/settings/apps/new"
        if options.github_org
        else "https://github.com/settings/apps/new"
    )
    console_url = f"{base}?{encoded}"
    requests = (
        VendorOAuthApiRequest(
            method="POST",
            url="https://api.github.com/app-manifests/{code}/conversions",
            description=(
                "Exchange the temporary manifest code returned by GitHub "
                "for app credentials; the wizard substitutes {code}."
            ),
            headers=(
                ("Accept", "application/vnd.github+json"),
                ("X-GitHub-Api-Version", "2022-11-28"),
            ),
        ),
    )
    return VendorOAuthAppConfigPlan(
        provider=vendor.provider_id,
        display_name=vendor.display_name,
        app_name=options.app_name,
        callback_url=callback_url,
        automation=_API_ASSISTED,
        console_url=console_url,
        api_requests=requests,
        instructions=(
            VendorOAuthInstruction(
                "Open the generated GitHub App manifest URL",
                "Review the prefilled name, homepage URL, and callback URL.",
            ),
            VendorOAuthInstruction(
                "Create the GitHub App",
                "Approve the manifest; GitHub redirects back with a one-time code.",
            ),
            VendorOAuthInstruction(
                "Exchange the manifest code",
                "Call the conversion API request and store client_id/client_secret as env.",
            ),
        ),
        required_env=("AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "AUTH_PROVIDER"),
        callback_changed=callback_changed,
        warnings=_warnings(vendor, callback_changed),
        metadata={
            "manifest": manifest,
            "scope": list(vendor.default_scopes),
            "supports_pkce": vendor.supports_pkce,
        },
    )


def _manual_plan(
    options: VendorOAuthAppConfigOptions,
    vendor: VendorConfig,
    callback_url: str,
    callback_changed: bool,
) -> VendorOAuthAppConfigPlan:
    console_url = _console_urls()[vendor.provider_id]
    instructions = (
        VendorOAuthInstruction(
            f"Open {vendor.display_name} developer console",
            f"Create a web OAuth app named '{options.app_name}'.",
        ),
        VendorOAuthInstruction(
            "Set redirect / callback URL",
            f"Add exactly: {callback_url}",
        ),
        VendorOAuthInstruction(
            "Set scopes",
            _scope_detail(vendor),
        ),
        VendorOAuthInstruction(
            "Copy credentials into generated-app env",
            "Store client_id and client_secret in secret storage; do not commit them.",
        ),
    )
    return VendorOAuthAppConfigPlan(
        provider=vendor.provider_id,
        display_name=vendor.display_name,
        app_name=options.app_name,
        callback_url=callback_url,
        automation=_MANUAL,
        console_url=console_url,
        instructions=instructions,
        required_env=("AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "AUTH_PROVIDER"),
        callback_changed=callback_changed,
        warnings=_warnings(vendor, callback_changed),
        metadata={
            "scope": list(vendor.default_scopes),
            "authorize_endpoint": vendor.authorize_endpoint,
            "token_endpoint": vendor.token_endpoint,
            "is_oidc": vendor.is_oidc,
            "supports_pkce": vendor.supports_pkce,
        },
    )


def _callback_url(*, base_url: str, callback_path: str, provider: str) -> str:
    base = base_url.strip().rstrip("/")
    path = callback_path.strip()
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path.format(provider=provider)}"


def _scope_detail(vendor: VendorConfig) -> str:
    if not vendor.default_scopes:
        return "Use the vendor default permissions; this provider does not use scopes."
    return "Request scopes: " + " ".join(vendor.default_scopes)


def _warnings(vendor: VendorConfig, callback_changed: bool) -> tuple[str, ...]:
    warnings: tuple[str, ...] = ()
    if vendor.provider_id != "github":
        warnings += (
            f"{vendor.display_name} does not expose a supported OAuth app creation API; "
            "the wizard must guide the operator through console setup.",
        )
    if not vendor.supports_pkce:
        warnings += ("Provider catalog marks PKCE as unsupported; keep client_secret server-side.",)
    if callback_changed:
        warnings += ("Callback URL changed from the previously registered set.",)
    return warnings


def _console_urls() -> Mapping[str, str]:
    return MappingProxyType({
        "github": "https://github.com/settings/developers",
        "google": "https://console.cloud.google.com/apis/credentials",
        "microsoft": (
            "https://entra.microsoft.com/#view/"
            "Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
        ),
        "apple": "https://developer.apple.com/account/resources/identifiers/list",
        "gitlab": "https://gitlab.com/-/profile/applications",
        "bitbucket": "https://bitbucket.org/account/settings/oauth-consumers/",
        "slack": "https://api.slack.com/apps",
        "notion": "https://www.notion.so/my-integrations",
        "salesforce": "https://login.salesforce.com/setup",
        "hubspot": "https://developers.hubspot.com/",
        "discord": "https://discord.com/developers/applications",
    })


__all__ = [
    "VendorOAuthApiRequest",
    "VendorOAuthAppConfigOptions",
    "VendorOAuthAppConfigPlan",
    "VendorOAuthInstruction",
    "list_vendor_oauth_plan_providers",
    "render_vendor_oauth_app_config_plan",
]
