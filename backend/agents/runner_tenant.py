"""OP-1778 (1A.1) — runner tenant-label binding + per-tenant workspace.

This module is the single place where the runner turns a JIRA ticket's
``tenant:<tid>`` label into (a) a tenant id to bind into the DB/FS context
via :func:`backend.db_context.set_tenant_id` and (b) a per-tenant workspace
directory under :mod:`backend.tenant_fs`.

Design contract (design §5/§7/§12/§13):

* **Label decision (§12).** A ticket carries at most one ``tenant:<tid>``
  label. The matched ``<tid>`` is validated against the same id grammar
  :mod:`backend.tenant_fs` enforces for on-disk roots, so a label can never
  widen the filesystem namespace.
* **Back-compat default (§13).** A label-less (internal) ticket defaults to
  ``omnisight-self``. ``omnisight-self`` is *not* a customer tenant and is
  deliberately distinct from :data:`backend.tenant_fs._DEFAULT_TENANT`
  (``t-default``) so an internal ticket can never silently inherit a
  customer-tenant default. For ``omnisight-self`` the runner keeps using the
  existing per-agent-class sibling worktree — i.e. it behaves exactly as it
  did before this ticket.
* **Per-tenant workspace (§5/§7).** A customer tenant gets a freshly cloned
  workspace under ``data/tenants/<tid>/workspace`` with its **own** git dir
  and a **fully independent object store** — no ``git worktree`` gitlink and
  no ``objects/info/alternates`` bind back to the host repo (closes L8-fs).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from backend import tenant_fs

# The internal/self tenant. Label-less tickets default here (§13). Kept
# distinct from tenant_fs._DEFAULT_TENANT ("t-default") on purpose so an
# internal ticket never inherits a customer-tenant default.
OMNISIGHT_SELF_TENANT = "omnisight-self"

# JIRA label that binds a ticket to a tenant, e.g. ``tenant:t-foo``.
TENANT_LABEL_PREFIX = "tenant:"

# Subdirectory under the tenant data root that holds the cloned working tree.
_WORKSPACE_DIRNAME = "workspace"

# Mirrors tenant_fs._TID_RE — a label must not be able to escape the on-disk
# tenant namespace grammar.
_TID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class TenantLabelError(ValueError):
    """Raised when a ticket's ``tenant:`` label(s) are malformed or conflict."""


class TenantWorkspaceError(RuntimeError):
    """Raised when a per-tenant workspace cannot be allocated in isolation."""


def _validate_tid(tid: str) -> str:
    if not tid or not _TID_RE.match(tid):
        raise TenantLabelError(f"invalid tenant id in label: {tid!r}")
    return tid


def resolve_tenant_id(labels: Iterable[str]) -> str:
    """Resolve the tenant id from a ticket's labels.

    Scans *labels* for ``tenant:<tid>`` (prefix match is case-insensitive;
    the ``<tid>`` is preserved verbatim and validated). Returns the single
    matched, validated tenant id, or :data:`OMNISIGHT_SELF_TENANT` when no
    ``tenant:`` label is present (§13 back-compat).

    Raises :class:`TenantLabelError` when more than one *distinct* tenant is
    requested, or when the matched id is malformed.
    """
    tids: list[str] = []
    for raw in labels or ():
        label = (raw or "").strip()
        if label.lower().startswith(TENANT_LABEL_PREFIX):
            tids.append(label[len(TENANT_LABEL_PREFIX):].strip())

    distinct = sorted(set(tids))
    if not distinct:
        return OMNISIGHT_SELF_TENANT
    if len(distinct) > 1:
        raise TenantLabelError(
            f"conflicting tenant labels on one ticket: {distinct}"
        )
    return _validate_tid(distinct[0])


def is_self_tenant(tenant_id: str) -> bool:
    """True for the internal/self tenant (keeps the legacy worktree path)."""
    return tenant_id == OMNISIGHT_SELF_TENANT


def tenant_workspace_path(tenant_id: str) -> Path:
    """Per-tenant workspace dir: ``data/tenants/<tid>/workspace``.

    Reuses :func:`backend.tenant_fs.tenant_data_root` so the workspace lives
    inside the same physically isolated tenant namespace as the tenant's
    artifacts/backups/ingest roots.
    """
    return tenant_fs.tenant_data_root(tenant_id) / _WORKSPACE_DIRNAME


def build_clone_argv(source_repo: Path | str, dest: Path | str) -> list[str]:
    """Argv for a fully-isolated clone of *source_repo* into *dest*.

    ``--no-local`` forces git's object-transfer transport even for a local
    source path, so objects are **copied** rather than hardlinked or shared
    via ``objects/info/alternates``. That is what gives the tenant clone an
    independent object store (the L8-fs isolation contract); a plain local
    ``git clone`` would hardlink the host repo's objects.
    """
    return ["git", "clone", "--no-local", str(source_repo), str(dest)]


def assert_isolated_git_dir(workspace: Path) -> None:
    """Assert *workspace* has its own git dir and no shared object store.

    Fails closed (raises :class:`TenantWorkspaceError`) when the workspace is
    not a git repo, when ``.git`` is a gitlink file (the ``git worktree``
    pattern, which shares the host object store), or when an
    ``objects/info/alternates`` file binds the object store to another repo.
    """
    git_dir = workspace / ".git"
    if not git_dir.exists():
        raise TenantWorkspaceError(
            f"tenant workspace {workspace} is not a git repo (no .git)"
        )
    if git_dir.is_file():
        raise TenantWorkspaceError(
            f"tenant workspace {workspace}/.git is a gitlink (shared worktree); "
            "an isolated tenant clone must own its git dir"
        )
    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        raise TenantWorkspaceError(
            f"tenant workspace {workspace} shares an object store via "
            f"{alternates} (alternates bind); isolation contract violated"
        )


def allocate_tenant_workspace(
    tenant_id: str,
    *,
    source_repo: Path | str,
    self_workspace: Path | str,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    """Return the workspace the runner should operate in for *tenant_id*.

    * ``omnisight-self`` → *self_workspace* unchanged (legacy sibling
      worktree; behaves exactly as today, §13).
    * any customer tenant → a freshly cloned, object-store-isolated workspace
      under ``data/tenants/<tid>/workspace`` (§5/§7). Idempotent: an existing
      isolated clone is reused; non-repo cruft at the path is refused rather
      than clobbered.

    *run* is injectable so the clone can be exercised without a real
    subprocess in unit tests.
    """
    if is_self_tenant(tenant_id):
        return Path(self_workspace)

    dest = tenant_workspace_path(tenant_id)
    if (dest / ".git").exists():
        # Reuse an already-allocated tenant clone, but re-verify isolation —
        # a tampered/relocated git dir must not silently pass.
        assert_isolated_git_dir(dest)
        return dest
    if dest.exists() and any(dest.iterdir()):
        raise TenantWorkspaceError(
            f"tenant workspace {dest} exists but is not a git repo; refusing "
            "to clobber. Operator: remove the stale directory and re-launch."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    run(
        build_clone_argv(source_repo, dest),
        check=True,
        capture_output=True,
        text=True,
    )
    assert_isolated_git_dir(dest)
    return dest
