"""clean fixture — candidate head, a direct descendant of the deployed head.

backwards-compat: safe
"""

# revision identifiers, used by Alembic.
revision = "0002child"
down_revision = "0001base"
branch_labels = None
depends_on = None


def upgrade():  # pragma: no cover - fixture stub (never executed; static parse only)
    pass


def downgrade():  # pragma: no cover - fixture stub
    pass
