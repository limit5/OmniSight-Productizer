"""OP-228 FX2.D3.1 -- KS envelope backfill for credential tables.

Mirrors the idempotent 0189 sessions pattern for the remaining
credential-bearing control-plane tables. Existing legacy Fernet cells are
decrypted once and rewritten as packed KS envelope carriers; rows already
in carrier form are skipped. ``api_keys.key_hash`` keeps a deterministic
``key_lookup_index`` so bearer validation can still use an indexed lookup
after the stored hash is encrypted.

Revision ID: 0203a_kse  (OP-1046: renamed from 0203 to disambiguate from 0203_conflict_observations.py)
Revises: 0202
Create Date: 2026-05-08
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from alembic import op
from sqlalchemy import text


# OP-1046: was revision="0203" — caused duplicate-revision collision with
# 0203_conflict_observations.py (both filed 2026-05-08, conflict_observations
# committed first at 15:37, this file at 16:03). 0204_deploy_audit's
# down_revision="0203" canonically refers to conflict_observations now; this
# migration becomes a parallel head off 0202 that gets unified by the
# audit-29 merge migrations.
revision = "0203a_kse"
down_revision = "0202"
branch_labels = None
depends_on = None


_log = logging.getLogger("alembic.0203a_kse")


def _looks_like_carrier(value: str) -> bool:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("fmt") == 1
        and isinstance(payload.get("ciphertext"), str)
        and isinstance(payload.get("dek_ref"), dict)
    )


def _pack_ks_payload(payload: str, tenant_id: str, purpose: str) -> str:
    from backend.security import envelope as ks_envelope

    ciphertext, dek_ref = ks_envelope.encrypt(payload, tenant_id, purpose=purpose)
    return json.dumps(
        {"fmt": 1, "ciphertext": ciphertext, "dek_ref": dek_ref.to_dict()},
        sort_keys=True,
        separators=(",", ":"),
    )


def _binding_payload(token: str, binding: Mapping[str, str]) -> str:
    return json.dumps(
        {"fmt": 1, "ctx": dict(binding), "tok": token},
        sort_keys=True,
        separators=(",", ":"),
    )


def _legacy_decrypt(value: str) -> str:
    from backend import secret_store

    return secret_store.decrypt(value)


def _legacy_encrypt(value: str) -> str:
    from backend import secret_store

    return secret_store.encrypt(value)


def _unpack_ks_payload(value: str) -> str:
    from backend.security import envelope as ks_envelope

    payload = json.loads(value)
    dek_ref = ks_envelope.TenantDEKRef.from_dict(payload["dek_ref"])
    return ks_envelope.decrypt(payload["ciphertext"], dek_ref)


def _token_from_binding(payload: str) -> str:
    decoded = json.loads(payload)
    token = decoded.get("tok") if isinstance(decoded, dict) else None
    if not isinstance(token, str):
        raise ValueError("binding payload missing tok")
    return token


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _ensure_lookup_columns(conn) -> None:
    cols = _columns(conn, "api_keys")
    if "key_lookup_index" not in cols:
        conn.exec_driver_sql("ALTER TABLE api_keys ADD COLUMN key_lookup_index TEXT")
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_lookup "
        "ON api_keys(key_lookup_index)"
    )

    storage_cols = _columns(conn, "provisioned_storage")
    if "bucket_lookup_index" not in storage_cols:
        conn.exec_driver_sql(
            "ALTER TABLE provisioned_storage ADD COLUMN bucket_lookup_index TEXT"
        )


def _backfill_api_keys(conn) -> tuple[int, int]:
    rows = conn.exec_driver_sql(
        "SELECT id, COALESCE(tenant_id, 't-default') AS tenant_id, "
        "key_hash, key_lookup_index FROM api_keys"
    ).fetchall()
    changed = skipped = 0
    for row in rows:
        key_id, tenant_id, key_hash, lookup = row
        if not isinstance(key_hash, str) or not key_hash:
            skipped += 1
            continue
        if _looks_like_carrier(key_hash):
            if not lookup:
                try:
                    lookup = _token_from_binding(_unpack_ks_payload(key_hash))
                except Exception:
                    skipped += 1
                    continue
                conn.execute(
                    text(
                        "UPDATE api_keys SET key_lookup_index = :lookup "
                        "WHERE id = :id"
                    ),
                    {"lookup": lookup, "id": key_id},
                )
                changed += 1
            else:
                skipped += 1
            continue
        packed = _pack_ks_payload(
            _binding_payload(key_hash, {"table": "api_keys", "id": key_id}),
            tenant_id or "t-default",
            "api-key-hash",
        )
        conn.execute(
            text(
                "UPDATE api_keys SET key_hash = :packed, "
                "key_lookup_index = :lookup WHERE id = :id"
            ),
            {"packed": packed, "lookup": key_hash, "id": key_id},
        )
        changed += 1
    return changed, skipped


def _backfill_oauth_tokens(conn) -> tuple[int, int]:
    rows = conn.exec_driver_sql(
        "SELECT o.user_id, o.provider, o.access_token_enc, o.refresh_token_enc, "
        "COALESCE(u.tenant_id, o.user_id) AS tenant_id "
        "FROM oauth_tokens o LEFT JOIN users u ON o.user_id = u.id"
    ).fetchall()
    changed = skipped = 0
    for user_id, provider, access, refresh, tenant_id in rows:
        updates: dict[str, str | int] = {}
        for column, value in (
            ("access_token_enc", access),
            ("refresh_token_enc", refresh),
        ):
            if not isinstance(value, str) or not value:
                skipped += 1
                continue
            if _looks_like_carrier(value):
                skipped += 1
                continue
            try:
                payload = _legacy_decrypt(value)
            except Exception:
                skipped += 1
                continue
            updates[column] = _pack_ks_payload(
                payload,
                tenant_id or user_id,
                "as-token-vault",
            )
        if updates:
            updates["key_version"] = 1
            conn.execute(
                text(
                    "UPDATE oauth_tokens SET "
                    + ", ".join(f"{key} = :{key}" for key in updates)
                    + " WHERE user_id = :user_id AND provider = :provider"
                ),
                {**updates, "user_id": user_id, "provider": provider},
            )
            changed += len(updates) - 1
    return changed, skipped


def _backfill_tenant_secrets(conn) -> tuple[int, int]:
    rows = conn.exec_driver_sql(
        "SELECT id, tenant_id, secret_type, key_name, encrypted_value "
        "FROM tenant_secrets"
    ).fetchall()
    changed = skipped = 0
    for row in rows:
        secret_id, tenant_id, secret_type, key_name, value = row
        if not isinstance(value, str) or not value:
            skipped += 1
            continue
        if _looks_like_carrier(value):
            skipped += 1
            continue
        try:
            plaintext = _legacy_decrypt(value)
        except Exception:
            skipped += 1
            continue
        payload = json.dumps(
            {
                "fmt": 1,
                "tid": tenant_id,
                "typ": secret_type,
                "key": key_name,
                "tok": plaintext,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        packed = _pack_ks_payload(payload, tenant_id or "t-default", "tenant-secret")
        conn.execute(
            text(
                "UPDATE tenant_secrets SET encrypted_value = :packed "
                "WHERE id = :id"
            ),
            {"packed": packed, "id": secret_id},
        )
        changed += 1
    return changed, skipped


def _backfill_bound_columns(
    conn,
    *,
    table: str,
    id_columns: tuple[str, ...],
    value_columns: tuple[str, ...],
    purpose: str,
) -> tuple[int, int]:
    select_cols = ", ".join((*id_columns, "COALESCE(tenant_id, 't-default')", *value_columns))
    rows = conn.exec_driver_sql(f"SELECT {select_cols} FROM {table}").fetchall()
    changed = skipped = 0
    for row in rows:
        ids = tuple(str(row[i]) for i in range(len(id_columns)))
        tenant_id = row[len(id_columns)] or "t-default"
        values_offset = len(id_columns) + 1
        updates: dict[str, str] = {}
        row_id = "|".join(ids)
        for idx, column in enumerate(value_columns):
            value = row[values_offset + idx]
            if not isinstance(value, str) or not value:
                skipped += 1
                continue
            if _looks_like_carrier(value):
                skipped += 1
                continue
            try:
                plaintext = _legacy_decrypt(value)
            except Exception:
                skipped += 1
                continue
            updates[column] = _pack_ks_payload(
                _binding_payload(plaintext, {"table": table, "id": row_id}),
                tenant_id,
                purpose,
            )
        if updates:
            where = " AND ".join(f"{col} = :id_{i}" for i, col in enumerate(id_columns))
            params = {f"id_{i}": ids[i] for i in range(len(ids))}
            params.update(updates)
            conn.execute(
                text(
                    f"UPDATE {table} SET "
                    + ", ".join(f"{key} = :{key}" for key in updates)
                    + f" WHERE {where}"
                ),
                params,
            )
            changed += len(updates)
    return changed, skipped


def _backfill_provisioned_storage(conn) -> tuple[int, int]:
    rows = conn.exec_driver_sql(
        "SELECT tenant_id, provider, bucket_name, bucket_lookup_index "
        "FROM provisioned_storage"
    ).fetchall()
    changed = skipped = 0
    for tenant_id, provider, bucket_name, lookup in rows:
        if not isinstance(bucket_name, str) or not bucket_name:
            skipped += 1
            continue
        if _looks_like_carrier(bucket_name):
            skipped += 1
            continue
        packed = _pack_ks_payload(
            _binding_payload(
                bucket_name,
                {"table": "provisioned_storage", "id": f"{tenant_id}|{provider}"},
            ),
            tenant_id or "t-default",
            "provisioned-storage",
        )
        conn.execute(
            text(
                "UPDATE provisioned_storage SET bucket_name = :packed, "
                "bucket_lookup_index = :lookup "
                "WHERE tenant_id = :tenant_id AND provider = :provider"
            ),
            {
                "packed": packed,
                "lookup": lookup or bucket_name,
                "tenant_id": tenant_id,
                "provider": provider,
            },
        )
        changed += 1
    return changed, skipped


def upgrade() -> None:
    conn = op.get_bind()
    _ensure_lookup_columns(conn)

    results = {
        "api_keys": _backfill_api_keys(conn),
        "oauth_tokens": _backfill_oauth_tokens(conn),
        "tenant_secrets": _backfill_tenant_secrets(conn),
        "git_accounts": _backfill_bound_columns(
            conn,
            table="git_accounts",
            id_columns=("id",),
            value_columns=(
                "encrypted_token",
                "encrypted_ssh_key",
                "encrypted_webhook_secret",
            ),
            purpose="git-account",
        ),
        "llm_credentials": _backfill_bound_columns(
            conn,
            table="llm_credentials",
            id_columns=("id",),
            value_columns=("encrypted_value",),
            purpose="llm-credential",
        ),
        "provisioned_databases": _backfill_bound_columns(
            conn,
            table="provisioned_databases",
            id_columns=("tenant_id", "provider"),
            value_columns=("connection_url_enc",),
            purpose="provisioned-database",
        ),
        "provisioned_storage": _backfill_provisioned_storage(conn),
    }
    _log.info("alembic 0203 KS envelope credential backfill: %s", results)


def downgrade() -> None:
    conn = op.get_bind()
    # Best-effort reversals for downgrade drills. Rows that cannot be
    # decrypted are left in carrier form for operator reconciliation.
    for table, id_columns, value_columns in (
        ("git_accounts", ("id",), ("encrypted_token", "encrypted_ssh_key", "encrypted_webhook_secret")),
        ("llm_credentials", ("id",), ("encrypted_value",)),
        ("provisioned_databases", ("tenant_id", "provider"), ("connection_url_enc",)),
    ):
        select_cols = ", ".join((*id_columns, *value_columns))
        rows = conn.exec_driver_sql(f"SELECT {select_cols} FROM {table}").fetchall()
        for row in rows:
            ids = tuple(str(row[i]) for i in range(len(id_columns)))
            updates: dict[str, str] = {}
            for idx, column in enumerate(value_columns):
                value = row[len(id_columns) + idx]
                if isinstance(value, str) and _looks_like_carrier(value):
                    try:
                        updates[column] = _legacy_encrypt(
                            _token_from_binding(_unpack_ks_payload(value))
                        )
                    except Exception:
                        continue
            if updates:
                where = " AND ".join(f"{col} = :id_{i}" for i, col in enumerate(id_columns))
                params = {f"id_{i}": ids[i] for i in range(len(ids))}
                params.update(updates)
                conn.execute(
                    text(
                        f"UPDATE {table} SET "
                        + ", ".join(f"{key} = :{key}" for key in updates)
                        + f" WHERE {where}"
                    ),
                    params,
                )

    for table, key_cols, value_col in (
        ("provisioned_storage", ("tenant_id", "provider"), "bucket_name"),
    ):
        select_cols = ", ".join((*key_cols, value_col))
        rows = conn.exec_driver_sql(f"SELECT {select_cols} FROM {table}").fetchall()
        for row in rows:
            ids = tuple(str(row[i]) for i in range(len(key_cols)))
            value = row[len(key_cols)]
            if not isinstance(value, str) or not _looks_like_carrier(value):
                continue
            try:
                payload = _unpack_ks_payload(value)
                plain = _token_from_binding(payload)
            except Exception:
                continue
            where = " AND ".join(f"{col} = :id_{i}" for i, col in enumerate(key_cols))
            params = {f"id_{i}": ids[i] for i in range(len(ids))}
            params["plain"] = plain
            conn.execute(
                text(f"UPDATE {table} SET {value_col} = :plain WHERE {where}"),
                params,
            )

    api_rows = conn.exec_driver_sql("SELECT id, key_hash FROM api_keys").fetchall()
    for key_id, key_hash in api_rows:
        if not isinstance(key_hash, str) or not _looks_like_carrier(key_hash):
            continue
        try:
            plain_hash = _token_from_binding(_unpack_ks_payload(key_hash))
        except Exception:
            continue
        conn.execute(
            text(
                "UPDATE api_keys SET key_hash = :plain_hash, "
                "key_lookup_index = NULL WHERE id = :id"
            ),
            {"plain_hash": plain_hash, "id": key_id},
        )

    secret_rows = conn.exec_driver_sql(
        "SELECT id, encrypted_value FROM tenant_secrets"
    ).fetchall()
    for secret_id, value in secret_rows:
        if not isinstance(value, str) or not _looks_like_carrier(value):
            continue
        try:
            plaintext = _token_from_binding(_unpack_ks_payload(value))
        except Exception:
            continue
        conn.execute(
            text(
                "UPDATE tenant_secrets SET encrypted_value = :legacy "
                "WHERE id = :id"
            ),
            {"legacy": _legacy_encrypt(plaintext), "id": secret_id},
        )

    oauth_rows = conn.exec_driver_sql(
        "SELECT user_id, provider, access_token_enc, refresh_token_enc FROM oauth_tokens"
    ).fetchall()
    for user_id, provider, access, refresh in oauth_rows:
        updates: dict[str, str] = {}
        for column, value in (("access_token_enc", access), ("refresh_token_enc", refresh)):
            if isinstance(value, str) and _looks_like_carrier(value):
                try:
                    updates[column] = _legacy_encrypt(_unpack_ks_payload(value))
                except Exception:
                    continue
        if updates:
            conn.execute(
                text(
                    "UPDATE oauth_tokens SET "
                    + ", ".join(f"{key} = :{key}" for key in updates)
                    + " WHERE user_id = :user_id AND provider = :provider"
                ),
                {**updates, "user_id": user_id, "provider": provider},
            )
