"""OP-863 — Anthropic SDK floor and beta-surface compatibility tests."""

from __future__ import annotations

import types
from typing import Any

import pytest

from backend.agents.anthropic_sdk_audit import (
    ANTHROPIC_SDK_MIN_VERSION,
    ANTHROPIC_SDK_NEXT_MINOR_PROBE_VERSION,
    MANAGED_AGENTS_BETA_HEADER,
    REQUIRED_BETA_CLIENT_NAMESPACES,
    REQUIRED_BETA_TYPE_MODULES,
    SDKBreakingChangeDetected,
    SDKDeprecatedAPIUsed,
    SDKVersionTooOld,
    assert_anthropic_sdk_compatible,
    assert_no_deprecated_beta_messages_call,
)


def _fake_anthropic_module(missing_namespace: str | None = None) -> Any:
    class _Client:
        def __init__(self, *, api_key: str) -> None:  # noqa: ARG002
            beta = types.SimpleNamespace()
            for namespace in REQUIRED_BETA_CLIENT_NAMESPACES:
                if namespace != missing_namespace:
                    setattr(beta, namespace, object())
            self.beta = beta

    return types.SimpleNamespace(Anthropic=_Client)


def _module_loader(module_name: str) -> Any:
    if module_name == "anthropic":
        return _fake_anthropic_module()
    if module_name in REQUIRED_BETA_TYPE_MODULES:
        return types.SimpleNamespace()
    raise ModuleNotFoundError(module_name)


def test_pin_gate_accepts_current_managed_agents_floor() -> None:
    report = assert_anthropic_sdk_compatible(
        version_getter=lambda _package_name: str(ANTHROPIC_SDK_MIN_VERSION),
        module_loader=_module_loader,
    )

    assert report.version == ANTHROPIC_SDK_MIN_VERSION
    assert report.managed_agents_beta_header == MANAGED_AGENTS_BETA_HEADER
    assert "memory_stores" in report.required_beta_namespaces


def test_pin_gate_rejects_too_old_sdk() -> None:
    with pytest.raises(SDKVersionTooOld, match="SDKVersionTooOld"):
        assert_anthropic_sdk_compatible(
            version_getter=lambda _package_name: "0.99.0",
            module_loader=_module_loader,
        )


def test_next_minor_probe_reveals_missing_beta_surface() -> None:
    def module_loader(module_name: str) -> Any:
        if module_name == "anthropic":
            return _fake_anthropic_module(missing_namespace="memory_stores")
        if module_name in REQUIRED_BETA_TYPE_MODULES:
            return types.SimpleNamespace()
        raise ModuleNotFoundError(module_name)

    with pytest.raises(SDKBreakingChangeDetected, match="memory_stores"):
        assert_anthropic_sdk_compatible(
            version_getter=lambda _package_name: str(
                ANTHROPIC_SDK_NEXT_MINOR_PROBE_VERSION
            ),
            module_loader=module_loader,
        )


def test_next_minor_probe_accepts_compatible_sdk_surface() -> None:
    report = assert_anthropic_sdk_compatible(
        version_getter=lambda _package_name: str(ANTHROPIC_SDK_NEXT_MINOR_PROBE_VERSION),
        module_loader=_module_loader,
    )

    assert report.version == ANTHROPIC_SDK_NEXT_MINOR_PROBE_VERSION


def test_deprecated_beta_messages_call_is_rejected() -> None:
    with pytest.raises(SDKDeprecatedAPIUsed, match="client.beta.messages.create"):
        assert_no_deprecated_beta_messages_call(
            requires_beta_messages=True,
            used_beta_messages=False,
        )


def test_current_beta_messages_call_emits_no_deprecation_warning() -> None:
    assert_no_deprecated_beta_messages_call(
        requires_beta_messages=True,
        used_beta_messages=True,
    )
