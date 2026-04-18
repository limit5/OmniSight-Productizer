"""V1 #4 (issue #317) — post-generation component-consistency linter.

Scans React + shadcn/ui + Tailwind code emitted by the UI Designer
agent (see ``configs/roles/ui-designer.md``) for the **Anti-patterns**
listed in that skill.  Each violation is surfaced with a rule id,
severity, line/column, a short message, and — where mechanically safe —
a suggested fix or an auto-applied rewrite.

Why a separate linter (and not just "ask the model to self-check")
-----------------------------------------------------------------

* The skill's post-generation hook is deterministic: the agent can emit
  TSX, hand it to :func:`run_consistency_linter`, read back a
  structured :class:`LintReport`, and decide whether to self-repair or
  hand the diff off.  A free-text self-review is fuzzy and
  non-reproducible.
* The rules encode the **exact** sibling contracts:
    - raw ``<button>``/``<input>``/… → must prefer the shadcn component
      from :mod:`backend.ui_component_registry`;
    - inline hex / ``bg-slate-900`` / ``text-[13px]`` → must prefer a
      design-token utility from :mod:`backend.design_token_loader`.
  Both sibling modules publish their accepted surfaces; this linter
  policies against them.
* Auto-fix is intentionally conservative: it only rewrites the
  mechanical 1:1 tag swaps (``<button>`` → ``<Button>``, etc.) and adds
  the matching import.  Anything semantic (inline hex vs. the right
  token, ``outline-none`` vs. the right ring) becomes a ``suggested_fix``
  string the agent must apply — we never invent colour or ring values
  the caller didn't ask for.

Contract (pinned by ``backend/tests/test_component_consistency_linter.py``)
--------------------------------------------------------------------------

* :class:`LintViolation` and :class:`LintReport` are frozen,
  JSON-serialisable, and ordered by (line, column, rule_id).
* :data:`RULES` is an immutable mapping from rule id → :class:`LintRule`
  and is the single source of truth for severities + auto-fixability.
* :func:`lint_code` and :func:`lint_file` never raise on syntactic
  quirks — malformed TSX yields *no false positives* (we degrade
  gracefully) but still catches the unambiguous cases.
* :func:`auto_fix_code` is idempotent: running it twice produces the
  same source text as running it once.
* :func:`run_consistency_linter` returns a JSON-safe dict (no
  dataclass instances leak across the agent tool boundary).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "LINTER_SCHEMA_VERSION",
    "SEVERITIES",
    "LintRule",
    "LintViolation",
    "LintReport",
    "RULES",
    "lint_code",
    "lint_file",
    "lint_directory",
    "auto_fix_code",
    "auto_fix_file",
    "render_report",
    "run_consistency_linter",
]

# Bump when the shape of a LintViolation / LintReport dict changes.
LINTER_SCHEMA_VERSION = "1.0.0"

#: Ordered from most to least urgent.
SEVERITIES: tuple[str, ...] = ("error", "warn", "info")


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class LintRule:
    """One lint rule definition.

    ``rule_id`` is stable — the UI Designer skill can cite it verbatim
    ("suppressed raw-button because …").  ``severity`` drives whether
    the rule blocks the acceptance gate (``error`` does, ``warn``
    doesn't).  ``auto_fixable`` tells the agent whether
    :func:`auto_fix_code` will rewrite the match or only annotate it.
    """

    rule_id: str
    severity: str
    summary: str
    auto_fixable: bool = False

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"Unknown severity {self.severity!r} for {self.rule_id!r}; "
                f"must be one of {SEVERITIES}"
            )
        if not self.rule_id:
            raise ValueError("LintRule.rule_id must be non-empty")
        if not self.summary.strip():
            raise ValueError(f"{self.rule_id}: summary must be non-empty")


@dataclass(frozen=True)
class LintViolation:
    """One offending location in the scanned source."""

    rule_id: str
    severity: str
    line: int            # 1-based
    column: int          # 1-based
    message: str
    snippet: str = ""
    suggested_fix: str | None = None
    auto_fixable: bool = False

    def __post_init__(self) -> None:
        if self.rule_id not in RULES:
            raise ValueError(f"Unknown rule_id {self.rule_id!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"Unknown severity {self.severity!r}")
        if self.line < 1 or self.column < 1:
            raise ValueError("line/column must be 1-based positive ints")


@dataclass(frozen=True)
class LintReport:
    """Aggregate report for a single source file / snippet."""

    violations: tuple[LintViolation, ...] = ()
    source: str | None = None

    @property
    def is_clean(self) -> bool:
        """No error-severity violations.

        Warnings do NOT block acceptance — they are coaching signals.
        """
        return not any(v.severity == "error" for v in self.violations)

    @property
    def severity_counts(self) -> Mapping[str, int]:
        out: dict[str, int] = {s: 0 for s in SEVERITIES}
        for v in self.violations:
            out[v.severity] += 1
        return MappingProxyType(out)

    @property
    def rule_counts(self) -> Mapping[str, int]:
        out: dict[str, int] = {}
        for v in self.violations:
            out[v.rule_id] = out.get(v.rule_id, 0) + 1
        return MappingProxyType(dict(sorted(out.items())))

    def to_dict(self) -> dict:
        return {
            "schema_version": LINTER_SCHEMA_VERSION,
            "source": self.source,
            "is_clean": self.is_clean,
            "severity_counts": dict(self.severity_counts),
            "rule_counts": dict(self.rule_counts),
            "violations": [asdict(v) for v in self.violations],
        }


# ── Rule catalogue ───────────────────────────────────────────────────
#
# The UI Designer skill's "Anti-patterns" list is the source of truth.
# Keep rule ids short, kebab-case, stable.

_RULE_DEFS: tuple[LintRule, ...] = (
    # Raw HTML → shadcn component rewrites (mechanical, auto-fixable).
    LintRule("raw-button", "error",
             "Replace raw <button> with shadcn <Button>.", auto_fixable=True),
    LintRule("raw-input", "error",
             "Replace raw <input> with shadcn <Input>.", auto_fixable=True),
    LintRule("raw-textarea", "error",
             "Replace raw <textarea> with shadcn <Textarea>.", auto_fixable=True),
    LintRule("raw-select", "error",
             "Replace raw <select> with shadcn <Select>."),
    LintRule("raw-dialog", "error",
             "Replace raw <dialog> with shadcn <Dialog>."),
    LintRule("raw-progress", "error",
             "Replace raw <progress> with shadcn <Progress>.", auto_fixable=True),
    # Semantic / a11y errors.
    LintRule("div-onclick", "error",
             "Use <Button> / <a> — not <div onClick>."),
    LintRule("role-button-on-div", "error",
             "Use a real <Button> — not role=\"button\" on <div>/<span>."),
    LintRule("img-without-alt", "error",
             "<img> must declare alt (use alt=\"\" for decorative images)."),
    LintRule("tabindex-positive", "error",
             "Do not use tabIndex > 0 — it breaks the natural tab order."),
    LintRule("focus-outline-none-unsafe", "error",
             "Removed focus outline without a replacement ring."),
    # Design-token / style errors.
    LintRule("inline-hex-color", "error",
             "Inline hex colour — use a design-token utility or var(--…)."),
    LintRule("hard-pinned-palette", "warn",
             "Tailwind palette class (bg-slate-900 …) bypasses design tokens."),
    LintRule("arbitrary-size", "warn",
             "Arbitrary Tailwind value — pick a scale step instead."),
    LintRule("arbitrary-breakpoint", "warn",
             "Non-standard breakpoint prefix — prefer sm/md/lg/xl/2xl."),
    LintRule("important-hack", "warn",
             "!important short-circuits the cascade — use cn() instead."),
    LintRule("dark-prefix-on-dark-only", "warn",
             "Project is dark-only; `dark:` prefix is dead code."),
)

RULES: Mapping[str, LintRule] = MappingProxyType({r.rule_id: r for r in _RULE_DEFS})


# ── Shadcn component mapping (for auto-fix & suggestion text) ────────
#
# Pairs must agree with :data:`backend.ui_component_registry.REGISTRY`.
# Keep this hand-curated rather than derived so a shadcn re-installation
# can't silently change the linter's auto-fix behaviour.

@dataclass(frozen=True)
class _TagSwap:
    html_tag: str
    component: str        # JSX tag to emit
    import_from: str      # "@/components/ui/<stem>"


_TAG_SWAPS: Mapping[str, _TagSwap] = MappingProxyType({
    "button": _TagSwap("button", "Button", "@/components/ui/button"),
    "input": _TagSwap("input", "Input", "@/components/ui/input"),
    "textarea": _TagSwap("textarea", "Textarea", "@/components/ui/textarea"),
    "progress": _TagSwap("progress", "Progress", "@/components/ui/progress"),
})


# ── Pre-processing helpers ───────────────────────────────────────────

# JSX comments (`{/* … */}`) and JS block comments (`/* … */`).  We keep
# the newlines to preserve line numbers.
_BLOCK_COMMENT_RE = re.compile(r"\{/\*.*?\*/\}|/\*.*?\*/", re.DOTALL)
# JS line comments — strip from `//` to end of line.  We do NOT strip
# inside strings (a rare false positive in generated code; the linter
# skips it on purpose rather than build a full tokeniser).
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _strip_comments(code: str) -> str:
    """Return ``code`` with block + line comments replaced by blanks.

    Lines are preserved so violation line numbers still refer to the
    user's source.  Comment *contents* are zeroed out so a `<button>`
    inside `{/* example */}` does not trip the linter.
    """

    def _blank(match: re.Match[str]) -> str:
        text = match.group(0)
        # Preserve newlines so line counts don't shift.
        return "".join("\n" if ch == "\n" else " " for ch in text)

    code = _BLOCK_COMMENT_RE.sub(_blank, code)
    code = _LINE_COMMENT_RE.sub(_blank, code)
    return code


def _line_col(source: str, offset: int) -> tuple[int, int]:
    """Return (1-based line, 1-based column) for an offset into ``source``."""
    if offset <= 0:
        return (1, 1)
    line = source.count("\n", 0, offset) + 1
    last_nl = source.rfind("\n", 0, offset)
    col = offset - last_nl if last_nl >= 0 else offset + 1
    return (line, col)


def _snippet(source: str, offset: int, *, width: int = 80) -> str:
    """Return a one-line snippet around ``offset`` (trimmed, no trailing NL)."""
    line_start = source.rfind("\n", 0, offset) + 1
    line_end = source.find("\n", offset)
    if line_end < 0:
        line_end = len(source)
    fragment = source[line_start:line_end].strip()
    if len(fragment) > width:
        fragment = fragment[: width - 1] + "…"
    return fragment


# ── Detectors ────────────────────────────────────────────────────────
#
# Each detector is a generator yielding :class:`LintViolation`.  They
# receive the pre-stripped source (see :func:`_strip_comments`) so
# comment bodies never trip them.


# JSX opening tag: `<name` where name starts with a lowercase letter.
# We capture the tag start offset and the full tag body (up to `>` or
# `/>`), which lets attribute-based rules inspect only the tag.
_OPEN_TAG_RE = re.compile(
    r"<([a-z][a-zA-Z0-9-]*)\b([^>]*?)(/?)>",
    re.DOTALL,
)

# Inline hex colour in any string literal or JSX attribute value.
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")

# Tailwind palette colours that should be design-token utilities.
_PALETTE_FAMILIES = (
    "slate", "zinc", "gray", "neutral", "stone",
    "red", "orange", "amber", "yellow", "lime",
    "green", "emerald", "teal", "cyan", "sky",
    "blue", "indigo", "violet", "purple", "fuchsia", "pink", "rose",
)
_HARD_PALETTE_RE = re.compile(
    r"\b(?:bg|text|border|ring|from|to|via|fill|stroke|divide|outline|decoration|placeholder|caret|accent|shadow)"
    r"-(?:" + "|".join(_PALETTE_FAMILIES) + r")-\d{2,3}\b"
)

# Arbitrary Tailwind spacing / typography values: `p-[5px]`, `text-[13px]`, …
# Restrict to the common layout/sizing prefixes so `grid-cols-[...]`
# (legit for complex grid templates) stays silent.
_ARBITRARY_SIZE_RE = re.compile(
    r"\b(?:text|p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|w|h|min-w|max-w|min-h|max-h|gap|space-x|space-y|rounded)-\[([^\]]+)\]"
)

# Arbitrary breakpoint: `min-[412px]:` / `max-[1000px]:`.
_ARBITRARY_BREAKPOINT_RE = re.compile(r"\b(?:min|max)-\[[^\]]+\]:")

# !important anywhere in the source, or the Tailwind `!utility-class`
# shortcut (`!text-red-500`) that expands to the same thing.  We
# anchor the shortcut after whitespace or a quote so it doesn't flag
# `!==` / `!isValid` operators.
_IMPORTANT_RE = re.compile(
    r"!\s*important\b"
    r"|(?<=[\s\"'`])!(?=[a-z])"
)

# `dark:` utility prefix — dead code when the project is dark-only.
_DARK_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9-])dark:[a-z][a-zA-Z0-9/-]*")

# `focus:outline-none` / `outline-none` that is NOT followed on the
# same className by a `focus-visible:ring-*` substitute.
_OUTLINE_NONE_RE = re.compile(r"\b(?:focus:)?outline-none\b")

# Inline `outline: none` in a style={{...}} object.
_STYLE_OUTLINE_NONE_RE = re.compile(r"outline\s*:\s*['\"]?none['\"]?")

# `tabIndex={N}` — flag positive values.
_TABINDEX_RE = re.compile(r"\btabIndex\s*=\s*\{?\s*(-?\d+)")


def _detect_raw_tags(source: str) -> Iterator[LintViolation]:
    for match in _OPEN_TAG_RE.finditer(source):
        tag = match.group(1)
        attrs = match.group(2)
        offset = match.start()
        line, col = _line_col(source, offset)
        snippet = _snippet(source, offset)

        # Core set of raw HTML that has a first-party shadcn replacement.
        rule_for_tag = {
            "button": "raw-button",
            "input": "raw-input",
            "textarea": "raw-textarea",
            "select": "raw-select",
            "dialog": "raw-dialog",
            "progress": "raw-progress",
        }
        rule_id = rule_for_tag.get(tag)
        if rule_id:
            # Skip hidden utility inputs the agent legitimately emits
            # inside shadcn primitives (e.g. Command uses a hidden
            # native input under the hood via Radix — but user-level
            # code is the one being linted, not vendor internals).
            # For now: if the input explicitly opts in via
            # `data-slot="native-input"`, skip.  (Matches the
            # convention used in `components/ui/*.tsx`.)
            if tag == "input" and 'data-slot="native-input"' in attrs:
                continue
            yield _mk(
                rule_id,
                line, col, snippet,
                message=RULES[rule_id].summary,
                suggested_fix=_suggest_tag_swap(tag),
            )

        # <div onClick> / <span onClick> — lose keyboard/focus/semantics.
        if tag in {"div", "span"} and re.search(r"\bonClick\b", attrs):
            if not re.search(r"\brole\s*=\s*[\"']button[\"']", attrs):
                yield _mk(
                    "div-onclick",
                    line, col, snippet,
                    message=(
                        f"<{tag} onClick> — use <Button> (or <a href> for "
                        "navigation) so keyboard + AT still work."
                    ),
                    suggested_fix=(
                        f"Wrap the children in <Button variant=\"ghost\" "
                        f"onClick={{…}}> instead of <{tag} onClick>."
                    ),
                )

        # role="button" on div/span — use a real button.
        if tag in {"div", "span"} and re.search(
            r"\brole\s*=\s*[\"']button[\"']", attrs
        ):
            yield _mk(
                "role-button-on-div",
                line, col, snippet,
                message=(
                    f'role="button" on <{tag}> — prefer a real <Button> '
                    "so focus + Enter/Space work natively."
                ),
                suggested_fix="Replace with <Button variant=\"ghost\"> …",
            )

        # <img> without alt.
        if tag == "img" and not re.search(r"\balt\s*=", attrs):
            yield _mk(
                "img-without-alt",
                line, col, snippet,
                message=(
                    "<img> missing alt — decorative images still need "
                    'alt="" (explicit empty), not a missing attribute.'
                ),
                suggested_fix='Add alt="" (decorative) or alt="<describe>" (meaningful).',
            )

        # tabIndex={N} — flag positive values on any tag.
        tab_match = _TABINDEX_RE.search(attrs)
        if tab_match:
            try:
                val = int(tab_match.group(1))
            except ValueError:
                val = 0
            if val > 0:
                yield _mk(
                    "tabindex-positive",
                    line, col, snippet,
                    message=(
                        f"tabIndex={val} — break the natural tab order. "
                        "Use 0 (focusable) or -1 (programmatic focus) only."
                    ),
                    suggested_fix="Drop the attribute or set tabIndex={0}.",
                )


def _detect_inline_hex(source: str) -> Iterator[LintViolation]:
    for match in _HEX_COLOR_RE.finditer(source):
        offset = match.start()
        line, col = _line_col(source, offset)
        yield _mk(
            "inline-hex-color",
            line, col, _snippet(source, offset),
            message=(
                f'Inline hex colour {match.group(0)!r} — use a '
                "design-token utility (bg-primary, text-foreground, …) "
                "or var(--…) so theme swaps still work."
            ),
            suggested_fix=(
                "Replace with the matching Tailwind utility "
                "(e.g. `text-primary`) or `var(--primary)`."
            ),
        )


def _detect_hard_palette(source: str) -> Iterator[LintViolation]:
    for match in _HARD_PALETTE_RE.finditer(source):
        offset = match.start()
        line, col = _line_col(source, offset)
        yield _mk(
            "hard-pinned-palette",
            line, col, _snippet(source, offset),
            message=(
                f"Hard-pinned Tailwind palette class {match.group(0)!r} — "
                "the project's design tokens will not propagate into it."
            ),
            suggested_fix=(
                "Use a semantic utility (bg-background / bg-card / "
                "bg-primary / text-muted-foreground / …) instead."
            ),
        )


def _detect_arbitrary_sizes(source: str) -> Iterator[LintViolation]:
    for match in _ARBITRARY_SIZE_RE.finditer(source):
        offset = match.start()
        line, col = _line_col(source, offset)
        yield _mk(
            "arbitrary-size",
            line, col, _snippet(source, offset),
            message=(
                f"Arbitrary Tailwind value {match.group(0)!r} — pick a "
                "scale step (text-sm/base/lg, p-2/3/4, …) to stay on the "
                "4-base spacing / type scale."
            ),
            suggested_fix=(
                "Round to the nearest scale step; if truly needed for a "
                "one-off, move the value into a design token first."
            ),
        )


def _detect_arbitrary_breakpoint(source: str) -> Iterator[LintViolation]:
    for match in _ARBITRARY_BREAKPOINT_RE.finditer(source):
        offset = match.start()
        line, col = _line_col(source, offset)
        yield _mk(
            "arbitrary-breakpoint",
            line, col, _snippet(source, offset),
            message=(
                f"Arbitrary breakpoint {match.group(0)!r} — prefer the "
                "standard sm/md/lg/xl/2xl prefixes, or container queries "
                "(`@container`) for component-local responsive rules."
            ),
        )


def _detect_important(source: str) -> Iterator[LintViolation]:
    for match in _IMPORTANT_RE.finditer(source):
        offset = match.start()
        line, col = _line_col(source, offset)
        yield _mk(
            "important-hack",
            line, col, _snippet(source, offset),
            message=(
                "!important short-circuits the cascade — use cn() to "
                "compose utilities in priority order instead."
            ),
        )


def _detect_dark_prefix(source: str) -> Iterator[LintViolation]:
    for match in _DARK_PREFIX_RE.finditer(source):
        offset = match.start()
        line, col = _line_col(source, offset)
        yield _mk(
            "dark-prefix-on-dark-only",
            line, col, _snippet(source, offset),
            message=(
                f"`{match.group(0)}` — the project is dark-only "
                "(`html { color-scheme: dark }`); the `dark:` prefix is "
                "never activated and acts as dead code."
            ),
            suggested_fix="Drop the `dark:` prefix; write the class once.",
        )


def _detect_outline_none(source: str) -> Iterator[LintViolation]:
    """Flag `outline-none` / inline `outline: none` without a replacement ring.

    The rule is className-scoped: if the *same* className string also
    contains `focus-visible:ring-` (or `focus:ring-`), we consider the
    replacement satisfied.
    """
    # Scan each className / class attribute independently.
    for cls_match in re.finditer(
        r"className\s*=\s*(?:\{[^}]*\}|\"[^\"]*\"|'[^']*')",
        source,
    ):
        cls_text = cls_match.group(0)
        if "outline-none" not in cls_text:
            continue
        has_replacement = bool(
            re.search(r"focus(?:-visible)?:ring-", cls_text)
            or re.search(r"focus(?:-visible)?:outline-", cls_text)
        )
        if has_replacement:
            continue
        for m in _OUTLINE_NONE_RE.finditer(cls_text):
            offset = cls_match.start() + m.start()
            line, col = _line_col(source, offset)
            yield _mk(
                "focus-outline-none-unsafe",
                line, col, _snippet(source, offset),
                message=(
                    "`outline-none` removes the focus ring without a "
                    "replacement — keyboard users lose the focus "
                    "indicator."
                ),
                suggested_fix=(
                    "Add `focus-visible:outline-none "
                    "focus-visible:ring-2 focus-visible:ring-ring "
                    "focus-visible:ring-offset-2` alongside it."
                ),
            )

    # Inline style={{ outline: "none" }}.
    for m in _STYLE_OUTLINE_NONE_RE.finditer(source):
        offset = m.start()
        # Skip if a sibling `boxShadow` / `ring` replacement is within
        # the same style object — heuristic, accepts most safe cases.
        window_start = source.rfind("{", 0, offset)
        window_end = source.find("}", offset)
        window = source[max(window_start, 0) : window_end if window_end >= 0 else len(source)]
        if "boxShadow" in window or "ring" in window:
            continue
        line, col = _line_col(source, offset)
        yield _mk(
            "focus-outline-none-unsafe",
            line, col, _snippet(source, offset),
            message=(
                "Inline `outline: none` without a replacement focus "
                "indicator — reinstate via boxShadow or a focus-visible ring."
            ),
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _mk(
    rule_id: str,
    line: int,
    column: int,
    snippet: str,
    *,
    message: str,
    suggested_fix: str | None = None,
) -> LintViolation:
    rule = RULES[rule_id]
    return LintViolation(
        rule_id=rule_id,
        severity=rule.severity,
        line=line,
        column=column,
        message=message,
        snippet=snippet,
        suggested_fix=suggested_fix,
        auto_fixable=rule.auto_fixable,
    )


def _suggest_tag_swap(tag: str) -> str:
    swap = _TAG_SWAPS.get(tag)
    if not swap:
        return (
            f"Swap <{tag}> for the matching shadcn component "
            "(see backend.ui_component_registry)."
        )
    return (
        f"Swap <{tag}> for <{swap.component}> from `{swap.import_from}` "
        "(see backend.ui_component_registry)."
    )


# ── Public: lint ─────────────────────────────────────────────────────


_DETECTORS = (
    _detect_raw_tags,
    _detect_inline_hex,
    _detect_hard_palette,
    _detect_arbitrary_sizes,
    _detect_arbitrary_breakpoint,
    _detect_important,
    _detect_dark_prefix,
    _detect_outline_none,
)


def lint_code(code: str, *, source: str | None = None) -> LintReport:
    """Lint a TSX / JSX snippet.

    Returns a :class:`LintReport` with violations sorted by
    (line, column, rule_id) so the output is deterministic and
    diff-friendly.  The report is always well-formed — on empty input
    the report is clean with an empty violations tuple.
    """
    if not isinstance(code, str):
        raise TypeError("code must be str")
    if not code.strip():
        return LintReport(violations=(), source=source)

    stripped = _strip_comments(code)

    collected: list[LintViolation] = []
    for detector in _DETECTORS:
        try:
            collected.extend(detector(stripped))
        except Exception:  # noqa: BLE001 — never crash the agent pipeline
            logger.exception(
                "consistency linter: detector %s raised — skipping",
                detector.__name__,
            )
            continue

    collected.sort(key=lambda v: (v.line, v.column, v.rule_id))
    return LintReport(violations=tuple(collected), source=source)


def lint_file(path: Path | str) -> LintReport:
    """Lint a file on disk.  Missing / unreadable files yield a clean report."""
    p = Path(path)
    try:
        code = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.info("consistency linter: cannot read %s: %s", p, exc)
        return LintReport(violations=(), source=str(p))
    return lint_code(code, source=str(p))


def lint_directory(
    root: Path | str,
    *,
    extensions: tuple[str, ...] = (".tsx", ".jsx"),
    exclude: tuple[str, ...] = (
        "node_modules",
        ".next",
        "dist",
        "build",
        "out",
        "components/ui",
    ),
) -> tuple[LintReport, ...]:
    """Lint every matching file under ``root``.

    ``components/ui/`` is excluded by default — those files are
    vendored shadcn source that legitimately uses raw HTML (they *are*
    the wrappers).  Callers that want to audit vendored code can pass
    ``exclude=()``.
    """
    r = Path(root)
    if not r.is_dir():
        return ()

    out: list[LintReport] = []
    for ext in extensions:
        for match in sorted(r.rglob(f"*{ext}")):
            rel = match.relative_to(r).as_posix()
            if any(part in rel.split("/") for part in exclude):
                continue
            # Also filter where exclude entries match the leading path.
            if any(rel.startswith(part + "/") for part in exclude):
                continue
            out.append(lint_file(match))
    return tuple(out)


# ── Public: auto-fix ─────────────────────────────────────────────────
#
# Auto-fix is deliberately narrow: the tag rewrites where the html→
# shadcn swap is mechanical.  We do NOT touch className / style content
# beyond that — fixing an inline hex the caller picked is out of scope;
# we flag it and let the agent choose the design token.


_AUTO_FIXABLE_TAGS = frozenset(
    t for t, s in _TAG_SWAPS.items() if RULES[f"raw-{t}"].auto_fixable
)

# Regexes for mechanical tag rewrites — run against the *original*
# source (not the stripped version).  We deliberately don't touch
# self-closing internals or attribute values.
_TAG_REWRITE_RE = re.compile(
    r"<(" + "|".join(sorted(_AUTO_FIXABLE_TAGS)) + r")\b"
    r"(?P<attrs>(?:[^<>'\"{}]|'[^']*'|\"[^\"]*\"|\{[^{}]*\})*?)"
    r"(?P<close>/?)>",
)

_CLOSING_TAG_RE = re.compile(
    r"</(" + "|".join(sorted(_AUTO_FIXABLE_TAGS)) + r")\s*>"
)


def auto_fix_code(code: str) -> tuple[str, LintReport]:
    """Rewrite mechanical HTML→shadcn tag swaps in ``code``.

    Returns ``(fixed_code, remaining_report)``.  The fix is idempotent:
    running :func:`auto_fix_code` on its own output yields the same
    text and the same (cleaner) report.

    The rewrite:

      * ``<button …>…</button>`` → ``<Button …>…</Button>``
      * ``<input …/>`` → ``<Input …/>``
      * ``<textarea …>…</textarea>`` → ``<Textarea …>…</Textarea>``
      * ``<progress …>…</progress>`` → ``<Progress …>…</Progress>``

    Imports are added at the top of the file (once) so the result
    compiles.  Existing ``import {...} from "@/components/ui/..."``
    lines are respected — we merge into them instead of duplicating.
    """
    if not isinstance(code, str):
        raise TypeError("code must be str")

    used_components: set[str] = set()

    def _rewrite_open(match: re.Match[str]) -> str:
        tag = match.group(1)
        attrs = match.group("attrs")
        close = match.group("close")
        swap = _TAG_SWAPS[tag]
        used_components.add(tag)
        return f"<{swap.component}{attrs}{close}>"

    def _rewrite_close(match: re.Match[str]) -> str:
        tag = match.group(1)
        swap = _TAG_SWAPS[tag]
        used_components.add(tag)
        return f"</{swap.component}>"

    fixed = _TAG_REWRITE_RE.sub(_rewrite_open, code)
    fixed = _CLOSING_TAG_RE.sub(_rewrite_close, fixed)

    if used_components:
        fixed = _ensure_imports(fixed, used_components)

    report = lint_code(fixed)
    return fixed, report


def auto_fix_file(path: Path | str, *, write: bool = True) -> tuple[str, LintReport]:
    """Read ``path``, run :func:`auto_fix_code`, optionally write it back."""
    p = Path(path)
    original = p.read_text(encoding="utf-8")
    fixed, report = auto_fix_code(original)
    if write and fixed != original:
        p.write_text(fixed, encoding="utf-8")
    return fixed, report


def _ensure_imports(code: str, used: Iterable[str]) -> str:
    """Insert or extend imports for the shadcn components just emitted.

    Adds a single block near the top of the file (after any existing
    `"use client"` / license-banner line), or merges into an existing
    import-from the same path.  Idempotent.
    """
    lines = code.splitlines(keepends=True)
    insert_idx = _find_import_insert_point(lines)

    # What's already imported?
    existing: dict[str, set[str]] = {}
    for i, line in enumerate(lines):
        m = re.match(
            r"\s*import\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]",
            line,
        )
        if not m:
            continue
        mod = m.group(2)
        names = {n.strip() for n in m.group(1).split(",") if n.strip()}
        existing.setdefault(mod, set()).update(names)

    added_lines: list[str] = []
    for tag in sorted(used):
        swap = _TAG_SWAPS[tag]
        already = existing.get(swap.import_from, set())
        if swap.component in already:
            continue
        # Try to extend an existing import line from the same module.
        extended = False
        for i, line in enumerate(lines):
            m = re.match(
                r"(\s*import\s*\{)([^}]*)(\}\s*from\s*['\"])"
                + re.escape(swap.import_from)
                + r"(['\"].*)",
                line,
            )
            if not m:
                continue
            current = [n.strip() for n in m.group(2).split(",") if n.strip()]
            if swap.component in current:
                extended = True
                break
            current.append(swap.component)
            current.sort()
            lines[i] = f"{m.group(1)} {', '.join(current)} {m.group(3)}{swap.import_from}{m.group(4)}"
            existing.setdefault(swap.import_from, set()).add(swap.component)
            extended = True
            break
        if not extended:
            added_lines.append(
                f'import {{ {swap.component} }} from "{swap.import_from}"\n'
            )
            existing.setdefault(swap.import_from, set()).add(swap.component)

    if added_lines:
        lines[insert_idx:insert_idx] = added_lines

    return "".join(lines)


def _find_import_insert_point(lines: list[str]) -> int:
    """Return the index at which new import lines should be inserted.

    Heuristic: after any "use client" / "use server" directive and
    after any existing contiguous import block at the top of the file;
    else at index 0.
    """
    i = 0
    n = len(lines)
    # Skip leading directive + blank lines.
    while i < n and lines[i].strip() in {
        '"use client"', '"use client";',
        "'use client'", "'use client';",
        '"use server"', '"use server";',
        "'use server'", "'use server';",
        "",
    }:
        i += 1
    # Walk over any contiguous `import …` lines.
    last_import = i
    while last_import < n and (
        lines[last_import].lstrip().startswith("import")
        or lines[last_import].strip() == ""
    ):
        if lines[last_import].lstrip().startswith("import"):
            last_import += 1
        else:
            # stop at the first blank line that isn't between imports
            break
    return last_import if last_import > 0 else i


# ── Public: reporting ────────────────────────────────────────────────


def render_report(report: LintReport | Iterable[LintReport]) -> str:
    """Render one or more reports as compact, deterministic markdown.

    The output is suitable for agent context re-injection ("here's what
    failed; fix it") or for surfacing in CI logs.
    """
    if isinstance(report, LintReport):
        reports: tuple[LintReport, ...] = (report,)
    else:
        reports = tuple(report)

    lines: list[str] = [f"# Component consistency lint (v{LINTER_SCHEMA_VERSION})", ""]
    total_errors = sum(r.severity_counts["error"] for r in reports)
    total_warns = sum(r.severity_counts["warn"] for r in reports)

    if not reports:
        lines.append("_No files scanned._")
        return "\n".join(lines).rstrip() + "\n"

    if total_errors == 0 and total_warns == 0:
        lines.append(f"All {len(reports)} file(s) clean.")
        return "\n".join(lines).rstrip() + "\n"

    lines.append(
        f"Summary: {total_errors} error(s), {total_warns} warning(s) "
        f"across {len(reports)} file(s)."
    )
    lines.append("")

    for rep in reports:
        if not rep.violations:
            continue
        header = rep.source or "<inline snippet>"
        lines.append(f"## {header}")
        for v in rep.violations:
            lines.append(
                f"- [{v.severity}] `{v.rule_id}` at {v.line}:{v.column} — "
                f"{v.message}"
            )
            if v.snippet:
                lines.append(f"    `{v.snippet}`")
            if v.suggested_fix:
                lines.append(f"    → fix: {v.suggested_fix}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── Public: agent-facing tool ────────────────────────────────────────


def run_consistency_linter(
    code: str | None = None,
    *,
    path: str | Path | None = None,
    auto_fix: bool = False,
) -> dict:
    """Agent-callable entry point (see UI Designer skill tool list).

    Exactly one of ``code`` or ``path`` must be supplied.  Returns a
    JSON-safe dict so the tool boundary doesn't leak dataclass
    instances.
    """
    if (code is None) == (path is None):
        raise ValueError("run_consistency_linter: supply exactly one of code= / path=")

    fixed_code: str | None = None
    if auto_fix:
        if code is not None:
            fixed_code, report = auto_fix_code(code)
        else:
            fixed_code, report = auto_fix_file(Path(path), write=False)
    else:
        if code is not None:
            report = lint_code(code)
        else:
            report = lint_file(Path(path))

    result = report.to_dict()
    result["auto_fix_applied"] = bool(auto_fix)
    if fixed_code is not None:
        result["fixed_code"] = fixed_code
    result["markdown"] = render_report(report)
    return result
