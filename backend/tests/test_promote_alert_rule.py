from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "promote-alert-rule.py"
RUNNER_ATLAS = REPO_ROOT / "deploy" / "prometheus" / "rules" / "runner_atlas.yml"


def _write_rule(tmp_path: Path, *, labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None, cap: int = 10) -> Path:
    labels = labels or {
        "severity": "warn",
        "area": "runner",
        "family": "10",
        "defense_dimension": "D1",
    }
    annotations = annotations or {
        "summary": "Runner claim stale",
        "description": "Runner claim has not advanced inside the expected window.",
        "runbook_url": "https://docs.sora.services/runbooks/runner-claim-stale",
        "remediation_hint": "Inspect runner release-1 before requeueing the claim.",
    }
    label_lines = "\n".join(f"          {key}: {value!r}" for key, value in labels.items())
    annotation_lines = "\n".join(f"          {key}: {value!r}" for key, value in annotations.items())
    path = tmp_path / "rule.yml"
    path.write_text(
        f"""
groups:
  - name: synthetic
    rules:
      - alert: synthetic_runner_alert
        expr: max by (instance) (runner_claim_heartbeat_age_seconds) > 300
        labels:
{label_lines}
        annotations:
{annotation_lines}
        __bridge:
          critical_labels: [instance]
          cardinality_caps:
            instance: {cap}
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _run(path: Path) -> tuple[int, dict[str, object]]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_valid_synthetic_rule_passes(tmp_path: Path) -> None:
    code, payload = _run(_write_rule(tmp_path))

    assert code == 0
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_missing_severity_label_fails(tmp_path: Path) -> None:
    labels = {
        "area": "runner",
        "family": "10",
        "defense_dimension": "D1",
    }

    code, payload = _run(_write_rule(tmp_path, labels=labels))

    assert code == 1
    assert payload["ok"] is False
    assert "missing required label: severity" in json.dumps(payload)


def test_unknown_severity_fails(tmp_path: Path) -> None:
    labels = {
        "severity": "critical",
        "area": "runner",
        "family": "10",
        "defense_dimension": "D1",
    }

    code, payload = _run(_write_rule(tmp_path, labels=labels))

    assert code == 1
    assert payload["ok"] is False
    assert "unknown severity: critical" in json.dumps(payload)


def test_cardinality_cap_violation_fails(tmp_path: Path) -> None:
    code, payload = _run(_write_rule(tmp_path, cap=11))

    assert code == 1
    assert payload["ok"] is False
    assert "cardinality cap for instance must be <= 10" in json.dumps(payload)


def test_missing_runbook_url_annotation_fails(tmp_path: Path) -> None:
    annotations = {
        "summary": "Runner claim stale",
        "description": "Runner claim has not advanced inside the expected window.",
        "remediation_hint": "Inspect runner release-1 before requeueing the claim.",
    }

    code, payload = _run(_write_rule(tmp_path, annotations=annotations))

    assert code == 1
    assert payload["ok"] is False
    assert "missing required annotation: runbook_url" in json.dumps(payload)


def test_existing_runner_atlas_rule_passes() -> None:
    code, payload = _run(RUNNER_ATLAS)

    assert code == 0
    assert payload["ok"] is True
    assert payload["errors"] == []
