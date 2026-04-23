"""Q.2 device-fingerprint history (2026-04-24) — session_fingerprints table.

Adds ``session_fingerprints``: an append-only / upsert-merged history of
every ``(user_id, ua_hash, ip_subnet)`` tuple that has ever successfully
created a session. ``create_session()`` consults this table on each
issuance to decide whether the requesting device is "new" — i.e. the
tuple has not been seen within the past 30 days — and flags the
returned :class:`backend.auth.Session` with ``is_new_device=True`` so
the Q.2 downstream (email + SSE ``security.new_device_login`` alert)
can fire.

Why a dedicated table rather than scanning ``sessions`` +
``session_revocations``:

* ``sessions`` rows are deleted by ``cleanup_expired_sessions`` on cold
  boot (``backend.main`` lifespan) and by ``_get_session_impl`` when a
  token is probed after expiry. Rotated rows are shrunk to a 30 s
  grace window, so any session older than 30 s + cold-boot is gone.
  The "past 30 days" check would miss almost every prior login.
* ``session_revocations`` is keyed by token and only records *why* a
  peer was kicked (password change, TOTP enrolled, …). It has no ip
  or ua_hash columns — adding them would bloat a table that only
  exists to drive the 401 banner in Q.1.

So this is the canonical fingerprint store going forward. Retention
is bounded by a ~90 day GC (scheduled follow-up; until it lands the
table grows ~1 row per unique (user × UA × /24) forever, which is
small — ~ a few KB per active user).

Schema:
    (user_id, ua_hash, ip_subnet) PRIMARY KEY
    first_seen_at / last_seen_at — when the tuple was first / most-
        recently observed (UNIX epoch seconds, same clock as
        ``sessions.created_at``)
    session_count — how many sessions have been issued to this
        fingerprint; monotonically increments on every hit

Indexes:
    PK covers (user_id, ua_hash, ip_subnet) lookups (the hot path
        from ``_create_session_impl``).
    idx_session_fingerprints_last_seen — supports the future GC
        sweep (``DELETE WHERE last_seen_at < now - 90d``).

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS session_fingerprints (
                user_id         TEXT NOT NULL,
                ua_hash         TEXT NOT NULL DEFAULT '',
                ip_subnet       TEXT NOT NULL DEFAULT '',
                first_seen_at   DOUBLE PRECISION NOT NULL,
                last_seen_at    DOUBLE PRECISION NOT NULL,
                session_count   INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, ua_hash, ip_subnet)
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_session_fingerprints_last_seen "
            "ON session_fingerprints(last_seen_at)"
        )
    else:
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS session_fingerprints (
                user_id         TEXT NOT NULL,
                ua_hash         TEXT NOT NULL DEFAULT '',
                ip_subnet       TEXT NOT NULL DEFAULT '',
                first_seen_at   REAL NOT NULL,
                last_seen_at    REAL NOT NULL,
                session_count   INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, ua_hash, ip_subnet)
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_session_fingerprints_last_seen "
            "ON session_fingerprints(last_seen_at)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_fingerprints")
