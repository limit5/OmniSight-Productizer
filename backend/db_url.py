"""G4 #2 (HA-04) — ``DATABASE_URL`` connection abstraction.

Pure-Python URL parser and normalizer that lets the rest of the codebase
speak **one** syntax (``DATABASE_URL=postgresql+asyncpg://...`` or
``DATABASE_URL=sqlite:///path/to/foo.db``) and get back a single
:class:`DatabaseURL` record with:

* ``dialect``   — ``"sqlite"`` / ``"postgresql"``
* ``driver``    — ``"aiosqlite"`` / ``"asyncpg"`` / ``"psycopg2"``
* ``sqlalchemy_url()`` — Alembic-ready SQLAlchemy URL string
* ``asyncpg_dsn()``    — DSN to hand to ``asyncpg.connect()``
* ``sqlite_path()``    — filesystem path for ``aiosqlite.connect()``

The parser is pure-Python (``urllib.parse`` only), so importing this
module does **not** require ``asyncpg`` / ``aiosqlite`` / ``sqlalchemy``
to be installed. The actual driver is pulled in lazily by
:mod:`backend.db_connection` when a connection is opened.

This is deliberately a small, boring surface. It exists solely to give
the rest of the system a single vocabulary for "which database?" without
having to re-implement URL parsing in fifteen different call sites.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, quote, unquote, urlsplit

# ── Dialect / driver taxonomy ────────────────────────────────────────────

#: Canonical schemes we accept on input. Keys are the scheme (lowercased);
#: values are ``(dialect, driver)``. Anything else raises
#: :class:`UnsupportedURLError`.
_SCHEME_MAP: dict[str, tuple[str, str]] = {
    # SQLite
    "sqlite":              ("sqlite",     "aiosqlite"),
    "sqlite+aiosqlite":    ("sqlite",     "aiosqlite"),
    "sqlite+pysqlite":     ("sqlite",     "pysqlite"),
    # Postgres — async
    "postgresql+asyncpg":  ("postgresql", "asyncpg"),
    "postgres+asyncpg":    ("postgresql", "asyncpg"),
    "asyncpg":             ("postgresql", "asyncpg"),
    # Postgres — sync (psycopg2). Accepted so OMNI_TEST_PG_URL and
    # existing Alembic tooling keeps working through the same abstraction.
    "postgresql":          ("postgresql", "psycopg2"),
    "postgres":            ("postgresql", "psycopg2"),
    "postgresql+psycopg2": ("postgresql", "psycopg2"),
}


class UnsupportedURLError(ValueError):
    """Raised when a URL uses a scheme we don't support."""


class MalformedURLError(ValueError):
    """Raised when a URL is syntactically broken (missing host, etc.)."""


