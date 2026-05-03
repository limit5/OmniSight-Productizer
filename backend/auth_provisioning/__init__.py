"""FS.2.1 -- Inbound auth provisioning adapters package."""

from __future__ import annotations

from backend.auth_provisioning.base import (
    DEFAULT_OIDC_SCOPES,
    AuthProviderSetupResult,
    AuthProvisionAdapter,
    AuthProvisionConflictError,
    AuthProvisionError,
    AuthProvisionRateLimitError,
    InvalidAuthProvisionTokenError,
    MissingAuthProvisionScopeError,
)
from backend.auth_provisioning.self_hosted import (
    AuthScaffoldEnvVar,
    AuthScaffoldFile,
    SelfHostedAuthScaffoldOptions,
    SelfHostedAuthScaffoldResult,
    UnsupportedSelfHostedAuthFrameworkError,
    list_self_hosted_frameworks,
    normalize_self_hosted_framework,
    render_self_hosted_auth_scaffold,
)
from backend.auth_provisioning.bot_defense import (
    DEFAULT_BOT_CHALLENGE_BRIDGE_PATH,
    DEFAULT_BOT_CHALLENGE_IMPORT,
    DEFAULT_BOT_DEFENSE_FORMS_PATH,
    BotDefenseFormItem,
    BotDefenseProviderItem,
    BotDefenseScaffoldOptions,
    BotDefenseScaffoldResult,
    list_bot_defense_forms,
    list_bot_defense_providers,
    render_bot_defense_scaffold,
)
from backend.auth_provisioning.account_linking import (
    AccountLinkingProviderStackItem,
    AccountLinkingStackOptions,
    AccountLinkingStackResult,
    UnsupportedAccountLinkingProviderError,
    list_account_linking_stack_providers,
    render_account_linking_stack,
)
from backend.auth_provisioning.email_mfa import (
    EmailMfaBaselineOptions,
    EmailMfaBaselineResult,
    list_email_mfa_baseline_methods,
    render_email_mfa_baseline,
)
from backend.auth_provisioning.vendor_oauth import (
    VendorOAuthApiRequest,
    VendorOAuthAppConfigOptions,
    VendorOAuthAppConfigPlan,
    VendorOAuthInstruction,
    list_vendor_oauth_plan_providers,
    render_vendor_oauth_app_config_plan,
)
from backend.auth_provisioning.outbound_oauth import (
    OUTBOUND_OAUTH_VENDOR_IDS,
    OUTBOUND_OAUTH_VENDOR_ITEMS,
    OUTBOUND_OAUTH_VENDORS,
    OutboundOAuthFlowProviderItem,
    OutboundOAuthFlowScaffoldOptions,
    OutboundOAuthFlowScaffoldResult,
    OutboundOAuthVendorCatalogItem,
    OutboundOAuthVendorCatalogOptions,
    get_outbound_oauth_vendor,
    list_outbound_oauth_flow_providers,
    render_outbound_oauth_vendor_catalog,
    render_outbound_oauth_flow_scaffold,
)


def list_providers() -> list[str]:
    """Return the canonical id for every shipped auth provisioning adapter."""
    return ["clerk", "auth0", "workos"]


def get_adapter(provider: str) -> type[AuthProvisionAdapter]:
    """Look up an adapter class by canonical provider string."""
    key = provider.strip().lower().replace("_", "-")
    if key == "clerk":
        from backend.auth_provisioning.clerk import ClerkAuthProvisionAdapter
        return ClerkAuthProvisionAdapter
    if key == "auth0":
        from backend.auth_provisioning.auth0 import Auth0AuthProvisionAdapter
        return Auth0AuthProvisionAdapter
    if key == "workos":
        from backend.auth_provisioning.workos import WorkOSAuthProvisionAdapter
        return WorkOSAuthProvisionAdapter
    raise ValueError(
        f"Unknown auth provisioning provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "DEFAULT_OIDC_SCOPES",
    "AuthProvisionAdapter",
    "AuthProviderSetupResult",
    "AuthProvisionConflictError",
    "AuthProvisionError",
    "AuthProvisionRateLimitError",
    "InvalidAuthProvisionTokenError",
    "MissingAuthProvisionScopeError",
    "AuthScaffoldEnvVar",
    "AuthScaffoldFile",
    "BotDefenseProviderItem",
    "BotDefenseFormItem",
    "BotDefenseScaffoldOptions",
    "BotDefenseScaffoldResult",
    "DEFAULT_BOT_CHALLENGE_BRIDGE_PATH",
    "DEFAULT_BOT_CHALLENGE_IMPORT",
    "DEFAULT_BOT_DEFENSE_FORMS_PATH",
    "SelfHostedAuthScaffoldOptions",
    "SelfHostedAuthScaffoldResult",
    "UnsupportedSelfHostedAuthFrameworkError",
    "AccountLinkingProviderStackItem",
    "AccountLinkingStackOptions",
    "AccountLinkingStackResult",
    "UnsupportedAccountLinkingProviderError",
    "EmailMfaBaselineOptions",
    "EmailMfaBaselineResult",
    "VendorOAuthApiRequest",
    "VendorOAuthAppConfigOptions",
    "VendorOAuthAppConfigPlan",
    "VendorOAuthInstruction",
    "OUTBOUND_OAUTH_VENDOR_IDS",
    "OUTBOUND_OAUTH_VENDOR_ITEMS",
    "OUTBOUND_OAUTH_VENDORS",
    "OutboundOAuthFlowProviderItem",
    "OutboundOAuthFlowScaffoldOptions",
    "OutboundOAuthFlowScaffoldResult",
    "OutboundOAuthVendorCatalogItem",
    "OutboundOAuthVendorCatalogOptions",
    "get_adapter",
    "get_outbound_oauth_vendor",
    "list_account_linking_stack_providers",
    "list_bot_defense_forms",
    "list_bot_defense_providers",
    "list_email_mfa_baseline_methods",
    "list_outbound_oauth_flow_providers",
    "list_vendor_oauth_plan_providers",
    "list_self_hosted_frameworks",
    "list_providers",
    "normalize_self_hosted_framework",
    "render_account_linking_stack",
    "render_bot_defense_scaffold",
    "render_email_mfa_baseline",
    "render_outbound_oauth_vendor_catalog",
    "render_outbound_oauth_flow_scaffold",
    "render_vendor_oauth_app_config_plan",
    "render_self_hosted_auth_scaffold",
]
