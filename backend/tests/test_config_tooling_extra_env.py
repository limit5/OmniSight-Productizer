"""[OP-1702] (deploy-pipeline remediation, finding #26) — the gate/promote
tooling must tolerate operator-shell env pollution; the prod runtime must NOT.

The release tooling's ``Settings`` load tripped pydantic-settings'
``extra_forbidden`` on unrelated env / ``.env`` keys (``neo4j_*``,
``grafana_*``, ``omnisight_project_state_inject``, …) that operator shells
carry. Because the staging-gate audit-DB write imports ``backend.config``
lazily inside a best-effort ``try/except``, that raise silently fail-opened
the audit row (and forced ``env -i`` to get a clean run).

The fix scopes a tolerant ``extra='ignore'`` Settings variant to the
gate/promote tooling only — the prod-runtime ``Settings`` stays strict
(``extra='forbid'``), which is the security-sensitive invariant a human +2
verifies. These tests pin both halves.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# A representative slice of the operator-shell pollution from the audit:
# OMNISIGHT_-prefixed keys that do NOT map to any declared Settings field.
POLLUTING_ENV_LINES = (
    "OMNISIGHT_NEO4J_PASSWORD=hunter2",
    "OMNISIGHT_GRAFANA_URL=http://grafana.internal:3000",
    "OMNISIGHT_PROJECT_STATE_INJECT=1",
)
# A real, declared field so we can prove the tolerant model still loads
# legitimate values rather than ignoring everything.
DECLARED_ENV_LINE = "OMNISIGHT_APP_NAME=GateTooling"

REPO_ROOT = Path(__file__).resolve().parents[2]
FLAG = "OMNISIGHT_TOOLING_TOLERATE_EXTRA_ENV"


@pytest.fixture()
def polluted_dotenv(tmp_path: Path) -> Path:
    """A ``.env`` that mixes one declared field with operator pollution.

    The pollution trips ``extra_forbidden`` via pydantic-settings' dotenv
    source under the strict (prod) policy — that is precisely the boot
    failure OP-1702 fixes for the tooling path.
    """
    p = tmp_path / "polluted.env"
    p.write_text(
        "\n".join((DECLARED_ENV_LINE, *POLLUTING_ENV_LINES)) + "\n",
        encoding="utf-8",
    )
    return p


# ───────────────────────── in-process unit coverage ─────────────────────


def test_prod_settings_strict_rejects_unknown_env(polluted_dotenv: Path):
    """AC: prod-runtime Settings stays strict — unknown env is a hard error."""
    from pydantic import ValidationError

    from backend.config import Settings

    assert Settings.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=str(polluted_dotenv))
    # The failure is specifically the extra-key gate, not some other field.
    assert "extra_forbidden" in str(exc.value)


def test_tooling_settings_tolerates_unknown_env(polluted_dotenv: Path):
    """AC: the tooling-scoped Settings variant ignores the pollution and
    still loads declared fields."""
    from backend.config import Settings, _ToolingSettings

    assert _ToolingSettings.model_config["extra"] == "ignore"
    assert issubclass(_ToolingSettings, Settings)

    s = _ToolingSettings(_env_file=str(polluted_dotenv))
    # Declared field loaded from the same polluted file...
    assert s.app_name == "GateTooling"
    # ...and the pollution was dropped, not promoted onto the model.
    assert not hasattr(s, "neo4j_password")
    assert not hasattr(s, "project_state_inject")


def test_singleton_factory_honours_flag(monkeypatch):
    """The process-wide singleton is tolerant only when the tooling flag
    is set; strict otherwise."""
    from backend import config as cfg

    monkeypatch.setenv(FLAG, "1")
    assert type(cfg._build_settings_singleton()) is cfg._ToolingSettings

    for falsey in ("", "0", "false", "no"):
        monkeypatch.setenv(FLAG, falsey)
        assert type(cfg._build_settings_singleton()) is cfg.Settings

    monkeypatch.delenv(FLAG, raising=False)
    assert type(cfg._build_settings_singleton()) is cfg.Settings


# ───────────────────────── end-to-end (polluted shell) ──────────────────


def _run_child(code: str, dotenv: Path) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter under a polluted shell + .env.

    The child env is built clean (the parent test process may already carry
    the tooling flag because another test imported the gate module) and then
    polluted both via shell env vars and via the dotenv the child loads.
    """
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "OMNISIGHT_DOTENV_FILE": str(dotenv),
        # Shell-level pollution too, matching the AC's "polluted shell".
        "OMNISIGHT_NEO4J_PASSWORD": "hunter2",
        "OMNISIGHT_PROJECT_STATE_INJECT": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_prod_import_fails_under_polluted_shell(polluted_dotenv: Path):
    """Control: importing backend.config WITHOUT the tooling opt-in trips
    extra_forbidden under a polluted shell/.env — proving the strict prod
    policy is genuinely in force (and that the tooling fix is load-bearing)."""
    proc = _run_child("import backend.config", polluted_dotenv)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "extra_forbidden" in proc.stderr or "Extra inputs are not permitted" in proc.stderr


def test_staging_gate_tolerates_polluted_shell(polluted_dotenv: Path):
    """AC: the staging gate runs from a polluted shell without
    extra_forbidden — importing it opts the process into ``extra='ignore'``
    before backend.config builds its singleton."""
    code = (
        "import backend.agents.staging_gate\n"
        "from backend.config import settings\n"
        "print('OK', settings.app_name)\n"
    )
    proc = _run_child(code, polluted_dotenv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("OK")


def test_auto_promote_tolerates_polluted_shell(polluted_dotenv: Path):
    """AC: the promote tooling likewise tolerates a polluted shell —
    jira_dispatch imports backend.config at module load, so the flag is set
    before that import runs."""
    code = (
        "import backend.agents.auto_promote_main\n"
        "from backend.config import settings\n"
        "print('OK', settings.app_name)\n"
    )
    proc = _run_child(code, polluted_dotenv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("OK")
