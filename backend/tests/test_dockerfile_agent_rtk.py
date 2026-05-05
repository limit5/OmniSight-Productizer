"""BP.R.1 contract tests for the agent Dockerfile RTK install path."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_AGENT = REPO_ROOT / "backend/docker/Dockerfile.agent"


def test_agent_rtk_install_hard_fails_and_writes_prod_log() -> None:
    text = DOCKERFILE_AGENT.read_text(encoding="utf-8")
    rtk_block = text[text.index("# ── RTK (Rust Token Killer)"):]
    rtk_block = rtk_block[:rtk_block.index("# ── Git configuration ──")]

    assert "|| true" not in rtk_block
    assert "2>/dev/null" not in rtk_block
    assert "/var/log/omnisight/rtk-install.log" in rtk_block
    assert "mkdir -p /root/.claude" in rtk_block
    assert "ln -sf /root/.local/bin/rtk /usr/local/bin/rtk" in rtk_block
    assert re.search(r"sh\s+/tmp/rtk-install\.sh\s+>\"\$RTK_INSTALL_LOG\"\s+2>&1", rtk_block)
    assert re.search(r"rtk\s+--version\s+>>\"\$RTK_INSTALL_LOG\"\s+2>&1", rtk_block)
    assert re.search(r"rtk\s+init\s+--global\s+>>\"\$RTK_INSTALL_LOG\"\s+2>&1", rtk_block)
    assert rtk_block.count("exit 1") >= 2
