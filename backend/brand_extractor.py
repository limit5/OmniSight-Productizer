"""W12.2 — :func:`extract_brand_from_url`: 5-dim brand fingerprint extractor.

Pulls a :class:`backend.brand_spec.BrandSpec` *from an external reference
URL*.  W12.1 landed the type backbone; this module is the reverse-mode
counterpart of the B5 forward-mode validator
(:mod:`backend.brand_consistency_validator`).  W12.4 will wire the
result into the Scaffold's ``--reference-url`` flag, W12.5 will persist
it to ``.omnisight/brand.json``, and W12.6 will pin the contract via
the 8-URL × 5-dim reference matrix.

Pipeline
--------

For every fetched payload we:

1. Tally every colour literal (hex / ``rgb()`` / ``hsl()``) as a
   weighted RGB pixel.  "Rendered pixels" without a real headless
   browser would be a per-character canvas — instead we treat each
   colour reference as one pixel and weigh it by occurrence count.
   This stays stdlib-only (Production Readiness Gate §158 auto-pass —
   no new pip dep / no image rebuild / no Alembic migration), at the
   cost of not tallying images: a brand whose primary colour appears
   only inside an SVG ``<image>`` is invisible to us.  W12.6 pins
   what is and is not detectable in the reference matrix.
2. Run **weighted k-means** (k-means++ seeded, deterministic via
   :class:`random.Random`) over the pixels.  Centroid hexes are
   sorted by cluster total weight descending — that is the dominance
   order :class:`BrandSpec` documents.
3. Walk every ``font-family`` (CSS) and ``fontFamily`` (JSX inline)
   declaration, drop generic CSS keywords + ``var(--…)`` indirection,
   tally per-family frequency, surface in dominance order.
4. Parse every CSS rule whose selector mentions ``h1`` … ``h6`` for
   ``font-size`` and convert the value to CSS pixels (``px`` direct,
   ``rem`` × 16, ``em`` × 16 — see :data:`_REM_BASE_PX` for the
   approximation note).  First declaration wins per level so a
   reset-CSS preceding a brand stylesheet does not clobber the brand.
5. Tally every ``padding`` / ``margin`` / ``gap`` value as the
   spacing scale; every ``border-radius`` value as the radius scale.
   Both are de-duped + sorted ascending by :func:`BrandSpec.__post_init__`
   on construction; we only need to surface the raw set.

Wire contract
-------------

* :func:`extract_brand_from_url` is the public entry point — accepts
  an injectable ``fetch`` callable for testing.  Default fetcher is
  stdlib :mod:`urllib.request` capped at :data:`_MAX_FETCH_BYTES`.
* :func:`extract_brand_from_text` is the pure-function entry point
  used by tests / callers that already have the payload (e.g. the
  Scaffold has the HTML in memory after a snapshot).
* Every failure path (DNS / connection / non-200 / decode) returns an
  **empty** :class:`BrandSpec` carrying ``source_url`` and
  ``extracted_at`` so the audit trail records "we tried, got nothing".
  Mirrors the B5 forward-mode validator's never-crash-mid-deploy
  contract.
* Determinism: identical input + identical ``seed`` ⇒ identical
  palette tuple.  This is what makes W12.6's reference matrix a
  meaningful regression gate — a drift in extraction surfaces as a
  diff, not as flake.

Module-global state audit (SOP §1)
----------------------------------

Only immutable constants (compiled-regex singletons,
``_GENERIC_FONT_KEYWORDS`` frozenset, the ``DEFAULT_*`` ints, the
``_REM_BASE_PX`` float) plus the standard module-level ``logger``.
Cross-worker consistency: SOP answer #1 — every ``uvicorn`` worker
derives identical constants from identical source.

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure function family, no DB, no shared in-memory state.

Compat-fingerprint grep (SOP §3)
--------------------------------

N/A — no DB / PG / SQLite code path; grep returns 0 hits for the
4 fingerprints (``_conn()`` / ``await conn.commit()`` / ``datetime('now')`` /
``VALUES (?, ?)``).
"""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping

