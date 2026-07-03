"""RPG.W15 — add ``character_def.voice`` + seed the roster's personas.

Adds a single ``voice`` TEXT column to ``character_def`` and seeds the
speech-style + catchphrase line for the eight roster characters (built-ins
nova/pixel/sage/rex + recruits argus/iris/kai/vega). ``voice`` is COSMETIC:
``backend/routers/agents`` renders it on the Character Card, and it is
deliberately never injected into any task-execution prompt, so a character's
persona cannot colour the runner's professional work output.

Loader interplay (``character_registry``): built-in voices are code-
authoritative (the constant wins; ``voice`` is excluded from
``_SHADOW_COMPARE_FIELDS`` so a divergent DB voice for a built-in is ignored
silently, not logged as drift). Recruited characters read their voice from
this column.

Idempotency
-----------
* PG: ``ADD COLUMN IF NOT EXISTS``. SQLite has no such clause, so the column
  presence is probed via ``PRAGMA table_info`` before ``ADD COLUMN``.
* The seed ``UPDATE`` is guarded ``WHERE voice IS NULL OR voice = ''`` so a
  re-run — or a later operator edit of a character's voice — is preserved.

Revision ID: 0255
Revises: 0254
Create Date: 2026-07-03
backwards-compat: safe (additive column, cosmetic; inert for non-UI paths)
"""
from __future__ import annotations

from alembic import op


revision = "0255"
down_revision = "0254"
branch_labels = None
depends_on = None


# slug -> voice (tone + 口頭禪). Built-in voices are an EXACT copy of
# backend/agents/character_registry.CHARACTERS; the companion test drift-guards
# them. Recruit voices are sourced from the character design docs' epithets
# (docs/design/rpg/characters/*.md).
VOICES: tuple[tuple[str, str], ...] = (
    ("nova", "沉穩可靠的兄長，把複雜架構講得讓人安心。口頭禪:「交給哥哥，架構我來扛。」"),
    ("pixel", "潮味十足的設計小子，講究每個像素好不好看。口頭禪:「這樣才夠潮，交給我調到發光。」"),
    ("sage", "愛鑽研的小博學者，凡事先追根究柢。口頭禪:「讓我想想…啊，原來如此！」"),
    ("rex", "風風火火的值班小快手，最愛把雜活秒殺。口頭禪:「包在我身上，三兩下搞定！」"),
    ("iris", "講究光影細節的影像大師，眼裡容不下一格糊掉的畫面。口頭禪:「對到焦，讓畫面自己說話。」"),
    ("argus", "認真又警覺的小哨兵，任何破綻都逃不過。口頭禪:「發見——這裡有問題。」"),
    ("kai", "從容的行動派高手，一出手就把 app 送上裝置。口頭禪:「上機實測，帥氣收工。」"),
    ("vega", "精力充沛的弟弟，滿腦子想著把每毫秒榨乾。口頭禪:「哥，看我的——再快一點！」"),
)


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _voice_column_exists(bind, dialect: str) -> bool:
    if dialect == "postgresql":
        row = bind.exec_driver_sql(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'character_def' AND column_name = 'voice'"
        ).fetchone()
        return row is not None
    # SQLite
    cols = bind.exec_driver_sql("PRAGMA table_info(character_def)").fetchall()
    return any(c[1] == "voice" for c in cols)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE character_def "
            "ADD COLUMN IF NOT EXISTS voice TEXT NOT NULL DEFAULT ''"
        )
    elif not _voice_column_exists(bind, dialect):
        bind.exec_driver_sql(
            "ALTER TABLE character_def ADD COLUMN voice TEXT NOT NULL DEFAULT ''"
        )
    for slug, voice in VOICES:
        bind.exec_driver_sql(
            f"UPDATE character_def SET voice = '{_sql_escape(voice)}' "
            f"WHERE slug = '{_sql_escape(slug)}' "
            f"AND (voice IS NULL OR voice = '')"
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE character_def DROP COLUMN IF EXISTS voice"
        )
    # SQLite: legacy DROP COLUMN is unsupported on old engines; leaving the
    # cosmetic column in place on downgrade is harmless (readers tolerate it).
