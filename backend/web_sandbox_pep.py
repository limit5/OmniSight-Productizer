"""W14.8 — Policy-Enforcement-Point HOLD before the first web-preview launch.

The W14.2 ``POST /web-sandbox/preview`` endpoint cold-launches a sidecar
container, bind-mounts the operator's checked-out source, and runs
``pnpm install`` followed by ``pnpm dev``. On a fresh workspace the
install step pulls **50–500 MB** of node_modules and typically blocks
**30–90s** before the dev server is reachable — non-trivial host
resources committed to a single click. W14.8 wires a PEP HOLD before
the first launch so the operator sees the cost upfront and can approve
or reject before docker fires.

Why this is its own module
==========================

Three reasons:

1. **Single responsibility** — :mod:`backend.web_sandbox` is the
   launcher / lifecycle module; :mod:`backend.cf_ingress` is the
   tunnel-rule splicer; :mod:`backend.cf_access` is the SSO-app
   manager. Splitting the PEP gate into its own module keeps each
   surface small and testable in isolation.
2. **Pure pre-launch** — the gate only runs *before* docker is
   touched. A separate module makes the boundary explicit and the
   call-site in the router obvious.
3. **Mirrors the installer pattern** — the installer
   (:mod:`backend.routers.installer`) follows the same "tool name +
   tier_unlisted HOLD + arguments dict" shape. Keeping the W14
   variant as a sibling helper module rather than baking it into
   the router makes the call sites symmetric and discoverable.

Row boundary
============

W14.8 owns:

  1. The ``WEB_PREVIEW_PEP_TOOL`` tool name (``web_sandbox_preview``)
     — never on any tier whitelist, so :func:`pep_gateway.classify`
     returns HOLD via the ``tier_unlisted`` rule for every call.
  2. The ``build_pep_arguments`` helper — turns
     ``WebSandboxConfig`` plus operator metadata into the dict shape
     :func:`pep_gateway._build_web_preview_coaching` interpolates
     into the 4-line toast card.
  3. The ``requires_first_preview_hold`` predicate — pure helper
     that distinguishes a cold-launch (no manager entry, terminal
     entry) from an idempotent re-launch (live entry).
  4. :func:`evaluate_first_preview_hold` — async wrapper around
     :func:`pep_gateway.evaluate` that returns a simple
     :class:`WebPreviewPepResult` (action ∈ approved / rejected /
     gateway_error).

W14.8 explicitly does NOT own:

  - The actual sidecar launch (W14.2 :class:`WebSandboxManager`).
  - CF Tunnel ingress / CF Access SSO (W14.3 / W14.4).
  - Idle-timeout reaper (W14.5).
  - Frontend ``<LivePreviewPanel/>`` (W14.6).
  - HMR WebSocket passthrough (W14.7).
  - cgroup resource limits (W14.9).
  - Alembic 0059 ``web_sandbox_instances`` table (W14.10).

Module-global state audit (SOP §1)
==================================

This module owns **no module-level mutable state** — every public
helper is either a pure function or a thin async wrapper around
:func:`pep_gateway.evaluate`. The PEP gateway itself owns its own
breaker / held-registry / recent-decisions ring (already SOP-audited
in :mod:`backend.pep_gateway`). Cross-worker consistency for the
HOLD decision is therefore answer **#2** (PG / decision-engine
coordination): the proposal id lives in :mod:`backend.decision_engine`
which is PG-backed. Every uvicorn worker can resolve the same
``decision_id`` through ``decision_engine.get(...)`` so two workers
that race the same first-preview launch both observe the same HOLD
outcome.

Read-after-write timing audit (SOP §2)
======================================

N/A — no DB pool changes, no compat→pool migration, no read-after-write
race surface inside this module. The HOLD itself is serialised by the
decision-engine (PG-backed); two concurrent ``launch_preview`` calls
for the same ``workspace_id`` race the docker daemon's name-conflict
detection (W14.2 already documents this — the loser falls into
idempotent recovery via ``inspect``). The PEP HOLD runs before docker
is touched so the cold-launch race is intentional: each request gets
its own HOLD, the loser observes the existing instance and short-
circuits via :func:`requires_first_preview_hold` returning ``False``.

Compat fingerprint grep (SOP §3)
================================

Pure-Python module; uses :mod:`backend.pep_gateway` (already audited)
plus stdlib. Verified clean via the SOP §3 grep before commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from backend import pep_gateway as _pep

logger = logging.getLogger(__name__)


__all__ = [
    "WEB_PREVIEW_PEP_SCHEMA_VERSION",
    "WEB_PREVIEW_PEP_TOOL",
    "WEB_PREVIEW_PEP_HOLD_TIMEOUT_S",
    "WEB_PREVIEW_PEP_TIER",
    "WEB_PREVIEW_PEP_AGENT_ID_PREFIX",
    "DEFAULT_NPM_INSTALL_SIZE_TEXT",
    "DEFAULT_INSTALL_ETA_TEXT",
    "WebPreviewPepError",
    "WebPreviewPepResult",
    "build_pep_arguments",
    "format_size_estimate_text",
    "format_eta_text",
    "requires_first_preview_hold",
    "evaluate_first_preview_hold",
]


#: Bump when :func:`build_pep_arguments` / :class:`WebPreviewPepResult`
#: shape changes — kept independent of W14.2's
#: :data:`backend.web_sandbox.WEB_SANDBOX_SCHEMA_VERSION` because a PEP
#: shape bump does not require a sandbox-manager schema bump.
WEB_PREVIEW_PEP_SCHEMA_VERSION: str = "1.0.0"

#: PEP tool identifier for first-preview launches. Not on any tier
#: whitelist (T1/T2/T3) by design — every cold launch lands in
#: ``tier_unlisted`` HOLD via :func:`pep_gateway.classify`. The string
#: is referenced by the W14.8 coaching-card lookup
#: (:func:`pep_gateway._build_web_preview_coaching`) when the toast
#: renders.
WEB_PREVIEW_PEP_TOOL: str = "web_sandbox_preview"

#: HOLD timeout — caps how long the launch endpoint will block waiting
#: for an operator decision. The ``pep_gateway.evaluate`` default is
#: 30 min but a running HTTP request blocking 30 min would clog the
#: uvicorn worker pool. 600s (10 min) mirrors the BS.7 installer's
#: ceiling — long enough for a distracted operator to come back to the
#: tab, short enough to keep the request from monopolising a worker.
WEB_PREVIEW_PEP_HOLD_TIMEOUT_S: float = 600.0

#: Tier reported to the PEP gateway. We launch as ``t1`` (the most
#: restrictive) so even a well-meaning future change to the tier
#: whitelist cannot accidentally auto-approve a first-preview launch.
WEB_PREVIEW_PEP_TIER: str = "t1"

#: Prefix the router prepends to the operator's email when constructing
#: the ``agent_id`` for ``pep_gateway.evaluate``. Mirrors the installer
#: convention so audit rows are filterable on
#: ``actor LIKE 'operator:%'``.
WEB_PREVIEW_PEP_AGENT_ID_PREFIX: str = "operator:"

#: Default human copy interpolated into the coaching card's "why" line
#: when the caller does not override. Pinned to the W14 row spec
#: (50–500 MB / 30–90s) so a future row that wants to widen the
#: estimate can pass overrides via :func:`build_pep_arguments` without
#: touching this module.
DEFAULT_NPM_INSTALL_SIZE_TEXT: str = "50–500 MB"
DEFAULT_INSTALL_ETA_TEXT: str = "30–90s"


class WebPreviewPepError(Exception):
    """Base class for W14.8 PEP errors raised by this module.

    Wrappers around :func:`pep_gateway.evaluate` re-raise the underlying
    gateway error as :class:`WebPreviewPepError` so callers can ``except
    WebPreviewPepError`` once instead of having to know about every
    transitive PEP / decision-engine error class.
    """


@dataclass(frozen=True)
class WebPreviewPepResult:
    """Outcome of a single W14.8 first-preview HOLD evaluation.

    Designed to mirror the inputs the router needs without leaking the
    full :class:`pep_gateway.PepDecision` (decision_id is the only
    field a caller normally wants for audit / response shaping).

    ``action`` ∈ ``"approved"`` | ``"rejected"`` | ``"gateway_error"``.

    * ``approved`` — operator clicked APPROVE; caller proceeds to
      :meth:`backend.web_sandbox.WebSandboxManager.launch`.
    * ``rejected`` — operator clicked REJECT, the HOLD timed out, or
      the PEP circuit breaker is open. Caller surfaces 403 to the
      frontend and does **not** touch docker.
    * ``gateway_error`` — :func:`pep_gateway.evaluate` raised
      synchronously (very rare — gateway propose / wait failures
      already get translated into ``rejected`` by the gateway's
      circuit breaker). Caller surfaces 503 so the operator can
      retry. ``reason`` carries the exception class name.
    """

    action: str
    reason: str = ""
    decision_id: str | None = None
    rule: str = ""
    degraded: bool = False
    schema_version: str = WEB_PREVIEW_PEP_SCHEMA_VERSION

    @property
    def is_approved(self) -> bool:
        return self.action == "approved"

    @property
    def is_rejected(self) -> bool:
        return self.action == "rejected"

    @property
    def is_error(self) -> bool:
        return self.action == "gateway_error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action": self.action,
            "reason": self.reason,
            "decision_id": self.decision_id,
            "rule": self.rule,
            "degraded": self.degraded,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def format_size_estimate_text(low_mb: int | None, high_mb: int | None) -> str:
    """Format a human "low–high MB" range.

    Both ``None`` ⇒ :data:`DEFAULT_NPM_INSTALL_SIZE_TEXT`. One ``None``
    ⇒ render the other side as a fixed estimate (``"~120 MB"``). Any
    non-positive value is treated as ``None``.
    """

    def _coerce(v: int | None) -> int | None:
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    lo = _coerce(low_mb)
    hi = _coerce(high_mb)
    if lo is None and hi is None:
        return DEFAULT_NPM_INSTALL_SIZE_TEXT
    if lo is None and hi is not None:
        return f"~{hi} MB"
    if hi is None and lo is not None:
        return f"~{lo} MB"
    if lo == hi:
        return f"~{lo} MB"
    if lo > hi:  # type: ignore[operator]
        lo, hi = hi, lo
    return f"{lo}–{hi} MB"


def format_eta_text(low_seconds: int | None, high_seconds: int | None) -> str:
    """Format a human "low–high seconds" range, e.g. ``"30–90s"``.

    Same fall-through semantics as :func:`format_size_estimate_text`.
    """

    def _coerce(v: int | None) -> int | None:
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    lo = _coerce(low_seconds)
    hi = _coerce(high_seconds)
    if lo is None and hi is None:
        return DEFAULT_INSTALL_ETA_TEXT
    if lo is None and hi is not None:
        return f"~{hi}s"
    if hi is None and lo is not None:
        return f"~{lo}s"
    if lo == hi:
        return f"~{lo}s"
    if lo > hi:  # type: ignore[operator]
        lo, hi = hi, lo
    return f"{lo}–{hi}s"


def build_pep_arguments(
    *,
    workspace_id: str,
    workspace_path: str,
    image_tag: str,
    git_ref: str | None = None,
    container_port: int | None = None,
    actor_email: str | None = None,
    size_text: str | None = None,
    eta_text: str | None = None,
) -> dict[str, Any]:
    """Build the ``arguments`` dict passed to
    :func:`pep_gateway.evaluate`. The shape is consumed by
    :func:`pep_gateway._build_web_preview_coaching` to render the
    operator-facing toast card.

    Every required field is enforced as a non-empty string so a
    misconfigured caller surfaces a :class:`ValueError` here rather
    than a confusing KeyError downstream when the toast renders.
    """

    if not isinstance(workspace_id, str) or not workspace_id:
        raise ValueError("workspace_id must be a non-empty string")
    if not isinstance(workspace_path, str) or not workspace_path:
        raise ValueError("workspace_path must be a non-empty string")
    if not isinstance(image_tag, str) or not image_tag:
        raise ValueError("image_tag must be a non-empty string")
    if size_text is not None and not isinstance(size_text, str):
        raise ValueError("size_text must be a string or None")
    if eta_text is not None and not isinstance(eta_text, str):
        raise ValueError("eta_text must be a string or None")
    args: dict[str, Any] = {
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
        "image_tag": image_tag,
        "size_estimate_text": size_text or DEFAULT_NPM_INSTALL_SIZE_TEXT,
        "eta_text": eta_text or DEFAULT_INSTALL_ETA_TEXT,
    }
    if git_ref:
        args["git_ref"] = git_ref
    if container_port is not None:
        args["container_port"] = int(container_port)
    if actor_email:
        args["actor"] = actor_email
    return args


def requires_first_preview_hold(
    manager_get: Callable[[str], Any],
    workspace_id: str,
    *,
    force: bool = False,
) -> bool:
    """Decide whether a HOLD is needed for ``workspace_id``.

    ``manager_get`` is normally :meth:`backend.web_sandbox.WebSandboxManager.get`
    — accepted as a callable so this helper can be unit-tested without
    instantiating a full manager. Returns ``True`` when:

      * ``force`` is set (caller always wants a HOLD), OR
      * ``manager_get(workspace_id)`` returns ``None`` (no instance), OR
      * the existing instance is in a terminal status (``stopped`` /
        ``failed``) — operator's previous launch ended, so the next one
        is again "first" and re-pays the cold-install cost.

    Returns ``False`` for a non-terminal instance (``pending`` /
    ``installing`` / ``running`` / ``stopping``) — that path will be
    handled by :class:`WebSandboxManager.launch`'s idempotent recovery
    and never re-runs ``pnpm install``.
    """

    if force:
        return True
    if not isinstance(workspace_id, str) or not workspace_id:
        raise ValueError("workspace_id must be a non-empty string")
    instance = manager_get(workspace_id)
    if instance is None:
        return True
    is_terminal = bool(getattr(instance, "is_terminal", False))
    return is_terminal


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async evaluate wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def evaluate_first_preview_hold(
    *,
    workspace_id: str,
    workspace_path: str,
    image_tag: str,
    actor_email: str | None = None,
    git_ref: str | None = None,
    container_port: int | None = None,
    size_text: str | None = None,
    eta_text: str | None = None,
    hold_timeout_s: float | None = None,
    propose_fn: Callable[..., Any] | None = None,
    wait_for_decision: Callable[[str, float], Awaitable[Any]] | None = None,
    arguments_extra: Mapping[str, Any] | None = None,
) -> WebPreviewPepResult:
    """Run the W14.8 PEP HOLD for a first-preview launch.

    Returns a :class:`WebPreviewPepResult` instead of raising on a
    rejected outcome — the router maps the result to an HTTP response
    code (approved → continue, rejected → 403, gateway_error → 503).

    ``propose_fn`` / ``wait_for_decision`` are forwarded to
    :func:`pep_gateway.evaluate` so callers (and tests) can inject
    fakes without monkey-patching the gateway module-globals.
    """

    args = build_pep_arguments(
        workspace_id=workspace_id,
        workspace_path=workspace_path,
        image_tag=image_tag,
        git_ref=git_ref,
        container_port=container_port,
        actor_email=actor_email,
        size_text=size_text,
        eta_text=eta_text,
    )
    if arguments_extra:
        for k, v in arguments_extra.items():
            if k in args:
                # Caller-supplied extras must not silently overwrite the
                # PEP-mandated keys — if the caller wants a different
                # value for ``size_estimate_text`` they pass ``size_text``.
                continue
            args[k] = v

    timeout = (
        WEB_PREVIEW_PEP_HOLD_TIMEOUT_S
        if hold_timeout_s is None
        else float(hold_timeout_s)
    )
    if timeout <= 0:
        raise ValueError("hold_timeout_s must be > 0")

    agent_id = (
        f"{WEB_PREVIEW_PEP_AGENT_ID_PREFIX}{actor_email}"
        if actor_email
        else WEB_PREVIEW_PEP_AGENT_ID_PREFIX.rstrip(":")
    )

    try:
        decision = await _pep.evaluate(
            tool=WEB_PREVIEW_PEP_TOOL,
            arguments=args,
            agent_id=agent_id,
            tier=WEB_PREVIEW_PEP_TIER,
            propose_fn=propose_fn,
            wait_for_decision=wait_for_decision,
            hold_timeout_s=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — gateway breaker handles
        logger.warning(
            "web_sandbox_pep: pep_gateway.evaluate raised for workspace %r: %s",
            workspace_id, exc,
        )
        return WebPreviewPepResult(
            action="gateway_error",
            reason=f"pep_gateway_error:{exc.__class__.__name__}",
        )

    if decision.action is _pep.PepAction.auto_allow:
        return WebPreviewPepResult(
            action="approved",
            reason=decision.reason or "operator approved",
            decision_id=decision.decision_id,
            rule=decision.rule or "",
            degraded=bool(decision.degraded),
        )
    # PepAction.deny / hold-collapsed-to-deny / circuit-open fallback
    return WebPreviewPepResult(
        action="rejected",
        reason=decision.reason or "operator rejected",
        decision_id=decision.decision_id,
        rule=decision.rule or "",
        degraded=bool(decision.degraded),
    )
