"""OP-840 tree-sitter import/reference extraction for repo maps.

The repo-map subsystem builds a directed graph of intra-repo references
so the orchestrator can answer "which files transitively touch X" without
re-reading every file on every turn. This module is the *parsing edge*
of that pipeline: given a single source file, extract the set of
import-like references it makes (the graph builder upstream is what
connects them into edges).

Supported languages
-------------------
Selected by file suffix via :data:`LANGUAGE_BY_SUFFIX`:

* ``.py``   → ``python``  — parsed with ``tree_sitter_python``; the
  import statement substring is then re-parsed with the stdlib
  :mod:`ast` to recover precise alias / level information.
* ``.ts``   → ``typescript`` — parsed with ``tree_sitter_typescript``
  (``typescript`` grammar); references are scraped from import,
  export, and call expressions via regex.
* ``.tsx``  → ``tsx`` — same package, ``tsx`` grammar variant.

Any other suffix is treated as unsupported (callers see
:class:`GrammarMissing` so they can decide whether to skip or warn).

Public API
----------
* :class:`TreeSitterImportParser` — stateful parser; caches one
  ``tree_sitter.Parser`` instance per language after first use.
* :class:`ParsedSource` — frozen result record returned by
  ``parse_file`` (path, language, sorted reference tuple).
* :func:`language_for_path` — pure suffix → language-key lookup,
  exported so the graph builder can pre-filter the file list without
  instantiating a parser.
* Exceptions: :class:`TreeSitterUnavailable` (binding not installed —
  hard stop, fleet-wide), :class:`GrammarMissing` (one language's
  grammar package is missing — degrade for that language), and
  :class:`FileParseError` (one file failed — skip that file).

Telemetry codes
---------------
Diagnostics emitted via ``log.warning(..., extra={"code": ...})`` use
the module-level constant codes :data:`TREE_SITTER_BINARY_MISSING`,
:data:`TREE_SITTER_GRAMMAR_MISSING_LANG`, and
:data:`PARSE_ERROR_PER_FILE`. These are stable identifiers that the
B7 telemetry sink groups on; do not inline-rename them without
updating the dashboard query.
"""

from __future__ import annotations

import ast
import importlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TREE_SITTER_BINARY_MISSING = "tree_sitter_binary_missing"
TREE_SITTER_GRAMMAR_MISSING_LANG = "tree_sitter_grammar_missing_lang"
PARSE_ERROR_PER_FILE = "parse_error_per_file"

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
}

_TS_IMPORT_RE = re.compile(r"""(?:from\s+|import\s*\(\s*)["']([^"']+)["']""")
_TS_SIDE_EFFECT_RE = re.compile(r"""^\s*import\s+["']([^"']+)["']""")
_PY_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class TreeSitterUnavailable(Exception):
    """The ``tree_sitter`` Python binding is not installed.

    Raised once, eagerly, the first time :meth:`TreeSitterImportParser._get_parser`
    is asked for any language. Callers should treat this as fleet-wide
    degradation (every file will fail) and fall back to a non-AST repo
    map rather than retrying per file.
    """


class GrammarMissing(Exception):
    """A grammar package for one supported language is not installed.

    Carries the offending language key on :attr:`language` so the caller
    can decide whether to skip that language entirely or surface a
    targeted "install ``tree_sitter_<language>``" hint. Construction
    sets the message to ``"tree-sitter grammar missing for <language>"``.
    """

    def __init__(self, language: str):
        self.language = language
        super().__init__(f"tree-sitter grammar missing for {language}")


class FileParseError(Exception):
    """One source file failed parsing and should be skipped.

    Raised when tree-sitter either threw during ``parse()`` or returned
    a tree whose root reports ``has_error``. The message is prefixed
    with the repo-relative path so log scrapers can attribute the
    failure without consulting the original traceback.
    """


@dataclass(frozen=True)
class ParsedSource:
    """References extracted from one source file.

    Attributes:
        path: Repo-relative POSIX path (forward slashes on every OS) so
            downstream graph keys are platform-stable.
        language: One of the values of :data:`LANGUAGE_BY_SUFFIX`
            (``"python"``, ``"typescript"``, or ``"tsx"``).
        references: De-duplicated, lexicographically sorted tuple of
            module-like reference strings. Sorted so two runs over the
            same input produce byte-identical output, which the graph
            cache relies on for hash-equality.
    """

    path: str
    language: str
    references: tuple[str, ...]


