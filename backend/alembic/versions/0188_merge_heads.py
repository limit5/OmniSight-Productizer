"""FX.9.4 -- Merge 4 alembic heads into a single head.

After the 2026-05-04 prod deploy attempt, ``alembic upgrade head`` failed
with ``MultipleHeads``: the version graph had branched into 4 unconverged
tips that grew independently across feature lines:

* ``0059`` -- ``web_sandbox_instances`` (provisioning branch tip)
* ``0106`` -- ``ks_envelope_tables`` (KS envelope / DSAR / compliance branch tip)
* ``0183`` -- ``ab_cost_guard`` (AB cost guard sub-branch tip)
* ``0187`` -- ``firewall_events`` (AB main / firewall branch tip)

These came from the natural fan-out of parallel feature work after
``0058`` (``users_auth_methods``) -- the divergence point where multiple
unrelated tracks each ran ``alembic revision`` against the then-current
single head.

This migration is a **pure no-op merge**: it carries no DDL, no data
movement, no env knob. It exists solely to give alembic a single
deterministic terminal node so ``upgrade head`` (singular) is well-defined
in production. ``upgrade heads`` (plural) was previously the only working
form, and the deploy script does not use that form.

Module-global / cross-worker state audit
----------------------------------------
No upgrade body. No state read or written. Workers converge by virtue of
each worker reading the same alembic ``version_num`` row from the shared
PG database. SOP Step 1 answer #1 (every worker derives the same value
from the same source).

Read-after-write timing audit
-----------------------------
No tables touched. No new writers introduced. There is no read-after-write
relationship to disturb.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* No new tables -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  drift guard is unaffected.
* SQLite dev parity -- merge nodes are alembic-internal and have no DDL,
  so ``backend.db._SCHEMA`` is not touched.
* Idempotent: ``upgrade`` and ``downgrade`` are both ``pass``. Re-running
  in either direction across the merge node is a no-op.

Future revisions should set ``down_revision = "0188"`` so the chain stays
linear after this point.
"""
from __future__ import annotations

# revision identifiers
revision = "0188"
down_revision = ("0059", "0106", "0183", "0187")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
