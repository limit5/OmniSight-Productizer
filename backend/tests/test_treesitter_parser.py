"""OP-1301 tree-sitter parser input validation tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from backend.agents.treesitter_parser import (
    FileParseError,
    GrammarMissing,
    TreeSitterImportParser,
    language_for_path,
)


@dataclass
class StubNode:
    type: str
    start_byte: int = 0
    end_byte: int = 0
    has_error: bool = False
    children: list["StubNode"] = field(default_factory=list)


@dataclass
class StubTree:
    root_node: StubNode


class StubParser:
    def __init__(self, root: StubNode | None = None, error: Exception | None = None):
        self.root = root or StubNode("module")
        self.error = error
        self.payloads: list[bytes] = []

    def parse(self, payload: bytes) -> StubTree:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return StubTree(self.root)


# language_for_path


@pytest.mark.parametrize(
    "path,expected",
    [
        (Path("app.py"), "python"),
        (Path("app.ts"), "typescript"),
        (Path("app.tsx"), "tsx"),
        (Path(""), None),
        (Path("README"), None),
        (Path("archive.tar.gz"), None),
    ],
)
def test_language_for_path_handles_supported_empty_and_unknown_paths(
    path: Path,
    expected: str | None,
) -> None:
    assert language_for_path(path) == expected


@pytest.mark.parametrize("path", [None, "app.py", 42, []])
def test_language_for_path_rejects_non_path_inputs(path: Any) -> None:
    with pytest.raises(AttributeError):
        language_for_path(path)  # type: ignore[arg-type]


def test_language_for_path_handles_very_large_path_value() -> None:
    path = Path(f"{'pkg/' * 2000}module.py")

    assert language_for_path(path) == "python"


# TreeSitterImportParser.parse_file


def test_parse_file_rejects_none_inputs(tmp_path: Path) -> None:
    parser = TreeSitterImportParser(parser_by_language={"python": StubParser()})
    source_path = tmp_path / "app.py"
    source_path.write_text("", encoding="utf-8")

    with pytest.raises(TypeError):
        parser.parse_file(None, source_path)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        parser.parse_file(tmp_path, None)  # type: ignore[arg-type]


def test_parse_file_rejects_wrong_type_inputs(tmp_path: Path) -> None:
    parser = TreeSitterImportParser(parser_by_language={"python": StubParser()})
    source_path = tmp_path / "app.py"
    source_path.write_text("", encoding="utf-8")

    with pytest.raises(TypeError):
        parser.parse_file(42, source_path)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        parser.parse_file(tmp_path, str(source_path))  # type: ignore[arg-type]


def test_parse_file_rejects_empty_collection_inputs() -> None:
    parser = TreeSitterImportParser(parser_by_language={"python": StubParser()})

    with pytest.raises(AttributeError):
        parser.parse_file([], [])  # type: ignore[arg-type]


def test_parse_file_rejects_unsupported_empty_suffix(tmp_path: Path) -> None:
    source_path = tmp_path / "README"
    source_path.write_text("", encoding="utf-8")
    parser = TreeSitterImportParser()

    with pytest.raises(GrammarMissing) as exc_info:
        parser.parse_file(tmp_path, source_path)

    assert exc_info.value.language == ""


def test_parse_file_handles_empty_supported_file(tmp_path: Path) -> None:
    source_path = tmp_path / "pkg" / "empty.py"
    source_path.parent.mkdir()
    source_path.write_text("", encoding="utf-8")
    stub_parser = StubParser()
    parser = TreeSitterImportParser(parser_by_language={"python": stub_parser})

    parsed = parser.parse_file(tmp_path, source_path)

    assert parsed.path == "pkg/empty.py"
    assert parsed.language == "python"
    assert parsed.references == ()
    assert stub_parser.payloads == [b""]


def test_parse_file_wraps_parser_exceptions(tmp_path: Path) -> None:
    source_path = tmp_path / "app.py"
    source_path.write_text("import os\n", encoding="utf-8")
    parser = TreeSitterImportParser(
        parser_by_language={"python": StubParser(error=RuntimeError("boom"))}
    )

    with pytest.raises(FileParseError, match=r"app\.py: boom"):
        parser.parse_file(tmp_path, source_path)


def test_parse_file_rejects_tree_sitter_error_roots(tmp_path: Path) -> None:
    source_path = tmp_path / "app.py"
    source_path.write_text("import os\n", encoding="utf-8")
    parser = TreeSitterImportParser(
        parser_by_language={"python": StubParser(StubNode("module", has_error=True))}
    )

    with pytest.raises(FileParseError, match=r"app\.py: tree-sitter parse error"):
        parser.parse_file(tmp_path, source_path)


def test_parse_file_handles_very_large_supported_file(tmp_path: Path) -> None:
    text = "import os\n" + ("# filler\n" * 20_000)
    source_path = tmp_path / "large.py"
    source_path.write_text(text, encoding="utf-8")
    root = StubNode(
        "module",
        children=[StubNode("import_statement", 0, len("import os".encode("utf-8")))],
    )
    parser = TreeSitterImportParser(parser_by_language={"python": StubParser(root)})

    parsed = parser.parse_file(tmp_path, source_path)

    assert parsed.path == "large.py"
    assert parsed.language == "python"
    assert parsed.references == ("os",)
