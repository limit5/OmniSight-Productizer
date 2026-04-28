"""W12.4 — Scaffold ``--reference-url`` flag wiring.

The W12 epic adds a *reverse-mode* brand-style pipeline on top of the
B5 forward-mode validator (:mod:`backend.brand_consistency_validator`):

* W12.1 — :class:`backend.brand_spec.BrandSpec` 5-dim fingerprint type.
* W12.2 — :func:`backend.brand_extractor.extract_brand_from_url` — fetch
  + 5-dim k-means / parser pipeline.
* W12.3 — :mod:`backend.brand_canonical` — shared canonicalisation
  primitives between forward- and reverse-mode.
* **W12.4 (this module)** — argparse helper + a tiny resolver façade so
  any scaffolder CLI (``backend/nextjs_scaffolder.py``,
  ``backend/nuxt_scaffolder.py``, etc.) and the future unified
  ``scripts/scaffold.py`` dispatcher can register the
  ``--reference-url URL`` flag with one import and call one function to
  turn the URL into a usable :class:`BrandSpec`.
* W12.5 will persist the resolved spec to ``.omnisight/brand.json``.
* W12.6 will pin the contract via the 8-URL × 5-dim reference matrix.

Why a separate module
---------------------

The 12 per-stack scaffolders (``backend/<stack>_scaffolder.py``) each
own their own :class:`ScaffoldOptions` dataclass.  Adding a duplicate
``reference_url`` field to all 12 would (a) bloat each scaffolder, (b)
diverge on validation, and (c) violate the *Test-fixture blast-radius*
rule from
``docs/sop/implement_phase_step.md`` §"Step 2 拆分工作" — a touch in 12
scaffolders fans out into 12 tests' worth of fixture migration.  The
canonical pattern is to expose a single argparse helper any scaffolder
CLI can call.  W12.5 will then thread the resolved spec through the
existing ``ScaffoldOptions`` instances via the planned
``.omnisight/brand.json`` side-channel — no per-scaffolder field
needed.

The flag value also fail-soft routes to an empty :class:`BrandSpec`
when the network is unreachable (mirroring the
:func:`extract_brand_from_url` discipline) so a transient blip on the
reference site does not block the scaffold.

Public surface
--------------

* :data:`REFERENCE_URL_FLAG` — the canonical flag name.  Re-export so
  callers can grep for one literal across the codebase.
* :data:`REFERENCE_URL_DEST` — the argparse ``dest`` (``reference_url``,
  PEP 8 snake_case mirror of the flag).
* :func:`add_reference_url_argument` — register the flag onto an
  existing :class:`argparse.ArgumentParser`.  Idempotent (calling twice
  on the same parser raises :class:`argparse.ArgumentError` — argparse
  already enforces uniqueness, no extra logic needed).
* :func:`normalize_reference_url` — turn the raw flag value into a
  canonical URL or ``None``.  Trims whitespace, treats empty / missing
  as "no reference".  Rejects non-``http(s)`` schemes early so the
  scaffold fails loud rather than silently producing an empty spec.
* :func:`resolve_reference_url` — high-level: takes the raw flag value,
  delegates to the W12.2 extractor when set, and surfaces the
  :class:`BrandSpec` (or ``None`` when the flag was absent).
* :class:`ReferenceURLError` — typed error so callers can distinguish
  a misconfigured CLI invocation (``ftp://…``) from a transient fetch
  failure (which is a *warning* — empty spec, not exception).

Design contract
---------------

* The flag is **optional**.  Absence ⇒ :func:`resolve_reference_url`
  returns ``None``.  Empty string and whitespace are treated as
  absence (operators sometimes copy-paste a blank from a YAML file).
* When set, the value is normalised — leading/trailing whitespace
  trimmed; the scheme verified against
  :data:`SUPPORTED_REFERENCE_SCHEMES` (``{"http", "https"}`` mirroring
  :mod:`backend.url_to_reference`); URLs longer than
  :data:`MAX_REFERENCE_URL_LENGTH` rejected.
* The actual fetch + extraction is delegated to
  :func:`backend.brand_extractor.extract_brand_from_url`.  We do not
  re-implement the fail-soft envelope.  The extractor's own contract
  guarantees an empty :class:`BrandSpec` (with ``source_url`` + a UTC
  timestamp baked in) on any network / non-200 / decode failure — that
  is the correct shape for W12.5 to write into ``.omnisight/brand.json``
  so the audit record reads "we tried, got nothing".

Module-global state audit (SOP §1)
----------------------------------

Only immutable constants — string literals (the flag name), the
``SUPPORTED_REFERENCE_SCHEMES`` frozenset, the ``MAX_REFERENCE_URL_LENGTH``
int — plus the standard module-level ``logger``.  Cross-worker
consistency: SOP answer #1, every worker derives identical constants
from identical source.

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure-function family, no DB / shared state / concurrency.

Compat-fingerprint grep (SOP §3)
--------------------------------

N/A — no DB code path; ``grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]"``
returns 0 hits in this module.
"""

