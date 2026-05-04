"""FX.11.1 -- Backfill ``sessions.token`` to KS envelope JSON.

Background
----------
KS.1 envelope encryption (alembic 0106) landed the storage tables and the
``backend.security.envelope`` helper, but live ``sessions`` rows still
hold plaintext URL-safe session tokens. FX.10.7 wired the backup DLP
gate so it CAN refuse plaintext in ``sessions.token`` (via
``REQUIRED_ENVELOPE_COLUMNS``), but the gate is held empty until this
data migration converts the existing rows.

This revision is the data half of FX.11. It performs three things in one
alembic upgrade:

1. **Schema** -- adds ``sessions.token_lookup_index`` (TEXT, nullable
   initially) plus ``UNIQUE INDEX idx_sessions_token_lookup_index`` so
   FX.11.2 can resolve a cookie token to the stored envelope row by
   ``WHERE token_lookup_index = sha256(plaintext)`` instead of the old
   ``WHERE token = plaintext`` lookup that breaks once tokens are JSON.

2. **Backfill** -- iterates every existing session row and, for each
   row whose ``token`` field still parses as plaintext (not already a
   packed envelope JSON), encrypts the plaintext via
   ``backend.security.envelope.encrypt(plaintext, tenant_id)``.  The
   tenant id is resolved by ``LEFT JOIN users`` on ``sessions.user_id``
   with ``COALESCE(..., 't-default')`` so orphan sessions (user since
   deleted) still pick a usable tenant for KMS encryption_context.
   The packed value persisted is::

       {"ciphertext": "<envelope JSON>", "dek_ref": {<TenantDEKRef.to_dict>}}

   This packed shape matches the second branch of
   ``backup_dlp_scan._looks_like_ks_envelope`` and keeps the dek_ref
   colocated with the ciphertext so the FX.11.2 read path does not need
   a ``tenant_deks`` JOIN to decrypt a session.

3. **Idempotency** -- rows whose ``token`` field already parses as a
   packed envelope JSON are skipped. Re-running the migration on a
   previously-migrated DB is a no-op (``encrypted=0, skipped=N``).
   ``CREATE UNIQUE INDEX IF NOT EXISTS`` and the SQLite-side
   ``PRAGMA table_info`` precheck make the schema half re-runnable too.

Out of scope (deferred follow-ups)
----------------------------------
* ``backend/auth.py`` runtime read/write paths (FX.11.2). Until that
  ships, the live system is broken: cookies carry plaintext but the DB
  stores envelope JSON. Operators MUST land FX.11.2 + restart workers
  in the same maintenance window as this migration; otherwise active
  users get logged out at the next request and cannot re-establish a
  session through the legacy ``WHERE token = plaintext`` lookup.
* ``scripts/backup_dlp_scan.py`` allowlist switchover (FX.11.3).
* ``session_revocations.token`` is a separate plaintext-token store
  used by the post-eviction "why was I logged out" probe; it is NOT
  encrypted by this migration and remains a known follow-up.

Module-global / cross-worker state audit
----------------------------------------
Pure offline migration. Runs once at ``alembic upgrade head`` time
during the cutover window. There is no "every worker" question because
every uvicorn worker boots against the post-migration DB state. The
``backend.security.envelope`` helper has no module-level mutable state
(audited at envelope.py:42) and the ``LocalFernetKMSAdapter`` derives
its key through ``backend.secret_store``'s file-lock guarded
``_get_key()`` so any worker that later decrypts these envelopes
resolves the same KEK without in-memory coordination.

Read-after-write timing audit
-----------------------------
This migration runs in the same alembic transaction as its DDL +
backfill, so within the migration there is no read-after-write race:
the ``ALTER TABLE`` is visible to the subsequent UPDATE. Across the
cutover boundary the relationship is "stop writers (drain) → run
migration → start FX.11.2 writers" -- there is no concurrent reader
expecting the old plaintext-lookup timing.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
  The ``backend.security.envelope`` import is an existing dependency
  used by KS.1.x rows and shipped in the current image.
* No new tables -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is unaffected. The added column rides existing ``sessions`` table.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` keeps the legacy
  ``token TEXT PRIMARY KEY`` shape; fresh dev DBs (no rows) skip the
  backfill loop. The added column is not mirrored into ``_SCHEMA``
  because adding it there without the migration would diverge "fresh
  dev DB" from "migrated prod DB" -- the next session-touching
  alembic row should fold this column into ``_SCHEMA`` when the
  ``sessions`` shape next changes.
* Production status of THIS commit: **dev-only**. Next gate is
  ``deployed-inactive`` once FX.11.2 lands and operator runs alembic
  upgrade against prod PG. ``deployed-active`` requires FX.11.3 to flip
  the DLP gate from EXPECTED to REQUIRED.

Revision ID: 0189
Revises: 0188
Create Date: 2026-05-04
"""
from __future__ import annotations

import hashlib
import json
import logging

from alembic import op
from sqlalchemy import text


revision = "0189"
down_revision = "0188"
branch_labels = None
depends_on = None


_log = logging.getLogger("alembic.0189")


# -- DDL --------------------------------------------------------------------

