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

The package is intentionally thin at this point — W11.1 ships the entry
point + minimal ``CloneSpec`` container + ``CloneSource`` protocol so the
follow-up rows can build on a stable contract surface; W11.2 plugs the
two production-targeted backends behind that contract; subsequent rows
populate the spec / add defense layers.

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
    "BlockedDestinationError",
    "CloneCaptureTimeoutError",
    "CloneSource",
    "CloneSourceError",
    "CloneSpec",
    "CloneSpecBuildError",
    "DEFAULT_BROWSER",
    "DEFAULT_FIRECRAWL_BASE_URL",
    "DEFAULT_MAX_HTML_BYTES",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_WAIT_UNTIL",
    "FIRECRAWL_BACKEND_NAME",
    "FIRECRAWL_SCRAPE_PATH",
    "FirecrawlConfigError",
    "FirecrawlDependencyError",
    "FirecrawlSource",
    "InvalidCloneURLError",
    "KNOWN_CLONE_BACKENDS",
    "PLAYWRIGHT_BACKEND_NAME",
    "PlaywrightConfigError",
    "PlaywrightDependencyError",
    "PlaywrightSource",
    "RawCapture",
    "SUPPORTED_BROWSERS",
    "SUPPORTED_URL_SCHEMES",
    "SiteClonerError",
    "UnknownCloneBackendError",
    "build_clone_spec_from_capture",
    "clone_site",
    "extract_hostname",
    "is_public_destination",
    "make_clone_source",
    "normalize_url",
    "validate_clone_url",
]