from backend.brand_consistency_validator import (
    _split_font_stack,
    extract_font_families,
    extract_hex_colors,
    extract_hsl_colors,
    extract_rgb_colors,
    normalize_font_name,
    normalize_hex,
)
from backend.brand_spec import (
    BrandSpec,
    BrandSpecError,
    HEADING_LEVELS,
    HeadingScale,
)

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_KMEANS_MAX_ITER",
    "DEFAULT_KMEANS_SEED",
    "DEFAULT_PALETTE_K",
    "extract_brand_from_text",
    "extract_brand_from_url",
    "extract_font_stack",
    "extract_heading_scale",
    "extract_radius_scale",
    "extract_spacing_scale",
    "kmeans_palette",
    "tally_pixels",
]


#: Default cluster count for the palette k-means.  Five is the W12.1
#: ``DIMENSIONS`` legend default — most design systems publish a 5-tone
#: brand sheet (primary / secondary / accent / surface / muted) and
#: that's a good upper bound before centroid pollution from chrome /
#: text / shadow noise dominates.
DEFAULT_PALETTE_K = 5

#: Seed for the deterministic RNG used by k-means++ initialisation.
#: Pinned so the W12.6 reference-matrix comparison is byte-stable.
DEFAULT_KMEANS_SEED = 0

#: Hard cap on Lloyd-iteration loop.  K-means typically converges in
#: 10-20 iterations on real palette data; 50 leaves safety margin
#: without making degenerate inputs (all-black page) hang.
DEFAULT_KMEANS_MAX_ITER = 50

#: Maximum bytes the default fetcher will read from a URL.  A
#: misbehaving server cannot exhaust the worker — the cap matches the
#: ``max_bytes_per_file`` style bound used by the forward-mode
#: validator's per-file scanner.
_MAX_FETCH_BYTES = 8 * 1024 * 1024

#: Approximate root-em → px conversion.  We do not have a parent
#: stylesheet / cascade context (the extractor is selector-naive by
#: design), so ``em`` is treated identically to ``rem`` × 16.  This
#: is documented as approximation in W12.6's notes — the alternative
#: is to skip ``em`` entirely, which would lose every Bootstrap-style
#: theme that publishes ``em``-only typography.
_REM_BASE_PX = 16.0

#: Generic CSS font keywords reused from the B5 forward-mode validator.
#: Repeated here rather than imported from
#: :mod:`backend.brand_consistency_validator` because that module
#: keeps it module-private (single-leading-underscore) and W12.3 will
#: extract it into a shared types module.  Until W12.3 lands, mirror
#: the literal set so a divergence is impossible.
_GENERIC_FONT_KEYWORDS: frozenset[str] = frozenset({
    "sans-serif",
    "serif",
    "monospace",
    "cursive",
    "fantasy",
    "system-ui",
    "ui-sans-serif",
    "ui-serif",
    "ui-monospace",
    "ui-rounded",
    "emoji",
    "math",
    "fangsong",
    "inherit",
    "initial",
    "unset",
    "revert",
    "revert-layer",
    "-apple-system",
    "blinkmacsystemfont",
})


# ── Pixel tally + colour helpers ────────────────────────────────────


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    s = hex_str.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"


def _clamp_round(value: float) -> int:
    if value < 0:
        return 0
    if value > 255:
        return 255
    return int(round(value))


def tally_pixels(text: str) -> dict[tuple[int, int, int], int]:
    """Return ``{(r, g, b): occurrence_count}`` for every colour literal.

    Used as the input to :func:`kmeans_palette`.  Exposed publicly so
    callers (W12.6 reference matrix) can introspect the raw distribution
    before clustering, which is occasionally useful for debugging "why
    is this brand surfaced as ``#888888``".
    """
    if not isinstance(text, str) or not text:
        return {}
    counts: dict[tuple[int, int, int], int] = {}
    for raw, _ in extract_hex_colors(text):
        canonical = normalize_hex(raw)
        if canonical is None:
            continue
        rgb = _hex_to_rgb(canonical)
        counts[rgb] = counts.get(rgb, 0) + 1
    for canonical, _ in extract_rgb_colors(text):
        # ``extract_rgb_colors`` has already normalised to ``#rrggbb``.
        rgb = _hex_to_rgb(canonical)
        counts[rgb] = counts.get(rgb, 0) + 1
    for canonical, _ in extract_hsl_colors(text):
        rgb = _hex_to_rgb(canonical)
        counts[rgb] = counts.get(rgb, 0) + 1
    return counts


