"""[OP-1643] Boreas-B A2 — env-contract guard truth-table + exemption tests.

The guard ships a test/CI carveout (bypass when PYTEST_CURRENT_TEST/CI_MODE set
AND the DSN is not prod AND it is not a dev<->staging mismatch — see OP-1698).
To exercise the real-process truth table we clear those markers per-test via
``_as_runtime``.
"""

import pytest

from backend.env_contract import (
    EnvContractViolation,
    canonical_env,
    classify_dsn,
    enforce_env_db_contract,
)

PROD = "postgresql://omnisight:pw@127.0.0.1:5432/omnisight"
PROD_ASYNCPG = "postgresql+asyncpg://omnisight:pw@postgres:5432/omnisight"
DEV = "postgresql://omnisight_dev:pw@postgres:5432/omnisight_dev"
DEV_PORT = "postgresql://x:pw@127.0.0.1:58432/whatever"
STAGING = "postgresql://omnisight:pw@postgres:55432/omnisight_staging"
SQLITE = "sqlite:////app/data/omnisight.db"
MYSQL = "mysql://u:p@h/d"
UNKNOWN_PG = "postgresql://someone:pw@host:5432/some_other_db"


def _runtime(monkeypatch):
    """Simulate a real (non-pytest, non-CI) process so the carveout is off.

    MUST be called from the test *body*, not a fixture: pytest re-sets
    ``PYTEST_CURRENT_TEST`` for the call phase after fixture setup runs, so a
    delenv in a fixture would be clobbered before the test executes.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("OMNISIGHT_CI_MODE", raising=False)
    monkeypatch.delenv("OMNISIGHT_ENV_CONTRACT_DISABLE", raising=False)
    return monkeypatch


# ── classify_dsn ──────────────────────────────────────────────────────
@pytest.mark.parametrize("dsn,expected", [
    (PROD, "prod"),
    (PROD_ASYNCPG, "prod"),
    (DEV, "dev"),
    (DEV_PORT, "dev"),
    (STAGING, "staging"),
    ("postgresql://u:p@h:55433/anything", "staging"),
    (SQLITE, "sqlite"),
    ("", "skip"),
    (MYSQL, "skip"),
    (UNKNOWN_PG, "unknown"),
    # prod requires BOTH db and user == omnisight (Q2 fix)
    ("postgresql://someuser:pw@h:5432/omnisight", "unknown"),
])
def test_classify(dsn, expected):
    assert classify_dsn(dsn) == expected


def test_classify_handles_password_special_chars():
    # password contains '/' and '@' — must not be mistaken for path/userinfo.
    dsn = "postgresql://omnisight_dev:ab/cd@ef@postgres:5432/omnisight_dev"
    assert classify_dsn(dsn) == "dev"


def test_canonical_env():
    assert canonical_env("production") == "prod"
    assert canonical_env("PROD") == "prod"
    assert canonical_env("development") == "dev"
    assert canonical_env("develop") == "dev"
    assert canonical_env("staging") == "staging"
    assert canonical_env("") is None
    assert canonical_env(None) is None
    assert canonical_env("weird") == "weird"  # unknown passes through, fails match


# ── truth table (OK rows raise nothing) ───────────────────────────────
@pytest.mark.parametrize("env,dsn", [
    ("dev", DEV),
    ("prod", PROD),
    ("production", PROD),
    (None, PROD),       # legacy prod-by-default: runners/coordinator
    ("staging", STAGING),
    ("dev", SQLITE),    # sqlite always OK
    ("prod", MYSQL),    # non-PG skip
])
def test_ok_rows(monkeypatch, env, dsn):
    _runtime(monkeypatch)
    enforce_env_db_contract(dsn, source="test", env=env)  # no raise


# ── truth table (FAIL rows raise) ─────────────────────────────────────
@pytest.mark.parametrize("env,dsn", [
    ("dev", PROD),          # core protection
    ("dev", STAGING),
    ("prod", DEV),          # symmetric
    ("production", DEV),
    (None, DEV),            # non-prod DB without declaring non-prod
    (None, STAGING),
    ("dev", UNKNOWN_PG),    # unrecognised PG hard-fails
    (None, UNKNOWN_PG),
    ("prod", UNKNOWN_PG),
])
def test_fail_rows(monkeypatch, env, dsn):
    _runtime(monkeypatch)
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(dsn, source="test", env=env)


# ── exemptions ────────────────────────────────────────────────────────
def test_pytest_carveout_bypasses_genuine_test_setup(monkeypatch):
    # PYTEST_CURRENT_TEST is set by pytest while this runs; the genuinely-needed
    # test exemptions are still bypassed so the suite's own DB setup isn't
    # poisoned: env-unset connecting to a dev/staging DB, and env==DSN.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    enforce_env_db_contract(DEV, source="test", env=None)      # unset+dev test DB
    enforce_env_db_contract(STAGING, source="test", env=None)  # unset+staging test DB
    enforce_env_db_contract(DEV, source="test", env="dev")     # matching, no raise


def test_pytest_carveout_does_not_bypass_prod(monkeypatch):
    # but a PROD DSN mismatch is still a real violation, even under pytest.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(PROD, source="test", env="dev")


def test_pytest_carveout_does_not_bypass_dev_staging(monkeypatch):
    # OP-1698 finding #17: a dev<->staging mismatch is real cross-contamination,
    # NOT a test artifact, so it must fail even with PYTEST_CURRENT_TEST set.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(STAGING, source="test", env="dev")  # dev proc, staging DSN
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(DEV, source="test", env="staging")  # staging proc, dev DSN
    # also via the port-based staging classifier, not just the db-name suffix
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(DEV_PORT, source="test", env="staging")


def test_ci_mode_carveout(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("OMNISIGHT_CI_MODE", "1")
    enforce_env_db_contract(DEV, source="test", env="prod")  # non dev<->staging → bypass
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(PROD, source="test", env="dev")  # prod → enforced


def test_ci_mode_does_not_bypass_dev_staging(monkeypatch):
    # OP-1698 finding #17: dev<->staging contract enforced under CI_MODE too.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("OMNISIGHT_CI_MODE", "1")
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(STAGING, source="test", env="dev")
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(DEV, source="test", env="staging")


def test_disable_hatch_refused_in_prod(monkeypatch):
    _runtime(monkeypatch)
    monkeypatch.setenv("OMNISIGHT_ENV_CONTRACT_DISABLE", "1")
    # refused when env is prod
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(DEV, source="test", env="prod")
    # refused when DSN class is prod
    with pytest.raises(EnvContractViolation):
        enforce_env_db_contract(PROD, source="test", env="dev")


def test_disable_hatch_allowed_nonprod(monkeypatch):
    _runtime(monkeypatch)
    monkeypatch.setenv("OMNISIGHT_ENV_CONTRACT_DISABLE", "1")
    # non-prod break-glass: dev env + staging DSN normally fails, hatch allows it
    enforce_env_db_contract(STAGING, source="test", env="dev")  # no raise


def test_redaction_in_message(monkeypatch):
    _runtime(monkeypatch)
    try:
        enforce_env_db_contract(PROD, source="test", env="dev")
    except EnvContractViolation as exc:
        assert "pw" not in exc.message
        assert "***" in exc.message
    else:
        pytest.fail("expected EnvContractViolation")