class TreeSitterImportParser:
    """Parse supported source files and extract import-like references.

    The instance is stateful: each language's ``tree_sitter.Parser`` is
    constructed lazily on first use and cached on :attr:`_loaded` for
    the parser's lifetime. A single parser instance is therefore safe
    to reuse across many files but is **not** safe to share between
    threads (``tree_sitter.Parser`` itself is not thread-safe).

    Tests may pre-seed the cache by passing ``parser_by_language``
    (e.g. with a stub that emits a controlled AST), which bypasses
    :func:`_load_tree_sitter_parser` entirely so the test does not
    depend on the binding being installed.
    """

    def __init__(self, parser_by_language: dict[str, Any] | None = None):
        self._parser_by_language = parser_by_language or {}
        self._loaded: dict[str, Any | None] = {}

    def parse_file(self, repo_root: Path, source_path: Path) -> ParsedSource:
        """Parse ``source_path`` and return its sorted reference set.

        ``repo_root`` is used only to derive the repo-relative path
        recorded on :class:`ParsedSource`; the file itself is read from
        ``source_path`` (UTF-8 with ``errors="replace"`` so a stray
        non-UTF-8 byte cannot kill the run). Raises
        :class:`GrammarMissing` if the suffix is unsupported or the
        grammar package for the resolved language is not installed, and
        :class:`FileParseError` if tree-sitter either throws during
        ``parse()`` or returns a tree whose root reports ``has_error``.
        """

        rel_path = source_path.relative_to(repo_root).as_posix()
        language = language_for_path(source_path)
        if language is None:
            raise GrammarMissing(source_path.suffix)
        parser = self._get_parser(language)
        if parser is None:
            raise GrammarMissing(language)
        text = source_path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = parser.parse(text.encode("utf-8"))
        except Exception as exc:
            raise FileParseError(f"{rel_path}: {exc}") from exc
        root = tree.root_node
        if getattr(root, "has_error", False):
            raise FileParseError(f"{rel_path}: tree-sitter parse error")
        refs = _extract_references(root, text, language)
        return ParsedSource(path=rel_path, language=language, references=tuple(sorted(refs)))

    def _get_parser(self, language: str) -> Any | None:
        """Return a cached parser for ``language``, loading it on first use.

        Lookup order: explicit test override (``parser_by_language``)
        first, then the lazy-load cache, then a fresh
        :func:`_load_tree_sitter_parser`. A successful load is cached
        on :attr:`_loaded`; a per-language load failure (other than
        the binding itself being missing) is also cached as ``None``
        so subsequent files in the same language do not retry the
        failing import. :class:`TreeSitterUnavailable` is **not**
        cached — it is re-raised so the run can abort cleanly instead
        of degrading silently.
        """

        if language in self._parser_by_language:
            return self._parser_by_language[language]
        if language in self._loaded:
            return self._loaded[language]
        try:
            parser = _load_tree_sitter_parser(language)
        except TreeSitterUnavailable:
            raise
        except Exception as exc:
            log.warning(
                "repo_map_parse_gap",
                extra={
                    "code": TREE_SITTER_GRAMMAR_MISSING_LANG,
                    "language": language,
                    "error": str(exc),
                },
            )
            parser = None
        self._loaded[language] = parser
        return parser


def language_for_path(path: Path) -> str | None:
    """Return the repo-map language key for a supported path."""

    return LANGUAGE_BY_SUFFIX.get(path.suffix)


def _load_tree_sitter_parser(language: str) -> Any:
    """Build a fresh ``tree_sitter.Parser`` bound to ``language``.

    Maps the language key onto the (pip-installed) grammar module +
    grammar-name pair, opens the shipped ``_binding.abi3.so`` from
    that package, and configures a new Parser. Raises
    :class:`TreeSitterUnavailable` if the ``tree_sitter`` binding
    itself is missing (fleet-wide fault) and :class:`GrammarMissing`
    if ``language`` is outside the supported set. ImportErrors for
    the per-language grammar package propagate as-is so
    :meth:`TreeSitterImportParser._get_parser` can attribute them to
    that language.
    """

    try:
        from tree_sitter import Language, Parser
    except ImportError as exc:
        raise TreeSitterUnavailable(str(exc)) from exc

    if language == "python":
        module_name, grammar_name = "tree_sitter_python", "python"
    elif language == "typescript":
        module_name, grammar_name = "tree_sitter_typescript", "typescript"
    elif language == "tsx":
        module_name, grammar_name = "tree_sitter_typescript", "tsx"
    else:
        raise GrammarMissing(language)

    module = importlib.import_module(module_name)
    binding_path = Path(module.__path__[0]) / "_binding.abi3.so"
    lang = Language(str(binding_path), grammar_name)
    parser = Parser()
    parser.set_language(lang)
    return parser


