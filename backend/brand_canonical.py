"""W12.3 — shared brand canonicalisation primitives.

Type / helper backbone shared by the **B5 forward-mode validator**
(:mod:`backend.brand_consistency_validator`) and the **W12 reverse-mode
extractor** (:mod:`backend.brand_extractor`).

Why a separate module
---------------------

Before this split the extractor reached into the validator's
single-leading-underscore privates (``_split_font_stack``,
``_GENERIC_FONT_KEYWORDS``) — a fragile cross-module dependency on
non-public symbols.  Pulling the **shared** primitives out into one
public surface lets both directions (forward = "is this drift?",
reverse = "what is the brand?") import the *same* parser layer
without crossing privacy boundaries.

The split is conservative — only the genuinely shared primitives move
here.  Validator-specific rules / report types / scanners stay in
:mod:`backend.brand_consistency_validator`; extractor-specific
k-means / heading / spacing parsers stay in
:mod:`backend.brand_extractor`.

Public surface
--------------

* :data:`GENERIC_FONT_KEYWORDS` — frozen set of CSS font keywords
  always treated as universal fallbacks (``sans-serif`` / ``serif`` /
  ``monospace`` / system fonts).  Both directions skip these.
* :func:`normalize_hex` / :func:`normalize_font_name` — canonicalise
  raw user input into a comparable form.
* :func:`rgb_to_hex` / :func:`hsl_to_hex` — channel-triple → canonical
  ``#rrggbb`` lowercase hex.
* :func:`extract_hex_colors` / :func:`extract_rgb_colors` /
  :func:`extract_hsl_colors` — scan a payload for colour literals.
* :func:`extract_font_families` — scan a payload for CSS / JSX
  font-family declarations.
* :func:`extract_tailwind_palette_classes` — scan for hard-pinned
  Tailwind utility classes (``bg-slate-900`` etc).
* :func:`iter_css_vars` — yield ``(var_name, offset)`` for every
  ``var(--…)`` reference.
* :func:`split_font_stack` — split a CSS font stack on top-level
  commas (respects quotes + nested ``var()`` parens).

Module-global state audit (SOP §1)
----------------------------------

Only immutable singletons (compiled regex, frozenset of generic font
keywords, tuple constants) plus the module-level :data:`logger`.
Cross-worker consistency: SOP answer #1 — each ``uvicorn`` worker
derives identical constants from identical source.

Read-after-write timing audit (SOP §2)
--------------------------------------

N/A — pure function family, no DB, no shared mutable state.

Compat-fingerprint grep (SOP §3)
--------------------------------

N/A — no DB / PG / SQLite code paths; the four fingerprints
(``_conn()`` / ``await conn.commit()`` / ``datetime('now')`` /
``VALUES (?, ?)``) all return zero hits.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

logger = logging.getLogger(__name__)


__all__ = [
    "GENERIC_FONT_KEYWORDS",
    "extract_font_families",
    "extract_hex_colors",
    "extract_hsl_colors",
    "extract_rgb_colors",
    "extract_tailwind_palette_classes",
    "hsl_to_hex",
    "iter_css_vars",
    "normalize_font_name",
    "normalize_hex",
    "rgb_to_hex",
    "split_font_stack",
]


# ── Generic font keywords ────────────────────────────────────────────


#: CSS font keywords / system-font sentinels that are always allowed
#: as fallbacks.  The forward-mode validator never flags them; the
#: reverse-mode extractor never surfaces them as brand fonts.  Both
#: directions need the *same* set so a brand fingerprint rendered into
#: a stylesheet round-trips cleanly through the validator.
GENERIC_FONT_KEYWORDS: frozenset[str] = frozenset({
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


# ── Hex / RGB / HSL canonicalisation ─────────────────────────────────


_HEX3_RE = re.compile(r"^#([0-9a-fA-F]{3})$")
_HEX4_RE = re.compile(r"^#([0-9a-fA-F]{4})$")
_HEX6_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
_HEX8_RE = re.compile(r"^#([0-9a-fA-F]{8})$")


def normalize_hex(color: str) -> str | None:
    """Canonicalise a hex colour to ``#rrggbb`` lowercase.

    * ``#abc`` → ``#aabbcc``
    * ``#AABBCC`` → ``#aabbcc``
    * ``#abcd`` (RGBA short) → ``#aabbcc`` (alpha dropped — palette
      equality compares RGB only; operators may ship alpha-variants
      of a brand colour freely).
    * ``#aabbccdd`` → ``#aabbcc``
    * anything else → ``None``.

    Returning ``None`` lets callers flag "not a proper hex" offenders
    without raising.
    """
    if not isinstance(color, str):
        return None
    s = color.strip()
    if not s.startswith("#"):
        return None
    m = _HEX3_RE.match(s)
    if m:
        r, g, b = m.group(1)
        return f"#{r}{r}{g}{g}{b}{b}".lower()
    m = _HEX4_RE.match(s)
    if m:
        r, g, b, _ = m.group(1)
        return f"#{r}{r}{g}{g}{b}{b}".lower()
    m = _HEX6_RE.match(s)
    if m:
        return f"#{m.group(1)}".lower()
    m = _HEX8_RE.match(s)
    if m:
        return f"#{m.group(1)[:6]}".lower()
    return None


def normalize_font_name(name: str) -> str | None:
    """Strip quotes / whitespace / case from a font-family token.

    ``'Inter'`` / ``"Inter"`` / ``inter`` → ``inter``.

    Generic CSS keywords (``sans-serif`` / ``monospace`` / ``serif``)
    are preserved by this helper — it canonicalises but does not
    filter; the *caller* decides whether to drop them via
    :data:`GENERIC_FONT_KEYWORDS`.
    """
    if not isinstance(name, str):
        return None
    s = name.strip().strip("'").strip('"').strip()
    if not s:
        return None
    return s.lower()


def _clamp255(n: float) -> int:
    if n < 0:
        return 0
    if n > 255:
        return 255
    return int(round(n))


def rgb_to_hex(r: float, g: float, b: float) -> str:
    """Convert RGB channel triple (0-255) to ``#rrggbb`` lowercase."""
    return "#{:02x}{:02x}{:02x}".format(_clamp255(r), _clamp255(g), _clamp255(b))


