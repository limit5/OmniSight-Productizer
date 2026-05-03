"""W11 #XXX — Website cloning capability package.

Sub-modules:

    site_cloner          URL → ``CloneSpec`` orchestrator (W11.1).
                         Higher rows (W11.3 schema, W11.4–W11.8
                         defense-in-depth, W11.9 framework adapters,
                         etc.) plug in through the public surface
                         re-exported below.

    firecrawl_source     ``CloneSource`` adapter for the Firecrawl SaaS
                         API (W11.2 backend a). Default backend on
                         non-air-gapped deployments.

    playwright_source    ``CloneSource`` adapter that drives a local
                         Playwright headless browser (W11.2 backend b).
                         Mandatory for air-gapped deployments.

    refusal_signals      W11.4 L1 machine-refusal-signal scanner —
                         robots.txt + ai.txt + ``noai`` meta + X-Robots-
                         Tag + Cloudflare AI-bot-block detection. Run
                         **before** ``clone_site()`` so a refused URL
                         never burns a backend session.

    content_classifier   W11.5 L2 LLM content classifier — heuristic
                         prefilter + cheapest-model (Haiku 4.5 / Gemini
                         Flash / DeepSeek) classification of the
                         populated ``CloneSpec`` into ``risk_level`` +
                         categories. Run **after** ``clone_site()`` so
                         the classifier sees the full populated spec.

    output_transformer   W11.6 L3 output transformation — never-copy-
                         bytes invariant gate + LLM text rewrite
                         (cheapest-model chain) + image placeholder
                         substitution. Run **after** the L2 classifier
                         has cleared the spec; produces a frozen
                         ``TransformedSpec`` the W11.7 manifest pins.

    clone_manifest       W11.7 L4 forced traceability — emits the HTML
                         traceability comment + ``.omnisight/clone-
                         manifest.json`` on disk + appends a per-tenant
                         audit-log row. Run **after** the L3 transformer
                         has produced a ``TransformedSpec``; produces a
                         frozen ``CloneManifest`` and a
                         ``CloneManifestRecord`` summarising which
                         footprints landed.

    clone_rate_limit     W11.8 L5 rate limit + PEP HOLD — sliding-window
                         log (Redis ZSET / in-memory deque) capping any
                         (tenant, target-origin) pair at
                         ``DEFAULT_CLONE_RATE_LIMIT=3`` per
                         ``DEFAULT_CLONE_RATE_WINDOW_S=86400`` (24h).
                         Run **after** the L4 manifest is pinned;
                         ``assert_clone_rate_limit`` is the policy-
                         enforcement point that consumes one slot or
                         raises :class:`CloneRateLimitedError` (PEP
                         HOLD) with a precise ``retry_after_seconds``.

    framework_adapter    W11.9 multi-framework adapter — three render
                         paths (Next.js 14 / Nuxt 3 / Astro 4) producing
                         a :class:`RenderedProject` (frozen dataclass of
                         relative-path-pinned :class:`RenderedFile`
                         records) from a :class:`TransformedSpec` plus
                         the W11.7 :class:`CloneManifest`. Pure render
                         function (no FS I/O) + a separate
                         ``write_rendered_project`` writer mirrors the
                         W11.7 build/write split. Bakes the W11.7
                         traceability comment into a static
                         ``public/clone-traceability.html`` and
                         framework-idiomatic ``<meta>`` tags. Future Vue
                         / Svelte rows plug in via the same
                         :class:`FrameworkAdapter` Protocol.

    clone_spec_context   W11.10 frontend agent role-prompt context
                         block. :func:`build_clone_spec_context` turns a
                         :class:`TransformedSpec` (W11.6) plus the
                         optional :class:`CloneManifest` (W11.7) into a
                         deterministic markdown block that the prompt
                         loader injects into the frontend agent role
                         prompt so the specialist node can scaffold a
                         Next / Nuxt / Astro project (W11.9) using the
                         rewritten outline as design inspiration without
                         the LLM ever seeing source bytes, source brand
                         names, or original image URLs.

    clone_audit          W11.12 failure-path audit-log emitter.
                         :func:`record_clone_attempt_failure` writes one
                         ``web.clone.failed`` row per failed clone
                         attempt, classified across the full
                         :class:`SiteClonerError` subclass tree via
                         :func:`classify_clone_failure`. Complements the
                         W11.7 ``web.clone`` (success) and W11.8
                         ``web.clone.rate_limited`` (L5 HOLD) emitters
                         so a prefix-filter ``WHERE action LIKE
                         'web.clone.%'`` catches the full clone
                         lifecycle.

W11.1 ships the entry point + minimal ``CloneSpec`` container +
``CloneSource`` protocol; W11.2 plugs the two production-targeted
backends behind that contract; W11.3 populates the spec from rendered
HTML; W11.4 adds the L1 refusal-signal gate; W11.5 adds the L2 content
classifier; W11.6 adds the L3 transformer; W11.7 adds the L4
traceability layer; W11.8 adds the L5 rate limit + PEP HOLD; W11.9 adds
the multi-framework render path productizer; the 5-layer defense remains
intact (W11.9 is consumer-side of the L4 manifest).

Inspired by firecrawl/open-lovable (MIT). Attribution and license text
land alongside the W11.13 row (`LICENSES/open-lovable-mit.txt`).
"""

