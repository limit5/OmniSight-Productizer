"""WP.1.8 -- Block CRUD, permalink, and redaction round-trip contracts."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from backend.block_redaction import (
    REDACTION_CUSTOMER_IP,
    REDACTION_SECRET,
    redact_block_for_share,
)
from backend.models import Block


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0195 = BACKEND_ROOT / "alembic" / "versions" / "0195_blocks.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "_wp1_8_alembic_0195",
        MIGRATION_0195,
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["_wp1_8_alembic_0195"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _new_sqlite_blocks_db() -> sqlite3.Connection:
    m0195 = _load_migration()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(m0195._SQLITE_CREATE_TABLE)
    conn.execute(m0195._INDEX_TENANT_SESSION)
    conn.execute(m0195._INDEX_PARENT)
    return conn


def _row_to_block(row: sqlite3.Row) -> Block:
    data = dict(row)
    for key in ("payload", "metadata", "redaction_mask"):
        data[key] = json.loads(data[key])
    return Block(**data)


def test_blocks_sqlite_crud_round_trip_matches_wp_1_1_model_shape() -> None:
    conn = _new_sqlite_blocks_db()

    conn.execute(
        """
        INSERT INTO blocks (
            block_id, parent_id, tenant_id, user_id, project_id,
            session_id, kind, status, title, payload, metadata,
            redaction_mask, started_at, completed_at, created_at
        ) VALUES (
            :block_id, :parent_id, :tenant_id, :user_id, :project_id,
            :session_id, :kind, :status, :title, :payload, :metadata,
            :redaction_mask, :started_at, :completed_at, :created_at
        )
        """,
        {
            "block_id": "blk-parent",
            "parent_id": None,
            "tenant_id": "tenant-1",
            "user_id": "user-1",
            "project_id": "project-1",
            "session_id": "session-1",
            "kind": "turn.command",
            "status": "running",
            "title": "Run tests",
            "payload": json.dumps(
                {"command": "pytest backend/tests/test_block_redaction.py"}
            ),
            "metadata": json.dumps({"surface": "ORCHESTRATOR"}),
            "redaction_mask": json.dumps({}),
            "started_at": "2026-05-06T00:00:00Z",
            "completed_at": None,
            "created_at": "2026-05-06T00:00:00Z",
        },
    )
    conn.execute(
        """
        INSERT INTO blocks (
            block_id, parent_id, tenant_id, session_id, kind, status,
            title, payload, metadata, redaction_mask
        ) VALUES (
            :block_id, :parent_id, :tenant_id, :session_id, :kind,
            :status, :title, :payload, :metadata, :redaction_mask
        )
        """,
        {
            "block_id": "blk-child",
            "parent_id": "blk-parent",
            "tenant_id": "tenant-1",
            "session_id": "session-1",
            "kind": "turn.output",
            "status": "running",
            "title": "Command output",
            "payload": json.dumps({"stdout": "ok"}),
            "metadata": json.dumps({"surface": "TokenUsageStats"}),
            "redaction_mask": json.dumps({}),
        },
    )

    row = conn.execute(
        "SELECT * FROM blocks WHERE block_id = :block_id",
        {"block_id": "blk-child"},
    ).fetchone()
    block = _row_to_block(row)
    assert block.block_id == "blk-child"
    assert block.parent_id == "blk-parent"
    assert block.payload == {"stdout": "ok"}

    conn.execute(
        """
        UPDATE blocks
        SET status = :status, completed_at = :completed_at
        WHERE block_id = :block_id
        """,
        {
            "status": "completed",
            "completed_at": "2026-05-06T00:00:05Z",
            "block_id": "blk-child",
        },
    )
    updated = _row_to_block(
        conn.execute(
            "SELECT * FROM blocks WHERE block_id = :block_id",
            {"block_id": "blk-child"},
        ).fetchone(),
    )
    assert updated.status == "completed"
    assert updated.completed_at == "2026-05-06T00:00:05Z"

    conn.execute(
        "DELETE FROM blocks WHERE block_id = :block_id",
        {"block_id": "blk-child"},
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM blocks WHERE block_id = :block_id",
            {"block_id": "blk-child"},
        ).fetchone()[0]
        == 0
    )


def test_block_permalink_redaction_round_trip_masks_selected_regions() -> None:
    conn = _new_sqlite_blocks_db()
    payload = {
        "command": "curl -H 'Authorization: Bearer live-token' https://example.test",
        "stdout": "customer 203.0.113.9 ok",
        "stderr": "safe stderr",
    }
    metadata = {
        "customer_ip": "198.51.100.44",
        "surface": "HD bring-up workbench",
    }
    conn.execute(
        """
        INSERT INTO blocks (
            block_id, tenant_id, session_id, kind, status, title,
            payload, metadata, redaction_mask
        ) VALUES (
            :block_id, :tenant_id, :session_id, :kind, :status, :title,
            :payload, :metadata, :redaction_mask
        )
        """,
        {
            "block_id": "blk-share",
            "tenant_id": "tenant-1",
            "session_id": "session-1",
            "kind": "turn.command",
            "status": "completed",
            "title": "Shareable block",
            "payload": json.dumps(payload),
            "metadata": json.dumps(metadata),
            "redaction_mask": json.dumps({"payload.command": REDACTION_SECRET}),
        },
    )

    source = _row_to_block(
        conn.execute(
            "SELECT * FROM blocks WHERE block_id = :block_id",
            {"block_id": "blk-share"},
        ).fetchone(),
    )
    result = redact_block_for_share(source, regions=["command", "output", "metadata"])
    share_payload = {
        "share_id": "share-wp1-8",
        "object_kind": "block",
        "object_id": source.block_id,
        "tenant_id": source.tenant_id,
        "visibility": "private",
        "permalink_url": "https://omnisight.local/share/share-wp1-8",
        "regions": list(result.regions),
        "block": result.block,
        "redaction_mask": result.redaction_mask,
    }

    assert share_payload["object_id"] == "blk-share"
    assert share_payload["permalink_url"].endswith("/share/share-wp1-8")
    assert share_payload["regions"] == ["command", "output", "metadata"]
    assert share_payload["redaction_mask"] == {
        "payload.command": REDACTION_SECRET,
        "payload.stdout": REDACTION_CUSTOMER_IP,
        "metadata.customer_ip": REDACTION_CUSTOMER_IP,
    }
    assert share_payload["block"]["payload"]["command"] == "[REDACTED:secret]"
    assert share_payload["block"]["payload"]["stdout"] == (
        "customer [REDACTED:customer_ip] ok"
    )
    assert share_payload["block"]["metadata"]["customer_ip"] == (
        "[REDACTED:customer_ip]"
    )
    assert source.payload == payload
    assert source.metadata == metadata
