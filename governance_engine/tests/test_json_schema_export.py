from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from governance_engine.schema.v1 import TicketContractV1

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "governance" / "export-json-schema.py"


def _run_export(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _exported_schema(tmp_path: Path) -> dict[str, object]:
    out = tmp_path / "v1.schema.json"
    result = _run_export("--out", str(out))
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_export_produces_valid_json(tmp_path: Path) -> None:
    assert _exported_schema(tmp_path)

def test_export_includes_required_top_level_keys(tmp_path: Path) -> None:
    schema = _exported_schema(tmp_path)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "https://omnisight.example/schemas/ticket-contract/v1"
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)


def test_export_covers_all_v1_fields(tmp_path: Path) -> None:
    properties = _exported_schema(tmp_path)["properties"]
    expected_names = {
        field.alias or name
        for name, field in TicketContractV1.model_fields.items()
    }
    assert expected_names <= set(properties)


def test_export_is_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "v1.schema.json"
    first = _run_export("--out", str(out))
    first_bytes = out.read_bytes()
    second = _run_export("--out", str(out))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert out.read_bytes() == first_bytes


def test_check_mode_detects_drift(tmp_path: Path) -> None:
    out = tmp_path / "v1.schema.json"
    result = _run_export("--out", str(out))
    assert result.returncode == 0, result.stderr
    out.write_text('{"drift": true}\n', encoding="utf-8")

    check = _run_export("--out", str(out), "--check")

    assert check.returncode != 0
    assert "schema drift" in check.stderr
