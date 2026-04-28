"""W11 #XXX — Website cloning capability package.

Sub-modules:

    site_cloner    URL → ``CloneSpec`` orchestrator (W11.1).
                   Higher rows (W11.2 backends, W11.3 schema, W11.4–W11.8
                   defense-in-depth, W11.9 framework adapters, etc.) plug
                   in through the public surface re-exported below.

The package is intentionally thin at this point — W11.1 ships the entry
point + minimal ``CloneSpec`` container + ``CloneSource`` protocol so the
follow-up rows can build on a stable contract surface.

Inspired by firecrawl/open-lovable (MIT). Attribution and license text
land alongside the W11.13 row (`LICENSES/open-lovable-mit.txt`).
"""

from __future__ import annotations

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

__all__ = [
    "BlockedDestinationError",
    "CloneCaptureTimeoutError",
    "CloneSource",
    "CloneSourceError",
    "CloneSpec",
    "CloneSpecBuildError",
    "DEFAULT_MAX_HTML_BYTES",
    "DEFAULT_TIMEOUT_S",
    "InvalidCloneURLError",
    "RawCapture",
    "SUPPORTED_URL_SCHEMES",
    "SiteClonerError",
    "build_clone_spec_from_capture",
    "clone_site",
    "extract_hostname",
    "is_public_destination",
    "normalize_url",
    "validate_clone_url",
]
