"""OP-840 tree-sitter import/reference extraction for repo maps."""

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
    """The tree-sitter Python binding is not installed."""


class GrammarMissing(Exception):
    """A grammar package for one supported language is not installed."""

    def __init__(self, language: str):
        self.language = language
        super().__init__(f"tree-sitter grammar missing for {language}")


class FileParseError(Exception):
    """One source file failed parsing and should be skipped."""


@dataclass(frozen=True)
class ParsedSource:
    """References extracted from one source file."""

    path: str
    language: str
    references: tuple[str, ...]


class TreeSitterImportParser:
    """Parse supported source files and extract import-like references."""

    def __init__(self, parser_by_language: dict[str, Any] | None = None):
        self._parser_by_language = parser_by_language or {}
        self._loaded: dict[str, Any | None] = {}

    def parse_file(self, repo_root: Path, source_path: Path) -> ParsedSource:
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
    yield node
    for child in getattr(node, "children", []):
        yield from _walk(child)


def _node_text(node: Any, text: str) -> str:
    start = int(getattr(node, "start_byte", 0))
    end = int(getattr(node, "end_byte", 0))
    return text.encode("utf-8")[start:end].decode("utf-8", errors="replace")


def _python_import_refs(statement: str) -> set[str]:
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
    refs = {m.group(1) for m in _TS_IMPORT_RE.finditer(statement)}
    refs.update(m.group(1) for m in _TS_SIDE_EFFECT_RE.finditer(statement))
    return {ref for ref in refs if ref}