# ── Weighted k-means ────────────────────────────────────────────────


def _sq_distance(
    a: tuple[float, float, float] | tuple[int, int, int],
    b: tuple[float, float, float] | tuple[int, int, int],
) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _nearest_centroid(
    rgb: tuple[int, int, int],
    centroids: list[tuple[float, float, float]],
) -> int:
    best = 0
    best_d = _sq_distance(rgb, centroids[0])
    for i in range(1, len(centroids)):
        d = _sq_distance(rgb, centroids[i])
        if d < best_d:
            best_d = d
            best = i
    return best


def _weighted_choice(
    weights: list[float],
    rng: random.Random,
) -> int:
    total = sum(weights)
    if total <= 0:
        return 0
    target = rng.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if acc >= target:
            return i
    return len(weights) - 1


def _kmeans_pp_seed(
    points: list[tuple[tuple[int, int, int], int]],
    k: int,
    rng: random.Random,
) -> list[tuple[float, float, float]]:
    """k-means++ seeding with weighted points."""
    if not points or k <= 0:
        return []
    # First centroid: weighted-random by occurrence frequency.
    weights = [float(p[1]) for p in points]
    first_idx = _weighted_choice(weights, rng)
    centroids: list[tuple[float, float, float]] = [
        (float(points[first_idx][0][0]),
         float(points[first_idx][0][1]),
         float(points[first_idx][0][2])),
    ]
    while len(centroids) < k:
        # D²(p) × weight(p) for the next-centroid sampling distribution.
        d2_weights = [
            _min_sq_distance(p[0], centroids) * p[1]
            for p in points
        ]
        if sum(d2_weights) <= 0:
            # Every point already coincides with an existing centroid;
            # cannot grow further without duplicating.
            break
        idx = _weighted_choice(d2_weights, rng)
        centroids.append((
            float(points[idx][0][0]),
            float(points[idx][0][1]),
            float(points[idx][0][2]),
        ))
    return centroids


def _min_sq_distance(
    rgb: tuple[int, int, int],
    centroids: list[tuple[float, float, float]],
) -> float:
    if not centroids:
        return 0.0
    return min(_sq_distance(rgb, c) for c in centroids)


def kmeans_palette(
    pixels: Mapping[tuple[int, int, int], int]
    | Iterable[tuple[tuple[int, int, int], int]],
    *,
    k: int = DEFAULT_PALETTE_K,
    seed: int = DEFAULT_KMEANS_SEED,
    max_iter: int = DEFAULT_KMEANS_MAX_ITER,
) -> tuple[str, ...]:
    """Run weighted k-means; return centroids as canonical ``#rrggbb`` hexes.

    Output is sorted by cluster total weight descending — the most
    occurring colour cluster is first, which is the dominance order
    :class:`BrandSpec.palette` documents.  Empty clusters (no point
    landed nearer this centroid than any other) are dropped.
    Duplicate centroid hexes (two clusters rounded to the same RGB)
    are de-duped preserving the heavier one's position.
    """
    if isinstance(pixels, Mapping):
        points = list(pixels.items())
    else:
        points = list(pixels)
    if not points or k <= 0:
        return ()

    # Fewer unique colours than k: skip the whole loop and surface
    # them sorted by raw weight desc.  k-means on n < k would either
    # converge with empty clusters or duplicate centroids — both
    # noise the caller doesn't want.
    if len(points) <= k:
        sorted_pts = sorted(points, key=lambda p: (-p[1], p[0]))
        return tuple(_rgb_to_hex(p[0]) for p in sorted_pts)

    rng = random.Random(seed)
    centroids = _kmeans_pp_seed(points, k, rng)
    if not centroids:
        return ()

    weights = [0] * len(centroids)
    for _iteration in range(max_iter):
        sums = [[0.0, 0.0, 0.0] for _ in centroids]
        weights = [0] * len(centroids)
        for rgb, w in points:
            best = _nearest_centroid(rgb, centroids)
            sums[best][0] += rgb[0] * w
            sums[best][1] += rgb[1] * w
            sums[best][2] += rgb[2] * w
            weights[best] += w
        new_centroids: list[tuple[float, float, float]] = []
        for i, (s, w) in enumerate(zip(sums, weights)):
            if w == 0:
                # Empty cluster — keep the seed centroid so the loop
                # remains stable; the cluster is filtered out below.
                new_centroids.append(centroids[i])
            else:
                new_centroids.append((s[0] / w, s[1] / w, s[2] / w))
        movement = sum(
            _sq_distance(a, b)
            for a, b in zip(centroids, new_centroids)
        )
        centroids = new_centroids
        if movement < 0.5:
            break

    # Sort clusters by weight descending; tiebreak by canonical hex
    # so the order is total + deterministic regardless of init order.
    indexed = sorted(
        range(len(centroids)),
        key=lambda i: (
            -weights[i],
            _rgb_to_hex(
                (_clamp_round(centroids[i][0]),
                 _clamp_round(centroids[i][1]),
                 _clamp_round(centroids[i][2]))
            ),
        ),
    )
    out: list[str] = []
    seen: set[str] = set()
    for idx in indexed:
        if weights[idx] == 0:
            continue
        rgb = (
            _clamp_round(centroids[idx][0]),
            _clamp_round(centroids[idx][1]),
            _clamp_round(centroids[idx][2]),
        )
        hex_value = _rgb_to_hex(rgb)
        if hex_value not in seen:
            seen.add(hex_value)
            out.append(hex_value)
    return tuple(out)