from __future__ import annotations

import argparse
import logging
from typing import Callable

from backend.brand_extractor import extract_brand_from_url
from backend.brand_spec import BrandSpec, BrandSpecError

logger = logging.getLogger(__name__)


__all__ = [
    "MAX_REFERENCE_URL_LENGTH",
    "REFERENCE_URL_DEST",
    "REFERENCE_URL_FLAG",
    "ReferenceURLError",
    "SUPPORTED_REFERENCE_SCHEMES",
    "add_reference_url_argument",
    "normalize_reference_url",
    "resolve_reference_url",
]


#: Canonical flag literal — keep grep-friendly so any future docs /
#: scripts reference the same string.
REFERENCE_URL_FLAG = "--reference-url"

#: argparse ``dest`` derived from the flag (PEP 8 snake_case).  Exposed
#: so callers can introspect the parsed Namespace without re-deriving.
REFERENCE_URL_DEST = "reference_url"

#: Allowed URL schemes.  Mirrors :data:`backend.url_to_reference.SUPPORTED_URL_SCHEMES`
#: — keeping them aligned avoids the surprise where one entry point
#: accepts ``ftp://`` and another rejects it.
SUPPORTED_REFERENCE_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Hard cap on URL length — defence-in-depth, matches
#: :data:`backend.url_to_reference.MAX_URL_LENGTH`.  Browsers misbehave
#: well before 2 KiB; we want a refusal rather than a downstream
#: surprise.
MAX_REFERENCE_URL_LENGTH = 2048


class ReferenceURLError(ValueError):
    """Raised when the ``--reference-url`` value is structurally invalid.

    Distinct from :class:`BrandSpecError` (which the extractor raises
    for empty-string URLs) and from the *fail-soft* extractor envelope
    (which surfaces transient fetch failures as warnings + empty spec).

    A ``ReferenceURLError`` is a **caller bug** — the scaffold CLI was
    invoked with a malformed value (wrong scheme, overlong, etc.).
    Operators should fix the invocation, not retry.
    """


def add_reference_url_argument(
    parser: argparse.ArgumentParser,
    *,
    help_text: str | None = None,
) -> argparse.Action:
    """Register :data:`REFERENCE_URL_FLAG` onto ``parser``.

    Returns the :class:`argparse.Action` so callers that want to tweak
    metavar / group placement post-hoc can chain.  Idempotency is left
    to argparse — calling twice on the same parser raises
    :class:`argparse.ArgumentError`, surfacing the bug at the call site
    rather than producing surprising "last wins" semantics.

    The flag is **optional** with default ``None``; downstream
    :func:`resolve_reference_url` distinguishes ``None`` (no reference)
    from a non-empty string (extract).
    """
    if not isinstance(parser, argparse.ArgumentParser):
        raise TypeError(
            "add_reference_url_argument: parser must be argparse.ArgumentParser, "
            f"got {type(parser).__name__}"
        )
    return parser.add_argument(
        REFERENCE_URL_FLAG,
        dest=REFERENCE_URL_DEST,
        default=None,
        metavar="URL",
        help=help_text or (
            "Optional brand-style reference URL. When set, the scaffold "
            "extracts a 5-dim BrandSpec (palette/fonts/heading/spacing/radius) "
            "from the URL via backend.brand_extractor and threads it through "
            "the generated project's design-token config (W12.5 persists to "
            ".omnisight/brand.json). Only http(s) schemes are accepted."
        ),
    )


