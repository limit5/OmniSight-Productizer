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
    db_path = _resolve_db_url().replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(_resolve_db_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
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
