"""T1-A — get_platform_config default flipped from aarch64 to host-native.

Pre-T1-A an AMD 9950X dev box with no `.omnisight/platform` hint
silently defaulted to `aarch64`, then every DAG drafted from that
workspace tried to cross-compile. This test locks in the new
behaviour: default → `host_native` when the profile exists, fallback
→ `aarch64` otherwise.
"""

from __future__ import annotations

import pytest


def test_default_platform_is_host_native_when_profile_exists():
    """host_native.yaml ships in configs/platforms/; default must use it."""
    from backend.agents.tools import _default_platform_for_host
    assert _default_platform_for_host() == "host_native"


def test_default_platform_falls_back_to_aarch64_when_missing(monkeypatch):
    """If someone deletes host_native.yaml, fall back instead of
    raising — legacy users must not lose the ability to draft plans."""
    from backend.agents import tools as _tools
    from backend import sdk_provisioner as _sp

    # Pretend _platform_profile can't find host_native.
    def missing(name):
        if name == "host_native":
            from pathlib import Path
            return Path("/nonexistent/no-such-profile.yaml")
        return _sp._platform_profile(name)
    monkeypatch.setattr(_sp, "_platform_profile", missing)
    # Re-import path is cached by fn closure — just call helper.
    assert _tools._default_platform_for_host() == "aarch64"


def test_default_platform_swallows_import_errors(monkeypatch):
    """sdk_provisioner import fail → helper must still return a value,
    not raise. This is the robustness contract the workspace setup
    depends on (cold start before provisioner is ready)."""
    from backend.agents import tools as _tools

    def boom(*a, **kw):
        raise RuntimeError("provisioner not ready")
    monkeypatch.setattr(
        "backend.sdk_provisioner._platform_profile", boom,
    )
    # Should not raise.
    result = _tools._default_platform_for_host()
    assert result in ("host_native", "aarch64")


@pytest.mark.asyncio
async def test_get_platform_config_uses_default_when_no_hint(workspace):
    """Integration: a workspace with no .omnisight/platform file gets
    the host-native config, not the legacy aarch64 cross-compile setup.

    The `workspace` fixture (conftest) lives at backend/tests/.
    """
    from backend.agents.tools import get_platform_config

    # Workspace has no .omnisight/platform — verify default flows through.
    out = await get_platform_config.ainvoke({"platform": ""})
    # host_native profile sets cross_prefix=""; aarch64 would have
    # CROSS_COMPILE=aarch64-linux-gnu-. The absence of cross_prefix is
    # the observable signal of the T1-A fix.
    assert "PLATFORM=host_native" in out
    assert "CROSS_COMPILE=" in out
    assert "CROSS_COMPILE=aarch64-linux-gnu-" not in out


@pytest.mark.asyncio
async def test_explicit_aarch64_still_works():
    """The fix only changes the no-hint default. Callers that explicitly
    pass `platform=aarch64` must still get the cross-compile config."""
    from backend.agents.tools import get_platform_config
    out = await get_platform_config.ainvoke({"platform": "aarch64"})
    assert "PLATFORM=aarch64" in out
