"""Phase 67-E follow-up — platform tags wired through to the RAG
sandbox-error gate.

Two layers under test:
  1. `_resolve_platform_tags` reads workspace platform hint + profile
     YAML, returns (vendor, sdk). Bad / missing inputs degrade to
     ("", "") so the SDK hard-lock stays permissive by default.
  2. `GraphState` carries the tags through; nodes.error_check_node
     forwards them to prefetch_for_sandbox_error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.routers.invoke import _resolve_platform_tags
from backend.agents.state import GraphState


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _resolve_platform_tags
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_no_workspace_returns_empty_tuple():
    assert _resolve_platform_tags(None) == ("", "")
    assert _resolve_platform_tags("") == ("", "")


def test_workspace_without_platform_hint_returns_empty(tmp_path):
    # Workspace exists but no .omnisight/platform file.
    assert _resolve_platform_tags(str(tmp_path)) == ("", "")


def test_blank_platform_hint_returns_empty(tmp_path):
    (tmp_path / ".omnisight").mkdir()
    (tmp_path / ".omnisight" / "platform").write_text("   \n")
    assert _resolve_platform_tags(str(tmp_path)) == ("", "")


def test_invalid_platform_name_blocked(tmp_path):
    """Path-traversal-y platform names must not be looked up — the
    validator rejects them and we degrade to permissive."""
    (tmp_path / ".omnisight").mkdir()
    (tmp_path / ".omnisight" / "platform").write_text("../etc/passwd")
    assert _resolve_platform_tags(str(tmp_path)) == ("", "")


def test_unknown_platform_returns_empty(tmp_path):
    (tmp_path / ".omnisight").mkdir()
    (tmp_path / ".omnisight" / "platform").write_text("definitely-not-a-real-platform-xyz")
    assert _resolve_platform_tags(str(tmp_path)) == ("", "")


def test_resolves_tags_from_profile_yaml(tmp_path, monkeypatch):
    """Plant a fake platform profile via monkeypatch and verify the
    helper finds vendor_id + sdk_version. Avoids depending on the
    real `configs/platforms/*.yaml` shipping any specific entry."""
    profile = tmp_path / "fake-platform.yaml"
    profile.write_text(
        "platform: fake-platform\n"
        "vendor_id: AcmeChip\n"
        "sdk_version: SDK-v3.1\n"
    )

    from backend import sdk_provisioner as sp
    monkeypatch.setattr(sp, "_validate_platform_name", lambda name: name == "fake-platform")
    monkeypatch.setattr(sp, "_platform_profile", lambda name: profile)

    ws = tmp_path / "ws"
    (ws / ".omnisight").mkdir(parents=True)
    (ws / ".omnisight" / "platform").write_text("fake-platform")

    assert _resolve_platform_tags(str(ws)) == ("AcmeChip", "SDK-v3.1")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GraphState carries the tags
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_graphstate_defaults_are_empty():
    s = GraphState()
    assert s.soc_vendor == ""
    assert s.sdk_version == ""


def test_graphstate_round_trip():
    s = GraphState(soc_vendor="Rockchip", sdk_version="SDK-v2")
    assert s.soc_vendor == "Rockchip"
    assert s.sdk_version == "SDK-v2"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  error_check_node forwards tags to prefetch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_error_check_node_forwards_platform_tags(monkeypatch):
    """Patch prefetch_for_sandbox_error and assert the state's
    soc_vendor / sdk_version hit it as kwargs. This is the wire we
    just added; without it the SDK hard-lock can never fire."""
    from backend import rag_prefetch as rp
    from backend.agents import nodes
    from backend.agents.state import ToolResult

    captured: dict = {}

    async def spy(error_log, *, rc, soc_vendor, sdk_version):
        captured["error_log"] = error_log
        captured["rc"] = rc
        captured["soc_vendor"] = soc_vendor
        captured["sdk_version"] = sdk_version
        return None  # no injection — just verifying the call

    monkeypatch.setattr(rp, "prefetch_for_sandbox_error", spy)

    state = GraphState(
        user_command="x",
        retry_count=0, max_retries=3,
        last_error="boom",
        soc_vendor="Rockchip", sdk_version="SDK-v2",
        tool_results=[
            ToolResult(tool_name="bash", output="undefined reference to v4l2_open", success=False),
        ],
    )
    await nodes.error_check_node(state)

    assert captured["soc_vendor"] == "Rockchip"
    assert captured["sdk_version"] == "SDK-v2"
    assert captured["rc"] == 1
    assert "v4l2_open" in captured["error_log"]
