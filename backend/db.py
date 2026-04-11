"""SQLite persistence layer for agents, tasks, and token usage.

Uses aiosqlite for async access.  The database file lives at
``data/omnisight.db`` relative to the project root (auto-created).

All public functions are thin wrappers around ``_conn()`` so the
rest of the application stays unaware of SQL details.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "omnisight.db"
_db: aiosqlite.Connection | None = None


async def init() -> None:
    """Open the database and create tables if they don't exist."""
    global _db
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(str(_DB_PATH))
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    # Run lightweight migrations for schema evolution
    await _migrate(_db)
    await _db.commit()
    logger.info("Database ready: %s", _DB_PATH)


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Add columns that may be missing in older databases."""
    # Collect existing columns per table
    migrations = [
        ("agents", "sub_type", "TEXT NOT NULL DEFAULT ''"),
        ("tasks", "suggested_sub_type", "TEXT"),
        ("tasks", "parent_task_id", "TEXT"),
        ("tasks", "child_task_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ]
    for table, column, typedef in migrations:
        try:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
            logger.info("Migration: added %s.%s", table, column)
        except Exception:
            pass  # Column already exists


async def close() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized — call db.init() first")
    return _db


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    sub_type    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'idle',
    progress    TEXT NOT NULL DEFAULT '{"current":0,"total":0}',
    thought_chain TEXT NOT NULL DEFAULT '',
    ai_model    TEXT,
    sub_tasks   TEXT NOT NULL DEFAULT '[]',
    workspace   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    description         TEXT,
    priority            TEXT NOT NULL DEFAULT 'medium',
    status              TEXT NOT NULL DEFAULT 'backlog',
    assigned_agent_id   TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT,
    ai_analysis         TEXT,
    suggested_agent_type TEXT,
    suggested_sub_type  TEXT,
    parent_task_id      TEXT,
    child_task_ids      TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS handoffs (
    task_id     TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS token_usage (
    model           TEXT PRIMARY KEY,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost            REAL NOT NULL DEFAULT 0.0,
    request_count   INTEGER NOT NULL DEFAULT 0,
    avg_latency     INTEGER NOT NULL DEFAULT 0,
    last_used       TEXT NOT NULL DEFAULT ''
);
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agent CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def list_agents() -> list[dict]:
    async with _conn().execute("SELECT * FROM agents") as cur:
        rows = await cur.fetchall()
    return [_agent_row_to_dict(r) for r in rows]


async def get_agent(agent_id: str) -> dict | None:
    async with _conn().execute("SELECT * FROM agents WHERE id = ?", (agent_id,)) as cur:
        row = await cur.fetchone()
    return _agent_row_to_dict(row) if row else None


async def upsert_agent(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO agents (id, name, type, sub_type, status, progress, thought_chain, ai_model, sub_tasks, workspace)
           VALUES (:id, :name, :type, :sub_type, :status, :progress, :thought_chain, :ai_model, :sub_tasks, :workspace)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, type=excluded.type, sub_type=excluded.sub_type, status=excluded.status,
             progress=excluded.progress, thought_chain=excluded.thought_chain,
             ai_model=excluded.ai_model, sub_tasks=excluded.sub_tasks, workspace=excluded.workspace
        """,
        {
            "id": data["id"],
            "name": data["name"],
            "type": data["type"],
            "sub_type": data.get("sub_type", ""),
            "status": data.get("status", "idle"),
            "progress": json.dumps(data.get("progress", {"current": 0, "total": 0})),
            "thought_chain": data.get("thought_chain", ""),
            "ai_model": data.get("ai_model"),
            "sub_tasks": json.dumps(data.get("sub_tasks", [])),
            "workspace": json.dumps(data.get("workspace", {})),
        },
    )
    await _conn().commit()


async def delete_agent(agent_id: str) -> bool:
    cur = await _conn().execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    await _conn().commit()
    return cur.rowcount > 0


async def agent_count() -> int:
    async with _conn().execute("SELECT COUNT(*) FROM agents") as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


