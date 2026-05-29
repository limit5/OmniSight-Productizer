"""Per-project product-source resolution (OP-1842 / P2.1 — camviewpro B2).

The SOURCE-side analog of :func:`backend.agents.delivery_target.resolve_delivery_target`
(which is the DELIVERY side, OP-1837 / 1B). Where the delivery resolver answers
"*where* does a built product get pushed", this resolver answers "*from which
external repo* — and at which pinned ref — is the product *sourced* before it is
built". It exists because OmniSight is integrating the operator's
``camviewpro-android`` as the canonical Android product base (the B2 model; see
``docs/operations/2026-05-29-camviewpro-integration-P2-design.md`` §1/§6).

v1 is config-driven. A project-keyed JSON map in ``settings.product_sources``
maps a JIRA project key to a product source — an external repo (``repo_url``),
its product ``tier``, an optional human-meaningful ``branch``, the mandatory
immutable ``pinned_ref`` the build is taken at, and a *reference* to the
``git_accounts`` row that carries the (read) clone credential
(``git_account_ref``). A project with no configured source resolves to ``None``
— the current in-repo build flow, byte-identical to the pre-OP-1842 behaviour
(back-compat).

This differs from the delivery resolver in two deliberate ways:

* it returns ``None`` (not a default object) when a project is unconfigured —
  there is no "default external source", the absence simply means "build from
  the in-repo tree as before"; and
* it is *fail-closed* on a present-but-malformed entry. A project that opts in
  signals intent to source from an external base, so a configured entry must be
  well-formed: a non-empty ``pinned_ref`` (a blind HEAD is rejected so builds
  stay reproducible/auditable), a recognised ``tier``, and a ``repo_url``.
  A malformed entry raises rather than silently degrading to the in-repo flow,
  which would mask the operator's intent.

Security / scope (v1, OP-1842): resolution + a credential resolver only. This
ticket is the RESOLVER ONLY — the runner clone+build wiring is P2.2 and the B2
PR flow is P2.4; neither is implemented here. Credentials are NEVER inlined — a
configured source holds only the git_accounts row id, and
:func:`resolve_product_source_credential` resolves the actual credential through
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

# Product tiers an external source may declare. Kept in sync with the
# camviewpro integration design (§6): consumer is the camviewpro default,
# medical/automotive are the regulated bases.
ALLOWED_TIERS = frozenset({"consumer", "medical", "automotive"})


class ProductSourceError(RuntimeError):
    """A project's configured product source is not usable.

    Raised when a project IS configured (an entry is present in
    ``settings.product_sources``) but the entry is malformed — a missing
    ``repo_url`` or ``pinned_ref`` (blind-HEAD rejection) or an unrecognised
    ``tier`` — and by :func:`resolve_product_source_credential` when the
    ``git_account_ref`` does not resolve to a ``git_accounts`` row. It is
    fail-closed: an opted-in project must never silently fall back to the
    in-repo flow, which would hide the operator's intent.
    """


@dataclass(frozen=True)
class ProductSource:
    """The external repo (and credential reference) a product is sourced from.

    ``repo_url``        full git clone URL of the external product base (e.g.
                        an SSH GitHub URL for ``camviewpro-android``).
    ``tier``            product tier — one of :data:`ALLOWED_TIERS`
                        (``consumer`` / ``medical`` / ``automotive``).
    ``branch``          human-meaningful branch the ``pinned_ref`` lives on, or
                        ``None`` when the config omits it. Informational — the
                        build is taken at ``pinned_ref``, not the branch tip.
    ``pinned_ref``      the immutable ref the build is sourced at (a commit SHA
                        or tag). Mandatory: a blind HEAD is rejected so a build
                        is reproducible/auditable.
    ``git_account_ref`` id of the ``git_accounts`` row carrying the clone
                        credential, or ``None`` for a source that needs none
                        (a public repo).
    """

    repo_url: str
    tier: str
    branch: Optional[str]
    pinned_ref: str
    git_account_ref: Optional[str]


def _load_product_sources() -> dict:
    """Parse the project-keyed product-source map from settings.

    A globally malformed payload (invalid JSON or a non-object) yields an empty
    map: a config typo that cannot be attributed to any one project must not
    crash resolution for projects that may not even use this feature — they
    fall back to the in-repo flow (``None``). A *present* entry that is itself
    malformed is handled fail-closed by :func:`resolve_product_source`.
    """
    raw = settings.product_sources
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("product_sources: invalid JSON — ignoring (in-repo flow)")
        return {}
    if not isinstance(data, dict):
        log.warning("product_sources: expected a JSON object — ignoring")
        return {}
    return data


def resolve_product_source(project_key: Optional[str]) -> Optional[ProductSource]:
    """Resolve the pinned product source for *project_key*.

    Returns the configured :class:`ProductSource` when *project_key* has a
    well-formed entry in ``settings.product_sources``, otherwise ``None`` —
    the current in-repo build flow (back-compat, the pre-OP-1842 behaviour).

    Raises :class:`ProductSourceError` (fail-closed) when an entry IS present
    but malformed: an empty ``repo_url``, an empty ``pinned_ref`` (a blind HEAD
    is rejected — builds must be reproducible/auditable), or a ``tier`` outside
    :data:`ALLOWED_TIERS`. Opting a project in is an explicit operator intent to
    build from an external base, so a broken entry surfaces rather than silently
    degrading to the in-repo tree.
    """
    mapping = _load_product_sources()
    entry = mapping.get(project_key) if project_key else None
    if not isinstance(entry, dict):
        return None

    repo_url = str(entry.get("repo_url") or "").strip()
    if not repo_url:
        raise ProductSourceError(
            f"product source for project {project_key!r} has no repo_url"
        )

    pinned_ref = str(entry.get("pinned_ref") or "").strip()
    if not pinned_ref:
        raise ProductSourceError(
            f"product source for project {project_key!r} has no pinned_ref; "
            "refusing a blind-HEAD checkout (builds must be reproducible/auditable)"
        )

    tier = str(entry.get("tier") or "").strip().lower()
    if tier not in ALLOWED_TIERS:
        raise ProductSourceError(
            f"product source for project {project_key!r} has invalid tier "
            f"{tier!r}; expected one of {sorted(ALLOWED_TIERS)}"
        )

    branch = str(entry.get("branch") or "").strip() or None
    git_account_ref = str(entry.get("git_account_ref") or "").strip() or None
    return ProductSource(
        repo_url=repo_url,
        tier=tier,
        branch=branch,
        pinned_ref=pinned_ref,
        git_account_ref=git_account_ref,
    )


async def resolve_product_source_credential(
    source: ProductSource,
    *,
    tenant_id: Optional[str] = None,
) -> dict:
    """Resolve the ``git_accounts`` row backing a configured product source.

    Reuses the existing git_accounts model via
    :func:`backend.git_credentials.pick_by_id` — NO new secret store, and the
    credential is never inlined in config. Raises :class:`ProductSourceError`
    (fail-closed) when the source has no ``git_account_ref`` or the referenced
    row does not exist for the tenant.

    The returned row is the canonical git_accounts dict — callers extract only
    the fields they need (e.g. ``ssh_key``) and MUST NOT log it.
    """
    if not source.git_account_ref:
        raise ProductSourceError(
            f"product source {source.repo_url!r} has no git_account_ref; "
            "cannot resolve a clone credential"
        )
    from backend import git_credentials

    row = await git_credentials.pick_by_id(
        source.git_account_ref, tenant_id=tenant_id
    )
    if row is None:
        raise ProductSourceError(
            f"git_accounts row {source.git_account_ref!r} not found for the "
            f"product source {source.repo_url!r}"
        )
    return row
