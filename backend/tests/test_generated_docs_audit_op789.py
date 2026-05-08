"""OP-789 generated docs audit + migration contract."""
from __future__ import annotations

import subprocess
from pathlib import Path

from backend import docs_site_tool_reference

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_DOC = REPO_ROOT / "docs" / "operations" / "generated-docs-audit-op789.md"
MIGRATED_GENERATED_DOCS = ("docs/agents/tool-reference.md",)
AUDIT_CANDIDATES = (
    "docs/adr/README.md",
    "docs/sop/lessons-learned.md",
    "docs/status/handoff_status.yaml",
    "docs/agents/tool-reference.md",
    "docs/architecture.md",
    "docs/architecture/agents/*.md",
    "openapi.json",
    "lib/generated/api-types.ts",
    "lib/generated/openapi.ts",
    "lib/generated/README.md",
)


def test_audit_doc_lists_all_generated_doc_candidates_with_disposition() -> None:
    body = AUDIT_DOC.read_text(encoding="utf-8")

    for candidate in AUDIT_CANDIDATES:
        assert f"| `{candidate}` |" in body
    assert "migrate-to-docs-site" in body
    assert "keep-in-git" in body
    assert "already-migrated" in body


def test_migrated_generated_docs_are_ignored_and_untracked() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    for path in MIGRATED_GENERATED_DOCS:
        assert path in gitignore

    proc = subprocess.run(
        ["git", "ls-files", "--", *MIGRATED_GENERATED_DOCS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout == ""


def test_tool_reference_is_available_for_dynamic_docs_site_build() -> None:
    rendered = docs_site_tool_reference.build_tool_reference()

    assert rendered.startswith("# OmniSight Tool Reference\n")
    assert "### `Read`" in rendered
    assert "### `ToolSearch`" in rendered
