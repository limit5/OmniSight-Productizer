"""W11.12 #XXX — Contract tests for ``backend.web.clone_audit``.

Pins:

    * Public surface (constants, dataclass shape, error hierarchy,
      package re-exports + drift-guard pin).
    * ``classify_clone_failure`` maps every concrete ``SiteClonerError``
      subclass — and a non-W11 exception — to a stable category in
      :data:`CLONE_FAILURE_CATEGORIES`.
    * ``build_clone_attempt_record`` extracts the right metadata from
      ``MachineRefusedError`` / ``ContentRiskError`` /
      ``CloneRateLimitedError`` and from a non-W11 exception.
    * ``clone_attempt_record_to_audit_payload`` produces a stable shape
      with ``None`` / empty placeholders so the audit-replay UI can
      rely on a fixed schema.
    * ``record_clone_attempt_failure`` routes to ``backend.audit.log``
      with ``action="web.clone.failed"`` /
      ``entity_kind="web_clone_attempt"`` / ``entity_id=tenant_id`` and
      a payload built from the record. Best-effort on hook failure.
    * Cross-row invariants: action lives in the ``web.clone.*``
      namespace; entity-kind doesn't collide with W11.7 / W11.8.

Every test runs without network / DB / LLM I/O. The audit-log hook is
injected via the ``audit_log=`` kwarg so tests never touch the real
audit subsystem.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

import backend.web as web_pkg
from backend.web.clone_audit import (
    CLONE_ATTEMPT_FAILED_AUDIT_ACTION,
    CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND,
    CLONE_FAILURE_CATEGORIES,
    CloneAttemptRecord,
    CloneAuditError,
    MAX_FAILURE_MESSAGE_CHARS,
    MAX_FAILURE_REASONS,
    build_clone_attempt_record,
    classify_clone_failure,
    clone_attempt_record_to_audit_payload,
    record_clone_attempt_failure,
)
from backend.web.clone_manifest import (
    AUDIT_ACTION as W11_7_AUDIT_ACTION,
    AUDIT_ENTITY_KIND as W11_7_AUDIT_ENTITY_KIND,
    CloneManifest,
    CloneManifestError,
    ManifestSchemaError,
    ManifestWriteError,
    build_clone_manifest,
)
from backend.web.clone_rate_limit import (
    CLONE_RATE_AUDIT_ACTION,
    CLONE_RATE_AUDIT_ENTITY_KIND,
    CloneRateLimitDecision,
    CloneRateLimitError,
    CloneRateLimitedError,
)
from backend.web.clone_spec_context import CloneSpecContextError
from backend.web.content_classifier import (
    ClassifierUnavailableError,
    ContentClassifierError,
    ContentRiskError,
    RiskClassification,
    RiskScore,
)
from backend.web.firecrawl_source import (
    FirecrawlConfigError,
    FirecrawlDependencyError,
)
from backend.web.framework_adapter import (
    FrameworkAdapterError,
    RenderedProjectWriteError,
    UnknownFrameworkError,
)
from backend.web.output_transformer import (
    BytesLeakError,
    OutputTransformerError,
    RewriteUnavailableError,
    TransformedSpec,
)
from backend.web.playwright_source import (
    PlaywrightConfigError,
    PlaywrightDependencyError,
)
from backend.web.refusal_signals import MachineRefusedError, RefusalDecision
from backend.web.site_cloner import (
    BlockedDestinationError,
    CloneCaptureTimeoutError,
    CloneSourceError,
    CloneSpecBuildError,
    InvalidCloneURLError,
    SiteClonerError,
)


# ── Fixtures + test doubles ─────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


class _FakeAuditLog:
    """Capture-and-forward stand-in for :func:`backend.audit.log`.

    Mirrors the helper in ``test_clone_rate_limit.py`` so the W11.12
    tests use the same calling-convention normalisation. Captures
    positional + keyword args verbatim so a test can pin every field
    the audit row carried.
    """

    def __init__(self, rv: Any = 7) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.rv = rv

    async def __call__(self, *args, **kwargs):
        keys = ("action", "entity_kind", "entity_id", "before", "after",
                "actor", "session_id", "conn")
        captured: Dict[str, Any] = {}
        for i, val in enumerate(args):
            if i < len(keys):
                captured[keys[i]] = val
        captured.update(kwargs)
        self.calls.append(captured)
        return self.rv


class _RaisingAuditLog:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self.exc = exc or RuntimeError("audit subsystem down")

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


def _make_classification(
    risk_level: str = "high",
    *,
    categories=("regulated_advice", "phishing"),
    reasons=("triggers compliance review", "phishing-like CTA"),
    model: str = "claude-haiku-4.5",
) -> RiskClassification:
    # ``reasons`` is a property on RiskClassification, sourced from each
    # RiskScore.reason. Pad / trim so each category gets one reason.
    reason_list = list(reasons)
    while len(reason_list) < len(categories):
        reason_list.append("ok")
    scores = tuple(
        RiskScore(category=cat, level=risk_level, reason=reason_list[i])
        for i, cat in enumerate(categories)
    )
    return RiskClassification(
        risk_level=risk_level,
        scores=scores,
        model=model,
        signals_used=("heuristic", "llm"),
        prefilter_only=False,
    )


def _make_refusal(allowed: bool = False) -> RefusalDecision:
    return RefusalDecision(
        allowed=allowed,
        signals_checked=("robots.txt", "ai.txt"),
        reasons=() if allowed else ("robots.txt:disallow:/", "ai.txt:disallow"),
        details={},
    )


def _make_rate_limit_decision(
    *,
    allowed: bool = False,
    count: int = 4,
    limit: int = 3,
    window_seconds: float = 86400.0,
    retry_after_seconds: float = 12345.0,
    tenant_id: str = "tenant-42",
    target: str = "https://acme.example",
) -> CloneRateLimitDecision:
    return CloneRateLimitDecision(
        allowed=allowed,
        count=count,
        limit=limit,
        window_seconds=window_seconds,
        retry_after_seconds=retry_after_seconds,
        oldest_attempt_at=1.0,
        tenant_id=tenant_id,
        target=target,
    )


def _make_manifest(**overrides: Any) -> CloneManifest:
    transformed = TransformedSpec(
        source_url="https://acme.example",
        fetched_at="2026-04-29T00:00:00Z",
        backend="mock",
        title="Our Take",
        meta={"description": "p"},
        hero={"heading": "h", "tagline": "t", "cta_label": "c"},
        nav=({"label": "N"},),
        sections=({"heading": "S", "summary": "Sum"},),
        footer={"text": "F"},
        images=(),
        colors=(),
        fonts=(),
        spacing={},
        warnings=(),
        signals_used=("llm", "image_placeholder"),
        model="claude-haiku-4.5",
        transformations=("bytes_strip", "text_rewrite", "image_placeholder"),
    )
    classification = _make_classification(
        risk_level="low", categories=("clean",), reasons=()
    )
    base = dict(
        source_url="https://acme.example",
        fetched_at="2026-04-29T00:00:00Z",
        backend="mock",
        classification=classification,
        transformed=transformed,
        tenant_id="tenant-42",
        actor="alice@example.com",
        clone_id="clone-w1112",
        created_at="2026-04-29T00:00:01Z",
    )
    base.update(overrides)
    return build_clone_manifest(**base)


# ── Public surface ──────────────────────────────────────────────────────


def test_action_constants_pinned() -> None:
    assert CLONE_ATTEMPT_FAILED_AUDIT_ACTION == "web.clone.failed"
    assert CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND == "web_clone_attempt"


def test_max_constants_pinned() -> None:
    assert MAX_FAILURE_MESSAGE_CHARS == 1_000
    assert MAX_FAILURE_REASONS == 16


def test_clone_failure_categories_is_immutable_tuple_of_strings() -> None:
    assert isinstance(CLONE_FAILURE_CATEGORIES, tuple)
    assert all(isinstance(c, str) and c for c in CLONE_FAILURE_CATEGORIES)
    # Every entry unique.
    assert len(set(CLONE_FAILURE_CATEGORIES)) == len(CLONE_FAILURE_CATEGORIES)
    # ``unclassified`` is the catch-all and must be in the table.
    assert "unclassified" in CLONE_FAILURE_CATEGORIES
    # Five-layer pipeline buckets all represented.
    for required in (
        "machine_refused", "risk_blocked", "bytes_leak",
        "manifest_schema", "rate_limited", "framework_unknown",
        "context_error", "site_cloner_error",
    ):
        assert required in CLONE_FAILURE_CATEGORIES


def test_clone_attempt_record_is_frozen_dataclass() -> None:
    rec = CloneAttemptRecord(
        source_url="https://x", tenant_id="t", actor="a",
        failure_category="unclassified", failure_message="m",
        failure_class="Exception",
    )
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        rec.failure_category = "other"  # type: ignore[misc]


def test_clone_audit_error_chains_to_site_cloner_error() -> None:
    assert issubclass(CloneAuditError, SiteClonerError)


def test_action_lives_in_web_clone_namespace() -> None:
    """The full clone lifecycle (success / HOLD / failure) is queryable
    via a single ``WHERE action LIKE 'web.clone.%'`` predicate, so all
    three actions must share the same prefix."""
    assert CLONE_ATTEMPT_FAILED_AUDIT_ACTION.startswith("web.clone.")
    assert CLONE_RATE_AUDIT_ACTION.startswith("web.clone.")
    assert W11_7_AUDIT_ACTION == "web.clone"
    # And W11.12's action is distinct from W11.7 / W11.8.
    assert CLONE_ATTEMPT_FAILED_AUDIT_ACTION != W11_7_AUDIT_ACTION
    assert CLONE_ATTEMPT_FAILED_AUDIT_ACTION != CLONE_RATE_AUDIT_ACTION


def test_entity_kinds_distinct_across_w11_emitters() -> None:
    """Audit-replay queries that scope by ``entity_kind`` to a single
    emitter must not catch siblings — the three W11 emitters must use
    pairwise-distinct ``entity_kind`` values."""
    kinds = {
        W11_7_AUDIT_ENTITY_KIND,
        CLONE_RATE_AUDIT_ENTITY_KIND,
        CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND,
    }
    assert len(kinds) == 3


# ── classify_clone_failure ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc, expected",
    [
        (InvalidCloneURLError("bad url"), "invalid_url"),
        (BlockedDestinationError("loopback"), "blocked_destination"),
        (FirecrawlConfigError("missing key"), "backend_config"),
        (PlaywrightConfigError("bad browser"), "backend_config"),
        (FirecrawlDependencyError("no httpx"), "backend_dependency"),
        (PlaywrightDependencyError("no playwright"), "backend_dependency"),
        (CloneCaptureTimeoutError("timeout"), "capture_timeout"),
        (CloneSourceError("backend boom"), "capture_error"),
        (CloneSpecBuildError("not html"), "spec_build"),
        (ClassifierUnavailableError("no llm"), "classifier_unavailable"),
        (
            ContentClassifierError("classifier base"),
            "classifier_error",
        ),
        (BytesLeakError("data: uri"), "bytes_leak"),
        (RewriteUnavailableError("no rewriter"), "rewrite_unavailable"),
        (
            OutputTransformerError("transformer base"),
            "transformer_error",
        ),
        (ManifestSchemaError("bad shape"), "manifest_schema"),
        (ManifestWriteError("disk full"), "manifest_write"),
        (CloneManifestError("manifest base"), "manifest_error"),
        (
            CloneRateLimitError("rate base"),
            "rate_limit_error",
        ),
        (
            UnknownFrameworkError("vue?"),
            "framework_unknown",
        ),
        (
            RenderedProjectWriteError("perm denied"),
            "framework_write",
        ),
        (
            FrameworkAdapterError("framework base"),
            "framework_error",
        ),
        (
            CloneSpecContextError("bad spec"),
            "context_error",
        ),
        (
            SiteClonerError("generic w11 err"),
            "site_cloner_error",
        ),
        (RuntimeError("totally unrelated"), "unclassified"),
        (ValueError("definitely not w11"), "unclassified"),
    ],
)
def test_classify_clone_failure_maps_each_exception_class(
    exc: BaseException, expected: str,
) -> None:
    assert classify_clone_failure(exc) == expected


def test_classify_machine_refused_uses_dedicated_bucket() -> None:
    decision = _make_refusal(allowed=False)
    err = MachineRefusedError(decision, url="https://x")
    assert classify_clone_failure(err) == "machine_refused"


def test_classify_content_risk_uses_dedicated_bucket() -> None:
    err = ContentRiskError(_make_classification(), threshold="high")
    assert classify_clone_failure(err) == "risk_blocked"


def test_classify_clone_rate_limited_uses_dedicated_bucket() -> None:
    err = CloneRateLimitedError(_make_rate_limit_decision(), url="https://x")
    assert classify_clone_failure(err) == "rate_limited"


def test_classify_subclass_wins_over_base() -> None:
    """``CloneCaptureTimeoutError`` subclasses ``CloneSourceError`` —
    the more-specific bucket must win even though both are valid."""
    timeout = CloneCaptureTimeoutError("hit deadline")
    assert classify_clone_failure(timeout) == "capture_timeout"
    assert classify_clone_failure(timeout) != "capture_error"


def test_classify_returns_value_in_known_set() -> None:
    """No matter what the input, the output is one of
    :data:`CLONE_FAILURE_CATEGORIES` so the audit-replay UI's legend
    always covers the row."""
    for exc in (
        InvalidCloneURLError("x"), CloneSourceError("x"),
        RuntimeError("x"), TypeError("x"),
    ):
        assert classify_clone_failure(exc) in CLONE_FAILURE_CATEGORIES


def test_classify_rejects_non_exception() -> None:
    with pytest.raises(CloneAuditError):
        classify_clone_failure("not an exception")  # type: ignore[arg-type]


def test_classify_rejects_class_not_instance() -> None:
    with pytest.raises(CloneAuditError):
        classify_clone_failure(InvalidCloneURLError)  # type: ignore[arg-type]


# ── build_clone_attempt_record ──────────────────────────────────────────


def test_build_record_happy_path_minimal_inputs() -> None:
    err = InvalidCloneURLError("ftp not allowed")
    record = build_clone_attempt_record(
        err, source_url="ftp://x.example", tenant_id="tenant-1",
    )
    assert record.failure_category == "invalid_url"
    assert record.failure_class == "InvalidCloneURLError"
    assert record.failure_message == "ftp not allowed"
    assert record.source_url == "ftp://x.example"
    assert record.tenant_id == "tenant-1"
    # Default actor when omitted.
    assert record.actor == "system"
    # All optional buckets default to None / empty.
    assert record.target is None
    assert record.clone_id is None
    assert record.manifest_hash is None
    assert record.risk_level is None
    assert record.risk_categories == ()
    assert record.refusal_signals == ()
    assert record.refusal_reasons == ()
    assert record.rate_limit_count is None
    assert record.framework is None
    assert record.extras == {}


def test_build_record_uses_explicit_actor() -> None:
    err = SiteClonerError("anything")
    record = build_clone_attempt_record(
        err, source_url="https://x", tenant_id="t",
        actor="alice@example.com",
    )
    assert record.actor == "alice@example.com"


def test_build_record_extracts_machine_refused_metadata() -> None:
    decision = _make_refusal(allowed=False)
    err = MachineRefusedError(decision, url="https://x.example")
    record = build_clone_attempt_record(
        err, source_url="https://x.example", tenant_id="t",
    )
    assert record.failure_category == "machine_refused"
    assert record.target == "https://x.example"
    assert record.refusal_signals == decision.signals_checked
    assert record.refusal_reasons == decision.reasons


def test_build_record_extracts_content_risk_metadata() -> None:
    classification = _make_classification(
        risk_level="critical",
        categories=("phishing", "illegal"),
        reasons=("spoofs login form", "selling regulated goods"),
    )
    err = ContentRiskError(classification, threshold="high")
    record = build_clone_attempt_record(
        err, source_url="https://bad.example", tenant_id="t",
    )
    assert record.failure_category == "risk_blocked"
    assert record.risk_level == "critical"
    assert record.risk_categories == ("phishing", "illegal")
    assert record.refusal_reasons == classification.reasons


def test_build_record_extracts_rate_limit_metadata() -> None:
    decision = _make_rate_limit_decision(
        count=4, limit=3, window_seconds=86400.0,
        retry_after_seconds=42000.0,
        target="https://acme.example",
    )
    err = CloneRateLimitedError(decision, url="https://acme.example/page?cb=1")
    record = build_clone_attempt_record(
        err, source_url="https://acme.example/page?cb=1", tenant_id="tenant-42",
    )
    assert record.failure_category == "rate_limited"
    # Target is the canonical origin from the decision, not the raw url.
    assert record.target == "https://acme.example"
    assert record.rate_limit_count == 4
    assert record.rate_limit_limit == 3
    assert record.rate_limit_window_seconds == pytest.approx(86400.0)
    assert record.rate_limit_retry_after_seconds == pytest.approx(42000.0)


def test_build_record_falls_back_to_url_attr_when_decision_target_blank() -> None:
    """If a future regression hands us a decision with a blank target,
    we fall back to ``error.url`` so the audit row still records *some*
    target identifier rather than ``None``."""
    decision = _make_rate_limit_decision(target="")
    err = CloneRateLimitedError(decision, url="https://fallback.example")
    record = build_clone_attempt_record(
        err, source_url="https://fallback.example", tenant_id="t",
    )
    assert record.target == "https://fallback.example"


def test_build_record_pins_clone_id_and_hash_from_manifest() -> None:
    manifest = _make_manifest()
    err = CloneManifestError("diagnostic-only")
    record = build_clone_attempt_record(
        err, source_url="https://acme.example", tenant_id="tenant-42",
        manifest=manifest,
    )
    assert record.clone_id == manifest.clone_id
    assert record.manifest_hash == manifest.manifest_hash
    assert record.failure_category == "manifest_error"


def test_build_record_truncates_long_failure_message() -> None:
    huge = "x" * (MAX_FAILURE_MESSAGE_CHARS + 500)
    err = SiteClonerError(huge)
    record = build_clone_attempt_record(
        err, source_url="https://x", tenant_id="t",
    )
    assert len(record.failure_message) == MAX_FAILURE_MESSAGE_CHARS
    assert record.failure_message.endswith("…")


def test_build_record_caps_refusal_reasons() -> None:
    # 30 reasons → capped at MAX_FAILURE_REASONS.
    decision = RefusalDecision(
        allowed=False,
        signals_checked=("robots.txt",),
        reasons=tuple(f"reason-{i}" for i in range(30)),
        details={},
    )
    err = MachineRefusedError(decision, url="https://x")
    record = build_clone_attempt_record(
        err, source_url="https://x", tenant_id="t",
    )
    assert len(record.refusal_reasons) == MAX_FAILURE_REASONS
    assert record.refusal_reasons[0] == "reason-0"


def test_build_record_rejects_non_exception() -> None:
    with pytest.raises(CloneAuditError):
        build_clone_attempt_record(
            "boom",  # type: ignore[arg-type]
            source_url="https://x", tenant_id="t",
        )


def test_build_record_rejects_blank_source_url() -> None:
    with pytest.raises(CloneAuditError):
        build_clone_attempt_record(
            SiteClonerError("x"), source_url="   ", tenant_id="t",
        )


def test_build_record_rejects_non_string_source_url() -> None:
    with pytest.raises(CloneAuditError):
        build_clone_attempt_record(
            SiteClonerError("x"), source_url=42, tenant_id="t",  # type: ignore[arg-type]
        )


def test_build_record_rejects_blank_tenant_id() -> None:
    with pytest.raises(CloneAuditError):
        build_clone_attempt_record(
            SiteClonerError("x"), source_url="https://x", tenant_id="",
        )


def test_build_record_rejects_non_string_tenant_id() -> None:
    with pytest.raises(CloneAuditError):
        build_clone_attempt_record(
            SiteClonerError("x"), source_url="https://x", tenant_id=42,  # type: ignore[arg-type]
        )


def test_build_record_rejects_non_manifest() -> None:
    with pytest.raises(CloneAuditError):
        build_clone_attempt_record(
            SiteClonerError("x"), source_url="https://x", tenant_id="t",
            manifest={"clone_id": "fake"},  # type: ignore[arg-type]
        )


def test_build_record_rejects_non_string_framework() -> None:
    with pytest.raises(CloneAuditError):
        build_clone_attempt_record(
            SiteClonerError("x"), source_url="https://x", tenant_id="t",
            framework=42,  # type: ignore[arg-type]
        )


def test_build_record_normalises_blank_framework_to_none() -> None:
    record = build_clone_attempt_record(
        SiteClonerError("x"), source_url="https://x", tenant_id="t",
        framework="   ",
    )
    assert record.framework is None


def test_build_record_carries_extras_verbatim() -> None:
    record = build_clone_attempt_record(
        SiteClonerError("x"), source_url="https://x", tenant_id="t",
        extras={"http_status": 451, "retry": False},
    )
    assert record.extras == {"http_status": 451, "retry": False}


def test_build_record_rejects_non_mapping_extras() -> None:
    with pytest.raises(CloneAuditError):
        build_clone_attempt_record(
            SiteClonerError("x"), source_url="https://x", tenant_id="t",
            extras=["not", "a", "dict"],  # type: ignore[arg-type]
        )


def test_build_record_handles_unclassified_error() -> None:
    err = ZeroDivisionError("not w11")
    record = build_clone_attempt_record(
        err, source_url="https://x", tenant_id="t",
    )
    assert record.failure_category == "unclassified"
    assert record.failure_class == "ZeroDivisionError"


# ── clone_attempt_record_to_audit_payload ───────────────────────────────


def test_payload_has_stable_top_level_keys() -> None:
    record = build_clone_attempt_record(
        SiteClonerError("x"), source_url="https://x", tenant_id="t",
    )
    payload = clone_attempt_record_to_audit_payload(record)
    expected_keys = {
        "source_url", "tenant_id", "actor",
        "failure_category", "failure_class", "failure_message",
        "target", "clone_id", "manifest_hash",
        "risk_level", "risk_categories",
        "refusal_signals", "refusal_reasons",
        "rate_limit",
        "framework", "extras",
    }
    assert set(payload.keys()) == expected_keys


def test_payload_carries_none_for_unset_optionals() -> None:
    record = build_clone_attempt_record(
        SiteClonerError("x"), source_url="https://x", tenant_id="t",
    )
    payload = clone_attempt_record_to_audit_payload(record)
    assert payload["target"] is None
    assert payload["clone_id"] is None
    assert payload["manifest_hash"] is None
    assert payload["risk_level"] is None
    assert payload["framework"] is None
    assert payload["risk_categories"] == []
    assert payload["refusal_signals"] == []
    assert payload["refusal_reasons"] == []
    assert payload["rate_limit"] == {
        "count": None, "limit": None,
        "window_seconds": None, "retry_after_seconds": None,
    }
    assert payload["extras"] == {}


def test_payload_serialises_tuples_to_lists() -> None:
    """The audit subsystem JSON-encodes its payloads — tuples must
    become lists at the projection boundary so downstream readers don't
    have to special-case the JSON-encoding round trip."""
    decision = _make_refusal(allowed=False)
    err = MachineRefusedError(decision, url="https://x")
    record = build_clone_attempt_record(
        err, source_url="https://x", tenant_id="t",
    )
    payload = clone_attempt_record_to_audit_payload(record)
    assert isinstance(payload["refusal_signals"], list)
    assert isinstance(payload["refusal_reasons"], list)
    assert isinstance(payload["risk_categories"], list)


def test_payload_carries_rate_limit_block_when_present() -> None:
    decision = _make_rate_limit_decision(
        count=5, limit=3, window_seconds=86400.0,
        retry_after_seconds=99.0,
    )
    err = CloneRateLimitedError(decision, url="https://x.example")
    record = build_clone_attempt_record(
        err, source_url="https://x.example", tenant_id="t",
    )
    payload = clone_attempt_record_to_audit_payload(record)
    assert payload["rate_limit"]["count"] == 5
    assert payload["rate_limit"]["limit"] == 3
    assert payload["rate_limit"]["window_seconds"] == pytest.approx(86400.0)
    assert payload["rate_limit"]["retry_after_seconds"] == pytest.approx(99.0)


def test_payload_rejects_non_record() -> None:
    with pytest.raises(CloneAuditError):
        clone_attempt_record_to_audit_payload({"failure_category": "x"})  # type: ignore[arg-type]


# ── record_clone_attempt_failure ────────────────────────────────────────


def test_record_routes_to_audit_log_with_pinned_action_and_kind() -> None:
    audit = _FakeAuditLog(rv=42)
    err = InvalidCloneURLError("bad url")
    rid = _run(record_clone_attempt_failure(
        err, source_url="https://x", tenant_id="tenant-1",
        actor="alice@example.com", audit_log=audit,
    ))
    assert rid == 42
    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["action"] == CLONE_ATTEMPT_FAILED_AUDIT_ACTION
    assert call["entity_kind"] == CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND
    assert call["entity_id"] == "tenant-1"
    assert call["before"] is None
    assert call["actor"] == "alice@example.com"


def test_record_payload_carries_full_record() -> None:
    audit = _FakeAuditLog()
    decision = _make_refusal(allowed=False)
    err = MachineRefusedError(decision, url="https://x")
    _run(record_clone_attempt_failure(
        err, source_url="https://x", tenant_id="t", actor="bob",
        audit_log=audit,
    ))
    after = audit.calls[0]["after"]
    assert after["failure_category"] == "machine_refused"
    assert after["failure_class"] == "MachineRefusedError"
    assert after["target"] == "https://x"
    assert after["refusal_signals"] == list(decision.signals_checked)
    assert after["refusal_reasons"] == list(decision.reasons)


def test_record_session_id_and_conn_threaded_through() -> None:
    audit = _FakeAuditLog()
    sentinel_conn = object()
    _run(record_clone_attempt_failure(
        SiteClonerError("x"), source_url="https://x", tenant_id="t",
        audit_log=audit, session_id="sess-123", conn=sentinel_conn,
    ))
    call = audit.calls[0]
    assert call["session_id"] == "sess-123"
    assert call["conn"] is sentinel_conn


def test_record_default_actor_is_system() -> None:
    audit = _FakeAuditLog()
    _run(record_clone_attempt_failure(
        SiteClonerError("x"), source_url="https://x", tenant_id="t",
        audit_log=audit,
    ))
    assert audit.calls[0]["actor"] == "system"


def test_record_returns_none_when_audit_log_raises() -> None:
    audit = _RaisingAuditLog()
    rid = _run(record_clone_attempt_failure(
        SiteClonerError("x"), source_url="https://x", tenant_id="t",
        audit_log=audit,
    ))
    assert rid is None
    # The hook was still called once before raising.
    assert audit.calls == 1


def test_record_propagates_input_validation_errors() -> None:
    """Build-time invariant violations (blank tenant / non-exception
    error) are NOT best-effort — they're caller bugs, not transient
    audit-subsystem issues. We let those raise."""
    audit = _FakeAuditLog()
    with pytest.raises(CloneAuditError):
        _run(record_clone_attempt_failure(
            SiteClonerError("x"), source_url="https://x", tenant_id="",
            audit_log=audit,
        ))
    # The audit hook was never called because validation tripped first.
    assert audit.calls == []


def test_record_carries_manifest_breadcrumbs_when_provided() -> None:
    audit = _FakeAuditLog()
    manifest = _make_manifest()
    err = ManifestWriteError("disk full")
    _run(record_clone_attempt_failure(
        err, source_url="https://acme.example", tenant_id="tenant-42",
        manifest=manifest, audit_log=audit,
    ))
    after = audit.calls[0]["after"]
    assert after["clone_id"] == manifest.clone_id
    assert after["manifest_hash"] == manifest.manifest_hash
    assert after["failure_category"] == "manifest_write"


def test_record_routes_via_kwargs_when_hook_is_keyword_only() -> None:
    """Some hooks are wired by keyword. Our retry-on-TypeError path
    must succeed against such hooks too — otherwise tests / production
    fakes that bind by keyword silently drop the row."""

    class _KeywordOnlyAuditLog:
        def __init__(self) -> None:
            self.last: Dict[str, Any] = {}

        async def __call__(
            self, *,
            action: str, entity_kind: str, entity_id: str,
            before: Any, after: Any, actor: str,
            session_id: Any, conn: Any,
        ) -> Any:
            self.last = dict(
                action=action, entity_kind=entity_kind, entity_id=entity_id,
                before=before, after=after, actor=actor,
                session_id=session_id, conn=conn,
            )
            return 11

    audit = _KeywordOnlyAuditLog()
    rid = _run(record_clone_attempt_failure(
        InvalidCloneURLError("bad"), source_url="https://x",
        tenant_id="t", audit_log=audit,
    ))
    assert rid == 11
    assert audit.last["action"] == CLONE_ATTEMPT_FAILED_AUDIT_ACTION
    assert audit.last["entity_id"] == "t"


def test_record_distinct_from_w11_7_success_row() -> None:
    """A failed clone must not be muddled with a successful clone:
    different ``action`` AND different ``entity_kind`` AND different
    ``entity_id`` (clone_id vs tenant_id)."""
    audit = _FakeAuditLog()
    _run(record_clone_attempt_failure(
        SiteClonerError("x"), source_url="https://x",
        tenant_id="tenant-42", audit_log=audit,
    ))
    call = audit.calls[0]
    assert call["action"] != W11_7_AUDIT_ACTION
    assert call["entity_kind"] != W11_7_AUDIT_ENTITY_KIND
    # W11.7 rows use clone_id as entity_id; W11.12 uses tenant_id.
    assert call["entity_id"] == "tenant-42"


def test_record_distinct_from_w11_8_rate_limited_row() -> None:
    audit = _FakeAuditLog()
    decision = _make_rate_limit_decision()
    err = CloneRateLimitedError(decision, url="https://x")
    _run(record_clone_attempt_failure(
        err, source_url="https://x", tenant_id="tenant-42",
        audit_log=audit,
    ))
    call = audit.calls[0]
    # W11.12 emitter ALWAYS uses ``web.clone.failed`` — even for
    # rate-limited errors; the W11.8 emitter is the one that uses the
    # dedicated ``web.clone.rate_limited`` action. The two rows are
    # complementary, not interchangeable.
    assert call["action"] == CLONE_ATTEMPT_FAILED_AUDIT_ACTION
    assert call["action"] != CLONE_RATE_AUDIT_ACTION


def test_record_default_audit_hook_lazy_imports_backend_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``audit_log`` is not supplied, the emitter calls
    :func:`backend.audit.log` via lazy import (mirrors the W11.7 / W11.8
    discipline). Patching ``backend.audit.log`` proves the bridge
    actually routes there."""
    captured: Dict[str, Any] = {}

    async def fake_log(*args: Any, **kwargs: Any) -> Any:
        keys = ("action", "entity_kind", "entity_id", "before", "after",
                "actor", "session_id", "conn")
        for i, val in enumerate(args):
            if i < len(keys):
                captured[keys[i]] = val
        captured.update(kwargs)
        return 99

    monkeypatch.setattr("backend.audit.log", fake_log)
    rid = _run(record_clone_attempt_failure(
        InvalidCloneURLError("bad"), source_url="https://x",
        tenant_id="tenant-7",
    ))
    assert rid == 99
    assert captured["action"] == CLONE_ATTEMPT_FAILED_AUDIT_ACTION
    assert captured["entity_kind"] == CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND
    assert captured["entity_id"] == "tenant-7"


