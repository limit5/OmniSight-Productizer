"""AB.5.1 — external_tool_registry table.

Registers external tools that OmniSight agent batches can invoke
(MCP servers, subprocess CLIs, Docker sidecars, Python libs).
Operator-configured: which Docker images / image tags / REST URLs /
binary paths are actually deployed in this environment.

Code-side tool *handlers* are static (defined in
``backend/agents/external_tool_registry.py``); this table tracks the
*deployment binding* — what URL / command / image each handler should
talk to in this OmniSight instance.

Key fields:

  * ``tool_name`` — matches the ToolSchema name in
    ``backend/agents/tool_schemas.py`` (the contract surface visible
    to Anthropic via tools=[]).
  * ``integration_type`` — selects which handler class wraps it:
    ``python_lib`` / ``subprocess`` / ``docker_mcp`` / ``docker_sidecar``.
  * ``license_tier`` — boundary marker, mirrors the four tiers in
    ``docs/legal/oss-boundaries.md``: ``mit_apache_bsd`` (direct link),
    ``lgpl`` (dynamic link), ``gpl`` (subprocess only),
    ``agpl`` (Docker sidecar REST only), ``inspiration_only`` (pattern
    only, no source — Warp). The dispatcher refuses to invoke a
    GPL/AGPL tool whose ``sandbox_required=true`` flag isn't honored
    by its handler config.
  * ``sandbox_required`` — true for any tool that processes raw
    customer schematic / IP-sensitive payload (R51 sandbox escape).
  * ``config`` JSONB — handler-specific binding: e.g.
    ``{"docker_image": "ghcr.io/mixelpixx/kicad-mcp:9.0", "stdio": true}``
    or ``{"command": ["perl", "third_party/altium2kicad/altium2kicad.pl"]}``.
  * ``enabled`` — operator kill-switch; disabled tools never get
    handed to Anthropic in the tools=[] payload.
  * ``health_status`` + ``last_health_check`` — populated by the
    AB.5 health probe loop; stale > 5 min triggers re-check on next
    invocation.

Revision ID: 0184
Revises: 0181
Create Date: 2026-05-02
"""
from __future__ import annotations

from alembic import op


revision = "0184"
down_revision = "0181"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS external_tool_registry (
                tool_name TEXT PRIMARY KEY,
                integration_type TEXT NOT NULL,
                license_tier TEXT NOT NULL,
                sandbox_required BOOLEAN NOT NULL,
                config JSONB,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                deployed_at TIMESTAMPTZ,
                last_health_check TIMESTAMPTZ,
                health_status TEXT,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_external_tool_registry_enabled "
            "ON external_tool_registry(enabled) WHERE enabled = TRUE"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_external_tool_registry_health "
            "ON external_tool_registry(health_status, last_health_check)"
        )
    else:
        bind.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS external_tool_registry (
                tool_name TEXT PRIMARY KEY,
                integration_type TEXT NOT NULL,
                license_tier TEXT NOT NULL,
                sandbox_required INTEGER NOT NULL,
                config TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                deployed_at TEXT,
                last_health_check TEXT,
                health_status TEXT,
                description TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_external_tool_registry_enabled "
            "ON external_tool_registry(enabled)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_external_tool_registry_health "
            "ON external_tool_registry(health_status, last_health_check)"
        )


def downgrade() -> None:
    # alembic-allow-noop-downgrade: dropping the external_tool_registry
    # would orphan every deployed handler configuration and break the
    # already-active firewall reference. Hand-rolled migration required
    # for rollback (see FX.7.6 contract).
    pass
