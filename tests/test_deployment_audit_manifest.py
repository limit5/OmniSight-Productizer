"""OP-1631 deployment-audit expected-live manifest coverage."""

from __future__ import annotations

import re
import shlex
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "deployment-audit.sh"


def _builtin_manifest_rows() -> list[list[str]]:
    text = AUDIT_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"cat <<'EOF'\n(.*?)\nEOF", text, re.S)
    assert match, "deployment-audit built-in manifest heredoc not found"
    rows: list[list[str]] = []
    for raw in match.group(1).splitlines():
        if not raw or raw.startswith("#"):
            continue
        rows.append(shlex.split(raw))
    return rows


def test_coordinator_units_are_expected_live_in_deployment_audit_manifest() -> None:
    rows = {
        (row[0], row[1]): {
            "expected": row[2],
            "ticket": row[3],
            "note": " ".join(row[4:]),
        }
        for row in _builtin_manifest_rows()
    }

    assert rows[("systemd-unit", "pipeline-coordinator.service")] == {
        "expected": "yes",
        "ticket": "OP-1547",
        "note": "coordinator daemon (ADR-0021) — must be live",
    }
    assert rows[("systemd-unit", "pipeline-coordinator-watchdog.service")] == {
        "expected": "yes",
        "ticket": "OP-1547",
        "note": "coordinator liveness watchdog — must be live",
    }
