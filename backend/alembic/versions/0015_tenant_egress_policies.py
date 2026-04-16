"""M6: Per-tenant egress allowlist — policies + approval requests.

Adds two tables:

  * ``tenant_egress_policies`` — one row per tenant. Holds the JSON
    arrays of allowed hosts / CIDRs and the default action. The DB is
    the canonical source of truth; the host-side iptables/nftables
    installer reads from this table.
  * ``tenant_egress_requests`` — operator/viewer-filed allow-list
    additions awaiting admin approval. Approval mutates the policy
    row in-place; rejection just marks the request closed.

On upgrade we backfill ``t-default`` from the legacy
``OMNISIGHT_T1_EGRESS_ALLOW_HOSTS`` env var (CSV of host[:port]) and
the optional ``configs/t1_egress_allow_hosts.yaml`` file (top-level
``hosts:`` list), preserving the pre-M6 behaviour.

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-16
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = "t-default"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LEGACY_YAML = _PROJECT_ROOT / "configs" / "t1_egress_allow_hosts.yaml"


def _legacy_allow_hosts() -> list[str]:
    """Collect legacy hosts from env CSV + optional YAML file. The two
    sources are unioned (env wins on dupes) so the migration is safe to
    run on an operator who has either or both configured."""
    out: list[str] = []
    seen: set[str] = set()

    raw = (os.environ.get("OMNISIGHT_T1_EGRESS_ALLOW_HOSTS") or "").strip()
    for item in raw.split(","):
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)

    if _LEGACY_YAML.is_file():
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(_LEGACY_YAML.read_text()) or {}
            for h in data.get("hosts") or []:
                h = str(h).strip()
                if h and h not in seen:
                    seen.add(h)
                    out.append(h)
        except Exception as exc:  # pragma: no cover - YAML optional
            logger.warning("legacy egress YAML parse failed: %s", exc)
    return out


def upgrade() -> None:
    conn = op.get_bind()

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS tenant_egress_policies (
            tenant_id       TEXT PRIMARY KEY REFERENCES tenants(id),
            allowed_hosts   TEXT NOT NULL DEFAULT '[]',
            allowed_cidrs   TEXT NOT NULL DEFAULT '[]',
            default_action  TEXT NOT NULL DEFAULT 'deny',
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_by      TEXT NOT NULL DEFAULT 'system'
        )
        """
    )

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS tenant_egress_requests (
            id              TEXT PRIMARY KEY,
            tenant_id       TEXT NOT NULL REFERENCES tenants(id),
            requested_by    TEXT NOT NULL,
            kind            TEXT NOT NULL,            -- host | cidr
            value           TEXT NOT NULL,
            justification   TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
            decided_by      TEXT,
            decided_at      TEXT,
            decision_note   TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_egress_req_tenant "
        "ON tenant_egress_requests(tenant_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_egress_req_status "
        "ON tenant_egress_requests(status)"
    )

    legacy = _legacy_allow_hosts()
    if legacy:
        conn.exec_driver_sql(
            "INSERT OR REPLACE INTO tenant_egress_policies "
            "(tenant_id, allowed_hosts, allowed_cidrs, default_action, updated_by) "
            "VALUES (?, ?, ?, 'deny', 'legacy-migration')",
            (DEFAULT_TENANT_ID, json.dumps(legacy), "[]"),
        )
        logger.info(
            "M6 migration: backfilled %d legacy hosts to tenant %s",
            len(legacy), DEFAULT_TENANT_ID,
        )
    else:
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO tenant_egress_policies "
            "(tenant_id, allowed_hosts, allowed_cidrs, default_action) "
            "VALUES (?, '[]', '[]', 'deny')",
            (DEFAULT_TENANT_ID,),
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_egress_requests")
    op.execute("DROP TABLE IF EXISTS tenant_egress_policies")