def _hsl_channel(h: float, c: float, x: float, m: float, i: int) -> float:
    table = (
        (c, x, 0.0),
        (x, c, 0.0),
        (0.0, c, x),
        (0.0, x, c),
        (x, 0.0, c),
        (c, 0.0, x),
    )
    segment = int(h // 60) % 6
    return (table[segment][i] + m) * 255


def hsl_to_hex(h: float, s: float, lightness: float) -> str:
    """Convert HSL (h in degrees, s/l as fractions 0-1) to ``#rrggbb``.

    Values outside nominal ranges are clamped: we tolerate whatever
    the minifier emits (``hsl(380, 120%, 50%)``) rather than refusing
    to normalise.  Palette-match callers get ``#rrggbb`` or a close
    approximation; the helper never crashes on malformed CSS.
    """
    hue = h % 360 if h >= 0 else (h % 360 + 360) % 360
    sat = max(0.0, min(1.0, s))
    light = max(0.0, min(1.0, lightness))

    c = (1 - abs(2 * light - 1)) * sat
    x = c * (1 - abs(((hue / 60) % 2) - 1))
    m = light - c / 2

    r = _hsl_channel(hue, c, x, m, 0)
    g = _hsl_channel(hue, c, x, m, 1)
    b = _hsl_channel(hue, c, x, m, 2)
    return rgb_to_hex(r, g, b)


# ── Extractor regex ──────────────────────────────────────────────────


# Hex colour literal.  Word-boundary on either side prevents matching
# `href="#top"` fragment identifiers or `#/route` style hashes.  3 / 4
# / 6 / 8 hex digits are accepted (caller normalises further).
_HEX_RE = re.compile(r"(?<![0-9a-fA-F])#([0-9a-fA-F]{3,8})(?![0-9a-fA-F])")

# rgb / rgba functional notation.  Alpha optional; ignored when
# comparing palettes.
_RGB_RE = re.compile(
    r"\brgba?\(\s*"
    r"(-?\d*\.?\d+)\s*%?\s*[,\s]\s*"
    r"(-?\d*\.?\d+)\s*%?\s*[,\s]\s*"
    r"(-?\d*\.?\d+)\s*%?"
    r"(?:\s*[,/]\s*(-?\d*\.?\d+%?))?\s*\)",
    re.IGNORECASE,
)

# hsl / hsla functional notation.
_HSL_RE = re.compile(
    r"\bhsla?\(\s*"
    r"(-?\d*\.?\d+)(deg|rad|turn|grad)?\s*[,\s]\s*"
    r"(-?\d*\.?\d+)%?\s*[,\s]\s*"
    r"(-?\d*\.?\d+)%?"
    r"(?:\s*[,/]\s*(-?\d*\.?\d+%?))?\s*\)",
    re.IGNORECASE,
)

# CSS `font-family: "Inter", sans-serif;`.  Captures everything between
# `font-family:` and `;` or `}` (`}` handles `h1 { font-family: X }`
# without trailing semicolon).
_FONT_FAMILY_CSS_RE = re.compile(
    r"font-family\s*:\s*([^;}]*)",
    re.IGNORECASE,
)

# JSX inline style: `style={{ fontFamily: 'Inter, sans-serif' }}`.
_FONT_FAMILY_JSX_RE = re.compile(
    r"fontFamily\s*:\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

# Tailwind palette family list.  Mirrors the component consistency
# linter for parity.
_PALETTE_FAMILIES: tuple[str, ...] = (
    "slate", "zinc", "gray", "neutral", "stone",
    "red", "orange", "amber", "yellow", "lime",
    "green", "emerald", "teal", "cyan", "sky",
    "blue", "indigo", "violet", "purple", "fuchsia", "pink", "rose",
)

_TAILWIND_PALETTE_RE = re.compile(
    r"\b(?:bg|text|border|ring|from|to|via|fill|stroke|divide|outline|"
    r"decoration|placeholder|caret|accent|shadow)"
    r"-(?:" + "|".join(_PALETTE_FAMILIES) + r")-\d{2,3}\b"
)

# `var(--foo)` or `var(--foo, fallback)` — token name captured without
# the leading `--`.
_CSS_VAR_RE = re.compile(r"var\(\s*--([a-zA-Z0-9_-]+)")


def _hue_to_degrees(value: str, unit: str | None) -> float:
    n = float(value)
    if unit is None or unit == "":
        return n
    u = unit.lower()
    if u == "deg":
        return n
    if u == "rad":
        return n * 180.0 / 3.141592653589793
    if u == "turn":
        return n * 360.0
    if u == "grad":
        return n * 0.9
    return n


def _rgb_component(value: str, ctx: str) -> float:
    """Return an rgb channel in 0-255 from a numeric string.

    Handles the ``rgb(50% 50% 50%)`` percentage form by scanning the
    surrounding text snippet for a trailing ``%`` after the captured
    number.  Percentage detection is approximate but safe: we take
    ``<value>%`` as a percentage iff the literal ``<value>%`` substring
    appears in the function call.
    """
    n = float(value)
    if f"{value}%" in ctx:
        return n * 255.0 / 100.0
    return n


def extract_hex_colors(text: str) -> tuple[tuple[str, int], ...]:
    """Return ``((hex, offset), …)`` for every hex literal in ``text``."""
    if not isinstance(text, str):
        return ()
    out: list[tuple[str, int]] = []
    for m in _HEX_RE.finditer(text):
        digits = m.group(1)
        if len(digits) in (3, 4, 6, 8):
            out.append((f"#{digits}", m.start()))
    return tuple(out)


def extract_rgb_colors(text: str) -> tuple[tuple[str, int], ...]:
    """Return ``((canonical_hex, offset), …)`` for every ``rgb[a](…)``."""
    if not isinstance(text, str):
        return ()
    out: list[tuple[str, int]] = []
    for m in _RGB_RE.finditer(text):
        try:
            r = _rgb_component(m.group(1), m.string[m.start():m.end()])
            g = _rgb_component(m.group(2), m.string[m.start():m.end()])
            b = _rgb_component(m.group(3), m.string[m.start():m.end()])
        except ValueError:
            continue
        out.append((rgb_to_hex(r, g, b), m.start()))
    return tuple(out)


def extract_hsl_colors(text: str) -> tuple[tuple[str, int], ...]:
    """Return ``((canonical_hex, offset), …)`` for every ``hsl[a](…)``."""
    if not isinstance(text, str):
        return ()
    out: list[tuple[str, int]] = []
    for m in _HSL_RE.finditer(text):
        try:
            h = _hue_to_degrees(m.group(1), m.group(2))
            s = float(m.group(3)) / 100.0
            light = float(m.group(4)) / 100.0
        except ValueError:
            continue
        out.append((hsl_to_hex(h, s, light), m.start()))
    return tuple(out)


def extract_font_families(text: str) -> tuple[tuple[str, int], ...]:
    """Return ``((raw_family_decl, offset), …)`` for every font-family
    (CSS) or ``fontFamily`` (JSX) reference.

    The ``raw_family_decl`` is the entire value side — the caller
    still needs to split on comma (see :func:`split_font_stack`) and
    canonicalise each name.  That keeps this helper pure.
    """
    if not isinstance(text, str):
        return ()
    out: list[tuple[str, int]] = []
    for m in _FONT_FAMILY_CSS_RE.finditer(text):
        val = m.group(1).strip().strip(";").strip()
        if val:
            out.append((val, m.start(1)))
    for m in _FONT_FAMILY_JSX_RE.finditer(text):
        val = m.group(1).strip()
        if val:
            out.append((val, m.start(1)))
    return tuple(out)


def extract_tailwind_palette_classes(text: str) -> tuple[tuple[str, int], ...]:
    """Return ``((class_name, offset), …)`` for every Tailwind default
    palette class reference (``bg-slate-500``, ``text-blue-600``, …).
    """
    if not isinstance(text, str):
        return ()
    return tuple((m.group(0), m.start()) for m in _TAILWIND_PALETTE_RE.finditer(text))


def iter_css_vars(text: str) -> Iterator[tuple[str, int]]:
    """Yield ``(var_name, offset)`` for every ``var(--…)`` reference.

    The leading ``--`` is stripped from the yielded name to match the
    naming convention of :class:`backend.design_token_loader.DesignTokens`.
    """
    if not isinstance(text, str):
        return
    for m in _CSS_VAR_RE.finditer(text):
        yield m.group(1), m.start()


# ── Font stack split ─────────────────────────────────────────────────


def split_font_stack(value: str) -> list[str]:
    """Split a CSS font stack on commas that are NOT inside brackets.

    ``"var(--font-sans, 'Inter'), sans-serif"`` →
    ``["var(--font-sans, 'Inter')", "sans-serif"]``.

    Respects single-/double-quoted segments and nested ``var()``
    parens so a CSS variable fallback list does not split mid-token.
    """
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    in_single = False
    in_double = False
    for ch in value:
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            continue
        if in_single or in_double:
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [p for p in out if p]


__all__ = sorted(set(__all__))
