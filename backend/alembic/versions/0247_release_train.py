"""OP-1585 [RT-10a] -- ``release_train`` promotion-lifecycle table.

backwards-compat: safe (additive, no FK out, no data backfill)

The release-train promote path (design doc
``docs/design/2026-05-21-release-train-stories.md`` RT-10a) needs a
durable, CAS-protected record of the *promotion* lifecycle of a chosen
candidate SHA: which backend+frontend image digests it was built from,
the version reserved for it, and a single-writer promotion state that
two concurrent promote attempts race for. The companion
``backend.agents.release_train`` module owns the model + the CAS /
audit-hard-gate promote primitive that writes here.

Why a dedicated table and not a column on ``release_state``
==========================================================
``release_state`` (alembic 0233) is the per-``RELEASE-vX.Y.Z`` cached
projection of the *conductor* state machine (pending -> building ->
canary_* -> done) keyed on the JIRA META version. ``release_train``
tracks a different decision domain: the immutable identity of a
*candidate build* (its commit SHA + the exact backend/frontend digests
it produced) and the one-shot promote that retags those digests to a
reserved version. The two are temporally adjacent (a promoted train
feeds a release_state row) but have disjoint keys, lifecycles, and
write cadences -- a promote happens once per candidate, a conductor
transition many times per release. RT-10a's spec is explicit:
"Separate from release_state."

Schema rationale
----------------
* ``candidate_sha`` -- the 40-char lowercase hex commit SHA the
  candidate was built from. ``UNIQUE`` so a candidate has exactly one
  train row; a duplicate-instantiation slip fails the INSERT loudly
  rather than forking two promote lifecycles for the same SHA. The
  ``length(...) = 40`` CHECK is the portable DB-side backstop (sqlite
  has no regex operator, so hex enforcement lives in the app layer,
  ``backend.agents.release_train``); mirrors the 0246 green_evidence
  convention.
* ``source_digest_backend`` / ``source_digest_frontend`` -- the
  ``sha256:<hex>`` image digests the candidate build produced (RT-21:
  the train tracks the backend+frontend *pair*). NOT NULL: a train row
  without both source digests could not be promoted (there would be
  nothing to retag), so the not-null is a write-time guard rather than
  a nullable-then-backfill column.
* ``reserved_version`` -- the ``vX.Y.Z`` reserved for this candidate.
  Nullable: the reservation *flow* is RT-10b (out of scope here); this
  migration only owns the column the flow will populate.
* ``promotion_state`` -- closed enum enforced by CHECK (a typo in the
  promote primitive fails the constraint, not the wire format):
  ``pending`` (created, not yet promoted) / ``promoting`` (a single
  caller won the CAS and holds the in-flight promote) / ``promoted``
  (audit row written + tag write done) / ``failed`` (audit gate or tag
  write aborted; terminal-but-retryable by operator). The CAS that
  flips ``pending`` -> ``promoting`` is the single-winner gate
  (RT-10a AC: 2 concurrent promotes -> exactly one wins).
* ``actor`` -- the identity that initiated the winning promote; written
  by the CAS-acquire so the audit trail attributes the promote.
* ``row_version`` -- optimistic-locking counter, mirrors the
  ``release_state`` precedent. The promote primitive bumps it on every
  state transition with ``WHERE ... AND promotion_state = :from``; a
  concurrent writer racing the same transition lands 0 rows updated and
  surfaces the race (``PromoteRaceLost``).
* ``final_digest_equality`` -- nullable boolean recording whether the
  *retagged final* digest equalled the validated *source* digest at
  promote (the RT-12 retag verifies this; here it is the column RT-12
  writes). NULL until a promote completes a tag write.
* ``promotion_audit_id`` -- the id of the HARD-gate audit row written
  before any tag write (RT-10a AC: audit-insert failure aborts before
  any tag write). Nullable until the gate passes; RT-08's
  ``/api/version`` overlay surfaces this as ``promotion_audit_id``.
* ``created_at`` / ``updated_at`` -- row birth + last-transition
  breadcrumbs; ``updated_at`` is bumped by every promote transition.

Index on ``(promotion_state, updated_at DESC)`` serves the operator /
conductor hot path: "give me the trains currently promoting / most
recently promoted."

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton. The writers are
``backend.agents.release_train.create_train`` (one INSERT per candidate)
and ``...promote`` (the CAS transitions); readers are ``get_train`` in
the same module and future RT-10b/RT-12/RT-13a consumers. No background
task or process-global cache is introduced.

Revision ID: 0247
Revises: 0246
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0247"
down_revision = "0246"
branch_labels = None
depends_on = None


# Closed enum -- must stay in lock-step with ``PROMOTION_STATES`` in
# ``backend.agents.release_train`` (asserted in
# ``test_release_train.py::test_promotion_state_enum_matches_migration``
# and ``test_alembic_0247_release_train.py``).
_STATE_LITERAL = "'pending','promoting','promoted','failed'"


_PG_DDL = f"""
CREATE TABLE IF NOT EXISTS release_train (
    id                     BIGSERIAL PRIMARY KEY,
    candidate_sha          TEXT NOT NULL,
    source_digest_backend  TEXT NOT NULL,
    source_digest_frontend TEXT NOT NULL,
    reserved_version       TEXT,
    promotion_state        TEXT NOT NULL DEFAULT 'pending',
    actor                  TEXT NOT NULL DEFAULT '',
    row_version            INTEGER NOT NULL DEFAULT 0,
    final_digest_equality  BOOLEAN,
    promotion_audit_id     BIGINT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT release_train_promotion_state_chk
        CHECK (promotion_state IN ({_STATE_LITERAL})),
    CONSTRAINT release_train_candidate_sha_len_chk
        CHECK (length(candidate_sha) = 40),
    CONSTRAINT release_train_candidate_sha_uniq UNIQUE (candidate_sha)
)
"""


_SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS release_train (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_sha          TEXT NOT NULL,
    source_digest_backend  TEXT NOT NULL,
    source_digest_frontend TEXT NOT NULL,
    reserved_version       TEXT,
    promotion_state        TEXT NOT NULL DEFAULT 'pending',
    actor                  TEXT NOT NULL DEFAULT '',
    row_version            INTEGER NOT NULL DEFAULT 0,
    final_digest_equality  INTEGER,
    promotion_audit_id     INTEGER,
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT release_train_promotion_state_chk
        CHECK (promotion_state IN ({_STATE_LITERAL})),
    CONSTRAINT release_train_candidate_sha_len_chk
        CHECK (length(candidate_sha) = 40),
    CONSTRAINT release_train_candidate_sha_uniq UNIQUE (candidate_sha)
)
"""


_INDEX_STATE_UPDATED = """
CREATE INDEX IF NOT EXISTS idx_release_train_state_updated
    ON release_train (promotion_state, updated_at DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        _PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL
    )
    bind.exec_driver_sql(_INDEX_STATE_UPDATED)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_train_state_updated")
    bind.exec_driver_sql("DROP TABLE IF EXISTS release_train")
