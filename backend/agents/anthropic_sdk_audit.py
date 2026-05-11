"""OP-863 — Anthropic SDK version and beta-surface audit helpers."""

from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass
from typing import Any, Callable

from packaging.version import Version


class SDKVersionTooOld(RuntimeError):
    """Installed SDK cannot support the audited Managed Agents features."""


class SDKDeprecatedAPIUsed(RuntimeError):
    """Code path would use a deprecated SDK namespace for beta features."""


class SDKBreakingChangeDetected(RuntimeError):
    """A newer SDK no longer exposes the surfaces OmniSight depends on."""


ANTHROPIC_SDK_MIN_VERSION = Version("0.100.0")
ANTHROPIC_SDK_NEXT_MINOR_PROBE_VERSION = Version("0.101.0")
ANTHROPIC_SDK_MAJOR_CEILING = Version("1.0.0")
MANAGED_AGENTS_BETA_HEADER = "managed-agents-2026-04-01"

REQUIRED_BETA_CLIENT_NAMESPACES: tuple[str, ...] = (
    "agents",
    "environments",
    "memory_stores",
    "sessions",
    "vaults",
)
REQUIRED_BETA_TYPE_MODULES: tuple[str, ...] = (
    "anthropic.types.beta.beta_managed_agents_memory_store",
    "anthropic.types.beta.beta_managed_agents_outcome_evaluation_resource",
    "anthropic.types.beta.sessions.beta_managed_agents_user_define_outcome_event",
)


@dataclass(frozen=True)
class AnthropicSDKAuditReport:
    """Result of auditing the installed Anthropic SDK surface."""

    version: Version
    min_version: Version
    next_minor_probe_version: Version
    managed_agents_beta_header: str
    required_beta_namespaces: tuple[str, ...]
    required_type_modules: tuple[str, ...]


def _default_version_getter(_package_name: str) -> str:
    return importlib.metadata.version("anthropic")


def _default_module_loader(module_name: str) -> Any:
    return importlib.import_module(module_name)


def assert_anthropic_sdk_compatible(
    *,
    anthropic_module: Any | None = None,
    version_getter: Callable[[str], str] = _default_version_getter,
    module_loader: Callable[[str], Any] = _default_module_loader,
) -> AnthropicSDKAuditReport:
    """Validate the SDK floor and beta Managed Agents surface.

    The 2026-05 Managed Agents upgrade needs memory stores and outcomes
    generated in ``anthropic>=0.100.0``. Dreaming is a Managed Agents research
    preview that builds on memory stores rather than a separate Python SDK
    namespace, so the gate checks the shared managed-agents beta surface.
    """
    raw_version = version_getter("anthropic")
    version = Version(raw_version)
    if version < ANTHROPIC_SDK_MIN_VERSION:
        raise SDKVersionTooOld(
            "SDKVersionTooOld: anthropic "
            f"{version} is below required {ANTHROPIC_SDK_MIN_VERSION} for "
            "Memory Tool / Outcomes / Dreaming Managed Agents compatibility."
        )
    if version >= ANTHROPIC_SDK_MAJOR_CEILING:
        raise SDKBreakingChangeDetected(
            "SDKBreakingChangeDetected: anthropic "
            f"{version} crossed audited ceiling <{ANTHROPIC_SDK_MAJOR_CEILING}."
        )

    if anthropic_module is None:
        anthropic_module = module_loader("anthropic")

    client = anthropic_module.Anthropic(api_key="sdk-audit-placeholder")
    beta = getattr(client, "beta", None)
    missing = [
        namespace
        for namespace in REQUIRED_BETA_CLIENT_NAMESPACES
        if not hasattr(beta, namespace)
    ]
    if missing:
        raise SDKBreakingChangeDetected(
            "SDKBreakingChangeDetected: missing beta client namespace(s): "
            + ", ".join(sorted(missing))
        )

    missing_modules = []
    for module_name in REQUIRED_BETA_TYPE_MODULES:
        try:
            module_loader(module_name)
        except ModuleNotFoundError:
            missing_modules.append(module_name)
    if missing_modules:
        raise SDKBreakingChangeDetected(
            "SDKBreakingChangeDetected: missing generated type module(s): "
            + ", ".join(missing_modules)
        )

    return AnthropicSDKAuditReport(
        version=version,
        min_version=ANTHROPIC_SDK_MIN_VERSION,
        next_minor_probe_version=ANTHROPIC_SDK_NEXT_MINOR_PROBE_VERSION,
        managed_agents_beta_header=MANAGED_AGENTS_BETA_HEADER,
        required_beta_namespaces=REQUIRED_BETA_CLIENT_NAMESPACES,
        required_type_modules=REQUIRED_BETA_TYPE_MODULES,
    )


def assert_no_deprecated_beta_messages_call(
    *,
    requires_beta_messages: bool,
    used_beta_messages: bool,
) -> None:
    """Fail when a beta feature is sent through the stable messages namespace."""
    if requires_beta_messages and not used_beta_messages:
        raise SDKDeprecatedAPIUsed(
            "SDKDeprecatedAPIUsed: beta Anthropic features must use "
            "client.beta.messages.create()."
        )
