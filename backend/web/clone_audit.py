"""W11.12 — Audit log row per clone (failure path).

This module is the **failure-path** complement to the two audit emitters
that already live alongside the W11 pipeline:

================================  ==============================  ==================================
Emitter                           Action                          Owner / row
================================  ==============================  ==================================
``record_clone_audit``            ``web.clone``                   W11.7 (success path)
``record_clone_rate_limit_hold``  ``web.clone.rate_limited``      W11.8 (L5 PEP HOLD)
``record_clone_attempt_failure``  ``web.clone.failed``            W11.12 (everything else)
================================  ==============================  ==================================

Every other failure mode of the 5-layer pipeline (L1 robots/noai/CF
refusal → L2 risk threshold → L3 bytes-leak / rewrite-unavailable →
L4 manifest schema/write → W11.9 framework render error → W11.10
context error → W11.1/W11.2 capture-stage errors) lacked a dedicated
audit row before this module landed. ``record_clone_attempt_failure``
fills that gap by:

1. Classifying any caught exception against the full
   :class:`~backend.web.site_cloner.SiteClonerError` subclass tree via
   :func:`classify_clone_failure` (pure function, no I/O).
2. Building a stable :class:`CloneAttemptRecord` carrying tenant-scoped
   identifiers + the error metadata the audit replay UI needs.
3. Forwarding to :func:`backend.audit.log` (lazy import, mirrors the
   W11.7 / W11.8 lazy-import discipline so the module imports cleanly
   in unit-test environments where the audit subsystem isn't
   initialised).

**Best-effort contract**: like W11.7 and W11.8, this emitter never
raises on audit-subsystem unreachable. It returns the new row id on
success and ``None`` on best-effort failure (logged at warning). The
calling router must NOT swallow the original ``SiteClonerError`` —
this row records the failure so the chain captures it; the router's
own exception-translation layer remains responsible for shaping the
HTTP / SSE response.

**Module-global state audit (SOP §1)**: this module holds only
immutable string / tuple / int constants + ``logger``. No mutable
globals. Cross-worker consistency is answer #1 (every worker derives
the same constants from the same source). Audit chain serialisation
itself is handled by ``backend.audit.log`` via per-tenant
``pg_advisory_xact_lock`` (already audited in I8 / Phase 53).

Inspired by firecrawl/open-lovable (MIT). Attribution lands in W11.13
(``LICENSES/open-lovable-mit.txt``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Tuple

from backend.web.clone_manifest import (
    CloneManifest,
    CloneManifestError,
    ManifestSchemaError,
    ManifestWriteError,
)
from backend.web.clone_rate_limit import (
    CloneRateLimitedError,
    CloneRateLimitError,
)
from backend.web.clone_spec_context import CloneSpecContextError
from backend.web.content_classifier import (
    ClassifierUnavailableError,
    ContentClassifierError,
    ContentRiskError,
)
from backend.web.framework_adapter import (
    FrameworkAdapterError,
    RenderedProjectWriteError,
    UnknownFrameworkError,
)
from backend.web.firecrawl_source import (
    FirecrawlConfigError,
    FirecrawlDependencyError,
)
from backend.web.output_transformer import (
    BytesLeakError,
    OutputTransformerError,
    RewriteUnavailableError,
)
from backend.web.playwright_source import (
    PlaywrightConfigError,
    PlaywrightDependencyError,
)
from backend.web.refusal_signals import MachineRefusedError
from backend.web.site_cloner import (
    BlockedDestinationError,
    CloneCaptureTimeoutError,
    CloneSourceError,
    CloneSpecBuildError,
    InvalidCloneURLError,
    SiteClonerError,
)


logger = logging.getLogger(__name__)


# ── Module constants ─────────────────────────────────────────────────────


#: Stable audit-log ``action`` field for every failure-path row this
#: module emits. Lives in the ``web.clone.*`` namespace alongside the
#: existing W11.7 ``web.clone`` and W11.8 ``web.clone.rate_limited`` so a
#: prefix-filter ``WHERE action LIKE 'web.clone.%'`` catches the full
#: clone lifecycle in one query. Operators slice success vs. failure by
#: matching ``action`` exactly.
CLONE_ATTEMPT_FAILED_AUDIT_ACTION: str = "web.clone.failed"

#: Stable audit-log ``entity_kind`` value for failure-path rows.
#: Distinct from W11.7 (``web_clone``) and W11.8
#: (``web_clone_rate_limit``) so a kind-scoped query targeting only
#: failed attempts doesn't catch successful clones.
CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND: str = "web_clone_attempt"

#: Hard cap on the ``failure_message`` field carried in the audit row's
#: ``after`` payload. Prevents a hostile / oversized exception message
#: from bloating the chain. Long messages get truncated with an ellipsis
#: so the operator still sees the leading context.
MAX_FAILURE_MESSAGE_CHARS: int = 1_000

#: Hard cap on the number of refusal / risk / rate-limit reason strings
#: carried in the audit row. Same rationale as
#: :data:`MAX_FAILURE_MESSAGE_CHARS`.
MAX_FAILURE_REASONS: int = 16

#: All failure-category identifiers this module emits via
#: :func:`classify_clone_failure`. Pinned as a tuple so the test suite
#: can parametrize over the full set and the audit-replay UI can
#: enumerate the legend without reading source.
#:
#: Categories (ordered by 5-layer pipeline progression then catch-alls):
#:
#: - ``invalid_url`` / ``blocked_destination``     — W11.1 syntactic / SSRF gate
#: - ``backend_config`` / ``backend_dependency``   — W11.2 backend selection
#: - ``capture_timeout`` / ``capture_error``       — W11.1 capture stage
#: - ``spec_build``                                — W11.1 / W11.3 spec assembly
#: - ``machine_refused``                           — W11.4 L1 robots / noai / CF
#: - ``classifier_unavailable`` /                  — W11.5 L2 LLM classifier
#:   ``risk_blocked`` / ``classifier_error``
#: - ``bytes_leak`` / ``rewrite_unavailable`` /    — W11.6 L3 transformer
#:   ``transformer_error``
#: - ``manifest_schema`` / ``manifest_write`` /    — W11.7 L4 manifest
#:   ``manifest_error``
#: - ``rate_limited`` / ``rate_limit_error``       — W11.8 L5 rate limit
#: - ``framework_unknown`` / ``framework_write`` / — W11.9 multi-framework
#:   ``framework_error``
#: - ``context_error``                             — W11.10 prompt-context block
#: - ``site_cloner_error``                         — SiteClonerError fallback
#:                                                   (e.g. UnknownCloneBackendError —
#:                                                   it lives in ``backend.web.__init__``
#:                                                   so wiring it directly would create
#:                                                   a circular import)
#: - ``unclassified``                              — non-SiteClonerError catch-all
CLONE_FAILURE_CATEGORIES: Tuple[str, ...] = (
    "invalid_url",
    "blocked_destination",
    "backend_config",
    "backend_dependency",
    "capture_timeout",
    "capture_error",
    "spec_build",
    "machine_refused",
    "classifier_unavailable",
    "risk_blocked",
    "classifier_error",
    "bytes_leak",
    "rewrite_unavailable",
    "transformer_error",
    "manifest_schema",
    "manifest_write",
    "manifest_error",
    "rate_limited",
    "rate_limit_error",
    "framework_unknown",
    "framework_write",
    "framework_error",
    "context_error",
    "site_cloner_error",
    "unclassified",
)


# ── Errors ───────────────────────────────────────────────────────────────


class CloneAuditError(SiteClonerError):
    """Raised when :func:`build_clone_attempt_record` /
    :func:`record_clone_attempt_failure` are called with an input that
    fails the structural shape gate (non-string ``tenant_id`` / blank
    ``source_url`` / non-exception ``error`` / etc.).

    Subclass of :class:`backend.web.site_cloner.SiteClonerError` so a
    single ``except SiteClonerError`` in the calling router catches
    every W11 layer's input-shape errors uniformly.
    """


# ── Type aliases ─────────────────────────────────────────────────────────


#: Hook signature mirroring :func:`backend.audit.log`. Tests inject a
#: capture-and-forward fake here; the production default lazy-imports
#: ``backend.audit.log``.
AuditLogHook = Callable[..., Awaitable[Any]]


# ── Pure classifier ──────────────────────────────────────────────────────


# Order matters: the table walks subclass-first so a more specific
# subclass wins over its base. ``ContentRiskError`` is checked before
# ``ContentClassifierError`` etc. ``CloneCaptureTimeoutError`` is checked
# before ``CloneSourceError`` because it's a subclass of the latter.
_CLASSIFICATION_TABLE: Tuple[Tuple[type, str], ...] = (
    # W11.1 syntactic / SSRF gate
    (InvalidCloneURLError, "invalid_url"),
    (BlockedDestinationError, "blocked_destination"),
    # W11.2 backend selection
    (FirecrawlConfigError, "backend_config"),
    (PlaywrightConfigError, "backend_config"),
    (FirecrawlDependencyError, "backend_dependency"),
    (PlaywrightDependencyError, "backend_dependency"),
    # W11.1 capture stage (subclass first)
    (CloneCaptureTimeoutError, "capture_timeout"),
    (CloneSourceError, "capture_error"),
    (CloneSpecBuildError, "spec_build"),
    # W11.4 L1 refusal
    (MachineRefusedError, "machine_refused"),
    # W11.5 L2 classifier (subclass first)
    (ContentRiskError, "risk_blocked"),
    (ClassifierUnavailableError, "classifier_unavailable"),
    (ContentClassifierError, "classifier_error"),
    # W11.6 L3 transformer (subclass first)
    (BytesLeakError, "bytes_leak"),
    (RewriteUnavailableError, "rewrite_unavailable"),
    (OutputTransformerError, "transformer_error"),
    # W11.7 L4 manifest (subclass first)
    (ManifestSchemaError, "manifest_schema"),
    (ManifestWriteError, "manifest_write"),
    (CloneManifestError, "manifest_error"),
    # W11.8 L5 rate limit (subclass first)
    (CloneRateLimitedError, "rate_limited"),
    (CloneRateLimitError, "rate_limit_error"),
    # W11.9 framework adapter (subclass first)
    (UnknownFrameworkError, "framework_unknown"),
    (RenderedProjectWriteError, "framework_write"),
    (FrameworkAdapterError, "framework_error"),
    # W11.10 prompt context
    (CloneSpecContextError, "context_error"),
    # W11 generic fallback
    (SiteClonerError, "site_cloner_error"),
)


def classify_clone_failure(error: BaseException) -> str:
    """Map any caught exception to a stable W11.12 failure category.

    The category string is one of :data:`CLONE_FAILURE_CATEGORIES`.
    Routing is by ``isinstance`` — a subclass falls through to its base
    only if no more-specific entry matches first. Non-exception inputs
    raise :class:`CloneAuditError` so the audit row never ends up with a
    nonsense category.

    Pure function — no logging, no I/O. Safe to call anywhere.
    """
    if not isinstance(error, BaseException):
        raise CloneAuditError(
            f"error must be a BaseException instance, got {type(error).__name__}"
        )
    for cls, category in _CLASSIFICATION_TABLE:
        if isinstance(error, cls):
            return category
    return "unclassified"


# ── Record dataclass ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CloneAttemptRecord:
    """The frozen audit-row projection of a single failed clone attempt.

    Built by :func:`build_clone_attempt_record` and serialised onto the
    ``after`` slot of the ``web.clone.failed`` audit row by
    :func:`clone_attempt_record_to_audit_payload`.

    Frozen + pickle-safe — once the record has been written to the audit
    chain, downstream readers (W11.12 audit replay UI, DMCA / takedown
    tooling) consume it from a fixed snapshot.

    Attributes:
        source_url: The clone target URL the attempt was for. Stored
            verbatim — canonicalisation is the rate-limiter's job, not
            the audit row's.
        tenant_id: The owning tenant. Stable identifier scoped to the
            current chain (per-tenant chains are independent).
        actor: The auth principal who initiated the attempt. Defaults
            to ``"system"`` when not supplied.
        failure_category: One of :data:`CLONE_FAILURE_CATEGORIES`.
        failure_message: Truncated ``str(error)``. Capped at
            :data:`MAX_FAILURE_MESSAGE_CHARS` to stop a hostile error
            from bloating the chain row.
        failure_class: Concrete exception class name (e.g. ``"CloneRateLimitedError"``).
            Distinct from ``failure_category`` because the latter
            collapses several exception classes into one operator-facing
            bucket.
        target: Canonical origin string (``<scheme>://<host>``) when
            available — populated from :class:`CloneRateLimitedError` or
            :class:`MachineRefusedError`. Falls back to ``None``.
        clone_id: When the attempt got far enough to build a manifest,
            its ``clone_id``. Used to cross-reference with W11.7 rows
            (e.g. an L5 HOLD that fired *after* the manifest landed).
        manifest_hash: Same purpose as ``clone_id`` — the W11.7
            manifest fingerprint when one exists.
        risk_level: For W11.5 risk-blocked failures, the classification
            level (``"high"`` / ``"critical"``).
        risk_categories: For W11.5 risk-blocked failures, the categories
            that fired.
        refusal_signals: For W11.4 machine-refused failures, the
            signal-name tuple from the :class:`RefusalDecision`.
        refusal_reasons: Truncated reason list (W11.4 / W11.5 / W11.8
            depending on category). Capped at
            :data:`MAX_FAILURE_REASONS`.
        rate_limit_count: For W11.8 rate-limited failures, the count
            field of the :class:`CloneRateLimitDecision`.
        rate_limit_limit: For W11.8 rate-limited failures, the limit.
        rate_limit_window_seconds: For W11.8 rate-limited failures, the
            window in seconds.
        rate_limit_retry_after_seconds: For W11.8 rate-limited
            failures, the retry-after hint.
        framework: For W11.9 framework-render failures, the requested
            framework name.
        extras: Open dict for caller-supplied annotations (e.g.
            ``{"http_status": 451}``). Kept tiny by the
            :data:`MAX_FAILURE_REASONS` discipline applied at build time.
    """

    source_url: str
    tenant_id: str
    actor: str
    failure_category: str
    failure_message: str
    failure_class: str
    target: Optional[str] = None
    clone_id: Optional[str] = None
    manifest_hash: Optional[str] = None
    risk_level: Optional[str] = None
    risk_categories: Tuple[str, ...] = ()
    refusal_signals: Tuple[str, ...] = ()
    refusal_reasons: Tuple[str, ...] = ()
    rate_limit_count: Optional[int] = None
    rate_limit_limit: Optional[int] = None
    rate_limit_window_seconds: Optional[float] = None
    rate_limit_retry_after_seconds: Optional[float] = None
    framework: Optional[str] = None
    extras: Mapping[str, Any] = field(default_factory=dict)


# ── Helpers ─────────────────────────────────────────────────────────────


def _truncate(text: str, *, limit: int) -> str:
    """Truncate ``text`` to ``limit`` with an ellipsis when it exceeds.

    Operates on character counts (not bytes) — sufficient for the
    audit-row payload which is JSON-encoded downstream and therefore
    rendered as Unicode either way.
    """
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _normalise_reasons(values: Any) -> Tuple[str, ...]:
    """Coerce an arbitrary iterable to a tuple of trimmed strings,
    deduplicated by first occurrence, capped at
    :data:`MAX_FAILURE_REASONS`.
    """
    if values is None:
        return ()
    if isinstance(values, str):
        candidates = (values,)
    else:
        try:
            candidates = tuple(values)
        except TypeError:
            return ()
    seen: list[str] = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        trimmed = value.strip()
        if not trimmed or trimmed in seen:
            continue
        seen.append(trimmed)
        if len(seen) >= MAX_FAILURE_REASONS:
            break
    return tuple(seen)


def _validate_required_strings(
    *,
    source_url: str,
    tenant_id: str,
) -> None:
    """Fail-fast input gate. The audit row's identity hinges on these
    fields; an empty ``tenant_id`` would mean the row joins the wrong
    chain. We refuse the attempt at build time rather than write a
    polluted row.
    """
    if not isinstance(source_url, str) or not source_url.strip():
        raise CloneAuditError("source_url must be a non-empty string")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise CloneAuditError("tenant_id must be a non-empty string")


# ── Builder ─────────────────────────────────────────────────────────────


def build_clone_attempt_record(
    error: BaseException,
    *,
    source_url: str,
    tenant_id: str,
    actor: str | None = None,
    manifest: Optional[CloneManifest] = None,
    framework: Optional[str] = None,
    extras: Optional[Mapping[str, Any]] = None,
) -> CloneAttemptRecord:
    """Project an exception + its surrounding context onto a frozen
    :class:`CloneAttemptRecord` ready for audit-log serialisation.

    The function is **pure** — no I/O, no logging — so callers can
    cheaply build a record for inspection / metrics without committing
    to writing the row. :func:`record_clone_attempt_failure` is the
    one-shot variant that builds + writes in a single call.

    Args:
        error: The exception that ended the clone attempt. Must be a
            ``BaseException`` instance; non-exception inputs raise
            :class:`CloneAuditError`.
        source_url: The original clone-target URL.
        tenant_id: The chain-owning tenant.
        actor: The auth principal. Falls back to ``"system"`` when
            unset, matching :func:`backend.audit.log`'s default.
        manifest: Optional :class:`CloneManifest` if the attempt got
            past W11.7. Provides ``clone_id`` + ``manifest_hash`` for
            cross-referencing with the W11.7 success row.
        framework: Optional framework name (Next / Nuxt / Astro) when
            the failure happened during W11.9 render.
        extras: Caller-supplied annotations. Validated to be a mapping
            but otherwise carried verbatim.

    Returns:
        A new :class:`CloneAttemptRecord`.

    Raises:
        CloneAuditError: ``error`` is not a ``BaseException`` /
            ``source_url`` or ``tenant_id`` is empty / ``manifest`` is
            non-None and not a :class:`CloneManifest`.
    """
    _validate_required_strings(source_url=source_url, tenant_id=tenant_id)
    if manifest is not None and not isinstance(manifest, CloneManifest):
        raise CloneAuditError(
            "manifest must be a CloneManifest or None, got "
            f"{type(manifest).__name__}"
        )
    if extras is not None and not isinstance(extras, Mapping):
        raise CloneAuditError(
            f"extras must be a Mapping or None, got {type(extras).__name__}"
        )

    failure_category = classify_clone_failure(error)
    failure_message = _truncate(str(error), limit=MAX_FAILURE_MESSAGE_CHARS)
    failure_class = type(error).__name__

    target: Optional[str] = None
    refusal_signals: Tuple[str, ...] = ()
    refusal_reasons: Tuple[str, ...] = ()
    risk_level: Optional[str] = None
    risk_categories: Tuple[str, ...] = ()
    rl_count: Optional[int] = None
    rl_limit: Optional[int] = None
    rl_window: Optional[float] = None
    rl_retry: Optional[float] = None

    # Pull error-type-specific metadata. ``isinstance`` is the right
    # gate (not ``failure_category ==`` ...) because attribute access is
    # tied to the exception class, not the audit-row bucket name.
    if isinstance(error, MachineRefusedError):
        decision = error.decision
        if decision is not None:
            refusal_signals = tuple(decision.signals_checked)
            refusal_reasons = _normalise_reasons(decision.reasons)
        target = error.url or None
    elif isinstance(error, ContentRiskError):
        classification = error.classification
        if classification is not None:
            risk_level = classification.risk_level
            risk_categories = tuple(
                score.category for score in classification.scores
            )
            refusal_reasons = _normalise_reasons(classification.reasons)
    elif isinstance(error, CloneRateLimitedError):
        decision = error.decision
        if decision is not None:
            target = decision.target
            rl_count = decision.count
            rl_limit = decision.limit
            rl_window = float(decision.window_seconds)
            rl_retry = float(decision.retry_after_seconds)
        # ``CloneRateLimitedError.url`` may be set when the caller wired
        # a non-canonical original URL onto the error.
        if not target:
            target = getattr(error, "url", None)

    clone_id: Optional[str] = None
    manifest_hash: Optional[str] = None
    if manifest is not None:
        clone_id = manifest.clone_id or None
        manifest_hash = manifest.manifest_hash or None

    framework_value: Optional[str] = None
    if framework is not None:
        if not isinstance(framework, str):
            raise CloneAuditError(
                f"framework must be a string or None, got {type(framework).__name__}"
            )
        trimmed_fw = framework.strip()
        framework_value = trimmed_fw or None

    extras_payload: Mapping[str, Any] = dict(extras) if extras else {}

    return CloneAttemptRecord(
        source_url=source_url,
        tenant_id=tenant_id,
        actor=actor or "system",
        failure_category=failure_category,
        failure_message=failure_message,
        failure_class=failure_class,
        target=target,
        clone_id=clone_id,
        manifest_hash=manifest_hash,
        risk_level=risk_level,
        risk_categories=risk_categories,
        refusal_signals=refusal_signals,
        refusal_reasons=refusal_reasons,
        rate_limit_count=rl_count,
        rate_limit_limit=rl_limit,
        rate_limit_window_seconds=rl_window,
        rate_limit_retry_after_seconds=rl_retry,
        framework=framework_value,
        extras=extras_payload,
    )


# ── Audit-payload projector ─────────────────────────────────────────────


def clone_attempt_record_to_audit_payload(
    record: CloneAttemptRecord,
) -> dict[str, Any]:
    """Project a :class:`CloneAttemptRecord` onto the ``after`` slot of
    the ``web.clone.failed`` audit row.

    Output is a plain ``dict`` (not the frozen dataclass) because the
    audit subsystem JSON-encodes its payloads. Tuples become lists for
    the same reason. Optional fields whose value is ``None`` /
    empty-tuple are still emitted so the audit-replay UI can rely on a
    fixed schema (no shape-shifting between rows).
    """
    if not isinstance(record, CloneAttemptRecord):
        raise CloneAuditError(
            f"record must be a CloneAttemptRecord, got {type(record).__name__}"
        )
    return {
        "source_url": record.source_url,
        "tenant_id": record.tenant_id,
        "actor": record.actor,
        "failure_category": record.failure_category,
        "failure_class": record.failure_class,
        "failure_message": record.failure_message,
        "target": record.target,
        "clone_id": record.clone_id,
        "manifest_hash": record.manifest_hash,
        "risk_level": record.risk_level,
        "risk_categories": list(record.risk_categories),
        "refusal_signals": list(record.refusal_signals),
        "refusal_reasons": list(record.refusal_reasons),
        "rate_limit": {
            "count": record.rate_limit_count,
            "limit": record.rate_limit_limit,
            "window_seconds": record.rate_limit_window_seconds,
            "retry_after_seconds": record.rate_limit_retry_after_seconds,
        },
        "framework": record.framework,
        "extras": dict(record.extras),
    }


# ── Audit emitter ───────────────────────────────────────────────────────


async def _default_audit_log(*args: Any, **kwargs: Any) -> Any:
    """Lazy bridge to :func:`backend.audit.log`.

    Imported lazily so this module imports cleanly in unit-test
    environments where the audit subsystem isn't initialised. Mirror of
    the lazy import in
    :func:`backend.web.clone_manifest.record_clone_audit` and
    :func:`backend.web.clone_rate_limit.record_clone_rate_limit_hold`.
    """
    from backend import audit as _audit_mod

    return await _audit_mod.log(*args, **kwargs)


async def record_clone_attempt_failure(
    error: BaseException,
    *,
    source_url: str,
    tenant_id: str,
    actor: str | None = None,
    manifest: Optional[CloneManifest] = None,
    framework: Optional[str] = None,
    extras: Optional[Mapping[str, Any]] = None,
    conn: Any = None,
    session_id: Optional[str] = None,
    audit_log: AuditLogHook | None = None,
) -> Optional[int]:
    """Append one ``web.clone.failed`` row to the per-tenant audit
    chain capturing a failed clone attempt.

    Best-effort — returns ``None`` and does **not** raise if the audit
    subsystem is unreachable, mirroring :func:`backend.audit.log`'s
    contract. The caller's own exception bubbles up unchanged; this
    function only writes the row.

    The W11.7 ``record_clone_audit`` and W11.8
    ``record_clone_rate_limit_hold`` emitters cover the success and L5
    HOLD paths respectively. This emitter is the catch-all for every
    other failure mode — see :data:`CLONE_FAILURE_CATEGORIES` for the
    full enumeration.

    Recommended router shape::

        try:
            await assert_clone_allowed_pre_capture(url)
            capture = await source.capture(url)
            ...
            transformed = await transform_clone_spec(spec, classification=...)
            manifest = build_clone_manifest(...)
            await pin_clone_artefacts(manifest=manifest, ...)
            await assert_clone_rate_limit(tenant_id=..., target_url=url, ...)
            project = render_clone_project(transformed, framework=fw, manifest=manifest)
            write_rendered_project(project, project_root=output_dir)
        except CloneRateLimitedError:
            raise   # already audited via W11.8
        except SiteClonerError as exc:
            await record_clone_attempt_failure(
                exc, source_url=url, tenant_id=tenant.id,
                actor=actor_email, manifest=locals().get("manifest"),
                framework=fw if "fw" in locals() else None,
            )
            raise

    Args:
        error: The exception that ended the attempt. Forwarded verbatim
            to :func:`build_clone_attempt_record`.
        source_url: The clone-target URL.
        tenant_id: The chain-owning tenant.
        actor: The auth principal. Defaults to ``"system"``.
        manifest: Optional :class:`CloneManifest` when the attempt got
            past W11.7.
        framework: Optional framework name when W11.9 was reached.
        extras: Caller-supplied annotations.
        conn: Optional asyncpg connection (see
            :func:`backend.audit.log`).
        session_id: Optional auth session id.
        audit_log: Test-only hook overriding the default
            :func:`backend.audit.log` bridge.

    Returns:
        The new audit row id, or ``None`` on best-effort failure.
    """
    record = build_clone_attempt_record(
        error,
        source_url=source_url,
        tenant_id=tenant_id,
        actor=actor,
        manifest=manifest,
        framework=framework,
        extras=extras,
    )
    payload = clone_attempt_record_to_audit_payload(record)
    hook = audit_log or _default_audit_log

    try:
        return await hook(
            CLONE_ATTEMPT_FAILED_AUDIT_ACTION,
            CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND,
            record.tenant_id,
            None,
            payload,
            record.actor,
            session_id,
            conn,
        )
    except TypeError:
        # Some hooks (e.g. fakes) bind by keyword. Retry as kwargs so
        # the same emitter works across both calling conventions.
        return await hook(
            action=CLONE_ATTEMPT_FAILED_AUDIT_ACTION,
            entity_kind=CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND,
            entity_id=record.tenant_id,
            before=None,
            after=payload,
            actor=record.actor,
            session_id=session_id,
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 — audit best-effort
        logger.warning(
            "W11.12: clone attempt audit log failed (%s/%s tenant=%s "
            "category=%s): %s",
            CLONE_ATTEMPT_FAILED_AUDIT_ACTION,
            CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND,
            record.tenant_id,
            record.failure_category,
            exc,
        )
        return None


__all__ = [
    "AuditLogHook",
    "CLONE_ATTEMPT_FAILED_AUDIT_ACTION",
    "CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND",
    "CLONE_FAILURE_CATEGORIES",
    "CloneAttemptRecord",
    "CloneAuditError",
    "MAX_FAILURE_MESSAGE_CHARS",
    "MAX_FAILURE_REASONS",
    "build_clone_attempt_record",
    "classify_clone_failure",
    "clone_attempt_record_to_audit_payload",
    "record_clone_attempt_failure",
]