from __future__ import annotations

from typing import Optional

from backend.web.firecrawl_source import (
    DEFAULT_FIRECRAWL_BASE_URL,
    FIRECRAWL_BACKEND_NAME,
    FIRECRAWL_SCRAPE_PATH,
    FirecrawlConfigError,
    FirecrawlDependencyError,
    FirecrawlSource,
)
from backend.web.playwright_source import (
    DEFAULT_BROWSER,
    DEFAULT_WAIT_UNTIL,
    PLAYWRIGHT_BACKEND_NAME,
    PlaywrightConfigError,
    PlaywrightDependencyError,
    PlaywrightSource,
    SUPPORTED_BROWSERS,
)
from backend.web.content_classifier import (
    ClassifierLLM,
    ClassifierUnavailableError,
    ContentClassifierError,
    ContentRiskError,
    DEFAULT_CLASSIFIER_MODEL,
    DEFAULT_REFUSAL_THRESHOLD,
    LLM_SYSTEM_PROMPT,
    LLM_USER_PROMPT_TEMPLATE,
    LangchainClassifierLLM,
    MAX_PROMPT_INPUT_CHARS,
    MAX_REASON_CHARS,
    MAX_REASONS,
    RISK_CATEGORIES,
    RISK_LEVELS,
    RiskClassification,
    RiskScore,
    assert_clone_spec_safe,
    classify_clone_spec,
    heuristic_risk_signals,
    merge_risk_classifications,
)
from backend.web.clone_manifest import (
    AUDIT_ACTION,
    AUDIT_ENTITY_KIND,
    CloneManifest,
    CloneManifestError,
    CloneManifestRecord,
    HTML_COMMENT_BEGIN,
    HTML_COMMENT_END,
    MANIFEST_DIR,
    MANIFEST_FILENAME,
    MANIFEST_HASH_FIELD,
    MANIFEST_RELATIVE_PATH,
    MANIFEST_VERSION,
    ManifestSchemaError,
    ManifestWriteError,
    OPEN_LOVABLE_ATTRIBUTION,
    build_clone_manifest,
    compute_manifest_hash,
    finalise_manifest,
    inject_html_traceability_comment,
    manifest_to_audit_payload,
    manifest_to_dict,
    parse_html_traceability_comment,
    pin_clone_artefacts,
    read_manifest_file,
    record_clone_audit,
    render_html_traceability_comment,
    serialize_manifest_json,
    verify_manifest_hash,
    write_manifest_file,
)
from backend.web.clone_rate_limit import (
    CLONE_RATE_AUDIT_ACTION,
    CLONE_RATE_AUDIT_ENTITY_KIND,
    CLONE_RATE_KEY_PREFIX,
    CloneRateLimitDecision,
    CloneRateLimitError,
    CloneRateLimitedError,
    CloneRateLimiter,
    DEFAULT_CLONE_RATE_LIMIT,
    DEFAULT_CLONE_RATE_WINDOW_S,
    InMemoryCloneRateLimiter,
    RedisCloneRateLimiter,
    assert_clone_rate_limit,
    canonical_clone_target,
    clone_rate_limit_key,
    get_clone_rate_limiter,
    record_clone_rate_limit_hold,
    reset_clone_rate_limiter,
    resolve_clone_rate_limit,
    resolve_clone_rate_window_seconds,
)
from backend.web.framework_adapter import (
    ASTRO_FRAMEWORK_NAME,
    AstroFrameworkAdapter,
    FrameworkAdapter,
    FrameworkAdapterError,
    GENERATOR_META,
    MAX_RENDERED_IMAGES,
    MAX_RENDERED_NAV_ITEMS,
    MAX_RENDERED_SECTIONS,
    NEXT_FRAMEWORK_NAME,
    NUXT_FRAMEWORK_NAME,
    NextFrameworkAdapter,
    NuxtFrameworkAdapter,
    RenderedFile,
    RenderedProject,
    RenderedProjectWriteError,
    SUPPORTED_FRAMEWORKS,
    TRACEABILITY_HTML_FILENAME,
    TRACEABILITY_HTML_RELATIVE_PATH,
    UnknownFrameworkError,
    make_framework_adapter,
    project_to_audit_payload,
    render_clone_project,
    write_rendered_project,
)
from backend.web.clone_spec_context import (
    CLONE_SPEC_CONTEXT_HEADER,
    CloneSpecContextError,
    MAX_CLONE_SPEC_CONTEXT_CHARS,
    MAX_CONTEXT_COLOR_ITEMS,
    MAX_CONTEXT_FONT_ITEMS,
    MAX_CONTEXT_IMAGE_ITEMS,
    MAX_CONTEXT_NAV_ITEMS,
    MAX_CONTEXT_SECTION_ITEMS,
    MAX_CONTEXT_SECTION_SUMMARY_CHARS,
    TRUNCATION_MARKER,
    W11_INVARIANTS_BLOCK,
    build_clone_spec_context,
)
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
from backend.web.output_transformer import (
    BytesLeakError,
    DEFAULT_PLACEHOLDER_HEIGHT,
    DEFAULT_PLACEHOLDER_WIDTH,
    DEFAULT_REWRITE_MODEL,
    LLM_REWRITE_SYSTEM_PROMPT,
    LLM_REWRITE_USER_PROMPT_TEMPLATE,
    LangchainTextRewriteLLM,
    MAX_REWRITE_INPUT_CHARS,
    MAX_REWRITE_TEXT_CHARS,
    MAX_REWRITTEN_LIST_ITEMS,
    MAX_TRANSFORM_RISK_LEVEL,
    OutputTransformerError,
    PLACEHOLDER_PROVIDER,
    RewriteUnavailableError,
    TextRewriteLLM,
    TransformedSpec,
    apply_image_placeholders,
    assert_no_copied_bytes,
    transform_clone_spec,
)
from backend.web.refusal_signals import (
    AI_BOT_USER_AGENTS,
    AI_TXT_PATHS,
    CLOUDFLARE_AI_BLOCK_BODY_HINTS,
    CLOUDFLARE_MITIGATED_REFUSE_VALUES,
    DEFAULT_REFUSAL_FETCH_MAX_BYTES,
    DEFAULT_REFUSAL_FETCH_TIMEOUT_S,
    DEFAULT_USER_AGENT,
    MachineRefusedError,
    META_AI_BOT_NAMES,
    META_NOAI_TOKENS,
    ROBOTS_TXT_PATH,
    RefusalDecision,
    RefusalFetchResult,
    RefusalFetcher,
    assert_clone_allowed_post_capture,
    assert_clone_allowed_pre_capture,
    check_ai_txt,
    check_cloudflare_ai_block,
    check_machine_refusal_post_capture,
    check_machine_refusal_pre_capture,
    check_meta_noai,
    check_robots_txt,
    check_x_robots_tag,
    default_refusal_fetcher,
    merge_refusal_decisions,
)
from backend.web.site_cloner import (
    BlockedDestinationError,
    CloneCaptureTimeoutError,
    CloneSource,
    CloneSourceError,
    CloneSpec,
    CloneSpecBuildError,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_TIMEOUT_S,
    InvalidCloneURLError,
    RawCapture,
    SUPPORTED_URL_SCHEMES,
    SiteClonerError,
    build_clone_spec_from_capture,
    clone_site,
    extract_hostname,
    is_public_destination,
    normalize_url,
    validate_clone_url,
)
from backend.web.screenshot_breakpoints import (
    BREAKPOINT_DESKTOP_1440,
    BREAKPOINT_DESKTOP_1920,
    BREAKPOINT_MOBILE_375,
    BREAKPOINT_TABLET_768,
    DEFAULT_BREAKPOINTS,
    DEFAULT_BREAKPOINT_NAMES,
    resolve_breakpoints,
)
from backend.web.screenshot_writer import (
    SCREENSHOT_MANIFEST_FILENAME,
    SCREENSHOT_MANIFEST_RELATIVE_PATH,
    SCREENSHOT_MANIFEST_VERSION,
    SCREENSHOT_PNG_SUFFIX,
    SCREENSHOT_REFS_DIR,
    SCREENSHOT_REFS_RELATIVE_PATH,
    ScreenshotManifest,
    ScreenshotManifestEntry,
    ScreenshotReadError,
    ScreenshotWriteError,
    ScreenshotWriterError,
    delete_screenshots,
    read_screenshot_manifest,
    read_screenshot_manifest_if_exists,
    resolve_refs_dir,
    resolve_screenshot_manifest_path,
    resolve_screenshot_path,
    write_screenshots,
)
from backend.web.screenshot_ghost_overlay import (
    GHOST_OVERLAY_DIFF_VERSION,
    GHOST_OVERLAY_STATUSES,
    GHOST_OVERLAY_STATUS_DIMENSION_DRIFT,
    GHOST_OVERLAY_STATUS_IDENTICAL,
    GHOST_OVERLAY_STATUS_MISSING_IN_LIVE,
    GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE,
    GHOST_OVERLAY_STATUS_PIXEL_DRIFT,
    GhostOverlayDiff,
    GhostOverlayEntry,
    GhostOverlayError,
    GhostOverlayInputError,
    compute_ghost_overlay_diff,
    compute_ghost_overlay_diff_from_disk,
    ghost_overlay_diff_from_dict,
    ghost_overlay_diff_to_dict,
    serialize_ghost_overlay_diff_json,
)
from backend.web.vite_error_relay import (
    MAX_VITE_ERROR_HISTORY_ENTRIES,
    MAX_VITE_ERROR_HISTORY_LINE_BYTES,
    VITE_ERROR_HISTORY_KEY_PREFIX,
    VITE_ERROR_HISTORY_MAX,
    VITE_ERROR_HISTORY_NO_FILE_TOKEN,
    VITE_ERROR_HISTORY_UNKNOWN_TOKEN,
    build_vite_error_state_patch,
    format_vite_error_for_history,
    merge_vite_errors_into_history,
    vite_error_history_signature,
    vite_errors_for_history,
)


