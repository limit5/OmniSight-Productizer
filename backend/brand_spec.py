"""W12.1 — :class:`BrandSpec` dataclass: 5-dim brand-style fingerprint.

The B5 forward-mode validator (:mod:`backend.brand_consistency_validator`)
checks that a deployed build does not drift away from the project's own
design tokens.  W12 adds a **reverse mode** — pull a brand fingerprint
*from an external reference URL* and use it as the new design-spec
basis for downstream agent edits (W12.2 extracts; W12.4 wires it into
the scaffold flag; W12.5 persists it to ``.omnisight/brand.json``).

This module ships **only the type** (W12.1).  Extraction
(:func:`extract_brand_from_url`, k-means on rendered pixels), the
``--reference-url`` CLI flag, the JSON persistence path, and the test
matrix all land in W12.2–W12.6.  Splitting the type out first means
later rows can import from a stable surface without a cyclic
``brand_consistency_validator``-extension dance.

Wire contract
-------------

* :class:`BrandSpec` is a frozen dataclass — instances are hashable
  and safe to share across workers without a lock.  Mutation happens
  by ``dataclasses.replace`` (via :meth:`BrandSpec.replace_with`), not
  by attribute assignment.
* All five style dimensions normalise on construction:
  ``palette`` → lowercase ``#rrggbb`` tuple in input order (the
  extractor surfaces colours by cluster dominance, so order is
  meaningful); ``fonts`` → lowercase canonicalised tuple in input
  order (primary → fall-back); ``heading`` → :class:`HeadingScale`
  with non-negative floats; ``spacing`` / ``radius`` → tuples of
  non-negative floats sorted ascending and de-duplicated.
* :meth:`BrandSpec.to_dict` / :classmethod:`BrandSpec.from_dict`
  round-trip through canonical JSON — that is what W12.5 will write
  to ``.omnisight/brand.json``.  ``schema_version`` is baked into
  the dict so a future rev can refuse / migrate older payloads.

Module-global state audit (SOP §1)
----------------------------------

This module has **no mutable module-level state**.  Only immutable
constants (``BRAND_SPEC_SCHEMA_VERSION`` string, ``DIMENSIONS`` tuple,
``HEADING_LEVELS`` tuple, ``_HEX6_RE`` compiled-regex singleton, the
generic-font keyword frozenset reused from
:mod:`backend.brand_consistency_validator`) plus the standard
module-level ``logger``.  Cross-worker consistency falls under SOP
answer #1: each ``uvicorn`` worker derives identical constants from
identical source.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)


__all__ = [
    "BRAND_SPEC_SCHEMA_VERSION",
    "BrandSpec",
    "BrandSpecError",
    "DIMENSIONS",
    "HEADING_LEVELS",
    "HeadingScale",
    "canonicalise_font_name",
    "canonicalise_hex",
    "canonicalise_scale",
    "spec_from_json",
    "spec_to_json",
]


# Bumped when the JSON-safe shape of a :meth:`BrandSpec.to_dict`
# payload (or any nested dataclass) changes in a way that would force
# a reader written for the previous version to break.
BRAND_SPEC_SCHEMA_VERSION = "1.0.0"

#: The five style dimensions W12.1 freezes.  Used as the public legend
#: for ``BrandSpec.to_dict()`` consumers and as the parametrize axis
#: for the W12.6 test matrix.
DIMENSIONS: tuple[str, ...] = (
    "palette",
    "fonts",
    "heading",
    "spacing",
    "radius",
)

#: Heading levels in CSS order.  ``HeadingScale`` mirrors this tuple
#: 1-for-1 — lock-stepped so a future ``h7`` (would never happen, but)
#: would force a schema-version bump rather than a silent drift.
HEADING_LEVELS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")


# Reused validation regex — accept canonical ``#rrggbb`` lowercase
# only.  The extractor (W12.2) normalises before constructing a spec,
# so by the time a hex reaches :class:`BrandSpec` it has already been
# through :func:`canonicalise_hex`.  Re-validating keeps `from_dict`
# safe on hand-edited ``.omnisight/brand.json`` payloads.
_HEX6_RE = re.compile(r"^#[0-9a-f]{6}$")


class BrandSpecError(ValueError):
    """Raised on input-shape violations during construction or load.

    Subclasses :class:`ValueError` so existing ``except ValueError``
    chains catch it; callers that need to react specifically (W12.2's
    extractor wrapping a partial-spec failure, W12.5's loader on
    corrupted ``.omnisight/brand.json``) can ``except BrandSpecError``.
    """


# ── Canonicalisation helpers ────────────────────────────────────────


def canonicalise_hex(value: object) -> str:
    """Return canonical ``#rrggbb`` lowercase or raise :class:`BrandSpecError`.

    Accepts ``"#rrggbb"`` / ``"#RRGGBB"`` / ``"#rgb"`` (CSS short hex
    expanded by digit-doubling) / ``"#rrggbbaa"`` (alpha dropped — the
    palette compares on RGB only, matching the existing forward-mode
    validator's ``normalize_hex`` behaviour).  Anything else raises so
    a constructor never silently swallows a malformed colour.
    """
    if not isinstance(value, str):
        raise BrandSpecError(
            f"palette entry must be str, got {type(value).__name__}"
        )
    s = value.strip()
    if not s.startswith("#"):
        raise BrandSpecError(f"palette entry {value!r} missing leading '#'")
    digits = s[1:]
    if not all(c in "0123456789abcdefABCDEF" for c in digits):
        raise BrandSpecError(f"palette entry {value!r} contains non-hex digit")
    if len(digits) == 3:
        r, g, b = digits
        return f"#{r}{r}{g}{g}{b}{b}".lower()
    if len(digits) == 4:
        r, g, b, _alpha = digits
        return f"#{r}{r}{g}{g}{b}{b}".lower()
    if len(digits) == 6:
        return f"#{digits.lower()}"
    if len(digits) == 8:
        return f"#{digits[:6].lower()}"
    raise BrandSpecError(
        f"palette entry {value!r} must have 3/4/6/8 hex digits"
    )


def canonicalise_font_name(value: object) -> str:
    """Return a font-family name with quotes / whitespace / case stripped.

    Empty / whitespace-only inputs raise :class:`BrandSpecError` —
    the extractor must surface a real family name (an empty string in
    a font fall-back stack indicates a parser bug, not a brand
    decision).  Generic CSS keywords (``sans-serif`` etc.) are kept
    as-is — they are valid fall-backs and W12.5 readers may want to
    preserve them when rebuilding the CSS stack downstream.
    """
    if not isinstance(value, str):
        raise BrandSpecError(
            f"font entry must be str, got {type(value).__name__}"
        )
    s = value.strip().strip("'").strip('"').strip()
    if not s:
        raise BrandSpecError("font entry must not be empty after stripping quotes")
    return s.lower()


def canonicalise_scale(values: Iterable[object]) -> tuple[float, ...]:
    """Return a sorted-ascending de-duplicated tuple of non-negative floats.

    Used for both ``spacing`` and ``radius``.  De-dup tolerance is
    exact-equality on the float representation — the extractor (W12.2)
    is responsible for any clustering / rounding; the type only enforces
    invariant ordering + non-negativity so downstream readers can
    binary-search or pattern-match without re-sorting.
    """
    out: list[float] = []
    for raw in values:
        if isinstance(raw, bool):
            # ``bool`` is a subclass of ``int`` — guard so ``True``
            # isn't silently coerced to ``1.0`` in a scale.
            raise BrandSpecError(
                "scale entry must be int / float, not bool"
            )
        if not isinstance(raw, (int, float)):
            raise BrandSpecError(
                f"scale entry must be int / float, got {type(raw).__name__}"
            )
        v = float(raw)
        if v < 0:
            raise BrandSpecError(f"scale entry must be non-negative, got {v}")
        out.append(v)
    # Sort ascending then de-dup preserving the first occurrence.
    out.sort()
    deduped: list[float] = []
    for v in out:
        if not deduped or deduped[-1] != v:
            deduped.append(v)
    return tuple(deduped)


# ── Heading scale ───────────────────────────────────────────────────


@dataclass(frozen=True)
class HeadingScale:
    """Heading sizes in CSS pixels for ``h1`` through ``h6``.

    ``None`` for any level means "the reference site does not declare
    a distinct rule for this level" — the W12.2 extractor surfaces
    explicit declarations only, never a fabricated extrapolation, so
    downstream agents can tell "missing data" from "intentionally same
    as parent".
    """

    h1: float | None = None
    h2: float | None = None
    h3: float | None = None
    h4: float | None = None
    h5: float | None = None
    h6: float | None = None

    def __post_init__(self) -> None:
        for level in HEADING_LEVELS:
            v = getattr(self, level)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise BrandSpecError(
                    f"HeadingScale.{level} must be int / float / None, "
                    f"got {type(v).__name__}"
                )
            if v < 0:
                raise BrandSpecError(
                    f"HeadingScale.{level} must be non-negative, got {v}"
                )
            if isinstance(v, int):
                # Normalise to float so equality + JSON shape are stable
                # regardless of caller-side type.
                object.__setattr__(self, level, float(v))

    def to_dict(self) -> dict[str, float | None]:
        """Return a JSON-safe ``{h1: …, h2: …, …}`` mapping."""
        return {level: getattr(self, level) for level in HEADING_LEVELS}

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> "HeadingScale":
        """Inverse of :meth:`to_dict`.

        ``None`` / empty mapping → an empty :class:`HeadingScale`.
        Unknown keys → :class:`BrandSpecError` so a typo or schema
        drift is caught at load time, not at the first lookup.
        """
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise BrandSpecError(
                f"HeadingScale payload must be Mapping, got {type(data).__name__}"
            )
        unknown = set(data) - set(HEADING_LEVELS)
        if unknown:
            raise BrandSpecError(
                f"HeadingScale payload has unknown keys: {sorted(unknown)}"
            )
        kwargs: dict[str, float | None] = {}
        for level in HEADING_LEVELS:
            v = data.get(level)
            kwargs[level] = None if v is None else v  # type: ignore[assignment]
        return cls(**kwargs)

    @property
    def is_empty(self) -> bool:
        """``True`` iff every heading level is ``None``."""
        return all(getattr(self, level) is None for level in HEADING_LEVELS)


# ── BrandSpec ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class BrandSpec:
    """5-dim brand-style fingerprint (W12.1).

    Dimensions
    ----------

    palette : tuple[str, ...]
        Brand hex colours in dominance order (canonical ``#rrggbb``
        lowercase).  Order matters — W12.2 surfaces colours sorted by
        k-means cluster size; the first entry is the dominant brand
        colour and downstream readers may treat it as the implicit
        ``--primary``.

    fonts : tuple[str, ...]
        Font-family names in fall-back order (lowercase canonicalised).
        First entry is the primary display family.  Generic CSS
        keywords (``sans-serif`` etc.) are preserved as-is.

    heading : :class:`HeadingScale`
        Per-level heading sizes in CSS px (``h1`` … ``h6``).  ``None``
        for an unset level — distinct from "0px" which would mean
        "intentionally hidden".

    spacing : tuple[float, ...]
        Spacing rhythm in CSS px, sorted ascending and de-duplicated.

    radius : tuple[float, ...]
        Border-radius scale in CSS px, sorted ascending and de-duplicated.

    Provenance (optional)
    ---------------------

    source_url : str | None
        URL the spec was extracted from (W12.2).  ``None`` when the
        spec is hand-authored.

    extracted_at : str | None
        ISO-8601 UTC timestamp of extraction.  Free-form string here
        (validation only checks non-empty if present) — the extractor
        formats it; this type stays I/O-free.

    schema_version : str
        Pinned to :data:`BRAND_SPEC_SCHEMA_VERSION` at construction so
        a serialised payload is self-describing on round-trip.
    """

    palette: tuple[str, ...] = ()
    fonts: tuple[str, ...] = ()
    heading: HeadingScale = field(default_factory=HeadingScale)
    spacing: tuple[float, ...] = ()
    radius: tuple[float, ...] = ()
    source_url: str | None = None
    extracted_at: str | None = None
    schema_version: str = BRAND_SPEC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # ── palette ──
        if not isinstance(self.palette, (tuple, list)):
            raise BrandSpecError(
                f"palette must be tuple / list, got {type(self.palette).__name__}"
            )
        canonical_palette: list[str] = []
        seen_palette: set[str] = set()
        for entry in self.palette:
            canon = canonicalise_hex(entry)
            if canon not in seen_palette:
                seen_palette.add(canon)
                canonical_palette.append(canon)
        object.__setattr__(self, "palette", tuple(canonical_palette))

        # ── fonts ──
        if not isinstance(self.fonts, (tuple, list)):
            raise BrandSpecError(
                f"fonts must be tuple / list, got {type(self.fonts).__name__}"
            )
        canonical_fonts: list[str] = []
        seen_fonts: set[str] = set()
        for entry in self.fonts:
            canon = canonicalise_font_name(entry)
            if canon not in seen_fonts:
                seen_fonts.add(canon)
                canonical_fonts.append(canon)
        object.__setattr__(self, "fonts", tuple(canonical_fonts))

        # ── heading ──
        if not isinstance(self.heading, HeadingScale):
            raise BrandSpecError(
                f"heading must be HeadingScale, got {type(self.heading).__name__}"
            )

        # ── spacing / radius ──
        if not isinstance(self.spacing, (tuple, list)):
            raise BrandSpecError(
                f"spacing must be tuple / list, got {type(self.spacing).__name__}"
            )
        object.__setattr__(self, "spacing", canonicalise_scale(self.spacing))

        if not isinstance(self.radius, (tuple, list)):
            raise BrandSpecError(
                f"radius must be tuple / list, got {type(self.radius).__name__}"
            )
        object.__setattr__(self, "radius", canonicalise_scale(self.radius))

        # ── provenance ──
        if self.source_url is not None:
            if not isinstance(self.source_url, str) or not self.source_url.strip():
                raise BrandSpecError(
                    "source_url must be a non-empty string when provided"
                )
        if self.extracted_at is not None:
            if not isinstance(self.extracted_at, str) or not self.extracted_at.strip():
                raise BrandSpecError(
                    "extracted_at must be a non-empty string when provided"
                )
        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise BrandSpecError("schema_version must be a non-empty string")

    # ── Properties ──────────────────────────────────────────────────

    @property
    def is_empty(self) -> bool:
        """``True`` iff every style dimension is empty.

        Provenance fields (``source_url`` / ``extracted_at``) do not
        count — an empty extraction can still legitimately carry a
        timestamp + URL for audit.
        """
        return (
            not self.palette
            and not self.fonts
            and self.heading.is_empty
            and not self.spacing
            and not self.radius
        )

    @property
    def primary_color(self) -> str | None:
        """Most-dominant palette colour, or ``None`` if palette empty.

        Convenience for downstream agents that only need ``--primary``.
        """
        return self.palette[0] if self.palette else None

    @property
    def primary_font(self) -> str | None:
        """Primary font family, or ``None`` if fonts empty."""
        return self.fonts[0] if self.fonts else None

    # ── Mutation by replacement ─────────────────────────────────────

    def replace_with(self, **changes: object) -> "BrandSpec":
        """Return a new :class:`BrandSpec` with the given fields replaced.

        Frozen dataclasses cannot mutate in-place; this is the
        sanctioned way to "edit" a spec (e.g. W12.2 may build the
        palette in one pass and then call
        ``spec.replace_with(extracted_at=now())``).  Forwards to
        :func:`dataclasses.replace`, which re-runs ``__post_init__``
        — so canonicalisation + validation re-fires.
        """
        return replace(self, **changes)

    # ── Serialisation ───────────────────────────────────────────────

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe dict mirroring the dataclass fields.

        Tuples become lists at the boundary (JSON has no tuple); the
        ``heading`` nested dataclass becomes its own dict via
        :meth:`HeadingScale.to_dict`; ``schema_version`` rides along
        so a reader can refuse / migrate older payloads.
        """
        return {
            "schema_version": self.schema_version,
            "source_url": self.source_url,
            "extracted_at": self.extracted_at,
            "palette": list(self.palette),
            "fonts": list(self.fonts),
            "heading": self.heading.to_dict(),
            "spacing": list(self.spacing),
            "radius": list(self.radius),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "BrandSpec":
        """Inverse of :meth:`to_dict`.

        Unknown top-level keys are ignored (forward-compat — a future
        schema rev may add a 6th dimension and we want this loader
        not to choke on the new key when running on older code).
        Missing required fields fall back to the dataclass default.
        Type-shape violations raise :class:`BrandSpecError`.
        """
        if not isinstance(data, Mapping):
            raise BrandSpecError(
                f"BrandSpec payload must be Mapping, got {type(data).__name__}"
            )

        palette = data.get("palette", ())
        if not isinstance(palette, (list, tuple)):
            raise BrandSpecError("payload 'palette' must be list / tuple")

        fonts = data.get("fonts", ())
        if not isinstance(fonts, (list, tuple)):
            raise BrandSpecError("payload 'fonts' must be list / tuple")

        heading_payload = data.get("heading")
        if heading_payload is None:
            heading = HeadingScale()
        elif isinstance(heading_payload, HeadingScale):
            heading = heading_payload
        else:
            heading = HeadingScale.from_dict(
                heading_payload  # type: ignore[arg-type]
            )

        spacing = data.get("spacing", ())
        if not isinstance(spacing, (list, tuple)):
            raise BrandSpecError("payload 'spacing' must be list / tuple")

        radius = data.get("radius", ())
        if not isinstance(radius, (list, tuple)):
            raise BrandSpecError("payload 'radius' must be list / tuple")

        schema_version = data.get("schema_version", BRAND_SPEC_SCHEMA_VERSION)
        if not isinstance(schema_version, str):
            raise BrandSpecError("payload 'schema_version' must be str")

        source_url = data.get("source_url")
        extracted_at = data.get("extracted_at")
        return cls(
            palette=tuple(palette),
            fonts=tuple(fonts),
            heading=heading,
            spacing=tuple(spacing),  # type: ignore[arg-type]
            radius=tuple(radius),  # type: ignore[arg-type]
            source_url=source_url if source_url is None else str(source_url),
            extracted_at=extracted_at if extracted_at is None else str(extracted_at),
            schema_version=schema_version,
        )


# ── JSON helpers ────────────────────────────────────────────────────


def spec_to_json(spec: BrandSpec, *, indent: int | None = 2) -> str:
    """Serialise a :class:`BrandSpec` to deterministic JSON.

    ``sort_keys=True`` + ``ensure_ascii=False`` mirrors W11's manifest
    canonicalisation discipline — same input ⇒ byte-identical output,
    so a future ``.omnisight/brand.json`` diff is meaningful.
    """
    if not isinstance(spec, BrandSpec):
        raise BrandSpecError(
            f"spec_to_json expects BrandSpec, got {type(spec).__name__}"
        )
    return json.dumps(
        spec.to_dict(),
        indent=indent,
        ensure_ascii=False,
        sort_keys=True,
    )


def spec_from_json(text: str) -> BrandSpec:
    """Inverse of :func:`spec_to_json` — parse + reconstruct."""
    if not isinstance(text, str):
        raise BrandSpecError(
            f"spec_from_json expects str, got {type(text).__name__}"
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrandSpecError(f"spec_from_json: invalid JSON ({exc})") from exc
    return BrandSpec.from_dict(payload)
