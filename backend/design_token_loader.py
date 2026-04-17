"""V1 #3 (issue #317) — design-token loader.

Reads a target project's ``globals.css`` (and ``tailwind.config.*`` when
present) and extracts the **colour palette / font stack / border-radius
scale / spacing scale** the UI Designer agent (see
``configs/roles/ui-designer.md``) must constrain its generated code to.

Why this module exists
----------------------

The sibling :mod:`backend.ui_component_registry` tells the agent *what*
components are installed; this module tells the agent *how* to style
them.  Without it the agent falls back to training-memory defaults —
writing ``bg-slate-900`` or inlining ``#38bdf8`` — and drifts away from
the project's real palette.  By surfacing the live CSS custom
properties we turn the tokens into a hard constraint the UI Designer
skill can cite verbatim in its generated TSX.

Contract (pinned by ``backend/tests/test_design_token_loader.py``)
-----------------------------------------------------------------

* CSS tokens are parsed from four scopes:
    * ``:root { ... }``              — base palette (and custom brand vars);
    * ``.dark { ... }``              — dark-mode overrides;
    * ``@theme inline { ... }``      — Tailwind v4 utility bindings;
    * ``html { ... }``               — global flags (``color-scheme``).
  Nested blocks (``@keyframes``, ``@layer``, ``@media``) are ignored.
* A ``tailwind.config.{ts,js,mjs,cjs}`` file is parsed with a
  deliberately minimal regex for projects still on Tailwind v3.
* Every token is classified into one of :data:`KINDS`
  (``color``/``font``/``radius``/``spacing``/``shadow``/``other``) by
  name prefix first, then by value shape.
* :class:`DesignTokens` is JSON-serialisable via :func:`asdict` and
  frozen — public view properties return :class:`MappingProxyType` so
  callers cannot mutate the tokens they get back.
* :meth:`DesignTokens.to_agent_context` produces a **deterministic**,
  sorted-by-key markdown block suitable for LLM prompt injection
  (byte-identical on repeated calls → stable Anthropic prompt-cache key).

Graceful fallback: every failure path (missing project root, missing
CSS, unreadable file, empty file) returns an *empty* but well-formed
:class:`DesignTokens` instead of raising — the agent gets a "no tokens
extracted" signal rather than a crash mid-prompt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

logger = logging.getLogger(__name__)


# ── Public constants ─────────────────────────────────────────────────

# Bump when the schema of a DesignToken / DesignTokens changes
# (callers cache contexts across runs keyed off this).
LOADER_SCHEMA_VERSION = "1.0.0"

#: Fixed taxonomy of token kinds; the UI Designer skill references
#: these strings directly in its prompt ("colour token", "font
#: token", …) so they must not change lightly.
KINDS: tuple[str, ...] = (
    "color",
    "font",
    "radius",
    "spacing",
    "shadow",
    "other",
)

#: CSS scopes we understand.  Anything else (keyframes, @media,
#: @layer, nested component rules) is silently ignored.
SCOPES: tuple[str, ...] = (
    "root",
    "dark",
    "theme",
    "html",
    "tailwind-config",
)


# ── Classification helpers ───────────────────────────────────────────

# Colour-value detection: hex, rgb(), rgba(), hsl(), hsla(), oklch(),
# oklab(), lab(), lch(), color(), plus the two keyword values.
# The first token of the value must match — multi-stop gradients stay
# as "other".
_COLOR_VALUE_RE = re.compile(
    r"""^(?:
        \#[0-9a-fA-F]{3,8}            |
        rgba?\(                        |
        hsla?\(                        |
        oklch\(                        |
        oklab\(                        |
        lab\(                          |
        lch\(                          |
        color\(                        |
        transparent                    |
        currentColor                   |
        var\(--(?:color-|chart-|sidebar|neural|hardware|artifact|validation|critical|deep-space|holo|background|foreground|card|popover|primary|secondary|muted|accent|destructive|border|input|ring)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Pure length value: "1.25rem", "4px", "0.5rem", "100%".
_LENGTH_VALUE_RE = re.compile(
    r"^-?\d+(\.\d+)?(rem|em|px|%|vh|vw|ch|ex|pt|cm|mm|in|pc|vmin|vmax)?$"
)

# Name prefixes that unambiguously indicate a colour token.
_COLOR_NAME_PREFIXES: frozenset[str] = frozenset({
    # shadcn semantic palette
    "background", "foreground",
    "card", "card-foreground",
    "popover", "popover-foreground",
    "primary", "primary-foreground",
    "secondary", "secondary-foreground",
    "muted", "muted-foreground",
    "accent", "accent-foreground",
    "destructive", "destructive-foreground",
    "border", "input", "ring",
    # chart + sidebar
    "chart", "sidebar",
    # FUI brand palette used by this project
    "neural-blue", "neural-cyan", "neural-border", "neural-muted",
    "hardware-orange", "artifact-purple",
    "validation-emerald", "critical-red",
    "deep-space-start", "deep-space-end",
    "holo-glass", "holo-glass-border",
    # @theme-inline utility bindings always start with color-
    "color-",
})

# Names that belong to the shadcn "official" palette — anything *else*
# under scope="root" with kind="color" lands in :pyattr:`DesignTokens.brand`.
_SHADCN_SEMANTIC_COLORS: frozenset[str] = frozenset({
    "background", "foreground",
    "card", "card-foreground",
    "popover", "popover-foreground",
    "primary", "primary-foreground",
    "secondary", "secondary-foreground",
    "muted", "muted-foreground",
    "accent", "accent-foreground",
    "destructive", "destructive-foreground",
    "border", "input", "ring",
    "radius",
})

_SHADCN_CHART_COLORS: frozenset[str] = frozenset(f"chart-{i}" for i in range(1, 9))

_SHADCN_SIDEBAR_COLORS: frozenset[str] = frozenset({
    "sidebar",
    "sidebar-foreground",
    "sidebar-primary",
    "sidebar-primary-foreground",
    "sidebar-accent",
    "sidebar-accent-foreground",
    "sidebar-border",
    "sidebar-ring",
})


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class DesignToken:
    """One design token parsed from the project.

    Fields:
        name:    custom-property name WITHOUT the leading ``--``
                 (e.g. ``"primary"``, ``"radius-sm"``, ``"font-sans"``).
        value:   the raw CSS value as written
                 (e.g. ``"oklch(0.205 0 0)"``, ``"var(--background)"``,
                 ``"0.625rem"``).
        kind:    one of :data:`KINDS`.  Determines how the value is
                 consumed by the agent (utility class suggestion,
                 contrast check, font-stack citation, …).
        scope:   one of :data:`SCOPES`.  Tells the agent whether the
                 token is exposed as a Tailwind utility
                 (``scope="theme"``) or only as a raw CSS variable
                 (``scope="root"``).
        source:  relative path to the file the token was extracted
                 from — surfaces in debug / error messages.
    """

    name: str
    value: str
    kind: str
    scope: str
    source: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(
                f"Unknown kind {self.kind!r} for token --{self.name}; "
                f"must be one of {KINDS}"
            )
        if self.scope not in SCOPES:
            raise ValueError(
                f"Unknown scope {self.scope!r} for token --{self.name}; "
                f"must be one of {SCOPES}"
            )
        if not self.name:
            raise ValueError("DesignToken.name must be non-empty")

    @property
    def css_name(self) -> str:
        """Return the name with the CSS custom-property ``--`` prefix."""
        return f"--{self.name}"


@dataclass(frozen=True)
class DesignTokens:
    """Aggregate of design tokens extracted from a project.

    The frozen dataclass stores the *canonical* tuple of tokens plus
    boolean flags about theme shape.  All per-kind views
    (:pyattr:`palette`, :pyattr:`fonts`, …) are computed on access and
    return :class:`types.MappingProxyType` so callers cannot mutate
    the result.
    """

    all_tokens: tuple[DesignToken, ...] = ()
    sources: tuple[str, ...] = ()
    has_dark: bool = False
    is_dark_only: bool = False

    # ── Filtering primitive ──────────────────────────────────────

    def filter_tokens(
        self,
        *,
        kind: str | None = None,
        scope: str | None = None,
    ) -> tuple[DesignToken, ...]:
        """Return a tuple of tokens matching optional kind + scope filters."""
        if kind is not None and kind not in KINDS:
            raise ValueError(f"Unknown kind {kind!r}; must be one of {KINDS}")
        if scope is not None and scope not in SCOPES:
            raise ValueError(f"Unknown scope {scope!r}; must be one of {SCOPES}")
        out: list[DesignToken] = []
        for t in self.all_tokens:
            if kind is not None and t.kind != kind:
                continue
            if scope is not None and t.scope != scope:
                continue
            out.append(t)
        return tuple(out)

    # ── Per-kind views (read-only mappings) ──────────────────────

    @property
    def palette(self) -> Mapping[str, str]:
        """Base (light-mode) colour palette, keyed by token name.

        Priority: ``:root`` > ``@theme`` > ``tailwind-config`` for the
        same name — so the agent sees the *concrete* value, not the
        ``var(--…)`` indirection.
        """
        return self._ordered_view(kind="color", scope_order=("root", "theme", "tailwind-config"))

    @property
    def palette_dark(self) -> Mapping[str, str]:
        """Dark-mode colour overrides (``.dark { ... }``)."""
        out = {t.name: t.value for t in self.filter_tokens(kind="color", scope="dark")}
        return MappingProxyType(dict(sorted(out.items())))

    @property
    def fonts(self) -> Mapping[str, str]:
        """Font-family stacks (``font-sans``, ``font-mono``, …)."""
        return self._ordered_view(kind="font", scope_order=("theme", "root", "tailwind-config"))

    @property
    def radii(self) -> Mapping[str, str]:
        """Border-radius scale (``radius``, ``radius-sm``, …)."""
        return self._ordered_view(kind="radius", scope_order=("theme", "root", "tailwind-config"))

    @property
    def spacing(self) -> Mapping[str, str]:
        """Spacing scale (``spacing``, ``spacing-4``, …)."""
        return self._ordered_view(kind="spacing", scope_order=("theme", "root", "tailwind-config"))

    @property
    def shadows(self) -> Mapping[str, str]:
        """Box-shadow tokens (rarely present in shadcn defaults)."""
        return self._ordered_view(kind="shadow", scope_order=("theme", "root", "tailwind-config"))

    @property
    def brand(self) -> Mapping[str, str]:
        """Project-specific colour tokens NOT in the shadcn default set.

        E.g. ``neural-blue``, ``hardware-orange``, ``holo-glass``.
        These are visible to CSS (``var(--neural-blue)``) but are NOT
        exposed as Tailwind utility classes unless also bound under
        ``@theme inline`` — the agent should prefer utility classes
        where available and only inline ``var(--…)`` for brand-unique
        effects.
        """
        reserved = (
            _SHADCN_SEMANTIC_COLORS
            | _SHADCN_CHART_COLORS
            | _SHADCN_SIDEBAR_COLORS
        )
        out: dict[str, str] = {}
        for t in self.filter_tokens(kind="color", scope="root"):
            if t.name in reserved:
                continue
            if t.name.startswith("color-"):
                continue  # @theme rebind accidentally in :root — skip
            out[t.name] = t.value
        return MappingProxyType(dict(sorted(out.items())))

    # ── Derived: Tailwind utility classes ────────────────────────

    def utility_classes(self) -> tuple[str, ...]:
        """Return the Tailwind v4 utility classes synthesised from ``@theme``.

        Only tokens under ``scope="theme"`` become utility classes.
        The mapping follows Tailwind v4's auto-generation rules:

        * ``--color-X`` → ``bg-X``, ``text-X``, ``border-X``
        * ``--radius-X`` → ``rounded-X``
        * ``--spacing-X`` → ``p-X``, ``m-X``, ``gap-X``
        * ``--font-X`` → ``font-X``
        """
        classes: set[str] = set()
        for t in self.filter_tokens(scope="theme"):
            if t.name.startswith("color-"):
                base = t.name[len("color-"):]
                classes.update({f"bg-{base}", f"text-{base}", f"border-{base}"})
            elif t.name.startswith("radius-"):
                classes.add(f"rounded-{t.name[len('radius-'):]}")
            elif t.name.startswith("spacing-"):
                base = t.name[len("spacing-"):]
                classes.update({f"p-{base}", f"m-{base}", f"gap-{base}"})
            elif t.name.startswith("font-"):
                classes.add(t.name)
        return tuple(sorted(classes))

    def token_names(self, *, kind: str | None = None) -> tuple[str, ...]:
        """Return all token names (optionally filtered by kind), sorted, deduped."""
        names: set[str] = set()
        for t in self.all_tokens:
            if kind is not None and t.kind != kind:
                continue
            names.add(t.name)
        return tuple(sorted(names))

    # ── Serialisation ────────────────────────────────────────────

    def to_dict(self) -> dict:
        """JSON-safe dict of the entire aggregate (for tool-boundary hop)."""
        return {
            "schema_version": LOADER_SCHEMA_VERSION,
            "sources": list(self.sources),
            "has_dark": self.has_dark,
            "is_dark_only": self.is_dark_only,
            "tokens": [asdict(t) for t in self.all_tokens],
            "palette": dict(self.palette),
            "palette_dark": dict(self.palette_dark),
            "fonts": dict(self.fonts),
            "radii": dict(self.radii),
            "spacing": dict(self.spacing),
            "shadows": dict(self.shadows),
            "brand": dict(self.brand),
            "utility_classes": list(self.utility_classes()),
        }

    # ── Agent-context rendering ──────────────────────────────────

    def to_agent_context(self) -> str:
        """Render a compact, deterministic markdown block for LLM injection.

        Stability rules (break at your peril — the UI Designer skill
        and its prompt-cache key depend on them):

        * Section headers are fixed.
        * Within a section, entries are sorted by token name.
        * Trailing newline always present.
        """
        lines: list[str] = [
            f"# Design tokens (v{LOADER_SCHEMA_VERSION})",
            "",
        ]

        if not self.all_tokens:
            lines.append(
                "_No design tokens extracted — project has neither "
                "`globals.css` nor `tailwind.config.*`._"
            )
            return "\n".join(lines).rstrip() + "\n"

        if self.sources:
            lines.append(f"Sources: {', '.join(self.sources)}")
        if self.is_dark_only:
            lines.append("Theme: **dark-only** (`html { color-scheme: dark }`).")
        elif self.has_dark:
            lines.append("Theme: supports light + dark via `.dark { … }` overrides.")
        else:
            lines.append("Theme: single-mode (no `.dark` overrides detected).")
        lines.append("")

        sections: tuple[tuple[str, Mapping[str, str]], ...] = (
            ("Palette (base)", self.palette),
            ("Palette (dark overrides)", self.palette_dark),
            ("Brand colours (not exposed as utilities)", self.brand),
            ("Fonts", self.fonts),
            ("Radii", self.radii),
            ("Spacing", self.spacing),
            ("Shadows", self.shadows),
        )
        for title, mapping in sections:
            if not mapping:
                continue
            lines.append(f"## {title}")
            for name, value in mapping.items():
                lines.append(f"- `--{name}`: `{value}`")
            lines.append("")

        utilities = self.utility_classes()
        if utilities:
            lines.append("## Tailwind utility classes (auto-generated from `@theme`)")
            # Keep the list bounded — it grows O(3 * |color tokens|).
            lines.append(", ".join(f"`{c}`" for c in utilities))
            lines.append("")

        lines.append("## Generation rules")
        lines.append(
            "- Prefer semantic utility classes (e.g. `bg-primary`, "
            "`text-foreground`) over raw hex / rgb / oklch values."
        )
        lines.append(
            "- Radius: pick one of the defined `rounded-*` classes; "
            "never write `rounded-[Npx]`."
        )
        lines.append(
            "- Fonts: `font-sans` for body / UI copy, `font-mono` "
            "for code and tabular numerics."
        )
        if self.is_dark_only:
            lines.append(
                "- Do NOT assume a light theme; the project is "
                "dark-only. Do not gate styles on `:not(.dark)`."
            )
        return "\n".join(lines).rstrip() + "\n"

    # ── Internal: ordered view builder ───────────────────────────

    def _ordered_view(
        self,
        *,
        kind: str,
        scope_order: tuple[str, ...],
    ) -> Mapping[str, str]:
        """Merge tokens of a single kind across several scopes.

        The earliest scope in ``scope_order`` wins for duplicate names —
        so ``palette`` surfaces the concrete ``:root`` colour over the
        ``var(--…)`` indirection in ``@theme``.
        """
        out: dict[str, str] = {}
        for scope in scope_order:
            for t in self.filter_tokens(kind=kind, scope=scope):
                if t.name in out:
                    continue
                out[t.name] = t.value
        return MappingProxyType(dict(sorted(out.items())))


# ── CSS parser ───────────────────────────────────────────────────────


def _strip_comments(css: str) -> str:
    """Remove ``/* … */`` comments.

    CSS does not officially support ``//`` line comments but Tailwind
    v4's PostCSS pipeline tolerates them in authoring; we leave them
    alone — they are rarely seen in shipped ``globals.css`` files.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _iter_top_level_blocks(css: str) -> Iterator[tuple[str, str]]:
    """Yield ``(selector_or_atrule, body)`` tuples for each top-level block.

    Handles three top-level constructs:

    * At-rule statements (``@import 'x';``, ``@custom-variant dark (…);``) —
      skipped.  Without this, the first real block's selector would
      swallow every preceding at-rule.
    * Block rules (``:root { … }``, ``@theme inline { … }``) — yielded.
    * Nested braces (``@keyframes x { 0% { … } }``) — the OUTER block
      is yielded whole; callers that don't care about it just skip
      via :func:`_classify_selector`.

    Parenthesis depth is tracked separately from brace depth so that
    ``@custom-variant dark (&:is(.dark *));`` — whose parenthesised
    value contains no braces but does contain a semicolon — is
    correctly treated as a statement, not the start of a block.
    """
    i, n = 0, len(css)
    while i < n:
        while i < n and css[i] in " \t\r\n":
            i += 1
        if i >= n:
            return

        # Find the next top-level `{` or `;` — whichever comes first.
        paren_depth = 0
        k = i
        sep: str | None = None
        while k < n:
            ch = css[k]
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth = max(0, paren_depth - 1)
            elif paren_depth == 0:
                if ch == ";":
                    sep = ";"
                    break
                if ch == "{":
                    sep = "{"
                    break
            k += 1
        if sep is None:
            return

        if sep == ";":
            # Whole segment is an at-rule statement — skip.
            i = k + 1
            continue

        # sep == "{" — read the matching brace body.
        selector = css[i:k].strip()
        brace_depth = 1
        j = k + 1
        while j < n and brace_depth > 0:
            ch = css[j]
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
            j += 1
        body = css[k + 1 : j - 1]
        if selector:
            yield selector, body
        i = j


def _remove_nested_blocks(body: str) -> str:
    """Strip any nested ``{ … }`` blocks from a block body.

    Lets :func:`_parse_declarations` see only the top-level
    declarations of the enclosing scope.
    """
    out: list[str] = []
    depth = 0
    for ch in body:
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _split_declarations(body: str) -> list[str]:
    """Split a declaration list on ``;`` while respecting parentheses.

    We cannot use a plain ``.split(";")`` because values like
    ``rgba(0, 0, 0, 0.5)`` do not contain semicolons but ``oklch(0.5 0
    0 / 0.5)`` might contain a slash, and nested ``var(a, var(b))``
    may contain commas.  Semicolons only terminate declarations when
    paren-depth is zero.
    """
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == ";" and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _parse_declarations(body: str) -> list[tuple[str, str]]:
    """Return ``(name, value)`` pairs for every ``--X: Y;`` in ``body``.

    Only CSS custom properties (names starting with ``--``) are kept;
    regular CSS declarations (``background: red;``) are ignored with
    two exceptions surfaced by :func:`_parse_css_raw_declarations`.
    """
    decls: list[tuple[str, str]] = []
    top_level = _remove_nested_blocks(body)
    for raw in _split_declarations(top_level):
        raw = raw.strip()
        if not raw or ":" not in raw:
            continue
        name, _, value = raw.partition(":")
        name = name.strip()
        value = value.strip().rstrip(";").strip()
        if not name.startswith("--"):
            continue
        name = name[2:]
        if not name or not value:
            continue
        decls.append((name, value))
    return decls


def _parse_raw_declarations(body: str) -> list[tuple[str, str]]:
    """Return ``(property, value)`` pairs for ALL declarations.

    Used by the ``html { color-scheme: dark; }`` detection path where
    we care about non-custom properties too.
    """
    decls: list[tuple[str, str]] = []
    top_level = _remove_nested_blocks(body)
    for raw in _split_declarations(top_level):
        raw = raw.strip()
        if not raw or ":" not in raw:
            continue
        name, _, value = raw.partition(":")
        name = name.strip()
        value = value.strip().rstrip(";").strip()
        if not name or not value:
            continue
        decls.append((name, value))
    return decls


def _classify_selector(selector: str) -> str | None:
    """Map a CSS selector/at-rule to one of our SCOPES (or None to skip)."""
    s = selector.strip()
    if s == ":root":
        return "root"
    if s == ".dark" or s.startswith(".dark "):
        return "dark"
    if s.startswith("@theme"):
        return "theme"
    if s == "html" or s.startswith("html "):
        return "html"
    return None


def _classify_kind(name: str, value: str) -> str:
    """Infer the kind of a token from its name (preferred) or value shape."""
    lower = name.lower()

    # Fast-path: exact prefixes.
    if lower == "font" or lower.startswith("font-"):
        return "font"
    if lower == "radius" or lower.startswith("radius-"):
        return "radius"
    if lower == "spacing" or lower.startswith("spacing-"):
        return "spacing"
    if lower == "shadow" or lower.startswith("shadow-") or "-shadow-" in lower:
        return "shadow"

    # Known colour-name prefixes (shadcn + this project's FUI vocab).
    for prefix in _COLOR_NAME_PREFIXES:
        if prefix.endswith("-"):
            if lower.startswith(prefix):
                return "color"
        elif lower == prefix or lower.startswith(prefix + "-"):
            return "color"

    # Value-shape fallback: does it LOOK like a colour?
    stripped = value.strip()
    if stripped and _COLOR_VALUE_RE.match(stripped):
        return "color"

    # Pure length?  We don't know if it's radius or spacing — log as other.
    return "other"


def _parse_css(text: str, source: str) -> list[DesignToken]:
    """Parse a CSS file into DesignToken instances.

    Parsing is **deliberately shallow**: we do not build an AST, just
    scan top-level blocks and extract custom-property declarations.
    This keeps the module zero-dependency and fast (<10 ms for a
    1500-line globals.css).  Complex CSS will simply yield no tokens
    from the unrecognised parts — never a parse error.
    """
    tokens: list[DesignToken] = []
    stripped = _strip_comments(text)
    for selector, body in _iter_top_level_blocks(stripped):
        scope = _classify_selector(selector)
        if scope is None:
            continue
        if scope == "html":
            # For `html` we don't want custom-property tokens (there
            # usually aren't any) — we scan raw declarations for
            # color-scheme detection, handled elsewhere.
            continue
        for name, value in _parse_declarations(body):
            kind = _classify_kind(name, value)
            tokens.append(
                DesignToken(
                    name=name,
                    value=value,
                    kind=kind,
                    scope=scope,
                    source=source,
                )
            )
    return tokens


def _detect_dark_only(text: str) -> bool:
    """Return True if the CSS declares ``color-scheme: dark`` on ``html``.

    We scan the top-level ``html { … }`` block and look for a
    ``color-scheme`` declaration whose value is exactly ``dark``.  Any
    value containing ``light`` (e.g. ``color-scheme: light dark``)
    means the theme supports both modes.
    """
    stripped = _strip_comments(text)
    for selector, body in _iter_top_level_blocks(stripped):
        scope = _classify_selector(selector)
        if scope != "html":
            continue
        for prop, value in _parse_raw_declarations(body):
            if prop.strip().lower() == "color-scheme":
                val = value.strip().lower()
                if val == "dark":
                    return True
    return False


# ── Tailwind config parser (v3) ──────────────────────────────────────

_CONFIG_SECTION_RE = re.compile(
    r"(?P<key>colors|fontFamily|borderRadius|spacing|boxShadow)\s*:\s*\{"
    r"(?P<body>[^{}]*)\}",
    re.DOTALL,
)

# Accept both quoted and bare keys; quoted values, or simple arrays.
_CONFIG_PAIR_RE = re.compile(
    r"""(?P<name>
            [A-Za-z_][\w\-]*           |
            "[\w\-\s]+"                |
            '[\w\-\s]+'
        )\s*:\s*(?P<value>
            "[^"]*"                    |
            '[^']*'                    |
            \[[^\]]*\]
        )""",
    re.VERBOSE,
)


def _parse_tailwind_config(text: str, source: str) -> list[DesignToken]:
    """Minimal tailwind.config.* parser.

    This is NOT a JS evaluator — it's a pattern scan that handles the
    most common shapes:

    * Quoted-string values: ``primary: "hsl(var(--primary))"``.
    * Array values: ``sans: ["Inter", "ui-sans-serif"]`` → joined.

    Anything more exotic (computed keys, function-call values,
    imports) is silently skipped.  Tailwind v4 projects don't need
    this path at all; we keep it only for backward compatibility.
    """
    tokens: list[DesignToken] = []
    # Strip line + block comments so // comments in TS don't confuse us.
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    kind_by_key = {
        "colors": "color",
        "fontFamily": "font",
        "borderRadius": "radius",
        "spacing": "spacing",
        "boxShadow": "shadow",
    }
    for m in _CONFIG_SECTION_RE.finditer(stripped):
        key = m.group("key")
        body = m.group("body")
        kind = kind_by_key[key]
        for pair in _CONFIG_PAIR_RE.finditer(body):
            name = pair.group("name").strip("\"' ")
            value = pair.group("value").strip()
            if value.startswith(("\"", "'")):
                value = value[1:-1]
            elif value.startswith("["):
                items = re.findall(r"[\"']([^\"']+)[\"']", value)
                value = ", ".join(items)
            if not name or not value:
                continue
            tokens.append(
                DesignToken(
                    name=name,
                    value=value,
                    kind=kind,
                    scope="tailwind-config",
                    source=source,
                )
            )
    return tokens


# ── File discovery ───────────────────────────────────────────────────


def _find_css_files(project_root: Path) -> list[Path]:
    """Find the canonical globals.css path for a project.

    Precedence:

    1. ``components.json``'s ``tailwind.css`` field (shadcn/ui's own
       "where does tailwind live" declaration).
    2. ``app/globals.css`` (Next.js App Router default).
    3. ``styles/globals.css`` (legacy Next.js Pages Router).
    4. ``src/styles/globals.css`` (custom layouts).

    Returns the *first* one that exists.  Returning a list (rather
    than a single Path) keeps the door open for multi-file stacks
    without breaking callers — today it's always length 0 or 1.
    """
    cfg = project_root / "components.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("design_token_loader: components.json unreadable: %s", exc)
        else:
            css_rel = (data.get("tailwind") or {}).get("css") or ""
            if css_rel:
                candidate = project_root / css_rel
                if candidate.is_file():
                    return [candidate]

    for rel in ("app/globals.css", "styles/globals.css", "src/styles/globals.css"):
        p = project_root / rel
        if p.is_file():
            return [p]
    return []


def _find_tailwind_config(project_root: Path) -> Path | None:
    """Find a ``tailwind.config.{ts,js,mjs,cjs}`` file at the project root."""
    for name in (
        "tailwind.config.ts",
        "tailwind.config.js",
        "tailwind.config.mjs",
        "tailwind.config.cjs",
    ):
        p = project_root / name
        if p.is_file():
            return p
    return None


# ── Public API ───────────────────────────────────────────────────────


def load_design_tokens(project_root: Path | str | None = None) -> DesignTokens:
    """Load design tokens for a project.

    ``project_root`` may be a :class:`Path`, a string path, or
    ``None``.  ``None`` / missing directory / unreadable files all
    degrade gracefully to an empty :class:`DesignTokens` — the
    caller (and the UI Designer agent) gets an explicit "no tokens"
    signal rather than a traceback mid-prompt.
    """
    if project_root is None:
        return _empty_tokens("no project_root supplied")
    root = Path(project_root)
    if not root.is_dir():
        return _empty_tokens(f"{root} is not a directory")

    css_files = _find_css_files(root)
    config_file = _find_tailwind_config(root)

    all_tokens: list[DesignToken] = []
    sources: list[str] = []
    has_dark = False
    is_dark_only = False

    for css_path in css_files:
        rel = _safe_relpath(css_path, root)
        try:
            text = css_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Either the file can't be opened (OSError) or it exists but
            # isn't valid UTF-8 (UnicodeDecodeError — e.g. someone
            # committed a BOM-less UTF-16 file). Both are survivable for
            # an agent-prompt producer: degrade to an empty read rather
            # than raise mid-prompt.
            logger.debug("design_token_loader: cannot read %s: %s", css_path, exc)
            continue
        sources.append(rel)
        css_tokens = _parse_css(text, rel)
        all_tokens.extend(css_tokens)
        if any(t.scope == "dark" for t in css_tokens):
            has_dark = True
        if _detect_dark_only(text):
            is_dark_only = True

    if config_file is not None:
        rel = _safe_relpath(config_file, root)
        try:
            text = config_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("design_token_loader: cannot read %s: %s", config_file, exc)
        else:
            sources.append(rel)
            all_tokens.extend(_parse_tailwind_config(text, rel))

    return DesignTokens(
        all_tokens=tuple(_dedupe_tokens(all_tokens)),
        sources=tuple(sources),
        has_dark=has_dark,
        is_dark_only=is_dark_only,
    )


def render_agent_context_block(project_root: Path | str | None = None) -> str:
    """Convenience: load tokens then render the agent-context block.

    The UI Designer skill calls this as a tool; keeping it at module
    level (not a method) parallels :func:`backend.ui_component_registry.
    render_agent_context_block` so both tools share a surface shape.
    """
    return load_design_tokens(project_root).to_agent_context()


# ── Internal helpers ─────────────────────────────────────────────────


def _dedupe_tokens(tokens: Iterable[DesignToken]) -> list[DesignToken]:
    """Collapse duplicate (scope, name) pairs — last write wins.

    Preserves insertion order (Python 3.7+ dict guarantee).
    """
    seen: dict[tuple[str, str], DesignToken] = {}
    for t in tokens:
        seen[(t.scope, t.name)] = t
    return list(seen.values())


def _empty_tokens(reason: str) -> DesignTokens:
    """Return a well-formed empty DesignTokens with a debug trace."""
    logger.debug("design_token_loader: %s", reason)
    return DesignTokens()


def _safe_relpath(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` if possible, else str(path)."""
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


# ── Module exports ───────────────────────────────────────────────────

__all__ = [
    "KINDS",
    "LOADER_SCHEMA_VERSION",
    "SCOPES",
    "DesignToken",
    "DesignTokens",
    "load_design_tokens",
    "render_agent_context_block",
]


if __name__ == "__main__":  # pragma: no cover
    from argparse import ArgumentParser

    ap = ArgumentParser(description=__doc__)
    ap.add_argument("project_root", nargs="?", default=".", help="project root")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()

    tokens = load_design_tokens(args.project_root)
    if args.json:
        print(json.dumps(tokens.to_dict(), indent=2, sort_keys=True))
    else:
        print(tokens.to_agent_context())
