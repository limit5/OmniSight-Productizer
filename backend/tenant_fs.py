"""I5 — Tenant-scoped filesystem namespace.

Provides per-tenant directory roots for artifacts, ingest cache, backups,
and workflow run outputs.  All write-path functions in the backend should
resolve directories through this module so that tenant data is physically
isolated on disk.

Layout::

    data/tenants/<tid>/artifacts/
    data/tenants/<tid>/backups/
    data/tenants/<tid>/workflow_runs/
    /tmp/omnisight_ingest/<tid>/
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from backend.db_context import current_tenant_id

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_ROOT = _PROJECT_ROOT / "data"
_TENANTS_ROOT = _DATA_ROOT / "tenants"
_INGEST_BASE = Path(tempfile.gettempdir()) / "omnisight_ingest"

_TID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_DEFAULT_TENANT = "t-default"


def _validate_tid(tid: str) -> str:
    if not tid or not _TID_RE.match(tid):
        raise ValueError(f"Invalid tenant id: {tid!r}")
    return tid


def _resolve_tid(tenant_id: str | None = None) -> str:
    tid = tenant_id or current_tenant_id() or _DEFAULT_TENANT
    return _validate_tid(tid)


def tenants_root() -> Path:
    return _TENANTS_ROOT


def tenant_data_root(tenant_id: str | None = None) -> Path:
    tid = _resolve_tid(tenant_id)
    p = _TENANTS_ROOT / tid
    p.mkdir(parents=True, exist_ok=True)
    return p


def tenant_artifacts_root(tenant_id: str | None = None) -> Path:
    p = tenant_data_root(tenant_id) / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tenant_ingest_root(tenant_id: str | None = None) -> Path:
    tid = _resolve_tid(tenant_id)
    p = _INGEST_BASE / tid
    p.mkdir(parents=True, exist_ok=True)
    return p


def tenant_backups_root(tenant_id: str | None = None) -> Path:
    p = tenant_data_root(tenant_id) / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tenant_workflow_runs_root(tenant_id: str | None = None) -> Path:
    p = tenant_data_root(tenant_id) / "workflow_runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_tenant_dirs(tenant_id: str | None = None) -> Path:
    """Create all standard subdirectories for a tenant. Returns the tenant data root."""
    root = tenant_data_root(tenant_id)
    for sub in ("artifacts", "backups", "workflow_runs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    tid = _resolve_tid(tenant_id)
    (_INGEST_BASE / tid).mkdir(parents=True, exist_ok=True)
    return root


def path_belongs_to_tenant(path: Path, tenant_id: str | None = None) -> bool:
    """Check whether *path* is inside the given tenant's data directory."""
    tid = _resolve_tid(tenant_id)
    tenant_root = (_TENANTS_ROOT / tid).resolve()
    try:
        path.resolve().relative_to(tenant_root)
        return True
    except ValueError:
        return False