_PG_ADD_COLUMN = (
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS token_lookup_index TEXT"
)
_SQLITE_ADD_COLUMN = "ALTER TABLE sessions ADD COLUMN token_lookup_index TEXT"
_CREATE_LOOKUP_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_token_lookup_index "
    "ON sessions(token_lookup_index)"
)
_DROP_LOOKUP_INDEX = "DROP INDEX IF EXISTS idx_sessions_token_lookup_index"


# -- helpers ----------------------------------------------------------------


def _looks_like_packed_envelope(value: str) -> bool:
    """True iff ``value`` is the packed ``{ciphertext, dek_ref}`` shape.

    Mirrors the first branch of ``scripts/backup_dlp_scan._looks_like_ks_envelope``.
    Used as the idempotency check: rows already in this shape are not re-encrypted.
    """

    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if "ciphertext" not in payload or "dek_ref" not in payload:
        return False
    return isinstance(payload["ciphertext"], str) and isinstance(
        payload["dek_ref"], dict
    )


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _pack(ciphertext_envelope: str, dek_ref) -> str:
    return json.dumps(
        {"ciphertext": ciphertext_envelope, "dek_ref": dek_ref.to_dict()},
        sort_keys=True,
        separators=(",", ":"),
    )


def _ensure_lookup_column(conn, dialect: str) -> None:
    if dialect == "postgresql":
        conn.exec_driver_sql(_PG_ADD_COLUMN)
        return
    existing = {
        row[1]
        for row in conn.exec_driver_sql("PRAGMA table_info(sessions)").fetchall()
    }
    if "token_lookup_index" not in existing:
        conn.exec_driver_sql(_SQLITE_ADD_COLUMN)


# -- upgrade / downgrade ----------------------------------------------------


def upgrade() -> None:
    # Lazy import: keeps the module-import cheap for alembic introspection
    # tools (e.g. ``alembic history``) that should not pull crypto deps.
    from backend.security import envelope as ks_envelope

    conn = op.get_bind()
    dialect = conn.dialect.name

    _ensure_lookup_column(conn, dialect)
    conn.exec_driver_sql(_CREATE_LOOKUP_INDEX)

    rows = conn.exec_driver_sql(
        "SELECT s.token, COALESCE(u.tenant_id, 't-default') AS tid "
        "FROM sessions s LEFT JOIN users u ON s.user_id = u.id"
    ).fetchall()

    encrypted = 0
    skipped = 0
    for row in rows:
        token = row[0]
        tenant_id = row[1] or "t-default"
        if not isinstance(token, str) or not token:
            skipped += 1
            continue
        if _looks_like_packed_envelope(token):
            skipped += 1
            continue

        ciphertext, dek_ref = ks_envelope.encrypt(token, tenant_id)
        packed = _pack(ciphertext, dek_ref)
        lookup = _hash_token(token)

        conn.execute(
            text(
                "UPDATE sessions "
                "SET token = :new_token, token_lookup_index = :lookup "
                "WHERE token = :old_token"
            ),
            {"new_token": packed, "lookup": lookup, "old_token": token},
        )
        encrypted += 1

    _log.info(
        "alembic 0189 sessions.token envelope backfill: encrypted=%d "
        "skipped=%d total=%d",
        encrypted,
        skipped,
        len(rows),
    )


def downgrade() -> None:
    """Best-effort reverse: decrypt envelope rows back to plaintext.

    Downgrade requires KMS access at run time. Rows that fail to decrypt
    (corrupt envelope, missing KEK, KMS provider unreachable) are left in
    place; the operator must reconcile manually before downgrading the
    schema column. The unique-index drop and column-drop run unconditionally
    at the end so a partial-decrypt downgrade still ends with the lookup
    artefacts gone.
    """

    from backend.security import envelope as ks_envelope

    conn = op.get_bind()
    dialect = conn.dialect.name

    rows = conn.exec_driver_sql("SELECT token FROM sessions").fetchall()
    for row in rows:
        packed = row[0]
        if not isinstance(packed, str) or not _looks_like_packed_envelope(packed):
            continue
        try:
            payload = json.loads(packed)
            dek_ref = ks_envelope.TenantDEKRef.from_dict(payload["dek_ref"])
            plaintext = ks_envelope.decrypt(payload["ciphertext"], dek_ref)
        except Exception:
            _log.warning(
                "alembic 0189 downgrade: skipping undecryptable session row "
                "(envelope corrupt or KMS unavailable)"
            )
            continue
        conn.execute(
            text(
                "UPDATE sessions SET token = :plain, token_lookup_index = NULL "
                "WHERE token = :packed"
            ),
            {"plain": plaintext, "packed": packed},
        )

    conn.exec_driver_sql(_DROP_LOOKUP_INDEX)
    if dialect == "postgresql":
        conn.exec_driver_sql(
            "ALTER TABLE sessions DROP COLUMN IF EXISTS token_lookup_index"
        )
    else:
        try:
            conn.exec_driver_sql(
                "ALTER TABLE sessions DROP COLUMN token_lookup_index"
            )
        except Exception:
            # SQLite < 3.35 has no DROP COLUMN -- leave the now-orphaned
            # column in place. The downgraded system tolerates an unused
            # nullable column (auth.py SELECT lists are explicit).
            _log.warning(
                "alembic 0189 downgrade: SQLite DROP COLUMN unsupported; "
                "leaving sessions.token_lookup_index in place"
            )
