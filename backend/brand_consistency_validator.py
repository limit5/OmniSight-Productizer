"""V4 #4 (issue #320) — post-deploy brand-consistency validator.

Scans a deployed build artifact (or a live URL) for **colours and
font families** that drift away from the project's design system.
The UI Designer agent (see ``configs/roles/ui-designer.md``) is
supposed to constrain its output to the tokens surfaced by
:mod:`backend.design_token_loader`, but some drift always leaks
through — minified bundles, third-party CSS, hand-edited tweaks —
and needs an *external* post-deploy gate that does not trust the
skill's own self-review.

Wire contract
-------------

* :func:`scan_build_artifact(path, tokens)` walks an output tree
  (``.next/`` / ``out/`` / ``dist/`` / Docker build context) and
  yields a :class:`BrandValidationReport`.
* :func:`scan_text(text, tokens, source=...)` is the single-file /
  single-snippet entry point — tests + any caller that already has
  the payload in memory (live URL fetch, SSR response) reuse it.
* Every violation is a *warning* by contract: the task rubric
  specifies "違規項列為 warning".  Errors would block release; this
  gate is coaching, not gating.
* Tokens come from :class:`backend.design_token_loader.DesignTokens`
  — that is the allow-list.  Any raw hex / rgb / hsl / named font
  that is not traceable back to the tokens is flagged.

Why a separate validator (not "ask the linter to do it")
--------------------------------------------------------

* :mod:`backend.component_consistency_linter` operates on source
  TSX **before** deploy — hand-written code.  This validator is
  the **post-deploy** mirror: it looks at *rendered* artefacts
  (HTML / minified CSS / inline styles) where the agent's "use
  design tokens" discipline is most likely to leak.
* The component linter's ``inline-hex-color`` rule is intentionally
  source-only (hard-error, blocks commit).  The post-deploy rule is
  more forgiving: third-party vendored CSS (a Stripe widget, a
  Radix primitive shipped pre-built) may carry its own palette —
  we surface it as a warning so operators can decide.
* The design-token allow-list is *resolved* here — ``var(--primary)``
  in the source deploys as the hex value the :root block defines,
  so the validator needs the concrete palette, not just the variable
  names.

Graceful fallback: every failure path (missing directory, unreadable
file, empty tokens) yields a well-formed empty report.  The agent
pipeline must never crash mid-deploy because this validator choked.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)


__all__ = [
    "VALIDATOR_SCHEMA_VERSION",
    "SEVERITIES",
    "SCAN_EXTENSIONS",
    "DEFAULT_EXCLUDES",
    "RULES",
    "BrandRule",
    "BrandViolation",
    "BrandValidationReport",
    "AllowedBrandSets",
    "collect_allowed_colors",
    "collect_allowed_fonts",
    "normalize_hex",
    "normalize_font_name",
    "rgb_to_hex",
    "hsl_to_hex",
    "extract_hex_colors",
    "extract_rgb_colors",
    "extract_hsl_colors",
    "extract_font_families",
    "extract_tailwind_palette_classes",
    "color_allowed",
    "font_allowed",
    "iter_asset_files",
    "scan_text",
    "scan_build_artifact",
    "scan_url",
    "render_report",
    "run_brand_consistency_validator",
]


# Bump when the JSON-safe shape of a BrandViolation / report changes.
VALIDATOR_SCHEMA_VERSION = "1.0.0"

#: The post-deploy gate is coaching-only; all rules emit warn.
SEVERITIES: tuple[str, ...] = ("warn",)

#: Extensions walked by :func:`scan_build_artifact`.  Static files
#: the build pipeline actually serves.  ``.map`` is skipped — source
#: maps can contain the same offenders as the original source and
#: would produce duplicate warnings.
SCAN_EXTENSIONS: tuple[str, ...] = (
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".cjs",
    ".svg",
)

#: Directory names elided from walks.  ``node_modules`` should never
#: be in a deployed artefact but operators occasionally pass the whole
#: repo by accident.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "node_modules",
    ".git",
    ".next/cache",
    ".cache",
)


# ── Rule catalogue ───────────────────────────────────────────────────


@dataclass(frozen=True)
class BrandRule:
    """Static description of a validator rule."""

    rule_id: str
    severity: str
    summary: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"Unknown severity {self.severity!r} for {self.rule_id!r}; "
                f"must be one of {SEVERITIES}"
            )
        if not self.rule_id:
            raise ValueError("BrandRule.rule_id must be non-empty")
        if not self.summary.strip():
            raise ValueError(f"{self.rule_id}: summary must be non-empty")


_RULE_DEFS: tuple[BrandRule, ...] = (
    BrandRule("color-out-of-palette", "warn",
              "Inline hex colour is not in the design-system palette."),
    BrandRule("rgb-out-of-palette", "warn",
              "rgb()/rgba() colour does not map to an allowed palette hex."),
    BrandRule("hsl-out-of-palette", "warn",
              "hsl()/hsla() colour does not map to an allowed palette hex."),
    BrandRule("font-out-of-stack", "warn",
              "font-family is not in the design system's allowed stacks."),
    BrandRule("hard-pinned-palette-class", "warn",
              "Tailwind palette class bypasses the design-token utilities."),
    BrandRule("unknown-css-var", "warn",
              "var(--…) references a token that is not defined in the design system."),
)

RULES: Mapping[str, BrandRule] = MappingProxyType(
    {r.rule_id: r for r in _RULE_DEFS}
)


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class BrandViolation:
    """One offending colour / font reference in a scanned artefact."""

    rule_id: str
    severity: str
    source: str           # path / URL the offender came from
    line: int             # 1-based
    column: int           # 1-based
    offender: str         # the raw token (#aabbcc / "Inter" / bg-slate-900)
    message: str
    suggestion: str | None = None

    def __post_init__(self) -> None:
        if self.rule_id not in RULES:
            raise ValueError(f"Unknown rule_id {self.rule_id!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"Unknown severity {self.severity!r}")
        if self.line < 1 or self.column < 1:
            raise ValueError("line/column must be 1-based positive ints")
        if not self.offender:
            raise ValueError("BrandViolation.offender must be non-empty")


@dataclass(frozen=True)
class AllowedBrandSets:
    """Canonical allow-lists distilled from :class:`DesignTokens`.

    Separating this from the tokens object keeps :func:`scan_text`
    pure — the caller distils once and reuses across many files.
    """

    colors: frozenset[str] = frozenset()          # canonical #rrggbb hexes
    fonts: frozenset[str] = frozenset()           # lowercase font names
    css_var_names: frozenset[str] = frozenset()   # without leading "--"

    def to_dict(self) -> dict:
        return {
            "colors": sorted(self.colors),
            "fonts": sorted(self.fonts),
            "css_var_names": sorted(self.css_var_names),
        }


@dataclass(frozen=True)
class BrandValidationReport:
    """Aggregate brand-consistency scan result."""

    violations: tuple[BrandViolation, ...] = ()
    scanned_sources: tuple[str, ...] = ()
    allowed: AllowedBrandSets = field(default_factory=AllowedBrandSets)

    @property
    def is_clean(self) -> bool:
        """True iff no violations were raised.

        Every violation is a warning — operators may still choose to
        ship — but downstream dashboards can gate on this flag if they
        want a zero-warning bar.
        """
        return not self.violations

    @property
    def rule_counts(self) -> Mapping[str, int]:
        out: dict[str, int] = {}
        for v in self.violations:
            out[v.rule_id] = out.get(v.rule_id, 0) + 1
        return MappingProxyType(dict(sorted(out.items())))

    @property
    def severity_counts(self) -> Mapping[str, int]:
        out: dict[str, int] = {s: 0 for s in SEVERITIES}
        for v in self.violations:
            out[v.severity] += 1
        return MappingProxyType(out)

    def violations_for(self, rule_id: str) -> tuple[BrandViolation, ...]:
        return tuple(v for v in self.violations if v.rule_id == rule_id)

    def to_dict(self) -> dict:
        return {
            "schema_version": VALIDATOR_SCHEMA_VERSION,
            "is_clean": self.is_clean,
            "scanned_sources": list(self.scanned_sources),
            "severity_counts": dict(self.severity_counts),
            "rule_counts": dict(self.rule_counts),
            "allowed": self.allowed.to_dict(),
            "violations": [asdict(v) for v in self.violations],
        }


# ── Canonicalisation helpers ─────────────────────────────────────────


_HEX3_RE = re.compile(r"^#([0-9a-fA-F]{3})$")
_HEX4_RE = re.compile(r"^#([0-9a-fA-F]{4})$")
_HEX6_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
_HEX8_RE = re.compile(r"^#([0-9a-fA-F]{8})$")


def normalize_hex(color: str) -> str | None:
    """Canonicalise a hex colour to ``#rrggbb`` lowercase.

    * ``#abc`` → ``#aabbcc``
    * ``#AABBCC`` → ``#aabbcc``
    * ``#abcd`` (RGBA short) → ``#aabbcc`` (alpha dropped — we
      compare palette equality on RGB only; operators may ship
      alpha-variants of a brand colour freely).
    * ``#aabbccdd`` → ``#aabbcc``
    * anything else → ``None``.

    Returning ``None`` is what lets the validator flag "not a proper
    hex" offenders without raising.
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

    Generic keywords (``sans-serif`` / ``monospace`` / ``serif``) are
    preserved: they are universally allowed fallbacks and the
    validator does not flag them.
    """
    if not isinstance(name, str):
        return None
    s = name.strip().strip("'").strip('"').strip()
    if not s:
        return None
    return s.lower()


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
    approximation; the validator never crashes on malformed CSS.
    """
    # Clamp hue into [0, 360)
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