# ── Font extraction ─────────────────────────────────────────────────


def extract_font_stack(text: str) -> tuple[str, ...]:
    """Return non-generic font families in dominance (frequency) order.

    Generic CSS keywords (``sans-serif`` etc.) and ``var(--…)``
    indirections are dropped — :class:`BrandSpec.fonts` is meant for
    concrete brand families, with the cascade fallback handled at the
    consumer site.  Tiebreak when two families appear equally often
    is alphabetical so the order is total.
    """
    if not isinstance(text, str) or not text:
        return ()
    counts: dict[str, int] = {}
    for raw_stack, _ in extract_font_families(text):
        for piece in _split_font_stack(raw_stack):
            stripped = piece.strip()
            if stripped.lower().startswith("var(--"):
                continue
            normalised = normalize_font_name(stripped)
            if not normalised:
                continue
            if normalised in _GENERIC_FONT_KEYWORDS:
                continue
            counts[normalised] = counts.get(normalised, 0) + 1
    sorted_fonts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(name for name, _ in sorted_fonts)


# ── Heading scale extraction ────────────────────────────────────────


# Match every CSS rule.  ``[^{}]+`` stops at brace boundaries so
# nested at-rules (``@media`` / ``@supports``) are *not* matched as a
# single block — they fall through and the inner h-selector blocks
# match individually on the next pass.  We do not implement a full CSS
# parser: a real brand stylesheet's heading rules sit at the top
# level and this regex catches them.  Pathological deeply-nested
# at-rules degrade silently to "no heading data" rather than crash.
_CSS_BLOCK_RE = re.compile(r"([^{}@]+?)\{([^{}]*)\}")

_FONT_SIZE_RE = re.compile(
    r"font-size\s*:\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>px|rem|em|%)?",
    re.IGNORECASE,
)

# Match a heading level token in a selector — the lookbehind/lookahead
# pair guards against ``.h1foo`` (lookahead) and ``foo-h1`` (lookbehind)
# false positives.  Allowed boundaries cover the CSS combinators we
# care about plus the start/end of the selector string.
_HEADING_TOKEN_RES: dict[str, re.Pattern[str]] = {
    level: re.compile(
        rf"(?:^|[\s,>+~])({level})(?=[\s,>{{\[:.]|$)",
        re.IGNORECASE,
    )
    for level in HEADING_LEVELS
}


