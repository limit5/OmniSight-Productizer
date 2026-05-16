"""merge 3 heads — 2026-05-16 ops cleanup (OP-1164)

Collapses develop's 3-head state into a single head. The previous merge
``m_audit_29_final`` left two unmerged sibling branches:

* ``0203_conflict_observations`` (OP-746) — never re-merged after the
  duplicate-revision cleanup that produced ``0203a_kse_*``.
* ``0237_runner_audit_events`` (OP-1118) — runner_audit_events table;
  introduced after ``m_audit_29_final`` shipped.

Plus ``m_audit_29_final`` itself (already a merge node of
``0203a_kse``, ``0207``, ``m_0221_children``, ``m_0226_children``).

Background: the 3-head state was blocking OP-1159
(``v2-⑤-Dockerfile-Manifest``) because the AC's build-time
``alembic heads`` invariant requires exactly one head. Per v2-⑥-1a
§3.0.6 subcontract #2 this WILL be enforced at image-build time once
Family ⑥ ships; for now the gap is fixed by hand here.

This is a pure merge node — no schema change. Both ``upgrade()`` and
``downgrade()`` are no-ops by design.

Revision ID: m_2026_05_16_3head
Revises: 0203, 0237, m_audit_29_final
Create Date: 2026-05-16
backwards-compat: safe
"""
from __future__ import annotations


# revision identifiers used by Alembic
revision = "m_2026_05_16_3head"
down_revision = ("0203", "0237", "m_audit_29_final")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Pure merge node — no schema change."""


def downgrade() -> None:
    """Pure merge node — no schema change to reverse."""
