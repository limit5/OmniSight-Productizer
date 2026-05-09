"""OP-111 tests for ``scripts/audit_mcp_servers.py``."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "audit_mcp_servers.py"
DOC = REPO_ROOT / "docs" / "operations" / "mcp-inventory.md"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import audit_mcp_servers as auditor  # noqa: E402


def test_discover_rows_includes_backend_catalog_and_required_fleet_entries() -> None:
    rows = auditor.discover_rows()
    names = {row.server_name for row in rows}
    assert {"claude_ai_Figma", "claude_ai_Gmail"} <= names
    assert {"mcp-atlassian", "mcp-gitlab", "mcp-gerrit", "local tool dispatcher"} <= names
    assert any("OMNISIGHT_MCP_FIGMA_TOKEN" in row.auth_type for row in rows)


def test_render_markdown_has_required_columns() -> None:
    body = auditor.render_markdown(auditor.discover_rows())
    assert "| server name | purpose | auth-type | status |" in body
    assert "| mcp-atlassian | JIRA issue workflow" in body
    assert body.count("\n|") >= 5


def test_cli_writes_doc_and_stdout_table() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("\n|") >= 5
    assert "| mcp-gerrit |" in proc.stdout
    assert DOC.read_text(encoding="utf-8") == proc.stdout