# ── Extractors ───────────────────────────────────────────────────────


# Hex colour literal.  Enforce word-boundary so we don't match the
# fragment identifier in `href="#top"` or inside a hash like
# `#/route`.  We allow 3, 4, 6 or 8 hex digits.
_HEX_RE = re.compile(r"(?<![0-9a-fA-F])#([0-9a-fA-F]{3,8})(?![0-9a-fA-F])")

# rgb / rgba functional notation.  The alpha field is optional; we
# ignore it when comparing palettes.
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

# CSS `font-family: "Inter", sans-serif;`.  We capture everything
# between `font-family:` and `;` or `}` (`}` handles
# `h1 { font-family: X }` without trailing semicolon).
_FONT_FAMILY_CSS_RE = re.compile(
    r"font-family\s*:\s*([^;}]*)",
    re.IGNORECASE,
)

# JSX inline style: `style={{ fontFamily: 'Inter, sans-serif' }}`.
_FONT_FAMILY_JSX_RE = re.compile(
    r"fontFamily\s*:\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

# Tailwind palette class — taken from the component consistency linter
# list for parity.
_PALETTE_FAMILIES = (
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

# `var(--foo)` or `var(--foo, fallback)` — the token name is captured
# without the leading `--`.
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
    still needs to split on comma and canonicalise each name.  That
    keeps this helper pure (no side-effect of font-name parsing).
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


def _iter_css_vars(text: str) -> Iterator[tuple[str, int]]:
    for m in _CSS_VAR_RE.finditer(text):
        yield m.group(1), m.start()


# ── Allowed-set builders ─────────────────────────────────────────────


def collect_allowed_colors(tokens) -> frozenset[str]:
    """Return the canonical ``#rrggbb`` set from a :class:`DesignTokens`.

    Walks every colour-kind token (``:root`` + ``.dark`` + ``@theme`` +
    ``tailwind-config``) and extracts whatever hex / rgb / hsl
    literal it resolves to.  ``var(--x)`` indirection is ignored here
    — the caller is the validator, which flags *raw* palette drift.
    """
    out: set[str] = set()
    if tokens is None or not hasattr(tokens, "all_tokens"):
        return frozenset(out)
    for tok in tokens.all_tokens:
        if getattr(tok, "kind", None) != "color":
            continue
        value = getattr(tok, "value", "") or ""
        # Direct hex literal.
        for hex_lit, _ in extract_hex_colors(value):
            normalised = normalize_hex(hex_lit)
            if normalised:
                out.add(normalised)
        for hex_lit, _ in extract_rgb_colors(value):
            normalised = normalize_hex(hex_lit)
            if normalised:
                out.add(normalised)
        for hex_lit, _ in extract_hsl_colors(value):
            normalised = normalize_hex(hex_lit)
            if normalised:
                out.add(normalised)
    return frozenset(out)


def collect_allowed_fonts(tokens) -> frozenset[str]:
    """Return lowercase font-family names from a :class:`DesignTokens`."""
    out: set[str] = set()
    if tokens is None or not hasattr(tokens, "all_tokens"):
        return frozenset(out)
    for tok in tokens.all_tokens:
        if getattr(tok, "kind", None) != "font":
            continue
        value = getattr(tok, "value", "") or ""
        for part in _split_font_stack(value):
            normalised = normalize_font_name(part)
            if normalised:
                out.add(normalised)
    return frozenset(out)


def collect_allowed_css_var_names(tokens) -> frozenset[str]:
    """Return the set of defined custom-property names (without ``--``)."""
    out: set[str] = set()
    if tokens is None or not hasattr(tokens, "all_tokens"):
        return frozenset(out)
    for tok in tokens.all_tokens:
        name = getattr(tok, "name", "")
        if name:
            out.add(name)
    return frozenset(out)


def _split_font_stack(value: str) -> list[str]:
    """Split a CSS font stack on commas that are NOT inside brackets.

    ``"var(--font-sans, 'Inter'), sans-serif"`` →
    ``["var(--font-sans, 'Inter')", "sans-serif"]``.
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


# ── Matching helpers ─────────────────────────────────────────────────


def color_allowed(color: str, allowed: Iterable[str]) -> bool:
    """Return True iff ``color`` normalises to a value in ``allowed``.

    ``allowed`` must be a set of lowercase ``#rrggbb`` hexes — use
    :func:`collect_allowed_colors` to build it.
    """
    normalised = normalize_hex(color)
    if normalised is None:
        return False
    return normalised in allowed


def font_allowed(font_name: str, allowed: Iterable[str]) -> bool:
    """Return True iff the canonical font name is allowed.

    Generic CSS keywords (``sans-serif`` / ``monospace`` etc) are
    always allowed — they are universal fallbacks and flagging them
    produces noise every operator will just suppress.
    """
    normalised = normalize_font_name(font_name)
    if normalised is None:
        return False
    if normalised in _GENERIC_FONT_KEYWORDS:
        return True
    if normalised.startswith("var(--"):
        # `var(--font-sans, 'Inter')` — leaves allow/deny to the var
        # check; here we only care about raw literal names.
        return True
    return normalised in allowed


# ── Scanners ─────────────────────────────────────────────────────────


def _line_col(text: str, offset: int) -> tuple[int, int]:
    if offset <= 0:
        return (1, 1)
    line = text.count("\n", 0, offset) + 1
    last_nl = text.rfind("\n", 0, offset)
    col = offset - last_nl if last_nl >= 0 else offset + 1
    return (line, col)


def _mk(rule_id: str, source: str, line: int, column: int,
        offender: str, message: str,
        suggestion: str | None = None) -> BrandViolation:
    rule = RULES[rule_id]
    return BrandViolation(
        rule_id=rule_id,
        severity=rule.severity,
        source=source,
        line=line,
        column=column,
        offender=offender,
        message=message,
        suggestion=suggestion,
    )


def scan_text(
    text: str,
    allowed: AllowedBrandSets,
    *,
    source: str = "<inline>",
) -> tuple[BrandViolation, ...]:
    """Scan a single payload for brand drift.

    Only raises :class:`TypeError` on a non-str ``text`` — every other
    failure path (empty text, unusable regex match) produces zero
    violations.  That matches the rest of the deploy pipeline's
    "never crash mid-flight" contract.
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not text:
        return ()
    if not isinstance(allowed, AllowedBrandSets):
        raise TypeError("allowed must be AllowedBrandSets")

    violations: list[BrandViolation] = []

    # ── Hex literals ─────────────────────────────────────────────
    for raw, offset in extract_hex_colors(text):
        canonical = normalize_hex(raw)
        if canonical is None:
            continue
        if canonical in allowed.colors:
            continue
        line, col = _line_col(text, offset)
        violations.append(_mk(
            "color-out-of-palette", source, line, col,
            offender=raw,
            message=f"Hex colour {raw} is not in the design-system palette.",
            suggestion=(
                "Replace with a token-backed utility "
                "(bg-primary / text-accent / …) or add the hex to "
                "your globals.css palette."
            ),
        ))

    # ── rgb() / rgba() ───────────────────────────────────────────
    for canonical, offset in extract_rgb_colors(text):
        if canonical in allowed.colors:
            continue
        line, col = _line_col(text, offset)
        violations.append(_mk(
            "rgb-out-of-palette", source, line, col,
            offender=canonical,
            message=(
                f"rgb() colour resolves to {canonical}, not in the "
                "design-system palette."
            ),
            suggestion=(
                "Swap for a var(--…) reference to an existing palette "
                "token or add a new token to globals.css."
            ),
        ))

    # ── hsl() / hsla() ───────────────────────────────────────────
    for canonical, offset in extract_hsl_colors(text):
        if canonical in allowed.colors:
            continue
        line, col = _line_col(text, offset)
        violations.append(_mk(
            "hsl-out-of-palette", source, line, col,
            offender=canonical,
            message=(
                f"hsl() colour resolves to {canonical}, not in the "
                "design-system palette."
            ),
            suggestion=(
                "Swap for a var(--…) reference to an existing palette "
                "token."
            ),
        ))

    # ── Font families ────────────────────────────────────────────
    for raw_stack, offset in extract_font_families(text):
        for piece in _split_font_stack(raw_stack):
            if piece.lower().startswith("var(--"):
                # Fall through to the unknown-css-var path; the name
                # check there is authoritative.
                continue
            if font_allowed(piece, allowed.fonts):
                continue
            line, col = _line_col(text, offset)
            violations.append(_mk(
                "font-out-of-stack", source, line, col,
                offender=piece,
                message=(
                    f"Font family {piece!r} is not in the design "
                    "system's allowed stacks."
                ),
                suggestion=(
                    "Use font-sans / font-mono utilities or the "
                    "canonical --font-* CSS variables defined in "
                    "globals.css."
                ),
            ))

    # ── Tailwind palette classes ─────────────────────────────────
    for name, offset in extract_tailwind_palette_classes(text):
        line, col = _line_col(text, offset)
        violations.append(_mk(
            "hard-pinned-palette-class", source, line, col,
            offender=name,
            message=(
                f"Tailwind palette class {name!r} bypasses the design "
                "system; prefer semantic utilities (bg-primary etc)."
            ),
        ))

    # ── CSS var references pointing at undefined tokens ──────────
    if allowed.css_var_names:
        for var_name, offset in _iter_css_vars(text):
            if var_name in allowed.css_var_names:
                continue
            line, col = _line_col(text, offset)
            violations.append(_mk(
                "unknown-css-var", source, line, col,
                offender=f"--{var_name}",
                message=(
                    f"var(--{var_name}) is not defined in the "
                    "design system."
                ),
                suggestion=(
                    "Add the token to :root / @theme in globals.css "
                    "or reference an existing token."
                ),
            ))

    violations.sort(key=lambda v: (v.source, v.line, v.column, v.rule_id))
    return tuple(violations)


# ── File/dir walkers ─────────────────────────────────────────────────


def iter_asset_files(
    root: Path | str,
    *,
    extensions: Sequence[str] = SCAN_EXTENSIONS,
    exclude: Sequence[str] = DEFAULT_EXCLUDES,
) -> tuple[Path, ...]:
    """Return every file under ``root`` matching ``extensions``.

    Missing ``root`` / unreadable ``root`` → empty tuple (graceful).
    Excluded directory names are dropped at any depth.  Results are
    sorted for determinism.
    """
    r = Path(root)
    if not r.exists() or not r.is_dir():
        return ()
    exclude_set = set(exclude)
    matched: list[Path] = []
    for ext in extensions:
        for path in r.rglob(f"*{ext}"):
            try:
                rel_parts = path.relative_to(r).parts
            except ValueError:
                continue
            if any(part in exclude_set for part in rel_parts):
                continue
            # Also allow "dir/subdir" excludes.
            rel_posix = path.relative_to(r).as_posix()
            if any(rel_posix.startswith(e + "/") for e in exclude):
                continue
            matched.append(path)
    matched.sort()
    return tuple(matched)


def scan_build_artifact(
    path: Path | str,
    tokens=None,
    *,
    extensions: Sequence[str] = SCAN_EXTENSIONS,
    exclude: Sequence[str] = DEFAULT_EXCLUDES,
    max_bytes_per_file: int = 2 * 1024 * 1024,
) -> BrandValidationReport:
    """Walk a build artefact directory and return the aggregate report.

    ``tokens`` should normally be a
    :class:`backend.design_token_loader.DesignTokens` but the scanner
    accepts ``None`` (empty allow-list — everything gets warned) or an
    :class:`AllowedBrandSets` (pre-distilled — skip the walk).
    """
    allowed = _resolve_allowed(tokens)
    root = Path(path)
    if not root.exists() or not root.is_dir():
        logger.info("brand validator: %s is not a directory", root)
        return BrandValidationReport(
            violations=(),
            scanned_sources=(),
            allowed=allowed,
        )

    files = iter_asset_files(root, extensions=extensions, exclude=exclude)
    all_violations: list[BrandViolation] = []
    scanned: list[str] = []
    for f in files:
        try:
            stat = f.stat()
        except OSError as exc:
            logger.debug("brand validator: stat failed on %s: %s", f, exc)
            continue
        if stat.st_size > max_bytes_per_file:
            logger.info(
                "brand validator: skipping %s (%d bytes > %d limit)",
                f, stat.st_size, max_bytes_per_file,
            )
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("brand validator: cannot read %s: %s", f, exc)
            continue
        rel = f.relative_to(root).as_posix()
        scanned.append(rel)
        all_violations.extend(scan_text(text, allowed, source=rel))

    all_violations.sort(key=lambda v: (v.source, v.line, v.column, v.rule_id))
    return BrandValidationReport(
        violations=tuple(all_violations),
        scanned_sources=tuple(scanned),
        allowed=allowed,
    )


def scan_url(
    url: str,
    tokens=None,
    *,
    fetch=None,
) -> BrandValidationReport:
    """Fetch a deployed URL and scan the response body.

    ``fetch`` is a callable ``(url) -> (status_code, text)``.  The
    default fetcher uses ``urllib.request``; tests inject their own.
    Non-200 responses → empty report (graceful degrade — the router
    typically converts this into a 502 / 503 itself).
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("scan_url: url must be a non-empty string")
    allowed = _resolve_allowed(tokens)

    fetcher = fetch or _default_fetch
    try:
        status, text = fetcher(url)
    except Exception as exc:  # noqa: BLE001
        logger.info("brand validator: URL fetch failed for %s: %s", url, exc)
        return BrandValidationReport(allowed=allowed)

    if status != 200 or not isinstance(text, str):
        logger.info(
            "brand validator: %s returned status=%s (not 200); skipping scan",
            url, status,
        )
        return BrandValidationReport(
            scanned_sources=(url,),
            allowed=allowed,
        )

    violations = scan_text(text, allowed, source=url)
    return BrandValidationReport(
        violations=violations,
        scanned_sources=(url,),
        allowed=allowed,
    )


def _default_fetch(url: str) -> tuple[int, str]:
    """Minimal stdlib fetcher used when the caller passes no ``fetch=``.

    Kept tiny on purpose — callers that need redirects, timeouts, or
    custom headers should inject their own.  Module-level import of
    urllib is avoided so zero-network test runs don't trigger DNS in
    the first place (stdlib still imports on call).
    """
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        body = resp.read()
        status = getattr(resp, "status", 200)
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
        return status, text


def _resolve_allowed(tokens) -> AllowedBrandSets:
    if tokens is None:
        return AllowedBrandSets()
    if isinstance(tokens, AllowedBrandSets):
        return tokens
    return AllowedBrandSets(
        colors=collect_allowed_colors(tokens),
        fonts=collect_allowed_fonts(tokens),
        css_var_names=collect_allowed_css_var_names(tokens),
    )


# ── Reporting ────────────────────────────────────────────────────────


def render_report(report: BrandValidationReport) -> str:
    """Render a human-readable markdown summary of the report.

    Deterministic — same input → byte-identical output.  Suitable
    for posting as a PR comment or a Slack notification.
    """
    lines: list[str] = [
        f"# Brand-consistency scan (v{VALIDATOR_SCHEMA_VERSION})",
        "",
    ]
    total = len(report.violations)
    if total == 0:
        lines.append(
            f"Scanned {len(report.scanned_sources)} source(s) — "
            "no brand drift detected."
        )
        return "\n".join(lines).rstrip() + "\n"

    lines.append(
        f"Summary: **{total} warning(s)** across "
        f"{len(report.scanned_sources)} source(s)."
    )
    lines.append("")
    lines.append("## Rule counts")
    for rule_id, count in report.rule_counts.items():
        lines.append(f"- `{rule_id}`: {count}")
    lines.append("")
    lines.append("## Details")
    by_source: dict[str, list[BrandViolation]] = {}
    for v in report.violations:
        by_source.setdefault(v.source, []).append(v)
    for src in sorted(by_source):
        lines.append(f"### `{src}`")
        for v in by_source[src]:
            lines.append(
                f"- [{v.severity}] `{v.rule_id}` at {v.line}:{v.column} — "
                f"`{v.offender}` — {v.message}"
            )
            if v.suggestion:
                lines.append(f"    → fix: {v.suggestion}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── Public agent-facing tool ─────────────────────────────────────────


def run_brand_consistency_validator(
    *,
    build_artifact: str | Path | None = None,
    url: str | None = None,
    text: str | None = None,
    source: str = "<inline>",
    tokens=None,
    project_root: str | Path | None = None,
    fetch=None,
) -> dict:
    """Agent-callable entry point (JSON-safe dict, no dataclasses leak).

    Exactly one of ``build_artifact``, ``url`` or ``text`` must be
    supplied.  ``tokens`` may be a
    :class:`backend.design_token_loader.DesignTokens` — if omitted and
    ``project_root`` is set, tokens are loaded automatically from the
    project root.
    """
    inputs = [x for x in (build_artifact, url, text) if x is not None]
    if len(inputs) != 1:
        raise ValueError(
            "run_brand_consistency_validator: supply exactly one of "
            "build_artifact= / url= / text="
        )

    if tokens is None and project_root is not None:
        from backend.design_token_loader import load_design_tokens
        tokens = load_design_tokens(project_root)

    allowed = _resolve_allowed(tokens)

    if build_artifact is not None:
        report = scan_build_artifact(build_artifact, allowed)
    elif url is not None:
        report = scan_url(url, allowed, fetch=fetch)
    else:
        violations = scan_text(text or "", allowed, source=source)
        report = BrandValidationReport(
            violations=violations,
            scanned_sources=(source,),
            allowed=allowed,
        )

    payload = report.to_dict()
    payload["markdown"] = render_report(report)
    return payload


# ── Serialisation helpers ────────────────────────────────────────────


def report_to_json(report: BrandValidationReport, *, indent: int | None = 2) -> str:
    """Serialise a report to JSON (unicode-safe, deterministic)."""
    return json.dumps(report.to_dict(), indent=indent, ensure_ascii=False, sort_keys=True)


# Preserve the export list in alphabetical order — makes grep / diff
# reviews easier.
__all__ = sorted(set(__all__ + ["collect_allowed_css_var_names", "report_to_json"]))
