"""Per-ticket routed-repo resolution (OP-2192 / R.0 — multi-project runner routing).

The SOURCE+DELIVERY analog for *internal* Gerrit projects other than
``OmniSight-Productizer``. Where :mod:`backend.agents.delivery_target` routes by
JIRA *project key* and is deliberately fail-OPEN (a config typo falls back to the
productizer default), this resolver routes by a per-ticket ``repo:<name>`` LABEL
and is deliberately **fail-CLOSED**: once a ticket asserts ``repo:<name>``, an
unconfigured or malformed entry MUST raise rather than silently fall back to the
productizer repo — that silent fallback is exactly the mis-pickup this feature
exists to prevent (see
``docs/architecture/2026-06-14-multi-project-runner-routing-epic-design.md`` R.0).

v1 is config-driven. A JSON object in ``settings.routed_repos`` maps a routed
repo name → its destination::

    {"conference-appliance": {
        "gerrit_url": "ssh://claude-bot@sora.services:29418/omnisight/conference-appliance",
        "ref": "refs/for/develop",
        "git_account_ref": "...",        # reference to a git_accounts row (optional for shared-bot SSH)
        "context": "conference-appliance" # prompt-context selector (R.5); not the productizer-internal corpus
    }}

R.0 ships the config field + dataclass + resolver ONLY. No call site is wired
(that is R.1+). With no ``routed_repos`` config and no ``repo:`` labels in play,
this module is inert — zero behavior change for the existing runner fleet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from backend.config import settings

log = logging.getLogger(__name__)

#: Label prefix a ticket uses to assert which Gerrit project it targets.
ROUTED_REPO_LABEL_PREFIX = "repo:"


class RoutedRepoError(RuntimeError):
    """A ticket asserts ``repo:<name>`` but it cannot be turned into a usable
    routed repo.

    Raised (fail-closed) when:
      * ``settings.routed_repos`` is malformed (invalid JSON / non-object), or
      * the asserted ``<name>`` has no entry, or
      * the entry is not an object, or
      * the entry has no non-empty ``gerrit_url``.

    The caller (R.1 dispatch pre-gate) turns this into an abstain +
    ``runner-blocked:repo-unresolved`` — it MUST NOT fall back to the
    productizer repo.
    """


@dataclass(frozen=True)
class RoutedRepo:
    """A resolved per-ticket routing destination.

    ``name``            the ``repo:<name>`` value (e.g. ``conference-appliance``).
    ``gerrit_url``      full SSH Gerrit URL to clone/fetch/push.
    ``ref``             push ref — the right-hand side of ``HEAD:<ref>``
                        (default ``refs/for/develop``, a Gerrit review queue).
    ``git_account_ref`` id of the ``git_accounts`` row carrying the push
                        credential, or ``None`` to use the shared-bot SSH key
                        (resolved at push time, never inlined here).
    ``context``         prompt-context selector for this repo (R.5); ``None``
                        means "decide at wiring time" — never silently the
                        productizer-internal corpus.
    """

    name: str
    gerrit_url: str
    ref: str
    git_account_ref: Optional[str]
    context: Optional[str]


def routed_repo_label_value(labels: Iterable[str]) -> Optional[str]:
    """Return the single ``repo:<name>`` value asserted by *labels*, or ``None``.

    Case-insensitive on the prefix; the ``<name>`` is preserved verbatim and
    stripped. Raises :class:`RoutedRepoError` if more than one *distinct*
    ``repo:`` label is present (an ambiguous ticket must not silently pick one).
    """
    names: list[str] = []
    for raw in labels or ():
        label = (raw or "").strip()
        if label.lower().startswith(ROUTED_REPO_LABEL_PREFIX):
            names.append(label[len(ROUTED_REPO_LABEL_PREFIX):].strip())
    distinct = sorted({n for n in names if n})
    if not distinct:
        return None
    if len(distinct) > 1:
        raise RoutedRepoError(
            f"conflicting repo: labels on one ticket: {distinct}"
        )
    return distinct[0]


def _load_routed_repos() -> dict:
    """Parse the routed-repo map from settings.

    Unlike :func:`delivery_target._load_delivery_targets`, a malformed payload
    here is NOT silently coerced to an empty map *for a ticket that asserts a
    repo:* — but parsing itself is separated from resolution so the no-label
    case (the common path) never raises. Returns ``{}`` for empty config and
    raises :class:`RoutedRepoError` for malformed config so resolution can fail
    closed.
    """
    raw = settings.routed_repos
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RoutedRepoError(f"routed_repos: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RoutedRepoError("routed_repos: expected a JSON object")
    return data


def resolve_routed_repo(labels: Iterable[str]) -> Optional[RoutedRepo]:
    """Resolve the routed repo for a ticket's *labels*.

    * No ``repo:`` label  → ``None`` (the caller runs the normal
      productizer path — unchanged; the safe floor).
    * ``repo:<name>`` present AND resolves → the :class:`RoutedRepo`.
    * ``repo:<name>`` present AND unconfigured/malformed → raises
      :class:`RoutedRepoError` (**fail-closed**; never the productizer default).
    """
    name = routed_repo_label_value(labels)
    if name is None:
        return None

    mapping = _load_routed_repos()
    entry = mapping.get(name)
    if not isinstance(entry, dict):
        raise RoutedRepoError(
            f"repo:{name} has no routed_repos entry (or it is not an object); "
            "refusing to fall back to the productizer repo"
        )

    gerrit_url = str(entry.get("gerrit_url") or "").strip()
    if not gerrit_url:
        raise RoutedRepoError(
            f"repo:{name} routed_repos entry has no gerrit_url; "
            "refusing to fall back to the productizer repo"
        )

    ref = str(entry.get("ref") or "").strip() or "refs/for/develop"
    git_account_ref = str(entry.get("git_account_ref") or "").strip() or None
    context = str(entry.get("context") or "").strip() or None
    return RoutedRepo(
        name=name,
        gerrit_url=gerrit_url,
        ref=ref,
        git_account_ref=git_account_ref,
        context=context,
    )