# ── Backend selection ─────────────────────────────────────────────────

#: Stable identifiers operators flip via ``OMNISIGHT_CLONE_BACKEND``. The
#: orchestrator never branches on these strings — it only constructs a
#: backend instance and hands it to ``clone_site(source=...)``.
KNOWN_CLONE_BACKENDS: frozenset[str] = frozenset({
    FIRECRAWL_BACKEND_NAME,
    PLAYWRIGHT_BACKEND_NAME,
})


class UnknownCloneBackendError(SiteClonerError):
    """``make_clone_source`` was asked for a backend identifier that
    isn't in ``KNOWN_CLONE_BACKENDS``. Distinct from
    ``FirecrawlConfigError`` / ``PlaywrightDependencyError`` so callers
    can disambiguate "wrong knob value" from "right knob value, wrong
    environment"."""


def make_clone_source(
    name: Optional[str] = None,
    *,
    settings: Optional[object] = None,
) -> CloneSource:
    """Construct the requested ``CloneSource`` backend.

    Resolution order:

        1. ``name`` arg (explicit caller request)
        2. ``settings.clone_backend`` (if a Settings object is passed
           and the field is set)
        3. ``OMNISIGHT_CLONE_BACKEND`` env var
        4. Auto: prefer Firecrawl when ``OMNISIGHT_FIRECRAWL_API_KEY``
           is set, else Playwright.

    Returns:
        A constructed backend instance. The caller is responsible for
        ``aclose()`` (or ``async with`` it) when finished — both
        backends amortise resource creation across calls.

    Raises:
        UnknownCloneBackendError: ``name`` was set to a value outside
            ``KNOWN_CLONE_BACKENDS``.
        FirecrawlConfigError: Firecrawl was selected but the API key is
            missing.
        PlaywrightDependencyError: Playwright was selected but the
            python package or browser binary is missing.

    Auto-selection rationale: defaulting to Firecrawl when a key is
    present matches the W11.2 row's "Firecrawl SaaS is the fastest path,
    Playwright is the air-gap fallback" framing. Operators that *want*
    air-gap behaviour even with a Firecrawl key in env (e.g. dev box
    that mirrors prod creds but should not egress) set
    ``OMNISIGHT_CLONE_BACKEND=playwright`` explicitly.
    """
    import os  # local — avoid module-level os dep

    # Resolve the requested backend name.
    if name is None and settings is not None:
        # Settings is duck-typed so callers can pass either
        # ``backend.config.Settings`` instances or test fakes.
        name = getattr(settings, "clone_backend", None) or None
    if name is None:
        env_name = os.environ.get("OMNISIGHT_CLONE_BACKEND", "").strip().lower()
        name = env_name or None

    if name is None:
        # Auto: prefer Firecrawl when a key is configured.
        has_key = bool(os.environ.get("OMNISIGHT_FIRECRAWL_API_KEY", "").strip())
        if not has_key and settings is not None:
            has_key = bool(getattr(settings, "firecrawl_api_key", "") or "")
        name = FIRECRAWL_BACKEND_NAME if has_key else PLAYWRIGHT_BACKEND_NAME

    name = name.strip().lower()
    if name not in KNOWN_CLONE_BACKENDS:
        raise UnknownCloneBackendError(
            f"unknown clone backend {name!r}; expected one of "
            f"{sorted(KNOWN_CLONE_BACKENDS)}"
        )

    if name == FIRECRAWL_BACKEND_NAME:
        api_key = None
        base_url = None
        if settings is not None:
            api_key = (getattr(settings, "firecrawl_api_key", "") or None) or api_key
            base_url = (getattr(settings, "firecrawl_base_url", "") or None) or base_url
        return FirecrawlSource(api_key=api_key, base_url=base_url)

    # Playwright path. Browser name resolves the same way (settings →
    # env → default) inside ``PlaywrightSource.__init__``.
    browser = None
    if settings is not None:
        browser = getattr(settings, "playwright_browser", "") or None
    return PlaywrightSource(browser=browser)


