"""Phase 54 — Users / Sessions / GitHub App installations.

Adds three tables:

  users                  authenticated identities + role
  sessions               cookie-backed session tokens (server side)
  github_installations   per-installation tokens for the GitHub App
                         (Open Agents borrow #3)

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-14
"""
from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'viewer',
    password_hash   TEXT NOT NULL DEFAULT '',
    oidc_provider   TEXT NOT NULL DEFAULT '',
    oidc_subject    TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_oidc ON users(oidc_provider, oidc_subject);

CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    csrf_token      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    ip              TEXT NOT NULL DEFAULT '',
    user_agent      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS github_installations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    installation_id     INTEGER NOT NULL UNIQUE,
    account_login       TEXT NOT NULL,
    account_type        TEXT NOT NULL DEFAULT 'User',
    target_type         TEXT NOT NULL DEFAULT 'Repository',
    repos_json          TEXT NOT NULL DEFAULT '[]',
    permissions_json    TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    suspended_at        TEXT
);
"""


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in [s.strip() for s in _SQL.split(";") if s.strip()]:
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in [
        "DROP INDEX IF EXISTS idx_sessions_expiry",
        "DROP INDEX IF EXISTS idx_sessions_user",
        "DROP INDEX IF EXISTS idx_users_oidc",
        "DROP INDEX IF EXISTS idx_users_role",
        "DROP TABLE IF EXISTS github_installations",
        "DROP TABLE IF EXISTS sessions",
        "DROP TABLE IF EXISTS users",
    ]:
        bind.exec_driver_sql(stmt)
