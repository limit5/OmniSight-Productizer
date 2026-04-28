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

W11.1 ships the entry point + minimal ``CloneSpec`` container +
``CloneSource`` protocol; W11.2 plugs the two production-targeted
backends behind that contract; W11.3 populates the spec from rendered
HTML; W11.4 adds the L1 refusal-signal gate; W11.5 adds the L2 content
classifier; W11.6 adds the L3 transformer; W11.7 adds the L4
traceability layer; subsequent rows add the remaining defense
(W11.8 rate limiter).

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
    "AUDIT_ACTION",
    "AUDIT_ENTITY_KIND",
    "BlockedDestinationError",
    "BytesLeakError",
    "CLOUDFLARE_AI_BLOCK_BODY_HINTS",
    "CLOUDFLARE_MITIGATED_REFUSE_VALUES",
    "ClassifierLLM",
    "ClassifierUnavailableError",
    "CloneCaptureTimeoutError",
    "CloneManifest",
    "CloneManifestError",
    "CloneManifestRecord",
    "CloneSource",
    "CloneSourceError",
    "CloneSpec",
    "CloneSpecBuildError",
    "ContentClassifierError",
    "ContentRiskError",
    "DEFAULT_BROWSER",
    "DEFAULT_CLASSIFIER_MODEL",
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
    "HTML_COMMENT_BEGIN",
    "HTML_COMMENT_END",
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
    "MAX_PROMPT_INPUT_CHARS",
    "MAX_REASON_CHARS",
    "MAX_REASONS",
    "MAX_REWRITE_INPUT_CHARS",
    "MAX_REWRITE_TEXT_CHARS",
    "MAX_REWRITTEN_LIST_ITEMS",
    "MAX_TRANSFORM_RISK_LEVEL",
    "META_AI_BOT_NAMES",
    "META_NOAI_TOKENS",
    "MachineRefusedError",
    "ManifestSchemaError",
    "ManifestWriteError",
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
    "RefusalDecision",
    "RefusalFetchResult",
    "RefusalFetcher",
    "RewriteUnavailableError",
    "RiskClassification",
    "RiskScore",
    "SUPPORTED_BROWSERS",
    "SUPPORTED_URL_SCHEMES",
    "SiteClonerError",
    "TextRewriteLLM",
    "TransformedSpec",
    "UnknownCloneBackendError",
    "apply_image_placeholders",
    "assert_clone_allowed_post_capture",
    "assert_clone_allowed_pre_capture",
    "assert_clone_spec_safe",
    "assert_no_copied_bytes",
    "build_clone_manifest",
    "build_clone_spec_from_capture",
    "check_ai_txt",
    "check_cloudflare_ai_block",
    "check_machine_refusal_post_capture",
    "check_machine_refusal_pre_capture",
    "check_meta_noai",
    "check_robots_txt",
    "check_x_robots_tag",
    "classify_clone_spec",
    "clone_site",
    "compute_manifest_hash",
    "default_refusal_fetcher",
    "extract_hostname",
    "finalise_manifest",
    "heuristic_risk_signals",
    "inject_html_traceability_comment",
    "is_public_destination",
    "make_clone_source",
    "manifest_to_audit_payload",
    "manifest_to_dict",
    "merge_refusal_decisions",
    "merge_risk_classifications",
    "normalize_url",
    "parse_html_traceability_comment",
    "pin_clone_artefacts",
    "read_manifest_file",
    "record_clone_audit",
    "render_html_traceability_comment",
    "serialize_manifest_json",
    "transform_clone_spec",
    "validate_clone_url",
    "verify_manifest_hash",
    "write_manifest_file",
]