def _agent_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "sub_type": row["sub_type"] if "sub_type" in row.keys() else "",
        "status": row["status"],
        "progress": json.loads(row["progress"]),
        "thought_chain": row["thought_chain"],
        "ai_model": row["ai_model"],
        "sub_tasks": json.loads(row["sub_tasks"]),
        "workspace": json.loads(row["workspace"]),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _task_row_to_dict(row) -> dict:
    d = dict(row)
    if isinstance(d.get("child_task_ids"), str):
        d["child_task_ids"] = json.loads(d["child_task_ids"])
    return d


async def list_tasks() -> list[dict]:
    async with _conn().execute("SELECT * FROM tasks") as cur:
        rows = await cur.fetchall()
    return [_task_row_to_dict(r) for r in rows]


async def get_task(task_id: str) -> dict | None:
    async with _conn().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return _task_row_to_dict(row) if row else None


async def upsert_task(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO tasks (id, title, description, priority, status, assigned_agent_id, created_at, completed_at, ai_analysis, suggested_agent_type, suggested_sub_type, parent_task_id, child_task_ids)
           VALUES (:id, :title, :description, :priority, :status, :assigned_agent_id, :created_at, :completed_at, :ai_analysis, :suggested_agent_type, :suggested_sub_type, :parent_task_id, :child_task_ids)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, description=excluded.description, priority=excluded.priority,
             status=excluded.status, assigned_agent_id=excluded.assigned_agent_id,
             completed_at=excluded.completed_at, ai_analysis=excluded.ai_analysis,
             suggested_agent_type=excluded.suggested_agent_type, suggested_sub_type=excluded.suggested_sub_type,
             parent_task_id=excluded.parent_task_id, child_task_ids=excluded.child_task_ids
        """,
        {
            "id": data["id"],
            "title": data["title"],
            "description": data.get("description"),
            "priority": data.get("priority", "medium"),
            "status": data.get("status", "backlog"),
            "assigned_agent_id": data.get("assigned_agent_id"),
            "created_at": data.get("created_at", ""),
            "completed_at": data.get("completed_at"),
            "ai_analysis": data.get("ai_analysis"),
            "suggested_agent_type": data.get("suggested_agent_type"),
            "suggested_sub_type": data.get("suggested_sub_type"),
            "parent_task_id": data.get("parent_task_id"),
            "child_task_ids": json.dumps(data.get("child_task_ids", [])),
        },
    )
    await _conn().commit()


async def delete_task(task_id: str) -> bool:
    cur = await _conn().execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await _conn().commit()
    return cur.rowcount > 0


async def task_count() -> int:
    async with _conn().execute("SELECT COUNT(*) FROM tasks") as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token usage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def list_token_usage() -> list[dict]:
    async with _conn().execute("SELECT * FROM token_usage") as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def upsert_token_usage(data: dict) -> None:
    await _conn().execute(
        """INSERT INTO token_usage (model, input_tokens, output_tokens, total_tokens, cost, request_count, avg_latency, last_used)
           VALUES (:model, :input_tokens, :output_tokens, :total_tokens, :cost, :request_count, :avg_latency, :last_used)
           ON CONFLICT(model) DO UPDATE SET
             input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
             total_tokens=excluded.total_tokens, cost=excluded.cost,
             request_count=excluded.request_count, avg_latency=excluded.avg_latency,
             last_used=excluded.last_used
        """,
        data,
    )
    await _conn().commit()


async def clear_token_usage() -> None:
    await _conn().execute("DELETE FROM token_usage")
    await _conn().commit()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Handoffs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def upsert_handoff(task_id: str, agent_id: str, content: str) -> None:
    await _conn().execute(
        """INSERT INTO handoffs (task_id, agent_id, content)
           VALUES (?, ?, ?)
           ON CONFLICT(task_id) DO UPDATE SET
             agent_id=excluded.agent_id, content=excluded.content,
             created_at=datetime('now')
        """,
        (task_id, agent_id, content),
    )
    await _conn().commit()


async def get_handoff(task_id: str) -> str:
    async with _conn().execute("SELECT content FROM handoffs WHERE task_id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return row["content"] if row else ""


async def list_handoffs() -> list[dict]:
    async with _conn().execute("SELECT task_id, agent_id, created_at FROM handoffs ORDER BY created_at DESC") as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