def normalize_reference_url(value: object) -> str | None:
    """Validate + canonicalise a raw ``--reference-url`` value.

    * ``None`` → ``None`` (flag absent).
    * Empty / whitespace-only string → ``None`` (operator passed an
      explicit blank — treat as absent so a YAML / .env interpolation
      that resolves to "" does not crash the scaffold).
    * Non-``http(s)`` scheme → :class:`ReferenceURLError`.
    * Length > :data:`MAX_REFERENCE_URL_LENGTH` → :class:`ReferenceURLError`.
    * Otherwise: returns the trimmed string.

    Why we do not call :mod:`urllib.parse` here: the extractor's
    default fetcher already rejects malformed URLs at network time
    with a fail-soft envelope.  We only need a quick syntactic gate
    so an obviously-wrong value (``ftp://``, ``javascript:``, a 5 KiB
    blob) fails loud at scaffold-CLI parse time rather than silently
    producing an empty spec.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReferenceURLError(
            "reference_url must be a string or None, "
            f"got {type(value).__name__}"
        )
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_REFERENCE_URL_LENGTH:
        raise ReferenceURLError(
            f"reference_url exceeds {MAX_REFERENCE_URL_LENGTH} characters "
            f"(got {len(trimmed)})"
        )
    scheme, sep, _ = trimmed.partition("://")
    if not sep:
        raise ReferenceURLError(
            f"reference_url missing scheme://: {trimmed!r}"
        )
    if scheme.lower() not in SUPPORTED_REFERENCE_SCHEMES:
        raise ReferenceURLError(
            f"reference_url scheme {scheme!r} not in "
            f"{sorted(SUPPORTED_REFERENCE_SCHEMES)}"
        )
    return trimmed


def resolve_reference_url(
    value: object,
    *,
    fetch: Callable[[str], tuple[int, str]] | None = None,
    now: Callable[[], str] | None = None,
) -> BrandSpec | None:
    """High-level: turn a raw ``--reference-url`` value into a BrandSpec.

    Flow::

        normalize_reference_url(value)  # syntax gate
            └──► None                   → return None (no reference)
            └──► "https://example.com"  → extract_brand_from_url(...)
                                          └──► BrandSpec (possibly empty
                                                  on transient fetch
                                                  failure — fail-soft)

    Parameters
    ----------
    value : object
        Raw value from :class:`argparse.Namespace` — typically
        ``args.reference_url``.  ``None`` / empty / whitespace ⇒ no
        reference (returns ``None``).
    fetch : callable, optional
        DI seam — forwarded to
        :func:`backend.brand_extractor.extract_brand_from_url`.  Tests
        inject a fake fetcher so this module never touches the network.
    now : callable, optional
        DI seam for the timestamp — forwarded to the extractor.

    Returns
    -------
    BrandSpec | None
        ``None`` when the flag was absent / blank.  Otherwise a
        :class:`BrandSpec` — possibly empty (palette/fonts/heading/spacing/radius
        all empty) carrying ``source_url`` + ``extracted_at`` for the
        audit trail when the fetch failed.

    Raises
    ------
    ReferenceURLError
        On a structurally invalid value (bad scheme, overlong, wrong
        type).  Network / non-200 failures do *not* raise — they
        surface as the empty-spec envelope per
        :func:`extract_brand_from_url`'s contract.
    """
    canonical = normalize_reference_url(value)
    if canonical is None:
        return None
    try:
        spec = extract_brand_from_url(canonical, fetch=fetch, now=now)
    except BrandSpecError as exc:
        # Should not happen — normalize_reference_url filters out the
        # empty-string case the extractor rejects — but keep the
        # transformation stable for forward-compat.
        raise ReferenceURLError(str(exc)) from exc
    logger.info(
        "resolve_reference_url: %s → palette=%d fonts=%d "
        "heading_levels=%d spacing=%d radius=%d (empty=%s)",
        canonical,
        len(spec.palette),
        len(spec.fonts),
        sum(
            1 for level in ("h1", "h2", "h3", "h4", "h5", "h6")
            if getattr(spec.heading, level) is not None
        ),
        len(spec.spacing),
        len(spec.radius),
        spec.is_empty,
    )
    return spec
