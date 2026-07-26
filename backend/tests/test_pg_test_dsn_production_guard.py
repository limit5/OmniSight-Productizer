"""OP-2733 / SP-5 — the suite must refuse to run against production Postgres.

On this host the production HA pair publishes to loopback: `omnisight-pg-primary`
on **5432 — the default PostgreSQL port** — and `omnisight-pg-standby` on 5433.
So `postgresql://omnisight@localhost/omnisight`, the most natural DSN anyone
would type, is production. ~110 test modules read `OMNI_TEST_PG_URL` (42 of them
directly from `os.environ`), and `pg_test_alembic_upgraded` runs
`alembic upgrade head` against whatever it finds. One wrong export is DDL and
fixture writes on the shared cluster, replicated to the standby.

These tests pin the guard, including the escape hatch, so a future refactor
cannot quietly drop it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_conftest_under_test", CONFTEST)
assert _spec and _spec.loader
_ct = importlib.util.module_from_spec(_spec)
sys.modules["_conftest_under_test"] = _ct
_spec.loader.exec_module(_ct)


@pytest.mark.parametrize(
    "dsn,expect",
    [
        # the production cluster by docker-network alias and by container name
        ("postgresql://u:p@pg-primary:5432/omnisight_test", "postgres-ha"),
        ("postgresql://u:p@pg-standby:5432/anything", "postgres-ha"),
        ("postgresql://u:p@omnisight-pg-primary:5432/x", "postgres-ha"),
        # the production database name, reached any way at all
        ("postgresql://u:p@somewhere-else:5999/omnisight", "production database"),
        # loopback on a port the production cluster publishes — the footgun
        ("postgresql://u:p@localhost:5432/omnisight_test", "loopback port"),
        ("postgresql://u:p@127.0.0.1:5433/whatever", "loopback port"),
        # ...including the implicit default port
        ("postgresql://u:p@localhost/omnisight_test", "loopback port"),
    ],
)
def test_production_dsns_are_rejected(dsn: str, expect: str) -> None:
    reason = _ct._production_dsn_reason(dsn)
    assert reason, f"should have been refused: {dsn}"
    assert expect in reason


@pytest.mark.parametrize(
    "dsn",
    [
        # what CI actually uses — must keep working
        "postgresql://omni_test:omni_test_pw@pg-test:5432/omnisight_staging",
        # throwaway containers on non-published ports
        "postgresql://u6test:u6test@localhost:5455/u6test",
        "postgresql://omni_test:pw@127.0.0.1:5434/omni_test",
        # SQLAlchemy-style prefixes still normalise and pass
        "postgresql+asyncpg://omni_test:pw@pg-test:5432/omnisight_staging",
        "",
    ],
)
def test_legitimate_dsns_are_allowed(dsn: str) -> None:
    assert _ct._production_dsn_reason(dsn) == "", f"false refusal: {dsn}"


def test_assert_raises_usage_error_and_names_the_escape_hatch(monkeypatch) -> None:
    monkeypatch.delenv(_ct._ALLOW_PROD_ENV, raising=False)
    with pytest.raises(pytest.UsageError) as exc:
        _ct._assert_test_dsn_not_production("postgresql://u:p@pg-primary:5432/omnisight")
    message = str(exc.value)
    assert "PRODUCTION" in message
    assert _ct._ALLOW_PROD_ENV in message, "the message must tell the operator the way out"


def test_escape_hatch_is_explicit_and_opt_in(monkeypatch) -> None:
    dsn = "postgresql://u:p@pg-primary:5432/omnisight"
    for value in ("1", "true", "yes"):
        monkeypatch.setenv(_ct._ALLOW_PROD_ENV, value)
        _ct._assert_test_dsn_not_production(dsn)  # must not raise
    for value in ("0", "", "no", "maybe"):
        monkeypatch.setenv(_ct._ALLOW_PROD_ENV, value)
        with pytest.raises(pytest.UsageError):
            _ct._assert_test_dsn_not_production(dsn)


def test_guard_runs_before_collection() -> None:
    """A fixture-level guard would miss the 42 modules that read os.environ
    directly; pytest_configure fires before collection, so it cannot be bypassed."""
    assert hasattr(_ct, "pytest_configure")


def test_malformed_dsn_does_not_crash_the_session() -> None:
    for junk in ("not a url", "://", "postgresql://", "   "):
        assert _ct._production_dsn_reason(junk) == ""
