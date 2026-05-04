"""Alembic env — Phase 51.

OmniSight uses raw aiosqlite (no SQLAlchemy ORM models), so we run
Alembic in offline-friendly mode where each migration carries plain
SQL `op.execute(...)` statements. The `target_metadata = None` line
disables autogenerate; new tables/columns are added by hand-written
migration files.

The DB URL respects `OMNISIGHT_DATABASE_PATH` env so CI runs
against a per-shard temp file and dev runs against
`data/omnisight.db`.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_db_url() -> str:
    # N8: SQLALCHEMY_URL wins so the dual-track validator can point
    # Alembic at a Postgres service container.
    # G4 #2 (HA-04): OMNISIGHT_DATABASE_URL / DATABASE_URL are next —
    # this is the new connection-abstraction env that the runtime also
    # reads. Alembic itself cannot drive asyncpg (sync engine only), so
    # we coerce `postgresql+asyncpg://` → `postgresql+psycopg2://` here.
    # Falls back to the legacy OMNISIGHT_DATABASE_PATH → sqlite:// path
    # so SQLite-only callers (dev, existing CI jobs) are untouched.
    full = os.environ.get("SQLALCHEMY_URL", "").strip()
    if full:
        return full
    for key in ("OMNISIGHT_DATABASE_URL", "DATABASE_URL"):
        url = os.environ.get(key, "").strip()
        if url:
            try:
                # Lazy import — keeps env.py usable even if the backend
                # package is not on sys.path (e.g. a minimal alembic tool
                # invocation).
                import sys
                root = Path(__file__).resolve().parents[2]
                if str(root) not in sys.path:
                    sys.path.insert(0, str(root))
                from backend.db_url import parse  # type: ignore
            except Exception:  # pragma: no cover — defensive
                return url
            parsed = parse(url)
            return parsed.sqlalchemy_url(sync=True)
    env = os.environ.get("OMNISIGHT_DATABASE_PATH", "").strip()
    if env:
        return f"sqlite:///{env}"
    here = Path(__file__).resolve().parents[2]
    return f"sqlite:///{here / 'data' / 'omnisight.db'}"


target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_db_url()
    # Only pre-create a parent directory when the URL is SQLite;
    # Postgres/MySQL URLs point at a network server, not a file path.
    if url.startswith("sqlite:///"):
        Path(url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as conn:
        # G4 #1: install the SQLite→Postgres compatibility shim so
        # existing migrations (written against SQLite) run cleanly on
        # Postgres. No-op for SQLite binds.
        # FX.9.2: absolute `backend.` import — keeps `/app` on sys.path
        # rather than `/app/backend`. (Pre-FX.9.3 this also defended
        # against `backend/platform.py` shadowing stdlib `platform`;
        # FX.9.3 renamed the module to `backend/platform_profile.py`
        # so the shadow is gone, but the absolute-import discipline
        # is kept as defence-in-depth — bare `from alembic_pg_compat
        # import …` would still require `/app/backend` on sys.path[0]
        # which we no longer want at all.)
        import sys
        root = Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from backend.alembic_pg_compat import install_pg_compat  # type: ignore

        install_pg_compat(conn)
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            render_as_batch=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
