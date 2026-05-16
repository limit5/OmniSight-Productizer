"""OP-1385 -- ``agent_achievement_milestone`` table for RPG.W16 badges.

ADR-0008 §"Achievements / Badges (W16)" defines operator-visible milestone
badges such as "100 PR Merged" and "0 Regression Streak x30". The existing
``backend.agents.achievement_registry`` module remains the import-time
definition registry; this table gives the backend a durable DB surface with
the same stable milestone ids for joins, unlock scans, and future UI reads.

Schema notes
------------
* ``achievement_id`` is the stable id from the registry. The seed rows mirror
  the five W16.1 milestones shipped in code.
* ``metric`` is intentionally free-form text rather than a DB enum so future
  RPG metrics can be added without a migration solely to alter an enum type.
* ``threshold`` is an inclusive minimum and must be positive, matching
  ``AchievementMilestone.__post_init__``.

Revision ID: 0240
Revises: 0239
Create Date: 2026-05-17
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0240"
down_revision = "0239"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS agent_achievement_milestone (
    achievement_id  TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    category        TEXT NOT NULL,
    metric          TEXT NOT NULL,
    threshold       INTEGER NOT NULL,
    summary         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_achievement_milestone_threshold_chk
        CHECK (threshold >= 1),
    CONSTRAINT agent_achievement_milestone_id_chk
        CHECK (length(trim(achievement_id)) > 0),
    CONSTRAINT agent_achievement_milestone_display_chk
        CHECK (length(trim(display_name)) > 0),
    CONSTRAINT agent_achievement_milestone_category_chk
        CHECK (length(trim(category)) > 0),
    CONSTRAINT agent_achievement_milestone_metric_chk
        CHECK (length(trim(metric)) > 0),
    CONSTRAINT agent_achievement_milestone_summary_chk
        CHECK (length(trim(summary)) > 0)
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS agent_achievement_milestone (
    achievement_id  TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    category        TEXT NOT NULL,
    metric          TEXT NOT NULL,
    threshold       INTEGER NOT NULL,
    summary         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT agent_achievement_milestone_threshold_chk
        CHECK (threshold >= 1),
    CONSTRAINT agent_achievement_milestone_id_chk
        CHECK (length(trim(achievement_id)) > 0),
    CONSTRAINT agent_achievement_milestone_display_chk
        CHECK (length(trim(display_name)) > 0),
    CONSTRAINT agent_achievement_milestone_category_chk
        CHECK (length(trim(category)) > 0),
    CONSTRAINT agent_achievement_milestone_metric_chk
        CHECK (length(trim(metric)) > 0),
    CONSTRAINT agent_achievement_milestone_summary_chk
        CHECK (length(trim(summary)) > 0)
)
"""


_SEED_ROWS = (
    (
        "merged_pr_100",
        "100 PR Merged",
        "delivery",
        "merged_pr_count",
        100,
        "Awarded after an agent has 100 accepted and merged PRs.",
    ),
    (
        "zero_regression_streak_30",
        "0 Regression Streak x30",
        "quality",
        "zero_regression_streak",
        30,
        "Awarded after 30 consecutive completed tasks with no regression.",
    ),
    (
        "taught_agents_5",
        "Taught 5 Agents",
        "mentorship",
        "agents_taught_count",
        5,
        "Awarded after successful same-Guild skill teaching for five agents.",
    ),
    (
        "tier_l_plus_success_10",
        "Tier L+ Veteran",
        "challenge",
        "tier_l_plus_success_count",
        10,
        "Awarded after 10 successful Tier L+ assignments.",
    ),
    (
        "first_time_skill_success_25",
        "Fast Learner",
        "learning",
        "first_time_skill_success_count",
        25,
        "Awarded after 25 successful first-time skill uses.",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_achievement_milestone_category "
        "ON agent_achievement_milestone (category)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_achievement_milestone_metric "
        "ON agent_achievement_milestone (metric)"
    )
    for row in _SEED_ROWS:
        bind.exec_driver_sql(
            f"""
            INSERT INTO agent_achievement_milestone (
                achievement_id, display_name, category, metric, threshold, summary
            )
            VALUES (
                {_sql(row[0])}, {_sql(row[1])}, {_sql(row[2])},
                {_sql(row[3])}, {row[4]}, {_sql(row[5])}
            )
            ON CONFLICT (achievement_id) DO UPDATE
                SET display_name = excluded.display_name,
                    category = excluded.category,
                    metric = excluded.metric,
                    threshold = excluded.threshold,
                    summary = excluded.summary
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_achievement_milestone_metric")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_agent_achievement_milestone_category")
    bind.exec_driver_sql("DROP TABLE IF EXISTS agent_achievement_milestone")


def _sql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