def _parse_size_to_px(value: str, unit: str | None) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    u = (unit or "px").lower()
    if u == "px":
        return n
    if u == "rem":
        return n * _REM_BASE_PX
    if u == "em":
        # No cascade context — treat as rem.  Documented approximation.
        return n * _REM_BASE_PX
    # ``%`` and any other unit have no concrete px mapping without
    # knowing the parent size.  Skip rather than guess.
    return None


def extract_heading_scale(text: str) -> HeadingScale:
    """Parse ``hN { font-size: …px }`` rules into a :class:`HeadingScale`.

    First declaration per level wins so a global reset stylesheet that
    sets ``h1 { font-size: 100% }`` does not clobber a later
    brand-specific rule — but the brand rule must precede or coexist
    in source order, which matches CSS author convention.  ``%`` /
    unitless values are skipped (no concrete px without cascade).
    """
    if not isinstance(text, str) or not text:
        return HeadingScale()
    sizes: dict[str, float] = {}
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1)
        body = m.group(2)
        fs_match = _FONT_SIZE_RE.search(body)
        if fs_match is None:
            continue
        px = _parse_size_to_px(
            fs_match.group("value"), fs_match.group("unit"),
        )
        if px is None:
            continue
        for level in HEADING_LEVELS:
            if level in sizes:
                continue
            if _HEADING_TOKEN_RES[level].search(selector):
                sizes[level] = px
    return HeadingScale(
        h1=sizes.get("h1"),
        h2=sizes.get("h2"),
        h3=sizes.get("h3"),
        h4=sizes.get("h4"),
        h5=sizes.get("h5"),
        h6=sizes.get("h6"),
    )


# ── Spacing / radius extraction ─────────────────────────────────────


_SPACING_PROP_RE = re.compile(
    r"(?:^|[;{}\s])"
    r"(padding(?:-(?:top|right|bottom|left))?"
    r"|margin(?:-(?:top|right|bottom|left))?"
    r"|gap|row-gap|column-gap)"
    r"\s*:\s*(?P<value>[^;}]+)",
    re.IGNORECASE,
)

_RADIUS_PROP_RE = re.compile(
    r"(?:^|[;{}\s])"
    r"(border-radius"
    r"|border-(?:top-left|top-right|bottom-left|bottom-right)-radius)"
    r"\s*:\s*(?P<value>[^;}]+)",
    re.IGNORECASE,
)

_PX_VALUE_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(px|rem|em)?",
    re.IGNORECASE,
)


def _values_from_decl(decl: str) -> list[float]:
    out: list[float] = []
    for m in _PX_VALUE_RE.finditer(decl):
        try:
            n = float(m.group(1))
        except ValueError:
            continue
        if n < 0:
            continue
        unit = (m.group(2) or "").lower()
        if unit == "" or unit == "px":
            # Treat unitless "0" as 0 px (the canonical CSS shortcut).
            # Other unitless numbers are likely font-weights / line-
            # heights swept up by the surrounding regex by accident;
            # we filter those out by only allowing px/rem/em explicitly
            # on non-zero values.
            if unit == "" and n != 0.0:
                continue
            out.append(n)
        elif unit == "rem":
            out.append(n * _REM_BASE_PX)
        elif unit == "em":
            out.append(n * _REM_BASE_PX)
    return out


def extract_spacing_scale(text: str) -> tuple[float, ...]:
    """Tally every ``padding``/``margin``/``gap`` value as the spacing scale.

    Returned tuple is *not* canonicalised — :class:`BrandSpec.__post_init__`
    will sort + dedup it.  Returning the raw set keeps this helper
    composable with other extraction sources (e.g. W12.6's reference
    matrix may want to introspect raw counts before canonicalisation).
    """
    if not isinstance(text, str) or not text:
        return ()
    values: list[float] = []
    for m in _SPACING_PROP_RE.finditer(text):
        values.extend(_values_from_decl(m.group("value")))
    return tuple(values)


def extract_radius_scale(text: str) -> tuple[float, ...]:
    """Tally every ``border-radius`` (long-hand or shorthand) value."""
    if not isinstance(text, str) or not text:
        return ()
    values: list[float] = []
    for m in _RADIUS_PROP_RE.finditer(text):
        values.extend(_values_from_decl(m.group("value")))
    return tuple(values)


