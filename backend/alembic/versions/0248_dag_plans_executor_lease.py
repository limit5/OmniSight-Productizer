"""OP-1654 [dag-executor] -- dag_plans executor-lease columns.

Revision ID: 0248
Revises: 0247
Create Date: 2026-05-24
backwards-compat: safe

Design doc section 7 B3 introduces a single-writer executor lease over
``dag_plans``: an executor "claims" a plan row, stamps a fencing token,
sets a claim expiry, and heartbeats while it holds the lease. This
migration only lays down the four nullable columns the lease will use --
there is NO consumer yet (a later OP-dag-executor ticket wires the
claim/heartbeat primitive that reads & writes them).

Columns (all nullable, additive -- existing rows stay untouched, an
unclaimed plan simply has NULL across all four):

* ``claim_owner``      -- identity (executor id) currently holding the
                          lease; NULL when unclaimed.
* ``claim_token``      -- opaque fencing token minted on claim so a
                          stale owner whose lease expired cannot keep
                          mutating the row after a re-claim.
* ``claim_expires_at`` -- epoch-seconds wall clock after which the lease
                          is reclaimable; REAL/Float to match the
                          existing ``created_at`` / ``updated_at``
                          convention on ``dag_plans``.
* ``heartbeat_at``     -- epoch-seconds of the owner's last heartbeat;
                          the reclaim path compares this against the
                          expiry to detect a dead owner.

No index is added here: the claim/reclaim query shape is owned by the
later consumer ticket, which will add the index it actually needs rather
than speculate now.

Idempotency: ``op.add_column`` is guarded by an existence probe so a
re-run (or a partially-applied upgrade) is a no-op rather than an error,
mirroring the 0238 ``api_keys.expires_at`` precedent.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0248"
down_revision = "0247"
branch_labels = None
depends_on = None


# (name, type) for the four additive nullable lease columns.
_LEASE_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("claim_owner", sa.Text()),
    ("claim_token", sa.Text()),
    ("claim_expires_at", sa.Float()),
    ("heartbeat_at", sa.Float()),
)


def _existing_columns() -> set[str]:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        rows = bind.exec_driver_sql(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'dag_plans'
            """
        ).fetchall()
        return {row[0] for row in rows}
    rows = bind.exec_driver_sql("PRAGMA table_info(dag_plans)").fetchall()
    return {row[1] for row in rows}


def upgrade() -> None:
    present = _existing_columns()
    for name, type_ in _LEASE_COLUMNS:
        if name not in present:
            op.add_column("dag_plans", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    present = _existing_columns()
    # Drop in reverse declaration order for symmetry with upgrade.
    for name, _type in reversed(_LEASE_COLUMNS):
        if name in present:
            op.drop_column("dag_plans", name)
