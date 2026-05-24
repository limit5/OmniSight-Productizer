"""[OP-1697] A2 guard coverage for the 3 prod-DB ``sa.create_engine`` paths.

Finding #16 of the deploy-pipeline deep audit: the A2 env-contract guard
claims to cover "every DB entry point", but three modules opened
``OMNISIGHT_DATABASE_URL`` via ``sa.create_engine`` in their private
``_engine()`` resolver with NO ``enforce_env_db_contract`` call:

    * backend/deploy_audit.py
    * backend/release_conductor/state_machine.py
    * backend/orchestrator/prod_deploy.py

These tests pin the fix: each ``_engine()`` resolver now fails closed
(``EnvContractViolation``) when the resolved DSN's class disagrees with
``OMNISIGHT_ENV``, and still resolves cleanly for the benign sqlite path.

Testing the prod path
---------------------
Each resolver short-circuits to the test-injected engine when
``_test_engine`` is set and caches the prod engine in ``_prod_engine``.
To reach the guarded ``create_engine`` branch we null both module globals
(via ``monkeypatch.setattr`` so they auto-restore) before calling
``_engine()``.

The A2 guard carves out non-prod mismatches while ``PYTEST_CURRENT_TEST``
is set, so to assert the *fail-closed direction* we use a PROD-class DSN
with ``OMNISIGHT_ENV=dev`` — a prod-class DSN is enforced even under the
pytest carveout (see ``test_pytest_carveout_does_not_bypass_prod`` in
``test_env_contract.py``). create_engine is lazy, so the guard raises
before any connection is attempted against the fake prod host.
"""
from __future__ import annotations

import pytest

from backend import deploy_audit
from backend.env_contract import EnvContractViolation
from backend.orchestrator import prod_deploy
from backend.release_conductor import state_machine

# Prod-class DSN: db == user == "omnisight" (classify_dsn -> "prod").
PROD_DSN = "postgresql://omnisight:pw@127.0.0.1:5432/omnisight"

# The three modules under test and their default-sqlite fallback marker.
_GUARDED_MODULES = [
    pytest.param(deploy_audit, "deploy_audit.db", id="deploy_audit"),
    pytest.param(state_machine, "release_state.db", id="release_conductor.state_machine"),
    pytest.param(prod_deploy, "prod_deploy_audit.db", id="orchestrator.prod_deploy"),
]


def _force_prod_path(monkeypatch, module) -> None:
    """Null the test-injected + cached engines so ``_engine()`` re-resolves
    through the guarded ``create_engine`` branch."""
    monkeypatch.setattr(module, "_test_engine", None, raising=False)
    monkeypatch.setattr(module, "_prod_engine", None, raising=False)


@pytest.mark.parametrize("module,_marker", _GUARDED_MODULES)
def test_engine_fails_closed_on_env_db_mismatch(monkeypatch, module, _marker):
    """dev process resolving a prod-looking DSN -> EnvContractViolation."""
    _force_prod_path(monkeypatch, module)
    monkeypatch.setenv("OMNISIGHT_ENV", "dev")
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", PROD_DSN)
    monkeypatch.delenv("OMNISIGHT_ENV_CONTRACT_DISABLE", raising=False)

    with pytest.raises(EnvContractViolation):
        module._engine()


@pytest.mark.parametrize("module,_marker", _GUARDED_MODULES)
def test_engine_fails_closed_symmetric_prod_env_dev_dsn(monkeypatch, module, _marker):
    """Symmetric direction: prod env must refuse a dev-class DSN.

    A dev-class DSN under pytest is normally carved out, so we clear the
    carveout markers to exercise the real-process truth table here."""
    _force_prod_path(monkeypatch, module)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("OMNISIGHT_CI_MODE", raising=False)
    monkeypatch.delenv("OMNISIGHT_ENV_CONTRACT_DISABLE", raising=False)
    monkeypatch.setenv("OMNISIGHT_ENV", "prod")
    monkeypatch.setenv(
        "OMNISIGHT_DATABASE_URL",
        "postgresql://omnisight_dev:pw@postgres:5432/omnisight_dev",
    )

    with pytest.raises(EnvContractViolation):
        module._engine()


@pytest.mark.parametrize("module,marker", _GUARDED_MODULES)
def test_engine_resolves_clean_for_sqlite_default(monkeypatch, module, marker):
    """No OMNISIGHT_DATABASE_URL -> sqlite fallback -> guard returns (skip)
    and a usable sqlite engine is built."""
    _force_prod_path(monkeypatch, module)
    monkeypatch.delenv("OMNISIGHT_DATABASE_URL", raising=False)
    monkeypatch.setenv("OMNISIGHT_ENV", "prod")  # would mismatch a PG DSN

    engine = module._engine()
    try:
        assert engine.dialect.name == "sqlite"
        assert marker in str(engine.url)
    finally:
        engine.dispose()


@pytest.mark.parametrize("module,_marker", _GUARDED_MODULES)
def test_engine_unrecognised_pg_fails_closed(monkeypatch, module, _marker):
    """An unplaceable Postgres DSN is refused (never fail-open)."""
    _force_prod_path(monkeypatch, module)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("OMNISIGHT_CI_MODE", raising=False)
    monkeypatch.delenv("OMNISIGHT_ENV_CONTRACT_DISABLE", raising=False)
    monkeypatch.setenv("OMNISIGHT_ENV", "prod")
    monkeypatch.setenv(
        "OMNISIGHT_DATABASE_URL",
        "postgresql://someone:pw@host:5432/some_other_db",
    )

    with pytest.raises(EnvContractViolation):
        module._engine()