# ── Top-level extractors ────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_brand_from_text(
    text: str,
    *,
    source_url: str | None = None,
    k: int = DEFAULT_PALETTE_K,
    seed: int = DEFAULT_KMEANS_SEED,
    max_iter: int = DEFAULT_KMEANS_MAX_ITER,
    extracted_at: str | None = None,
) -> BrandSpec:
    """Run the full 5-dim extraction pipeline over a raw HTML/CSS payload.

    Pure function — no I/O.  The companion :func:`extract_brand_from_url`
    fetches the payload then delegates here.  Tests / Scaffold callers
    that already hold the body in memory call this directly.
    """
    if not isinstance(text, str):
        raise BrandSpecError(
            f"extract_brand_from_text: text must be str, got {type(text).__name__}"
        )
    pixels = tally_pixels(text)
    palette = kmeans_palette(pixels, k=k, seed=seed, max_iter=max_iter)
    fonts = extract_font_stack(text)
    heading = extract_heading_scale(text)
    spacing = extract_spacing_scale(text)
    radius = extract_radius_scale(text)
    return BrandSpec(
        palette=palette,
        fonts=fonts,
        heading=heading,
        spacing=spacing,
        radius=radius,
        source_url=source_url,
        extracted_at=extracted_at if extracted_at is not None else _utc_now_iso(),
    )


def extract_brand_from_url(
    url: str,
    *,
    fetch: Callable[[str], tuple[int, str]] | None = None,
    k: int = DEFAULT_PALETTE_K,
    seed: int = DEFAULT_KMEANS_SEED,
    max_iter: int = DEFAULT_KMEANS_MAX_ITER,
    now: Callable[[], str] | None = None,
) -> BrandSpec:
    """Fetch ``url`` and return the extracted :class:`BrandSpec`.

    ``fetch`` is the dependency injection seam for tests — must be a
    callable ``(url) -> (status_code, text)``.  ``now`` similarly
    overrides the timestamp for deterministic snapshot tests.

    Fail-soft contract
    ------------------

    * Empty / non-string ``url`` → :class:`BrandSpecError`.
    * Fetcher exception → empty :class:`BrandSpec` carrying
      ``source_url`` + ``extracted_at`` (audit).
    * Non-200 status → same empty-spec response.

    Why we never re-raise: the extractor will be called in agent
    pipelines (W12.4 ``--reference-url`` flag) where a transient
    network blip should not crash the deploy.  The returned empty
    spec lets downstream readers detect "no brand data" and fall back
    to project-local tokens.
    """
    if not isinstance(url, str) or not url.strip():
        raise BrandSpecError(
            "extract_brand_from_url: url must be a non-empty string"
        )
    fetcher = fetch if fetch is not None else _default_fetch
    timestamp = now() if now is not None else _utc_now_iso()
    try:
        status, body = fetcher(url)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "extract_brand_from_url: fetch failed for %s: %s", url, exc,
        )
        return BrandSpec(source_url=url, extracted_at=timestamp)
    if status != 200 or not isinstance(body, str) or not body:
        logger.info(
            "extract_brand_from_url: %s status=%s; returning empty spec",
            url, status,
        )
        return BrandSpec(source_url=url, extracted_at=timestamp)
    return extract_brand_from_text(
        body,
        source_url=url,
        k=k,
        seed=seed,
        max_iter=max_iter,
        extracted_at=timestamp,
    )


def _default_fetch(url: str) -> tuple[int, str]:
    """Stdlib :mod:`urllib.request` fetcher capped at :data:`_MAX_FETCH_BYTES`.

    Kept tiny on purpose — operators that need redirects / custom
    headers / proxy / TLS pinning should inject their own ``fetch=``.
    Imported lazily so import-time of this module never touches the
    network stack (matches the B5 forward-mode validator's
    ``scan_url`` discipline).
    """
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        body_bytes = resp.read(_MAX_FETCH_BYTES)
        status = getattr(resp, "status", 200)
        text = body_bytes.decode("utf-8", errors="replace")
        return status, text


# Preserve alphabetical __all__ for grep-friendliness — matches the
# discipline at the bottom of brand_consistency_validator.py.
__all__ = sorted(set(__all__))