# ── Parsed URL record ────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatabaseURL:
    """Normalized connection target.

    All fields are already URL-decoded. Use :meth:`sqlalchemy_url` /
    :meth:`asyncpg_dsn` / :meth:`sqlite_path` when handing the value to
    a driver — they re-encode as needed.
    """

    scheme:   str                 # e.g. "postgresql+asyncpg"
    dialect:  str                 # "sqlite" | "postgresql"
    driver:   str                 # "aiosqlite" | "asyncpg" | "psycopg2" | "pysqlite"
    database: str = ""            # path (sqlite) or db name (pg); "" = default / ":memory:"
    host:     str = ""
    port:     int | None = None
    username: str = ""
    password: str = ""
    query:    Mapping[str, str] = field(default_factory=dict)

    # ── Convenience predicates ──
    @property
    def is_sqlite(self) -> bool:
        return self.dialect == "sqlite"

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgresql"

    @property
    def is_async_driver(self) -> bool:
        return self.driver in ("aiosqlite", "asyncpg")

    @property
    def is_memory_sqlite(self) -> bool:
        if not self.is_sqlite:
            return False
        # Match the SQLAlchemy convention: empty database OR ":memory:".
        return self.database in ("", ":memory:")

    # ── Format adapters ──
    def sqlalchemy_url(self, *, sync: bool | None = None) -> str:
        """Return a SQLAlchemy-ready URL string.

        ``sync`` controls whether we force a sync driver for tools that
        can't drive async engines (Alembic itself, dump scripts). Default
        (``None``) keeps whatever driver this URL was parsed as. When
        ``sync=True`` we coerce ``asyncpg`` → ``psycopg2`` and
        ``aiosqlite`` → ``pysqlite``; when ``sync=False`` we do the
        reverse coercion.
        """
        driver = self.driver
        if sync is True:
            driver = {"asyncpg": "psycopg2", "aiosqlite": "pysqlite"}.get(driver, driver)
        elif sync is False:
            driver = {"psycopg2": "asyncpg", "pysqlite": "aiosqlite"}.get(driver, driver)

        if self.dialect == "sqlite":
            scheme = "sqlite" if driver == "pysqlite" else f"sqlite+{driver}"
            # SQLite URLs use triple-slash for absolute paths and four
            # slashes for file paths that start with "/" — SQLAlchemy's
            # convention is `sqlite:///path` (relative) and
            # `sqlite:////abs/path` (absolute). We always pass through
            # the stored value verbatim.
            if self.is_memory_sqlite:
                return f"{scheme}:///:memory:"
            db = self.database
            if db.startswith("/"):
                return f"{scheme}:////{db.lstrip('/')}"
            return f"{scheme}:///{db}"

        # Postgres
        scheme = "postgresql" if driver == "psycopg2" else f"postgresql+{driver}"
        netloc = self._render_netloc()
        q = self._render_query()
        return f"{scheme}://{netloc}/{quote(self.database, safe='')}{q}"

    def asyncpg_dsn(self) -> str:
        """Return a DSN suitable for :func:`asyncpg.connect`.

        ``asyncpg`` expects the ``postgresql://`` or ``postgres://``
        form (no ``+asyncpg`` suffix). Callers should prefer passing the
        parsed fields directly (see :meth:`asyncpg_connect_kwargs`), but
        this is handy for tests and logging.
        """
        if not self.is_postgres:
            raise ValueError(
                f"asyncpg_dsn() is only valid for Postgres URLs; "
                f"dialect={self.dialect!r}"
            )
        netloc = self._render_netloc()
        q = self._render_query()
        return f"postgresql://{netloc}/{quote(self.database, safe='')}{q}"

    def asyncpg_connect_kwargs(self) -> dict[str, object]:
        """Return a kwargs dict for ``await asyncpg.connect(**kwargs)``.

        We prefer explicit kwargs over DSN string so credentials with
        unusual characters don't have to survive a second round-trip
        through a URL parser.
        """
        if not self.is_postgres:
            raise ValueError(
                f"asyncpg_connect_kwargs() only valid for Postgres; "
                f"dialect={self.dialect!r}"
            )
        kw: dict[str, object] = {}
        if self.host:
            kw["host"] = self.host
        if self.port is not None:
            kw["port"] = self.port
        if self.username:
            kw["user"] = self.username
        if self.password:
            kw["password"] = self.password
        if self.database:
            kw["database"] = self.database
        # Forward a safe subset of query params asyncpg understands.
        # Anything else is ignored rather than rejected — callers often
        # leak SQLAlchemy-specific options (e.g. ``sslmode``) and the
        # translation table is too finicky to enumerate here.
        for key in ("ssl", "server_settings", "statement_cache_size",
                    "connect_timeout", "command_timeout"):
            if key in self.query:
                kw[key] = self.query[key]
        return kw

    def sqlite_path(self) -> Path:
        """Return a :class:`pathlib.Path` for an on-disk SQLite database.

        Raises :class:`ValueError` for ``:memory:`` URLs — those should
        be handled by the caller via :attr:`is_memory_sqlite`.
        """
        if not self.is_sqlite:
            raise ValueError(
                f"sqlite_path() only valid for SQLite; dialect={self.dialect!r}"
            )
        if self.is_memory_sqlite:
            raise ValueError("sqlite_path() not applicable to :memory: URLs")
        return Path(self.database).expanduser()

    def redacted(self) -> str:
        """Return a URL suitable for logs (password replaced with ``***``)."""
        safe = self.sqlalchemy_url()
        if self.password:
            enc = quote(self.password, safe="")
            safe = safe.replace(f":{enc}@", ":***@")
        return safe

    # ── Internals ──
    def _render_netloc(self) -> str:
        host = self.host or "localhost"
        parts: list[str] = []
        if self.username or self.password:
            auth = quote(self.username, safe="")
            if self.password:
                auth += ":" + quote(self.password, safe="")
            parts.append(auth + "@")
        parts.append(host)
        if self.port is not None:
            parts.append(f":{self.port}")
        return "".join(parts)

    def _render_query(self) -> str:
        if not self.query:
            return ""
        from urllib.parse import urlencode
        return "?" + urlencode(self.query)


# ── Parser ──────────────────────────────────────────────────────────────