# ── Package re-exports + drift-guard ────────────────────────────────────


_W11_12_RE_EXPORTED_SYMBOLS = [
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


@pytest.mark.parametrize("symbol", _W11_12_RE_EXPORTED_SYMBOLS)
def test_clone_audit_symbol_re_exported_from_package(symbol: str) -> None:
    assert symbol in web_pkg.__all__
    assert getattr(web_pkg, symbol) is not None


def test_total_re_export_count_pinned_at_192() -> None:
    """Drift guard: every prior W11 row pinned the running total. W11.12
    adds 11 ``clone_audit`` symbols → 192. W13.2 adds 7 screenshot-
    breakpoint symbols → 199. W13.3 adds 18 screenshot-writer symbols →
    217. W13.4 adds 16 screenshot-ghost-overlay symbols → 233. W15.2
    adds 11 vite_error_relay symbols → 244. W15.3 adds 8
    vite_error_prompt symbols → 252. W15.4 adds 10 vite_retry_budget
    symbols → 262. W15.5 adds 13 vite_config_injection symbols → 275.
    W15.6 adds 13 vite_self_fix symbols → 288. W16.2 adds 25
    image_attachment symbols → 313. W16.3 adds 17 build_intent
    symbols → 330. Each row's drift guard is updated in lockstep so
    a future row that adds a new symbol fails every guard until each
    one acknowledges the new total."""
    assert len(web_pkg.__all__) == 466


# ── Whole-spec invariants ───────────────────────────────────────────────


def test_classification_table_covers_every_category() -> None:
    """Every category in :data:`CLONE_FAILURE_CATEGORIES` must be
    reachable — either via a concrete exception in the classifier table
    or via the ``unclassified`` catch-all. This test guards against
    adding a new category string without wiring it to anything.
    """
    sample_errors: Dict[str, BaseException] = {
        "invalid_url": InvalidCloneURLError("x"),
        "blocked_destination": BlockedDestinationError("x"),
        "backend_config": FirecrawlConfigError("x"),
        "backend_dependency": FirecrawlDependencyError("x"),
        "capture_timeout": CloneCaptureTimeoutError("x"),
        "capture_error": CloneSourceError("x"),
        "spec_build": CloneSpecBuildError("x"),
        "machine_refused": MachineRefusedError(_make_refusal(False), url="https://x"),
        "classifier_unavailable": ClassifierUnavailableError("x"),
        "risk_blocked": ContentRiskError(_make_classification(), threshold="high"),
        "classifier_error": ContentClassifierError("x"),
        "bytes_leak": BytesLeakError("x"),
        "rewrite_unavailable": RewriteUnavailableError("x"),
        "transformer_error": OutputTransformerError("x"),
        "manifest_schema": ManifestSchemaError("x"),
        "manifest_write": ManifestWriteError("x"),
        "manifest_error": CloneManifestError("x"),
        "rate_limited": CloneRateLimitedError(
            _make_rate_limit_decision(), url="https://x",
        ),
        "rate_limit_error": CloneRateLimitError("x"),
        "framework_unknown": UnknownFrameworkError("x"),
        "framework_write": RenderedProjectWriteError("x"),
        "framework_error": FrameworkAdapterError("x"),
        "context_error": CloneSpecContextError("x"),
        "site_cloner_error": SiteClonerError("x"),
        "unclassified": RuntimeError("x"),
    }
    # ``UnknownCloneBackendError`` lives in ``backend.web.__init__`` so
    # wiring it directly in the classification table would create a
    # circular import; it correctly falls through to the
    # ``site_cloner_error`` bucket. Verify that contract here.
    from backend.web import UnknownCloneBackendError
    assert classify_clone_failure(UnknownCloneBackendError("x")) == "site_cloner_error"

    for category in CLONE_FAILURE_CATEGORIES:
        assert category in sample_errors, (
            f"category {category!r} declared in CLONE_FAILURE_CATEGORIES "
            f"but no sample exception wired in test"
        )
        assert classify_clone_failure(sample_errors[category]) == category


def test_audit_payload_canonicalizable_to_json() -> None:
    """The audit subsystem JSON-serialises payloads via
    ``json.dumps(... default=str)``. This test pins that the payload
    we hand it has no shape that would defeat that serialiser.
    """
    import json

    decision = _make_rate_limit_decision()
    err = CloneRateLimitedError(decision, url="https://x.example")
    record = build_clone_attempt_record(
        err, source_url="https://x.example", tenant_id="t",
        manifest=_make_manifest(), framework="next",
        extras={"http_status": 429},
    )
    payload = clone_attempt_record_to_audit_payload(record)
    blob = json.dumps(payload, sort_keys=True, default=str)
    parsed = json.loads(blob)
    assert parsed["failure_category"] == "rate_limited"
    assert parsed["framework"] == "next"
    assert parsed["extras"] == {"http_status": 429}