def _extract_references(root: Any, text: str, language: str) -> set[str]:
    """Walk the tree-sitter AST and collect import-like references.

    Dispatches per language: Python ``import`` / ``import from`` nodes
    feed :func:`_python_import_refs` (which re-parses with the stdlib
    :mod:`ast` for alias precision), while TypeScript / TSX import,
    export, and call-expression nodes feed
    :func:`_typescript_import_refs` (regex scrape — ``require('x')``
    and dynamic ``import('x')`` look the same at this layer). Returns
    an unordered set; the caller is responsible for sorting before
    handing it to :class:`ParsedSource`.
    """

    refs: set[str] = set()
    for node in _walk(root):
        node_type = getattr(node, "type", "")
        if language == "python" and node_type in {"import_statement", "import_from_statement"}:
            refs.update(_python_import_refs(_node_text(node, text)))
        elif language in {"typescript", "tsx"} and node_type in {
            "import_statement",
            "export_statement",
            "call_expression",
        }:
            refs.update(_typescript_import_refs(_node_text(node, text)))
    return refs


def _walk(node: Any):
    """Yield ``node`` and every descendant in pre-order."""

    yield node
    for child in getattr(node, "children", []):
        yield from _walk(child)


def _node_text(node: Any, text: str) -> str:
    """Return the source substring spanned by ``node``.

    Slicing is done on the UTF-8 byte buffer (tree-sitter's
    ``start_byte`` / ``end_byte`` are byte offsets, not character
    offsets) and decoded back with ``errors="replace"`` so a
    partial-multibyte cut on either end cannot raise.
    """

    start = int(getattr(node, "start_byte", 0))
    end = int(getattr(node, "end_byte", 0))
    return text.encode("utf-8")[start:end].decode("utf-8", errors="replace")


def _python_import_refs(statement: str) -> set[str]:
    """Recover the imported names from one Python import statement.

    Re-parses ``statement`` with the stdlib :mod:`ast` so alias and
    relative-level information (``from .foo import bar``) is exact
    rather than regex-approximated. A :class:`SyntaxError` (e.g. the
    captured node text turned out to be a fragment) yields the empty
    set rather than propagating, so one malformed slice cannot blank
    out the whole file's reference set. Names that fail the
    :data:`_PY_IDENTIFIER_RE` shape check are dropped to keep the
    output safe for use as graph keys.
    """

    refs: set[str] = set()
    try:
        module = ast.parse(statement).body[0]
    except SyntaxError:
        return refs
    if isinstance(module, ast.Import):
        for alias in module.names:
            if _PY_IDENTIFIER_RE.match(alias.name):
                refs.add(alias.name)
    elif isinstance(module, ast.ImportFrom):
        base = "." * module.level + (module.module or "")
        if base:
            refs.add(base)
        for alias in module.names:
            if alias.name != "*" and _PY_IDENTIFIER_RE.match(alias.name):
                refs.add(f"{base}.{alias.name}" if base else alias.name)
    return refs


def _typescript_import_refs(statement: str) -> set[str]:
    """Scrape module specifiers out of a TypeScript / TSX statement.

    Two passes: :data:`_TS_IMPORT_RE` catches both static ``from "x"``
    and dynamic ``import("x")`` / ``require("x")`` forms, and
    :data:`_TS_SIDE_EFFECT_RE` additionally catches bare
    ``import "x"`` side-effect imports (which the first pattern would
    miss because there is no ``from`` keyword). Empty matches are
    filtered so the caller never sees ``""`` as a "reference".
    """

    refs = {m.group(1) for m in _TS_IMPORT_RE.finditer(statement)}
    refs.update(m.group(1) for m in _TS_SIDE_EFFECT_RE.finditer(statement))
    return {ref for ref in refs if ref}