def parse(url: str) -> DatabaseURL:
    """Parse a DATABASE_URL string into a :class:`DatabaseURL`.

    Accepts any scheme listed in :data:`_SCHEME_MAP`. Both
    ``postgresql+asyncpg://u:p@h:5432/db`` and ``sqlite:///path/to.db``
    parse cleanly. SQLAlchemy's ``sqlite:////abs/path`` (four slashes,
    absolute path) is also accepted.
    """
    if not isinstance(url, str) or not url.strip():
        raise MalformedURLError("DATABASE_URL is empty")
    raw = url.strip()

    # urlsplit can't reliably parse custom schemes with a ``+`` in them
    # on older Pythons, but 3.9+ is fine. Split scheme manually first so
    # we control normalization.
    if "://" not in raw:
        raise MalformedURLError(f"DATABASE_URL missing '://' separator: {raw!r}")
    scheme, rest = raw.split("://", 1)
    scheme = scheme.lower()

    if scheme not in _SCHEME_MAP:
        raise UnsupportedURLError(
            f"Unsupported DATABASE_URL scheme {scheme!r}. "
            f"Supported: {', '.join(sorted(_SCHEME_MAP))}"
        )
    dialect, driver = _SCHEME_MAP[scheme]

    # ── SQLite branch: rest is a path (possibly empty for :memory:) ──
    if dialect == "sqlite":
        # Strip a single leading "/" per SQLAlchemy convention; an extra
        # one indicates an absolute filesystem path.
        if rest.startswith("/"):
            rest = rest[1:]
        # Query string support, so ``sqlite:///foo.db?mode=ro`` parses.
        if "?" in rest:
            path, _, qs = rest.partition("?")
            query = dict(parse_qsl(qs, keep_blank_values=True))
        else:
            path, query = rest, {}
        if path == "" or path == ":memory:":
            database = ""
        else:
            database = path
        return DatabaseURL(
            scheme=scheme,
            dialect=dialect,
            driver=driver,
            database=database,
            query=query,
        )

    # ── Postgres branch ──
    # Use urlsplit on a synthetic scheme to leverage the stdlib parser.
    # We pin the scheme to "pg" because urlsplit rejects unknown schemes
    # that contain "+" on some Pythons when they lack registration.
    parsed = urlsplit("pg://" + rest)
    host = parsed.hostname or ""
    port = parsed.port
    user = unquote(parsed.username) if parsed.username else ""
    pwd = unquote(parsed.password) if parsed.password else ""
    # Path is "/dbname" (or empty). Strip the leading slash.
    db = parsed.path.lstrip("/")
    # Re-decode ``%XX`` in the db name in case someone encoded slashes.
    if db:
        db = unquote(db)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if not host:
        raise MalformedURLError(
            f"Postgres DATABASE_URL missing host: {raw!r}"
        )

    return DatabaseURL(
        scheme=scheme,
        dialect=dialect,
        driver=driver,
        database=db,
        host=host,
        port=port,
        username=user,
        password=pwd,
        query=query,
    )


def resolve_from_env(
    env: Mapping[str, str] | None = None,
    *,
    default_sqlite_path: Path | str | None = None,
) -> DatabaseURL:
    """Resolve the effective DATABASE_URL from env vars + defaults.

    Precedence (highest first):

    1. ``OMNISIGHT_DATABASE_URL``  — full SQLAlchemy-style URL
    2. ``DATABASE_URL``            — 12-factor convention (accepted but
       emits no deprecation warning; we actively want operators to be
       able to use the standard env name)
    3. ``OMNISIGHT_DATABASE_PATH`` — legacy SQLite-only setting
    4. ``default_sqlite_path``     — caller-provided fallback
       (typically ``data/omnisight.db``)

    Returns a parsed :class:`DatabaseURL` in all cases; raises
    :class:`MalformedURLError` / :class:`UnsupportedURLError` for a
    broken input. Never silently falls back on bad input — we'd rather
    fail fast than silently downgrade a misconfigured production to
    a temp SQLite file.
    """
    env = os.environ if env is None else env

    for key in ("OMNISIGHT_DATABASE_URL", "DATABASE_URL"):
        url = (env.get(key) or "").strip()
        if url:
            return parse(url)

    sqlite_path = (env.get("OMNISIGHT_DATABASE_PATH") or "").strip()
    if sqlite_path:
        return _sqlite_file(sqlite_path)

    if default_sqlite_path is not None:
        return _sqlite_file(str(default_sqlite_path))

    # No default supplied → caller wants us to signal "no config".
    raise MalformedURLError(
        "No database URL configured (set OMNISIGHT_DATABASE_URL, "
        "DATABASE_URL, or OMNISIGHT_DATABASE_PATH)"
    )


def _sqlite_file(path: str) -> DatabaseURL:
    """Build a DatabaseURL for an on-disk SQLite file path."""
    return DatabaseURL(
        scheme="sqlite",
        dialect="sqlite",
        driver="aiosqlite",
        database=path,
    )


# ── Public helpers re-exported for call sites ──────────────────────────

__all__ = [
    "DatabaseURL",
    "MalformedURLError",
    "UnsupportedURLError",
    "parse",
    "resolve_from_env",
]
