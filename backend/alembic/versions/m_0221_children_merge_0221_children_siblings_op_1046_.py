"""merge 0221 children siblings (OP-1046 audit-29)

Revision ID: m_0221_children
Revises: 0223, 0224, 0225
Create Date: 2026-05-13 11:40:16.682409
backwards-compat: <safe|breaking|deprecation-window-1of2|deprecation-window-2of2>

"""
from __future__ import annotations

from alembic import op

# revision identifiers
revision = 'm_0221_children'
down_revision = ('0223', '0224', '0225')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
