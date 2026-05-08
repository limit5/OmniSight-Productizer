"""OP-768 deploy.sh staging validation gate contract."""
from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"


def test_ac3_deploy_sh_runs_staging_validation_gate_after_restart() -> None:
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert "run_staging_validation_gate()" in text
    assert "python3 -m backend.staging_validation" in text
    assert "OMNISIGHT_STAGING_OBSERVE_SECONDS:-900" in text
    assert "staging_regression event emitted" in text
    assert re.search(r"\bexit\s+7\b", text)


def test_ac3_gate_is_staging_only_and_has_dangerous_skip_warning() -> None:
    text = DEPLOY_SH.read_text(encoding="utf-8")
    fn = re.search(
        r"run_staging_validation_gate\(\)\s*\{(.+?)^\}",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert fn, "deploy.sh must define run_staging_validation_gate"
    body = fn.group(1)
    assert '[[ "$ENV" != "staging" ]]' in body
    assert "OMNISIGHT_SKIP_STAGING_VALIDATION" in body
    assert "DANGEROUS" in body
