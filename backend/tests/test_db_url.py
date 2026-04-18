"""G4 #2 (HA-04) contract tests — DATABASE_URL connection abstraction.

Covers :mod:`backend.db_url` (pure parser) and :mod:`backend.db_connection`
(async driver dispatcher). The Postgres path is exercised with asyncio
mocks so the test pass on a plain sqlite CI box; a live Postgres
integration test lives in ``test_db_postgres_live.py`` and is gated on
``OMNI_TEST_PG_URL`` the same way the Alembic live-upgrade test is.

Sections:

*  1. Scheme acceptance / rejection matrix
*  2. Postgres URL parsing (host / port / user / pw / db / query)
*  3. SQLite URL parsing (relative / absolute / memory / query)
*  4. Round-trip: sqlalchemy_url() / asyncpg_dsn() / asyncpg_connect_kwargs()
*  5. Redaction
*  6. resolve_from_env() precedence
*  7. Config wiring (settings.database_url field)
*  8. Alembic env.py respects OMNISIGHT_DATABASE_URL
*  9. db_connection factory dispatch + SQLite end-to-end
* 10. Placeholder translation (? → $N) — string-literal aware
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from backend.db_url import (
    DatabaseURL,
    MalformedURLError,
    UnsupportedURLError,
    parse,
    resolve_from_env,
)


# ───────────────────────────────────────────────────────────────────────
# Section 1: Scheme acceptance / rejection matrix
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expect_dialect,expect_driver",
    [
        ("sqlite:///data/omnisight.db",              "sqlite",     "aiosqlite"),
        ("sqlite+aiosqlite:///data/omnisight.db",    "sqlite",     "aiosqlite"),
        ("sqlite+pysqlite:///data/omnisight.db",     "sqlite",     "pysqlite"),
        ("postgresql+asyncpg://u:p@h:5432/db",       "postgresql", "asyncpg"),
        ("postgres+asyncpg://u:p@h:5432/db",         "postgresql", "asyncpg"),
        ("asyncpg://u:p@h:5432/db",                  "postgresql", "asyncpg"),
        ("postgresql://u:p@h:5432/db",               "postgresql", "psycopg2"),
        ("postgres://u:p@h:5432/db",                 "postgresql", "psycopg2"),
        ("postgresql+psycopg2://u:p@h:5432/db",      "postgresql", "psycopg2"),
        # Case-insensitive scheme
        ("POSTGRESQL+ASYNCPG://u:p@h:5432/db",       "postgresql", "asyncpg"),
        ("SQLite:///foo.db",                         "sqlite",     "aiosqlite"),
    ],
)
def test_accepted_schemes(url: str, expect_dialect: str, expect_driver: str) -> None:
    r = parse(url)
    assert r.dialect == expect_dialect
    assert r.driver == expect_driver


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@h/db",
        "mongodb://h:27017",
        "oracle+cx_oracle://u:p@h/db",
        "redis://localhost:6379/0",
        "postgresql+psycopg3://u:p@h/db",  # psycopg3 not yet supported
    ],
)
def test_unsupported_scheme_rejected(url: str) -> None:
    with pytest.raises(UnsupportedURLError):
        parse(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not-a-url",
        "postgresql+asyncpg",          # no "://"
        "postgresql+asyncpg://",       # no host
        "postgresql+asyncpg:///db",    # no host
    ],
)
def test_malformed_urls_rejected(url: str) -> None:
    with pytest.raises((MalformedURLError, UnsupportedURLError)):
        parse(url)


def test_non_string_rejected() -> None:
    with pytest.raises(MalformedURLError):
        parse(None)  # type: ignore[arg-type]


# ───────────────────────────────────────────────────────────────────────
# Section 2: Postgres URL parsing
# ───────────────────────────────────────────────────────────────────────


def test_postgres_full_form() -> None:
    r = parse("postgresql+asyncpg://alice:s3cr3t@db.internal:5433/omnisight")
    assert r.dialect == "postgresql"
    assert r.driver == "asyncpg"
    assert r.host == "db.internal"
    assert r.port == 5433
    assert r.username == "alice"
    assert r.password == "s3cr3t"
    assert r.database == "omnisight"
    assert r.query == {}


def test_postgres_no_port() -> None:
    r = parse("postgresql+asyncpg://alice:s3cr3t@db.internal/omnisight")
    assert r.port is None
    assert r.host == "db.internal"


def test_postgres_no_credentials() -> None:
    r = parse("postgresql+asyncpg://db.internal:5432/omnisight")
    assert r.username == ""
    assert r.password == ""
    assert r.host == "db.internal"


def test_postgres_query_params() -> None:
    r = parse(
        "postgresql+asyncpg://u:p@h:5432/db"
        "?sslmode=require&connect_timeout=10"
    )
    assert r.query["sslmode"] == "require"
    assert r.query["connect_timeout"] == "10"


def test_postgres_url_decodes_credentials() -> None:
    # Password contains URL-reserved characters
    r = parse("postgresql+asyncpg://u%40corp:p%40ss%21@h:5432/db")
    assert r.username == "u@corp"
    assert r.password == "p@ss!"


def test_postgres_url_no_db_is_empty() -> None:
    r = parse("postgresql+asyncpg://u:p@h:5432/")
    assert r.database == ""


def test_postgres_ipv6_host() -> None:
    r = parse("postgresql+asyncpg://u:p@[::1]:5432/db")
    assert r.host == "::1"
    assert r.port == 5432


# ───────────────────────────────────────────────────────────────────────
# Section 3: SQLite URL parsing
# ───────────────────────────────────────────────────────────────────────


def test_sqlite_relative_path() -> None:
    r = parse("sqlite:///data/omnisight.db")
    assert r.dialect == "sqlite"
    assert r.driver == "aiosqlite"
    assert r.database == "data/omnisight.db"
    assert r.host == ""
    assert r.port is None
    assert not r.is_memory_sqlite


def test_sqlite_absolute_path() -> None:
    r = parse("sqlite:////abs/path/omnisight.db")
    assert r.database == "/abs/path/omnisight.db"
    assert not r.is_memory_sqlite


def test_sqlite_memory() -> None:
    r = parse("sqlite:///:memory:")
    assert r.is_memory_sqlite
    assert r.database == ""


def test_sqlite_memory_empty_path() -> None:
    r = parse("sqlite:///")
    assert r.is_memory_sqlite


def test_sqlite_query_params() -> None:
    r = parse("sqlite:///foo.db?mode=ro&cache=shared")
    assert r.database == "foo.db"
    assert r.query == {"mode": "ro", "cache": "shared"}


def test_sqlite_path_helper() -> None:
    r = parse("sqlite:///data/omnisight.db")
    assert r.sqlite_path() == Path("data/omnisight.db")


def test_sqlite_path_rejects_memory() -> None:
    with pytest.raises(ValueError):
        parse("sqlite:///:memory:").sqlite_path()


def test_sqlite_path_rejects_pg() -> None:
    with pytest.raises(ValueError):
        parse("postgresql+asyncpg://h/db").sqlite_path()


# ───────────────────────────────────────────────────────────────────────
# Section 4: Format adapters round-trip
# ───────────────────────────────────────────────────────────────────────


def test_sqlalchemy_url_roundtrip_sqlite() -> None:
    r = parse("sqlite:///data/omnisight.db")
    assert r.sqlalchemy_url() == "sqlite+aiosqlite:///data/omnisight.db"
    assert r.sqlalchemy_url(sync=True) == "sqlite:///data/omnisight.db"


def test_sqlalchemy_url_roundtrip_sqlite_absolute() -> None:
    r = parse("sqlite:////abs/path/db")
    # The stored database path is "/abs/path/db" (with leading slash).
    # SQLAlchemy renders absolute sqlite with four slashes.
    assert r.sqlalchemy_url().startswith("sqlite+aiosqlite:////")


def test_sqlalchemy_url_roundtrip_sqlite_memory() -> None:
    r = parse("sqlite:///:memory:")
    assert r.sqlalchemy_url() == "sqlite+aiosqlite:///:memory:"
    assert r.sqlalchemy_url(sync=True) == "sqlite:///:memory:"


def test_sqlalchemy_url_roundtrip_postgres_async() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db"
    r = parse(url)
    # Default: keep asyncpg driver (it was parsed as async)
    assert r.sqlalchemy_url() == url
    # sync=True coerces asyncpg → psycopg2; SQLAlchemy's canonical form
    # for psycopg2 is the bare `postgresql://` scheme (no `+psycopg2`).
    assert r.sqlalchemy_url(sync=True) == "postgresql://u:p@h:5432/db"


def test_sqlalchemy_url_coerce_sync_to_async() -> None:
    r = parse("postgresql://u:p@h:5432/db")  # psycopg2
    assert r.sqlalchemy_url(sync=False) == "postgresql+asyncpg://u:p@h:5432/db"


def test_asyncpg_dsn_shape() -> None:
    r = parse("postgresql+asyncpg://u:p@h:5432/db?sslmode=require")
    dsn = r.asyncpg_dsn()
    assert dsn.startswith("postgresql://")
    assert "u:p@h:5432/db" in dsn
    assert "sslmode=require" in dsn


def test_asyncpg_dsn_rejects_sqlite() -> None:
    with pytest.raises(ValueError):
        parse("sqlite:///foo.db").asyncpg_dsn()


def test_asyncpg_connect_kwargs_shape() -> None:
    r = parse("postgresql+asyncpg://alice:secret@db.internal:5433/omnisight")
    kw = r.asyncpg_connect_kwargs()
    assert kw["host"] == "db.internal"
    assert kw["port"] == 5433
    assert kw["user"] == "alice"
    assert kw["password"] == "secret"
    assert kw["database"] == "omnisight"


def test_asyncpg_connect_kwargs_minimal() -> None:
    # Unix-socket-style: host only, no user/pw (asyncpg will pick up
    # $PGUSER/$PGPASSWORD then). Minimal form must still produce
    # something asyncpg.connect() accepts.
    r = parse("postgresql+asyncpg://localhost/omnisight")
    kw = r.asyncpg_connect_kwargs()
    assert kw["host"] == "localhost"
    assert kw["database"] == "omnisight"
    assert "user" not in kw
    assert "password" not in kw


def test_asyncpg_connect_kwargs_forwards_safe_query() -> None:
    r = parse(
        "postgresql+asyncpg://u:p@h/db"
        "?ssl=require&connect_timeout=5&unknown_param=x"
    )
    kw = r.asyncpg_connect_kwargs()
    assert kw["ssl"] == "require"
    assert kw["connect_timeout"] == "5"
    # Unknown params are silently dropped (not rejected).
    assert "unknown_param" not in kw


def test_asyncpg_connect_kwargs_rejects_sqlite() -> None:
    with pytest.raises(ValueError):
        parse("sqlite:///foo.db").asyncpg_connect_kwargs()


# ───────────────────────────────────────────────────────────────────────
# Section 5: Redaction
# ───────────────────────────────────────────────────────────────────────


def test_redacted_hides_password() -> None:
    r = parse("postgresql+asyncpg://alice:supers3cret@h:5432/db")
    safe = r.redacted()
    assert "supers3cret" not in safe
    assert "***" in safe
    assert "alice" in safe
    assert "h:5432" in safe


def test_redacted_ok_when_no_password() -> None:
    r = parse("postgresql+asyncpg://alice@h:5432/db")
    # Should not raise; format is just a URL without a colon+password.
    assert "alice" in r.redacted()


def test_redacted_sqlite_no_secret() -> None:
    r = parse("sqlite:///data/omnisight.db")
    # No secrets to redact, redacted() should equal sqlalchemy_url().
    assert r.redacted() == r.sqlalchemy_url()


# ───────────────────────────────────────────────────────────────────────
# Section 6: resolve_from_env precedence
# ───────────────────────────────────────────────────────────────────────


def test_resolve_prefers_omnisight_database_url() -> None:
    env = {
        "OMNISIGHT_DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
        "DATABASE_URL":            "sqlite:///should-be-ignored.db",
        "OMNISIGHT_DATABASE_PATH": "/should-be-ignored.db",
    }
    r = resolve_from_env(env)
    assert r.is_postgres
    assert r.database == "db"


def test_resolve_falls_back_to_database_url() -> None:
    env = {
        "DATABASE_URL":            "postgresql+asyncpg://u:p@h/db",
        "OMNISIGHT_DATABASE_PATH": "/should-be-ignored.db",
    }
    r = resolve_from_env(env)
    assert r.is_postgres


def test_resolve_falls_back_to_legacy_database_path() -> None:
    env = {"OMNISIGHT_DATABASE_PATH": "/tmp/legacy.db"}
    r = resolve_from_env(env)
    assert r.is_sqlite
    assert r.database == "/tmp/legacy.db"


def test_resolve_uses_default_when_nothing_set() -> None:
    env: dict[str, str] = {}
    r = resolve_from_env(env, default_sqlite_path="/var/lib/omnisight.db")
    assert r.is_sqlite
    assert r.database == "/var/lib/omnisight.db"


def test_resolve_raises_when_nothing_set_and_no_default() -> None:
    env: dict[str, str] = {}
    with pytest.raises(MalformedURLError):
        resolve_from_env(env)


def test_resolve_treats_whitespace_as_empty() -> None:
    env = {
        "OMNISIGHT_DATABASE_URL": "   ",
        "DATABASE_URL":           "   ",
        "OMNISIGHT_DATABASE_PATH": "/fallback.db",
    }
    r = resolve_from_env(env)
    assert r.is_sqlite
    assert r.database == "/fallback.db"


def test_resolve_uses_real_process_env_when_none_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNISIGHT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", "/from-process-env.db")
    r = resolve_from_env()
    assert r.is_sqlite
    assert r.database == "/from-process-env.db"


# ───────────────────────────────────────────────────────────────────────
# Section 7: Config wiring
# ───────────────────────────────────────────────────────────────────────


def test_settings_exposes_database_url_field() -> None:
    from backend.config import settings
    assert hasattr(settings, "database_url")
    # Default is empty string (legacy sqlite path still wins).
    assert isinstance(settings.database_url, str)


def test_settings_database_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    from backend.config import Settings
    s = Settings()
    assert s.database_url == "postgresql+asyncpg://u:p@h/db"


# ───────────────────────────────────────────────────────────────────────
# Section 8: Alembic env.py respects OMNISIGHT_DATABASE_URL
# ───────────────────────────────────────────────────────────────────────


def test_alembic_env_source_imports_db_url_parse() -> None:
    env_path = (
        Path(__file__).resolve().parents[1] / "alembic" / "env.py"
    )
    src = env_path.read_text(encoding="utf-8")
    # The coercion that lets an async URL drive sync Alembic:
    assert "OMNISIGHT_DATABASE_URL" in src
    assert "DATABASE_URL" in src
    assert "sqlalchemy_url(sync=True)" in src
    assert "from backend.db_url import parse" in src


# ───────────────────────────────────────────────────────────────────────
# Section 9: db_connection factory dispatch + SQLite end-to-end
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_connection_dispatches_sqlite(tmp_path: Path) -> None:
    from backend.db_connection import AsyncDBConnection, open_connection

    url = f"sqlite:///{tmp_path / 'test.db'}"
    conn = await open_connection(url)
    try:
        assert isinstance(conn, AsyncDBConnection)
        assert conn.dialect == "sqlite"
        assert conn.driver == "aiosqlite"
        # Round-trip an insert through the abstraction.
        await conn.executescript(
            "CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);"
        )
        await conn.execute("INSERT INTO kv VALUES (?, ?)", ("hello", "world"))
        await conn.commit()
        row = await conn.fetchone("SELECT v FROM kv WHERE k = ?", ("hello",))
        assert row is not None
        assert row[0] == "world"
        rows = await conn.fetchall("SELECT k, v FROM kv ORDER BY k")
        assert len(rows) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_open_connection_accepts_databaseurl_object(tmp_path: Path) -> None:
    from backend.db_connection import open_connection

    du = parse(f"sqlite:///{tmp_path / 'test2.db'}")
    conn = await open_connection(du)
    try:
        assert conn.dialect == "sqlite"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_open_connection_resolves_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", f"sqlite:///{tmp_path / 'env.db'}")
    from backend.db_connection import open_connection

    conn = await open_connection()
    try:
        assert conn.dialect == "sqlite"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_open_connection_rejects_psycopg2_driver() -> None:
    from backend.db_connection import open_connection

    # psycopg2 is a sync driver; runtime must refuse it and tell caller
    # to use postgresql+asyncpg:// instead.
    with pytest.raises(RuntimeError, match="asyncpg"):
        await open_connection("postgresql+psycopg2://u:p@h:5432/db")


@pytest.mark.asyncio
async def test_open_connection_postgres_imports_asyncpg_lazily() -> None:
    """Opening a Postgres URL should import asyncpg. Verify the dispatch
    branch reaches the asyncpg import site by stubbing it.
    """
    from backend import db_connection

    captured: dict[str, object] = {}

    async def _fake_connect(**kwargs):  # type: ignore[no-untyped-def]
        captured["kwargs"] = kwargs

        class _FakeConn:
            async def execute(self, *a, **k):  # noqa: D401
                return "OK"

            async def close(self):
                captured["closed"] = True

            def transaction(self):
                class _Tx:
                    async def start(self_inner):  # noqa: D401
                        captured["tx_started"] = True

                    async def commit(self_inner):
                        captured["tx_committed"] = True

                return _Tx()

        return _FakeConn()

    fake_module = mock.MagicMock()
    fake_module.connect = _fake_connect

    with mock.patch.dict("sys.modules", {"asyncpg": fake_module}):
        conn = await db_connection.open_connection(
            "postgresql+asyncpg://u:p@h:5432/db"
        )
        assert conn.dialect == "postgresql"
        assert conn.driver == "asyncpg"
        assert captured["kwargs"]["host"] == "h"
        assert captured["kwargs"]["port"] == 5432
        assert captured["kwargs"]["database"] == "db"
        assert captured["kwargs"]["user"] == "u"
        assert captured["kwargs"]["password"] == "p"
        # execute() starts a tx lazily
        await conn.execute("SELECT 1")
        assert captured.get("tx_started") is True
        # commit() closes the tx
        await conn.commit()
        assert captured.get("tx_committed") is True
        await conn.close()
        assert captured.get("closed") is True


# ───────────────────────────────────────────────────────────────────────
# Section 10: Placeholder translation
# ───────────────────────────────────────────────────────────────────────


def _translate(sql: str) -> str:
    from backend.db_connection import _PostgresAsyncConnection
    return _PostgresAsyncConnection._qmark_to_dollar(sql)


def test_qmark_translation_simple() -> None:
    assert _translate("SELECT * FROM t WHERE id = ?") == \
        "SELECT * FROM t WHERE id = $1"


def test_qmark_translation_multi() -> None:
    assert _translate("INSERT INTO t (a, b, c) VALUES (?, ?, ?)") == \
        "INSERT INTO t (a, b, c) VALUES ($1, $2, $3)"


def test_qmark_translation_preserves_literal_question_in_string() -> None:
    assert _translate("SELECT 'What?' FROM t WHERE id = ?") == \
        "SELECT 'What?' FROM t WHERE id = $1"


def test_qmark_translation_preserves_doubled_quote_escape() -> None:
    # 'it''s' is a single SQL string containing "it's". The ? outside
    # the string still gets translated.
    assert _translate("SELECT 'it''s ok?' WHERE id = ?") == \
        "SELECT 'it''s ok?' WHERE id = $1"


def test_qmark_translation_no_qmarks_is_pass_through() -> None:
    sql = "SELECT 1"
    assert _translate(sql) == sql


def test_qmark_translation_many() -> None:
    sql = "INSERT INTO t VALUES " + ", ".join(["(?)"] * 5)
    translated = _translate(sql)
    for i in range(1, 6):
        assert f"${i}" in translated
    assert "?" not in translated


# ───────────────────────────────────────────────────────────────────────
# Section 11: DatabaseURL is immutable (frozen dataclass)
# ───────────────────────────────────────────────────────────────────────


def test_database_url_is_frozen() -> None:
    r = parse("postgresql+asyncpg://u:p@h:5432/db")
    with pytest.raises(Exception):
        r.database = "other"  # type: ignore[misc]


def test_database_url_construction_defaults() -> None:
    # The dataclass is constructible for tests without a parser round-trip.
    d = DatabaseURL(
        scheme="sqlite", dialect="sqlite", driver="aiosqlite",
        database="foo.db",
    )
    assert d.is_sqlite
    assert not d.is_postgres
    assert d.is_async_driver
    assert d.host == ""
    assert d.port is None
    assert d.query == {}