__all__ = [
    "AI_BOT_USER_AGENTS",
    "AI_TXT_PATHS",
    "ASTRO_FRAMEWORK_NAME",
    "AUDIT_ACTION",
    "AUDIT_ENTITY_KIND",
    "AstroFrameworkAdapter",
    "BREAKPOINT_DESKTOP_1440",
    "BREAKPOINT_DESKTOP_1920",
    "BREAKPOINT_MOBILE_375",
    "BREAKPOINT_TABLET_768",
    "BlockedDestinationError",
    "BytesLeakError",
    "CLONE_ATTEMPT_FAILED_AUDIT_ACTION",
    "CLONE_ATTEMPT_FAILED_AUDIT_ENTITY_KIND",
    "CLONE_FAILURE_CATEGORIES",
    "CLONE_RATE_AUDIT_ACTION",
    "CLONE_RATE_AUDIT_ENTITY_KIND",
    "CLONE_RATE_KEY_PREFIX",
    "CLONE_SPEC_CONTEXT_HEADER",
    "CLOUDFLARE_AI_BLOCK_BODY_HINTS",
    "CLOUDFLARE_MITIGATED_REFUSE_VALUES",
    "ClassifierLLM",
    "ClassifierUnavailableError",
    "CloneAttemptRecord",
    "CloneAuditError",
    "CloneCaptureTimeoutError",
    "CloneManifest",
    "CloneManifestError",
    "CloneManifestRecord",
    "CloneRateLimitDecision",
    "CloneRateLimitError",
    "CloneRateLimitedError",
    "CloneRateLimiter",
    "CloneSource",
    "CloneSourceError",
    "CloneSpec",
    "CloneSpecBuildError",
    "CloneSpecContextError",
    "ContentClassifierError",
    "ContentRiskError",
    "DEFAULT_BREAKPOINT_NAMES",
    "DEFAULT_BREAKPOINTS",
    "DEFAULT_BROWSER",
    "DEFAULT_CLASSIFIER_MODEL",
    "DEFAULT_CLONE_RATE_LIMIT",
    "DEFAULT_CLONE_RATE_WINDOW_S",
    "DEFAULT_FIRECRAWL_BASE_URL",
    "DEFAULT_MAX_HTML_BYTES",
    "DEFAULT_PLACEHOLDER_HEIGHT",
    "DEFAULT_PLACEHOLDER_WIDTH",
    "DEFAULT_REFUSAL_FETCH_MAX_BYTES",
    "DEFAULT_REFUSAL_FETCH_TIMEOUT_S",
    "DEFAULT_REFUSAL_THRESHOLD",
    "DEFAULT_REWRITE_MODEL",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_USER_AGENT",
    "DEFAULT_WAIT_UNTIL",
    "FIRECRAWL_BACKEND_NAME",
    "FIRECRAWL_SCRAPE_PATH",
    "FirecrawlConfigError",
    "FirecrawlDependencyError",
    "FirecrawlSource",
    "FrameworkAdapter",
    "FrameworkAdapterError",
    "GENERATOR_META",
    "GHOST_OVERLAY_DIFF_VERSION",
    "GHOST_OVERLAY_STATUSES",
    "GHOST_OVERLAY_STATUS_DIMENSION_DRIFT",
    "GHOST_OVERLAY_STATUS_IDENTICAL",
    "GHOST_OVERLAY_STATUS_MISSING_IN_LIVE",
    "GHOST_OVERLAY_STATUS_MISSING_IN_REFERENCE",
    "GHOST_OVERLAY_STATUS_PIXEL_DRIFT",
    "GhostOverlayDiff",
    "GhostOverlayEntry",
    "GhostOverlayError",
    "GhostOverlayInputError",
    "HTML_COMMENT_BEGIN",
    "HTML_COMMENT_END",
    "InMemoryCloneRateLimiter",
    "InvalidCloneURLError",
    "KNOWN_CLONE_BACKENDS",
    "LLM_REWRITE_SYSTEM_PROMPT",
    "LLM_REWRITE_USER_PROMPT_TEMPLATE",
    "LLM_SYSTEM_PROMPT",
    "LLM_USER_PROMPT_TEMPLATE",
    "LangchainClassifierLLM",
    "LangchainTextRewriteLLM",
    "MANIFEST_DIR",
    "MANIFEST_FILENAME",
    "MANIFEST_HASH_FIELD",
    "MANIFEST_RELATIVE_PATH",
    "MANIFEST_VERSION",
    "MAX_CLONE_SPEC_CONTEXT_CHARS",
    "MAX_CONTEXT_COLOR_ITEMS",
    "MAX_CONTEXT_FONT_ITEMS",
    "MAX_CONTEXT_IMAGE_ITEMS",
    "MAX_CONTEXT_NAV_ITEMS",
    "MAX_CONTEXT_SECTION_ITEMS",
    "MAX_CONTEXT_SECTION_SUMMARY_CHARS",
    "MAX_FAILURE_MESSAGE_CHARS",
    "MAX_FAILURE_REASONS",
    "MAX_PROMPT_INPUT_CHARS",
    "MAX_REASON_CHARS",
    "MAX_REASONS",
    "MAX_RENDERED_IMAGES",
    "MAX_RENDERED_NAV_ITEMS",
    "MAX_RENDERED_SECTIONS",
    "MAX_REWRITE_INPUT_CHARS",
    "MAX_REWRITE_TEXT_CHARS",
    "MAX_REWRITTEN_LIST_ITEMS",
    "MAX_TRANSFORM_RISK_LEVEL",
    "MAX_VITE_ERROR_HISTORY_ENTRIES",
    "MAX_VITE_ERROR_HISTORY_LINE_BYTES",
    "META_AI_BOT_NAMES",
    "META_NOAI_TOKENS",
    "MachineRefusedError",
    "ManifestSchemaError",
    "ManifestWriteError",
    "NEXT_FRAMEWORK_NAME",
    "NUXT_FRAMEWORK_NAME",
    "NextFrameworkAdapter",
    "NuxtFrameworkAdapter",
    "OPEN_LOVABLE_ATTRIBUTION",
    "OutputTransformerError",
    "PLACEHOLDER_PROVIDER",
    "PLAYWRIGHT_BACKEND_NAME",
    "PlaywrightConfigError",
    "PlaywrightDependencyError",
    "PlaywrightSource",
    "RISK_CATEGORIES",
    "RISK_LEVELS",
    "ROBOTS_TXT_PATH",
    "RawCapture",
    "RedisCloneRateLimiter",
    "RefusalDecision",
    "RefusalFetchResult",
    "RefusalFetcher",
    "RenderedFile",
    "RenderedProject",
    "RenderedProjectWriteError",
    "RewriteUnavailableError",
    "RiskClassification",
    "RiskScore",
    "SCREENSHOT_MANIFEST_FILENAME",
    "SCREENSHOT_MANIFEST_RELATIVE_PATH",
    "SCREENSHOT_MANIFEST_VERSION",
    "SCREENSHOT_PNG_SUFFIX",
    "SCREENSHOT_REFS_DIR",
    "SCREENSHOT_REFS_RELATIVE_PATH",
    "SUPPORTED_BROWSERS",
    "SUPPORTED_FRAMEWORKS",
    "SUPPORTED_URL_SCHEMES",
    "ScreenshotManifest",
    "ScreenshotManifestEntry",
    "ScreenshotReadError",
    "ScreenshotWriteError",
    "ScreenshotWriterError",
    "SiteClonerError",
    "TRACEABILITY_HTML_FILENAME",
    "TRACEABILITY_HTML_RELATIVE_PATH",
    "TRUNCATION_MARKER",
    "TextRewriteLLM",
    "TransformedSpec",
    "UnknownCloneBackendError",
    "UnknownFrameworkError",
    "VITE_ERROR_HISTORY_KEY_PREFIX",
    "VITE_ERROR_HISTORY_MAX",
    "VITE_ERROR_HISTORY_NO_FILE_TOKEN",
    "VITE_ERROR_HISTORY_UNKNOWN_TOKEN",
    "W11_INVARIANTS_BLOCK",
    "apply_image_placeholders",
    "assert_clone_allowed_post_capture",
    "assert_clone_allowed_pre_capture",
    "assert_clone_rate_limit",
    "assert_clone_spec_safe",
    "assert_no_copied_bytes",
    "build_clone_attempt_record",
    "build_clone_manifest",
    "build_clone_spec_context",
    "build_clone_spec_from_capture",
    "build_vite_error_state_patch",
    "canonical_clone_target",
    "check_ai_txt",
    "check_cloudflare_ai_block",
    "check_machine_refusal_post_capture",
    "check_machine_refusal_pre_capture",
    "check_meta_noai",
    "check_robots_txt",
    "check_x_robots_tag",
    "classify_clone_failure",
    "classify_clone_spec",
    "clone_attempt_record_to_audit_payload",
    "clone_rate_limit_key",
    "clone_site",
    "compute_ghost_overlay_diff",
    "compute_ghost_overlay_diff_from_disk",
    "compute_manifest_hash",
    "default_refusal_fetcher",
    "delete_screenshots",
    "extract_hostname",
    "finalise_manifest",
    "format_vite_error_for_history",
    "get_clone_rate_limiter",
    "ghost_overlay_diff_from_dict",
    "ghost_overlay_diff_to_dict",
    "heuristic_risk_signals",
    "inject_html_traceability_comment",
    "is_public_destination",
    "make_clone_source",
    "make_framework_adapter",
    "manifest_to_audit_payload",
    "manifest_to_dict",
    "merge_refusal_decisions",
    "merge_risk_classifications",
    "merge_vite_errors_into_history",
    "normalize_url",
    "parse_html_traceability_comment",
    "pin_clone_artefacts",
    "project_to_audit_payload",
    "read_manifest_file",
    "read_screenshot_manifest",
    "read_screenshot_manifest_if_exists",
    "record_clone_attempt_failure",
    "record_clone_audit",
    "record_clone_rate_limit_hold",
    "render_clone_project",
    "render_html_traceability_comment",
    "reset_clone_rate_limiter",
    "resolve_breakpoints",
    "resolve_clone_rate_limit",
    "resolve_clone_rate_window_seconds",
    "resolve_refs_dir",
    "resolve_screenshot_manifest_path",
    "resolve_screenshot_path",
    "serialize_ghost_overlay_diff_json",
    "serialize_manifest_json",
    "transform_clone_spec",
    "validate_clone_url",
    "verify_manifest_hash",
    "vite_error_history_signature",
    "vite_errors_for_history",
    "write_manifest_file",
    "write_rendered_project",
    "write_screenshots",
]
