"""Per-project delivery-target resolution (OP-1837 / 1B v1).

Resolves *where* a built product is delivered (pushed) on a per-JIRA-project
basis, closing the audit's delivery gap (see
``docs/product/2026-05-28-case-1-2-4-delivery-decomposition.md`` —
"Shared platform"): until now the runner could push only to the hardcoded
OmniSight Gerrit repo and so could not deliver to a customer's repo.

v1 is config-driven. A project-keyed JSON map in ``settings.delivery_targets``
maps a JIRA project key to a delivery destination — a repo (``repo_url``),
a push ref (``ref_spec``) and a *reference* to the ``git_accounts`` row that
carries the push credential (``git_account_ref``). A project with no
configured target resolves to the OmniSight Gerrit review target, which is
byte-identical to the pre-OP-1837 hardcoded push path (back-compat).

Security / scope (v1): resolution + a credential resolver only. Credentials
are NEVER inlined here — a configured target holds only the git_accounts row
id, and :func:`resolve_delivery_credential` resolves the actual key through
:mod:`backend.git_credentials` (the existing git_accounts model). Resolved
credentials are never logged. No DB migration, no UI, no new secret store —
those are follow-ons.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from backend.config import settings

log = logging.getLogger(__name__)


class DeliveryTargetError(RuntimeError):
    """A configured delivery target could not be turned into a usable push.

    Raised by :func:`resolve_delivery_credential` when a per-project target
    is configured but its ``git_account_ref`` does not resolve to a
    ``git_accounts`` row (fail-closed — the push must never silently fall
    back to the OmniSight bot identity for a customer delivery).
    """


@dataclass(frozen=True)
class DeliveryTarget:
    """Where (and with which credential) a built product is delivered.

    ``repo_url``        full git push URL (e.g. an SSH Gerrit / GitHub URL).
    ``ref_spec``        destination ref — the right-hand side of
                        ``HEAD:<ref>`` (``refs/for/develop`` for a Gerrit
                        review queue, ``refs/heads/main`` for a branch push).
    ``git_account_ref`` id of the ``git_accounts`` row carrying the push
                        credential, or ``None`` for the default target
                        (which uses the OmniSight bot identity).
    ``is_default``      True for the back-compat OmniSight target (the
                        project has no configured override).
    ``project_key``     the JIRA project key this was resolved for (or None).
    """

    repo_url: str
    ref_spec: str
    git_account_ref: Optional[str]
    is_default: bool
    project_key: Optional[str] = None


def _omnisight_default_target(
    target: str, project_key: Optional[str]
) -> DeliveryTarget:
    """Build the back-compat OmniSight Gerrit review target.

    The concrete bot SSH user is resolved at push time by
    ``jira_dispatch.resolve_gerrit_push_identity`` (the default push path is
    left untouched), so the URL here is the user-less canonical form sourced
    from the single source of truth in :mod:`backend.agents.jira_dispatch`.
    The import is lazy to avoid an import cycle (jira_dispatch imports this
    module at top level).
    """
    from backend.agents import jira_dispatch

    repo_url = (
        f"ssh://{jira_dispatch.GERRIT_SSH_HOST}:{jira_dispatch.GERRIT_SSH_PORT}"
        f"/{jira_dispatch.GERRIT_PROJECT_PATH}"
    )
    return DeliveryTarget(
        repo_url=repo_url,
        ref_spec=f"refs/for/{target}",
        git_account_ref=None,
        is_default=True,
        project_key=project_key,
    )


def _load_delivery_targets() -> dict:
    """Parse the project-keyed delivery-target map from settings.

    A malformed payload (invalid JSON or a non-object) yields an empty map:
    a config typo must never crash the push path — it falls back to the
    OmniSight default instead.
    """
    raw = settings.delivery_targets
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("delivery_targets: invalid JSON — ignoring (using defaults)")
        return {}
    if not isinstance(data, dict):
        log.warning("delivery_targets: expected a JSON object — ignoring")
        return {}
    return data


def resolve_delivery_target(
    project_key: Optional[str],
    *,
    target: str = "develop",
) -> DeliveryTarget:
    """Resolve the delivery target for *project_key*.

    Returns the per-project target when one is configured in
    ``settings.delivery_targets`` (and carries a non-empty ``repo_url``),
    otherwise the OmniSight Gerrit default — back-compat, byte-identical to
    the pre-OP-1837 hardcoded push.

    *target* seeds the default ref (``refs/for/<target>``) and is the
    fallback ref for a configured entry that omits ``ref_spec``.
    """
    mapping = _load_delivery_targets()
    entry = mapping.get(project_key) if project_key else None
    if not isinstance(entry, dict):
        return _omnisight_default_target(target, project_key)

    repo_url = str(entry.get("repo_url") or "").strip()
    if not repo_url:
        # A config entry without a destination is not actionable — treat the
        # project as unconfigured so the push stays on the safe default.
        return _omnisight_default_target(target, project_key)

    ref_spec = str(entry.get("ref_spec") or "").strip() or f"refs/for/{target}"
    git_account_ref = str(entry.get("git_account_ref") or "").strip() or None
    return DeliveryTarget(
        repo_url=repo_url,
        ref_spec=ref_spec,
        git_account_ref=git_account_ref,
        is_default=False,
        project_key=project_key,
    )


async def resolve_delivery_credential(
    target: DeliveryTarget,
    *,
    tenant_id: Optional[str] = None,
) -> dict:
    """Resolve the ``git_accounts`` row backing a configured delivery target.

    Reuses the existing git_accounts model via
    :func:`backend.git_credentials.pick_by_id` — NO new secret store, and the
    credential is never inlined in config. Raises :class:`DeliveryTargetError`
    (fail-closed) when the target has no ``git_account_ref`` or the referenced
    row does not exist for the tenant; the push then refuses rather than
    borrowing the OmniSight bot identity.

    The returned row is the canonical git_accounts dict — callers extract only
    the fields they need (e.g. ``ssh_key``) and MUST NOT log it.
    """
    if target.is_default:
        raise DeliveryTargetError(
            "the OmniSight default target uses the bot identity, not a "
            "git_accounts row"
        )
    if not target.git_account_ref:
        raise DeliveryTargetError(
            f"delivery target for project {target.project_key!r} has no "
            "git_account_ref; cannot resolve a push credential"
        )
    from backend import git_credentials

    row = await git_credentials.pick_by_id(
        target.git_account_ref, tenant_id=tenant_id
    )
    if row is None:
        raise DeliveryTargetError(
            f"git_accounts row {target.git_account_ref!r} not found for the "
            f"delivery target of project {target.project_key!r}"
        )
    return row
