"""KS.2/KS.3 + FX.11 merge -- consolidate two heads after codex-work merge.

After merging codex-work's KS.2 (CMEK) + KS.3 (BYOG Proxy) commits into
master, the alembic version graph re-branched at ``0106``:

* Master chain:  ``0106 -> 0188 (merge_heads) -> 0189 (sessions.token envelope)``
* Codex chain:   ``0106 -> 0107 (cmek_tables) -> 0108 (byog_proxy_tables)``

This is the same fan-out problem FX.9.4 (``0188_merge_heads``) solved -- the
KS.2.11 (``0107``) + KS.3.12 (``0108``) migrations were authored on
codex-work before FX.9.4 landed, so they could not yet chain off ``0188``.

This migration is a **pure no-op merge** that gives alembic a single
deterministic terminal node again. No DDL, no data movement, no env knob.

Module-global / cross-worker state audit
----------------------------------------
No upgrade body. No state read or written. Workers converge by virtue of
each worker reading the same alembic ``version_num`` row.

Read-after-write timing audit
-----------------------------
No tables touched. No new writers introduced.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* No new tables -- migrator drift guard is unaffected.
* Idempotent: ``upgrade`` and ``downgrade`` are both ``pass``.

Future revisions should set ``down_revision = "0190"``.
"""
from __future__ import annotations

# revision identifiers
revision = "0190"
down_revision = ("0189", "0108")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
