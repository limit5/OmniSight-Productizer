"""OP-1167 — /readyz 503 remediation field."""

from __future__ import annotations

import json

import pytest

from backend import metrics
from backend.routers import health


def _json_body(response) -> dict:
    return json.loads(response.body.decode())


def _set_backward_drift() -> None:
    metrics.reset_for_tests()
    metrics.alembic_drift.labels(direction="backward").set(1.0)


@pytest.fixture(autouse=True)
def _readyz_unit_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _db_ok() -> tuple[bool, str]:
        return True, "ok"

    def _provider_ok() -> tuple[bool, str]:
        return True, "ready=ollama"

    monkeypatch.setattr(health, "_check_db", _db_ok)
    monkeypatch.setattr(health, "_check_provider_chain", _provider_ok)
    monkeypatch.setattr(
        health,
        "_check_db_pool",
        lambda: (True, "pool_not-initialised_dev mode"),
    )
    monkeypatch.setattr(health, "_check_jira", lambda: (True, "not_configured"))


@pytest.mark.asyncio
async def test_readyz_503_includes_remediation_field_on_backward_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _migrations_behind() -> tuple[bool, str]:
        return (
            False,
            "migration_pending: current=0239_future "
            "latest_file=0237_runner_audit_events.py",
        )

    monkeypatch.setattr(health, "_check_migrations", _migrations_behind)
    _set_backward_drift()

    response = await health._readyz_handler()
    body = _json_body(response)

    assert response.status_code == 503
    assert "remediation" in body
    assert body["remediation"].startswith("deploy backend image")


@pytest.mark.asyncio
async def test_readyz_503_remediation_references_db_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _migrations_behind() -> tuple[bool, str]:
        return (
            False,
            "migration_pending: current=0239_future "
            "latest_file=0237_runner_audit_events.py",
        )

    monkeypatch.setattr(health, "_check_migrations", _migrations_behind)
    _set_backward_drift()

    body = _json_body(await health._readyz_handler())

    assert ">= 0239_future" in body["remediation"]
    assert "current image head 0237_runner_audit_events" in body["remediation"]


@pytest.mark.asyncio
async def test_readyz_503_remediation_references_adr_0036(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _migrations_behind() -> tuple[bool, str]:
        return (
            False,
            "migration_pending: current=0239_future "
            "latest_file=0237_runner_audit_events.py",
        )

    monkeypatch.setattr(health, "_check_migrations", _migrations_behind)
    _set_backward_drift()

    body = _json_body(await health._readyz_handler())

    assert "ADR-0036" in body["remediation"]


@pytest.mark.asyncio
async def test_readyz_200_does_not_include_remediation_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _migrations_ok() -> tuple[bool, str]:
        return True, "current=0237_runner_audit_events"

    monkeypatch.setattr(health, "_check_migrations", _migrations_ok)
    metrics.reset_for_tests()

    response = await health._readyz_handler()
    body = _json_body(response)

    assert response.status_code == 200
    assert "remediation" not in body
