"""flagged fixture — candidate head on the divergent branch.

Its ancestry is {0009divergent, 0008other}; the deployed head 0001base is NOT
in it, so the candidate head is not descendant-reachable from the deployed head.

backwards-compat: safe
"""

# revision identifiers, used by Alembic.
revision = "0009divergent"
down_revision = "0008other"
branch_labels = None
depends_on = None


def upgrade():  # pragma: no cover - fixture stub (never executed; static parse only)
    pass


def downgrade():  # pragma: no cover - fixture stub
    pass
