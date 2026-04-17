"""V4 #3 (TODO row 1535) — Contract tests for the shadcn-CLI block
exporter (``backend/web_block_exporter.py``).

The block exporter packages agent-generated React/Tailwind components
into a single JSON file that ``npx shadcn add <url>`` consumes. The
tests pin the wire format (registry-item v2 schema), the dependency
inference heuristics, the path-safety guards, and the on-disk export
layout. No subprocess / network activity — every helper is pure.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from backend import web_block_exporter as wbe
from backend.web_block_exporter import (
    BLOCK_NAME_RE,
    BLOCK_TYPE_BLOCK,
    BLOCK_TYPE_COMPONENT,
    BLOCK_TYPE_FILE,
    BLOCK_TYPE_HOOK,
    BLOCK_TYPE_LIB,
    BLOCK_TYPE_PAGE,
    BLOCK_TYPE_STYLE,
    BLOCK_TYPE_THEME,
    BLOCK_TYPE_UI,
    DEFAULT_REGISTRY_DIR,
    KNOWN_NPM_PACKAGES,
    KNOWN_SHADCN_UI_COMPONENTS,
    MAX_BLOCK_FILE_BYTES,
    REGISTRY_FILE_TYPES,
    REGISTRY_INDEX_SCHEMA,
    REGISTRY_ITEM_SCHEMA,
    REGISTRY_ITEM_TYPES,
    BlockExport,
    BlockExportError,
    BlockFile,
    BlockValidationError,
    EmptyBlockError,
    ExportResult,
    InvalidBlockNameError,
    UnsafeBlockPathError,
    assert_safe_relative_path,
    block_export_filename,
    build_block,
    build_block_from_directory,
    build_registry_index,
    compute_block_url,
    compute_install_command,
    compute_registry_index_entry,
    detect_npm_dependencies,
    detect_shadcn_dependencies,
    export_block,
    extract_imports,
    infer_file_type,
    load_block_from_json,
    merge_unique,
    serialize_registry_item,
    slugify_block_name,
    to_registry_item_dict,
    validate_block_name,
)


# ── Module invariants ─────────────────────────────────────────────────

def test_all_exports_are_present():
    for name in wbe.__all__:
        assert hasattr(wbe, name), f"missing __all__ symbol: {name}"


def test_schema_constants_pinned():
    assert REGISTRY_ITEM_SCHEMA == "https://ui.shadcn.com/schema/registry-item.json"
    assert REGISTRY_INDEX_SCHEMA == "https://ui.shadcn.com/schema/registry.json"


def test_registry_item_types_cover_all_categories():
    expected = {
        BLOCK_TYPE_BLOCK, BLOCK_TYPE_COMPONENT, BLOCK_TYPE_UI,
        BLOCK_TYPE_HOOK, BLOCK_TYPE_LIB, BLOCK_TYPE_PAGE,
        BLOCK_TYPE_FILE, BLOCK_TYPE_STYLE, BLOCK_TYPE_THEME,
    }
    assert set(REGISTRY_ITEM_TYPES) == expected
    # All of them are valid file types too.
    assert set(REGISTRY_FILE_TYPES) == expected


def test_known_shadcn_ui_components_includes_core_set():
    must_have = {"button", "card", "input", "select", "tabs", "dialog"}
    assert must_have.issubset(KNOWN_SHADCN_UI_COMPONENTS)


def test_known_npm_packages_includes_essentials():
    must_have = {"lucide-react", "next", "clsx", "@radix-ui/react-slot"}
    assert must_have.issubset(KNOWN_NPM_PACKAGES)


def test_block_name_re_pattern():
    assert BLOCK_NAME_RE.match("hero-block")
    assert BLOCK_NAME_RE.match("a")
    assert BLOCK_NAME_RE.match("a" * 64)
    assert not BLOCK_NAME_RE.match("a" * 65)
    assert not BLOCK_NAME_RE.match("Hero-Block")
    assert not BLOCK_NAME_RE.match("1-block")
    assert not BLOCK_NAME_RE.match("hero_block")


def test_max_block_file_bytes_is_one_mib():
    assert MAX_BLOCK_FILE_BYTES == 1_048_576


def test_default_registry_dir_is_lowercase_r():
    assert DEFAULT_REGISTRY_DIR == "r"


def test_error_class_hierarchy():
    assert issubclass(InvalidBlockNameError, BlockExportError)
    assert issubclass(UnsafeBlockPathError, BlockExportError)
    assert issubclass(EmptyBlockError, BlockExportError)
    assert issubclass(BlockValidationError, BlockExportError)


# ── slugify_block_name ────────────────────────────────────────────────

def test_slugify_strips_and_lowers():
    assert slugify_block_name("  Hero Block  ") == "hero-block"


def test_slugify_collapses_runs_of_separators():
    assert slugify_block_name("Hero___ block!!!") == "hero-block"


def test_slugify_trims_leading_non_letter():
    assert slugify_block_name("123-hero") == "hero"


def test_slugify_caps_at_64():
    s = slugify_block_name("a" * 200)
    assert len(s) == 64
    assert s == "a" * 64


def test_slugify_rejects_non_str():
    with pytest.raises(InvalidBlockNameError):
        slugify_block_name(None)  # type: ignore[arg-type]


def test_slugify_rejects_empty_after_clean():
    with pytest.raises(InvalidBlockNameError):
        slugify_block_name("---!!!")


def test_slugify_unicode_collapses_to_dash():
    # Non-ascii letters fall outside the regex → collapsed to dashes.
    assert slugify_block_name("café-hero") == "caf-hero"


# ── validate_block_name ───────────────────────────────────────────────

@pytest.mark.parametrize("good", ["a", "hero", "hero-block", "x" * 64])
def test_validate_block_name_accepts_valid(good):
    assert validate_block_name(good) == good


@pytest.mark.parametrize("bad", [
    "Hero", "1block", "hero_block", "hero block", "x" * 65, "",
])
def test_validate_block_name_rejects_invalid(bad):
    with pytest.raises(InvalidBlockNameError):
        validate_block_name(bad)


def test_validate_block_name_rejects_non_str():
    with pytest.raises(InvalidBlockNameError):
        validate_block_name(None)  # type: ignore[arg-type]


# ── assert_safe_relative_path ─────────────────────────────────────────

def test_safe_path_accepts_simple():
    assert assert_safe_relative_path("hero.tsx") == "hero.tsx"


def test_safe_path_normalises_backslashes():
    assert assert_safe_relative_path("a\\b\\c.tsx") == "a/b/c.tsx"


def test_safe_path_rejects_absolute():
    with pytest.raises(UnsafeBlockPathError):
        assert_safe_relative_path("/etc/passwd")


def test_safe_path_rejects_drive_letter():
    with pytest.raises(UnsafeBlockPathError):
        assert_safe_relative_path("C:/Windows/system.ini")


def test_safe_path_rejects_dotdot():
    with pytest.raises(UnsafeBlockPathError):
        assert_safe_relative_path("../escape.tsx")
    with pytest.raises(UnsafeBlockPathError):
        assert_safe_relative_path("foo/../escape.tsx")


def test_safe_path_rejects_empty():
    with pytest.raises(UnsafeBlockPathError):
        assert_safe_relative_path("   ")


def test_safe_path_rejects_non_str():
    with pytest.raises(UnsafeBlockPathError):
        assert_safe_relative_path(None)  # type: ignore[arg-type]


# ── infer_file_type ───────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("components/ui/button.tsx", BLOCK_TYPE_UI),
    ("hooks/use-toast.ts", BLOCK_TYPE_HOOK),
    ("foo/hooks/use-x.ts", BLOCK_TYPE_HOOK),
    ("lib/utils.ts", BLOCK_TYPE_LIB),
    ("foo/lib/x.ts", BLOCK_TYPE_LIB),
    ("app/dashboard/page.tsx", BLOCK_TYPE_PAGE),
    ("app/page.jsx", BLOCK_TYPE_PAGE),
    ("globals.css", BLOCK_TYPE_STYLE),
    ("styles.scss", BLOCK_TYPE_STYLE),
    ("components/hero.tsx", BLOCK_TYPE_COMPONENT),
    ("anything.ts", BLOCK_TYPE_COMPONENT),
    ("README.md", BLOCK_TYPE_FILE),
    ("", BLOCK_TYPE_FILE),
])
def test_infer_file_type(path, expected):
    assert infer_file_type(path) == expected


def test_infer_file_type_handles_non_str():
    assert infer_file_type(None) == BLOCK_TYPE_FILE  # type: ignore[arg-type]


def test_infer_file_type_normalises_backslash():
    assert infer_file_type("components\\ui\\button.tsx") == BLOCK_TYPE_UI


# ── extract_imports ───────────────────────────────────────────────────

def test_extract_imports_named_default_namespace():
    src = '''
    import React from "react";
    import { Button } from "@/components/ui/button";
    import * as Foo from "@/lib/utils";
    import "tailwindcss/tailwind.css";
    '''
    out = extract_imports(src)
    assert "react" in out
    assert "@/components/ui/button" in out
    assert "@/lib/utils" in out
    assert "tailwindcss/tailwind.css" in out


def test_extract_imports_handles_export_from():
    src = 'export { Button } from "@/components/ui/button";'
    assert "@/components/ui/button" in extract_imports(src)


def test_extract_imports_dedupes_preserving_first_occurrence():
    src = '''
    import "a";
    import "b";
    import "a";
    '''
    assert extract_imports(src) == ["a", "b"]


def test_extract_imports_empty_inputs():
    assert extract_imports("") == []
    assert extract_imports(None) == []  # type: ignore[arg-type]


# ── detect_shadcn_dependencies ────────────────────────────────────────

def test_detect_shadcn_basic():
    src = (
        'import { Button } from "@/components/ui/button";\n'
        'import { Card } from "@/components/ui/card";\n'
        'import Anything from "react";'
    )
    assert detect_shadcn_dependencies(src) == ["button", "card"]


def test_detect_shadcn_strips_extension_subpath():
    src = (
        'import { X } from "@/components/ui/button.tsx";\n'
        'import { Y } from "@/components/ui/card/index.ts";'
    )
    out = detect_shadcn_dependencies(src)
    assert "button" in out and "card" in out


def test_detect_shadcn_ignores_unknown_primitive():
    src = 'import { X } from "@/components/ui/totally-made-up";'
    assert detect_shadcn_dependencies(src) == []


def test_detect_shadcn_custom_known_set():
    src = 'import { X } from "@/components/ui/foo";'
    assert detect_shadcn_dependencies(src, known=frozenset({"foo"})) == ["foo"]


def test_detect_shadcn_returns_sorted():
    src = (
        'import { Z } from "@/components/ui/tabs";\n'
        'import { A } from "@/components/ui/button";'
    )
    assert detect_shadcn_dependencies(src) == ["button", "tabs"]


# ── detect_npm_dependencies ───────────────────────────────────────────

def test_detect_npm_basic():
    src = (
        'import "lucide-react";\n'
        'import { Slot } from "@radix-ui/react-slot";\n'
        'import "next/link";'
    )
    out = detect_npm_dependencies(src)
    assert "lucide-react" in out
    assert "@radix-ui/react-slot" in out
    assert "next" in out


def test_detect_npm_drops_react_by_default():
    src = 'import React from "react";\nimport "react-dom";'
    assert detect_npm_dependencies(src) == []


def test_detect_npm_keeps_react_when_drop_disabled():
    src = 'import React from "react";'
    assert "react" in detect_npm_dependencies(src, drop_builtins=False)


def test_detect_npm_skips_relative_and_alias():
    src = (
        'import "./local";\n'
        'import "../foo";\n'
        'import "@/components/ui/button";'
    )
    assert detect_npm_dependencies(src) == []


def test_detect_npm_returns_sorted_unique():
    src = (
        'import "zebra";\n'
        'import "alpha";\n'
        'import "alpha";'
    )
    assert detect_npm_dependencies(src) == ["alpha", "zebra"]


# ── merge_unique ──────────────────────────────────────────────────────

def test_merge_unique_basic():
    assert merge_unique(["b", "a"], ["c", "a"]) == ("a", "b", "c")


def test_merge_unique_handles_none_and_blanks():
    assert merge_unique(None, [], ["", "  ", "x"]) == ("x",)


def test_merge_unique_skips_non_str():
    assert merge_unique(["a", 1, None, "b"]) == ("a", "b")


# ── compute_block_url + compute_install_command + filename ────────────

def test_compute_block_url_strips_trailing_slash():
    assert compute_block_url("https://x.com/", "hero-block") == \
        "https://x.com/r/hero-block.json"


def test_compute_block_url_rejects_bad_inputs():
    with pytest.raises(BlockExportError):
        compute_block_url("", "hero-block")
    with pytest.raises(InvalidBlockNameError):
        compute_block_url("https://x.com", "Bad Name")


def test_compute_install_command_default_runner():
    assert compute_install_command("https://x.com/r/h.json") == \
        "npx shadcn add https://x.com/r/h.json"


def test_compute_install_command_alt_runner():
    assert compute_install_command(
        "https://x.com/r/h.json", runner="pnpm dlx",
    ) == "pnpm dlx shadcn add https://x.com/r/h.json"


def test_compute_install_command_quotes_url_with_special_chars():
    cmd = compute_install_command("https://x.com/r/h.json?token=abc")
    assert '"https://x.com/r/h.json?token=abc"' in cmd


def test_compute_install_command_rejects_blank():
    with pytest.raises(BlockExportError):
        compute_install_command("")
    with pytest.raises(BlockExportError):
        compute_install_command("https://x", runner="")


def test_block_export_filename():
    assert block_export_filename("hero-block") == "hero-block.json"
    with pytest.raises(InvalidBlockNameError):
        block_export_filename("Bad")


# ── BlockFile dataclass ───────────────────────────────────────────────

def test_block_file_to_dict_minimal():
    bf = BlockFile(path="a.tsx", content="x")
    assert bf.to_dict() == {
        "path": "a.tsx", "content": "x", "type": BLOCK_TYPE_COMPONENT,
    }


def test_block_file_to_dict_with_target():
    bf = BlockFile(path="a.tsx", content="x", target="components/a.tsx")
    assert bf.to_dict()["target"] == "components/a.tsx"


def test_block_file_is_frozen():
    bf = BlockFile(path="a.tsx", content="x")
    with pytest.raises(Exception):
        bf.path = "b.tsx"  # type: ignore[misc]


# ── build_block — happy paths ─────────────────────────────────────────

def _hero_file():
    return BlockFile(
        path="hero.tsx",
        content=(
            'import { Button } from "@/components/ui/button";\n'
            'import { Card } from "@/components/ui/card";\n'
            'import { ArrowRight } from "lucide-react";\n'
            'export function Hero() { return <Card><Button><ArrowRight/></Button></Card>; }\n'
        ),
    )


def test_build_block_minimal():
    b = build_block(name="hero-block", files=[_hero_file()])
    assert b.name == "hero-block"
    assert b.type == BLOCK_TYPE_BLOCK
    assert b.title == "Hero Block"  # auto-titled from kebab-case
    assert b.dependencies == ("lucide-react",)
    assert b.registry_dependencies == ("button", "card")
    assert b.files[0].type == BLOCK_TYPE_COMPONENT
    assert b.meta == {}


def test_build_block_overrides_title_and_description():
    b = build_block(
        name="hero-block",
        files=[_hero_file()],
        title="My Hero",
        description="A custom hero.",
    )
    assert b.title == "My Hero"
    assert b.description == "A custom hero."


def test_build_block_explicit_deps_merged_with_inferred():
    b = build_block(
        name="hero-block",
        files=[_hero_file()],
        dependencies=["zod"],
        registry_dependencies=["badge"],
    )
    assert "zod" in b.dependencies and "lucide-react" in b.dependencies
    assert "badge" in b.registry_dependencies and "button" in b.registry_dependencies


def test_build_block_autoinfer_off_keeps_raw():
    b = build_block(
        name="hero-block",
        files=[_hero_file()],
        autoinfer=False,
    )
    assert b.dependencies == ()
    assert b.registry_dependencies == ()
    # Path-based type inference is also skipped.
    assert b.files[0].type == BLOCK_TYPE_COMPONENT


def test_build_block_path_type_inference_for_ui_file():
    f = BlockFile(path="components/ui/my-thing.tsx", content="export const X=1;")
    b = build_block(name="my-block", files=[f])
    assert b.files[0].type == BLOCK_TYPE_UI


def test_build_block_accepts_dict_files():
    b = build_block(
        name="my-block",
        files=[{"path": "x.tsx", "content": "export const X=1;"}],
    )
    assert b.files[0].path == "x.tsx"


def test_build_block_tailwind_and_cssvars_pass_through():
    tailwind = {"config": {"theme": {"extend": {}}}}
    css_vars = {"light": {"primary": "0 0% 0%"}, "dark": {"primary": "0 0% 100%"}}
    b = build_block(
        name="hero",
        files=[_hero_file()],
        tailwind=tailwind,
        css_vars=css_vars,
    )
    assert b.tailwind == tailwind
    assert b.css_vars == css_vars


def test_build_block_categories_sorted_unique():
    b = build_block(
        name="hero",
        files=[_hero_file()],
        categories=["marketing", "marketing", "hero"],
    )
    assert b.categories == ("hero", "marketing")


def test_build_block_meta_pass_through():
    b = build_block(
        name="hero", files=[_hero_file()],
        meta={"agent": "claude-opus-4-7", "iteration": 3},
    )
    assert b.meta == {"agent": "claude-opus-4-7", "iteration": 3}


# ── build_block — error paths ─────────────────────────────────────────

def test_build_block_rejects_invalid_name():
    with pytest.raises(InvalidBlockNameError):
        build_block(name="Bad Name", files=[_hero_file()])


def test_build_block_rejects_unknown_item_type():
    with pytest.raises(BlockValidationError):
        build_block(
            name="hero", files=[_hero_file()],
            item_type="registry:wibble",
        )


def test_build_block_rejects_empty_files():
    with pytest.raises(EmptyBlockError):
        build_block(name="hero", files=[])


def test_build_block_rejects_unknown_file_type():
    with pytest.raises(BlockValidationError):
        build_block(
            name="hero",
            files=[BlockFile(path="x.tsx", content="x", type="registry:weird")],
            autoinfer=False,
        )


def test_build_block_rejects_oversize_file():
    big = "x" * (MAX_BLOCK_FILE_BYTES + 1)
    with pytest.raises(BlockValidationError):
        build_block(
            name="hero",
            files=[BlockFile(path="x.tsx", content=big)],
        )


def test_build_block_rejects_duplicate_paths():
    with pytest.raises(BlockValidationError):
        build_block(
            name="hero",
            files=[
                BlockFile(path="x.tsx", content="a"),
                BlockFile(path="x.tsx", content="b"),
            ],
        )


def test_build_block_rejects_unsafe_path():
    with pytest.raises(UnsafeBlockPathError):
        build_block(
            name="hero",
            files=[BlockFile(path="../escape.tsx", content="x")],
        )


def test_build_block_rejects_unsupported_file_entry():
    with pytest.raises(BlockValidationError):
        build_block(name="hero", files=[42])  # type: ignore[list-item]


# ── build_block_from_directory ────────────────────────────────────────

def test_build_block_from_directory_happy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "hero.tsx").write_text(
        'import { Button } from "@/components/ui/button";\nexport const X=1;',
        encoding="utf-8",
    )
    (src / "utils.ts").write_text("export const y=2;", encoding="utf-8")
    b = build_block_from_directory(name="hero-block", source_dir=src)
    paths = sorted(f.path for f in b.files)
    assert paths == ["hero.tsx", "utils.ts"]
    assert "button" in b.registry_dependencies


def test_build_block_from_directory_with_prefix(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "hero.tsx").write_text("export const X=1;", encoding="utf-8")
    b = build_block_from_directory(
        name="hero-block", source_dir=src, file_path_prefix="components/hero",
    )
    assert b.files[0].path == "components/hero/hero.tsx"


def test_build_block_from_directory_missing_dir(tmp_path):
    with pytest.raises(BlockExportError):
        build_block_from_directory(name="x", source_dir=tmp_path / "missing")


def test_build_block_from_directory_no_matches(tmp_path):
    (tmp_path / "x.txt").write_text("z", encoding="utf-8")
    with pytest.raises(EmptyBlockError):
        build_block_from_directory(name="x", source_dir=tmp_path)


def test_build_block_from_directory_rejects_non_utf8(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "bad.tsx").write_bytes(b"\xff\xfe\x00\x00not utf8")
    with pytest.raises(BlockValidationError):
        build_block_from_directory(name="x", source_dir=src)


# ── to_registry_item_dict + serialize_registry_item ───────────────────

def test_to_registry_item_dict_minimal_payload():
    b = build_block(name="hero-block", files=[_hero_file()])
    d = to_registry_item_dict(b)
    assert d["$schema"] == REGISTRY_ITEM_SCHEMA
    assert d["name"] == "hero-block"
    assert d["type"] == BLOCK_TYPE_BLOCK
    assert d["title"] == "Hero Block"
    assert d["dependencies"] == ["lucide-react"]
    assert d["registryDependencies"] == ["button", "card"]
    assert isinstance(d["files"], list) and len(d["files"]) == 1
    assert d["files"][0]["path"] == "hero.tsx"
    # Optional empties stay omitted to keep payload lean.
    for k in ("devDependencies", "tailwind", "cssVars",
              "categories", "docs", "author", "meta"):
        assert k not in d


def test_to_registry_item_dict_includes_optional_fields_when_set():
    b = build_block(
        name="hero",
        files=[_hero_file()],
        author="agent",
        dev_dependencies=["typescript"],
        tailwind={"config": {}},
        css_vars={"light": {}},
        categories=["marketing"],
        docs="See README.",
        meta={"x": 1},
    )
    d = to_registry_item_dict(b)
    assert d["author"] == "agent"
    assert d["devDependencies"] == ["typescript"]
    assert d["tailwind"] == {"config": {}}
    assert d["cssVars"] == {"light": {}}
    assert d["categories"] == ["marketing"]
    assert d["docs"] == "See README."
    assert d["meta"] == {"x": 1}


def test_to_registry_item_dict_rejects_non_export():
    with pytest.raises(BlockValidationError):
        to_registry_item_dict({"name": "hero"})  # type: ignore[arg-type]


def test_to_registry_item_dict_rejects_empty_files():
    bad = BlockExport(name="hero", files=())
    with pytest.raises(EmptyBlockError):
        to_registry_item_dict(bad)


def test_serialize_registry_item_round_trips_via_json():
    b = build_block(name="hero-block", files=[_hero_file()])
    payload = serialize_registry_item(b)
    parsed = json.loads(payload)
    assert parsed["name"] == "hero-block"
    assert parsed["files"][0]["path"] == "hero.tsx"


def test_serialize_registry_item_compact_mode():
    b = build_block(name="hero", files=[_hero_file()])
    payload = serialize_registry_item(b, indent=None)
    assert "\n" not in payload  # single line


def test_serialize_registry_item_sort_keys_flag():
    b = build_block(name="hero", files=[_hero_file()])
    payload = serialize_registry_item(b, sort_keys=True)
    parsed = json.loads(payload)
    keys = list(parsed.keys())
    assert keys == sorted(keys)


def test_serialize_registry_item_preserves_unicode():
    f = BlockFile(path="x.tsx", content="// café")
    b = build_block(name="hero", files=[f])
    payload = serialize_registry_item(b)
    assert "café" in payload


# ── compute_registry_index_entry + build_registry_index ───────────────

def test_compute_registry_index_entry_basic():
    b = build_block(name="hero", files=[_hero_file()], description="d")
    e = compute_registry_index_entry(b)
    assert e["name"] == "hero"
    assert e["type"] == BLOCK_TYPE_BLOCK
    assert e["description"] == "d"
    assert e["files"] == ["hero.tsx"]


def test_build_registry_index_basic():
    b1 = build_block(name="hero", files=[_hero_file()])
    b2 = build_block(name="footer", files=[BlockFile(path="f.tsx", content="export const Z=1;")])
    idx = build_registry_index([b1, b2], homepage="https://example.com")
    assert idx["$schema"] == REGISTRY_INDEX_SCHEMA
    assert idx["name"] == "omnisight-registry"
    assert idx["homepage"] == "https://example.com"
    assert {i["name"] for i in idx["items"]} == {"hero", "footer"}


def test_build_registry_index_rejects_duplicates():
    b1 = build_block(name="hero", files=[_hero_file()])
    b2 = build_block(name="hero", files=[_hero_file()])
    with pytest.raises(BlockValidationError):
        build_registry_index([b1, b2])


# ── export_block ──────────────────────────────────────────────────────

def test_export_block_writes_json_artefact(tmp_path):
    b = build_block(name="hero-block", files=[_hero_file()])
    res = export_block(b, tmp_path)
    assert res.json_path == tmp_path / "r" / "hero-block.json"
    assert res.json_path.exists()
    assert res.bytes_written > 0
    assert res.file_count == 1
    assert res.block_url is None
    assert res.install_command is None
    parsed = json.loads(res.json_path.read_text(encoding="utf-8"))
    assert parsed["name"] == "hero-block"


def test_export_block_with_base_url_emits_install_command(tmp_path):
    b = build_block(name="hero-block", files=[_hero_file()])
    res = export_block(b, tmp_path, base_url="https://x.com")
    assert res.block_url == "https://x.com/r/hero-block.json"
    assert res.install_command == "npx shadcn add https://x.com/r/hero-block.json"


def test_export_block_alt_runner(tmp_path):
    b = build_block(name="hero-block", files=[_hero_file()])
    res = export_block(b, tmp_path, base_url="https://x.com", runner="bunx")
    assert res.install_command.startswith("bunx shadcn add ")


def test_export_block_writes_individual_files(tmp_path):
    b = build_block(
        name="hero",
        files=[
            BlockFile(path="hero.tsx", content="A"),
            BlockFile(path="sub/util.ts", content="B"),
        ],
        autoinfer=False,
    )
    res = export_block(b, tmp_path, write_individual_files=True)
    assert (tmp_path / "r" / "hero" / "hero.tsx").read_text() == "A"
    assert (tmp_path / "r" / "hero" / "sub" / "util.ts").read_text() == "B"
    assert len(res.files_emitted) == 1 + 2


def test_export_block_creates_registry_index(tmp_path):
    b = build_block(name="hero-block", files=[_hero_file()])
    res = export_block(b, tmp_path, update_registry_index=True)
    idx_path = tmp_path / "registry.json"
    assert idx_path.exists()
    assert res.registry_index_path == idx_path
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    assert idx["items"][0]["name"] == "hero-block"


def test_export_block_appends_to_existing_registry_index(tmp_path):
    b1 = build_block(name="hero", files=[_hero_file()])
    b2 = build_block(name="footer", files=[BlockFile(path="f.tsx", content="export const Z=1;")])
    export_block(b1, tmp_path, update_registry_index=True)
    export_block(b2, tmp_path, update_registry_index=True)
    idx = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    names = {i["name"] for i in idx["items"]}
    assert names == {"hero", "footer"}


def test_export_block_replaces_same_name_in_registry_index(tmp_path):
    b1 = build_block(name="hero", files=[_hero_file()])
    export_block(b1, tmp_path, update_registry_index=True)
    b2 = build_block(name="hero", files=[BlockFile(path="hero.tsx", content="// new")])
    export_block(b2, tmp_path, update_registry_index=True)
    idx = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    hero_entries = [i for i in idx["items"] if i["name"] == "hero"]
    assert len(hero_entries) == 1


def test_export_block_sha256_matches_file(tmp_path):
    b = build_block(name="hero", files=[_hero_file()])
    res = export_block(b, tmp_path)
    digest = hashlib.sha256(res.json_path.read_bytes()).hexdigest()
    assert res.sha256 == digest


def test_export_block_writer_seam_captures_writes(tmp_path):
    b = build_block(name="hero", files=[_hero_file()])
    captured: list[tuple[Path, str]] = []
    res = export_block(
        b, tmp_path,
        writer=lambda p, body: captured.append((p, body)),
    )
    assert captured  # at least the JSON artefact
    assert any(p.name == "hero.json" for p, _ in captured)
    assert not res.json_path.exists()  # no actual disk write


def test_export_block_result_to_dict(tmp_path):
    b = build_block(name="hero", files=[_hero_file()])
    res = export_block(b, tmp_path, base_url="https://x.com")
    d = res.to_dict()
    assert d["name"] == "hero"
    assert d["block_url"] == "https://x.com/r/hero.json"
    assert d["install_command"]
    assert d["sha256"]
    assert d["registry_index_path"] is None


def test_export_block_invalid_name_raises_before_write(tmp_path):
    bad = BlockExport(name="Bad", files=(_hero_file(),))
    with pytest.raises(InvalidBlockNameError):
        export_block(bad, tmp_path)
    assert not (tmp_path / "r").exists()


def test_export_block_recovers_from_corrupt_registry_json(tmp_path):
    (tmp_path / "registry.json").write_text("{not json", encoding="utf-8")
    b = build_block(name="hero", files=[_hero_file()])
    res = export_block(b, tmp_path, update_registry_index=True)
    assert res.registry_index_path is not None
    idx = json.loads(res.registry_index_path.read_text(encoding="utf-8"))
    assert idx["items"][0]["name"] == "hero"


# ── load_block_from_json ──────────────────────────────────────────────

def test_load_block_from_json_round_trip(tmp_path):
    b = build_block(name="hero", files=[_hero_file()], description="d")
    res = export_block(b, tmp_path)
    reloaded = load_block_from_json(res.json_path)
    assert reloaded.name == "hero"
    assert reloaded.description == "d"
    assert reloaded.files[0].path == "hero.tsx"
    assert reloaded.dependencies == ("lucide-react",)
    assert reloaded.registry_dependencies == ("button", "card")


def test_load_block_from_json_preserves_unknown_fields(tmp_path):
    payload = {
        "$schema": REGISTRY_ITEM_SCHEMA,
        "name": "hero",
        "type": BLOCK_TYPE_BLOCK,
        "files": [{"path": "x.tsx", "content": "export const X=1;"}],
        "futureField": {"value": 42},
    }
    p = tmp_path / "hero.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    b = load_block_from_json(p)
    assert b.meta["_unknown_fields"]["futureField"] == {"value": 42}


def test_load_block_from_json_rejects_non_object(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(BlockValidationError):
        load_block_from_json(p)


def test_load_block_from_json_rejects_missing_name(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"files": [{"path": "x", "content": ""}]}),
                 encoding="utf-8")
    with pytest.raises(BlockValidationError):
        load_block_from_json(p)


def test_load_block_from_json_rejects_missing_files(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"name": "hero", "files": []}), encoding="utf-8")
    with pytest.raises(EmptyBlockError):
        load_block_from_json(p)


# ── End-to-end shape: a full block JSON validates as shadcn-CLI ready ─

def test_full_block_export_matches_shadcn_cli_shape(tmp_path):
    """A complete block export should contain every field that
    ``npx shadcn add <url>`` reads. This is the contract test that
    pins compatibility with the shadcn CLI consumer."""
    files = [
        BlockFile(
            path="components/hero/hero.tsx",
            content=(
                'import { Button } from "@/components/ui/button";\n'
                'import { Card, CardContent } from "@/components/ui/card";\n'
                'import { ArrowRight } from "lucide-react";\n'
                'import { cn } from "@/lib/utils";\n'
                'export function Hero() {\n'
                '  return (<Card><CardContent>'
                '<Button>Go<ArrowRight/></Button></CardContent></Card>);\n'
                '}\n'
            ),
            target="components/hero.tsx",
        ),
        BlockFile(
            path="hooks/use-hero.ts",
            content="export function useHero(){return null;}",
        ),
    ]
    b = build_block(
        name="hero-block",
        files=files,
        title="Hero Block",
        description="A hero with CTA, generated by the agent.",
        author="omnisight-agent",
        tailwind={"config": {"theme": {"extend": {}}}},
        css_vars={
            "light": {"primary": "240 9% 9%"},
            "dark": {"primary": "0 0% 98%"},
        },
        categories=["marketing", "hero"],
    )
    res = export_block(
        b, tmp_path,
        base_url="https://blocks.example.com",
        update_registry_index=True,
        write_individual_files=True,
    )

    payload = json.loads(res.json_path.read_text(encoding="utf-8"))
    # Required CLI surface.
    assert payload["$schema"] == REGISTRY_ITEM_SCHEMA
    assert payload["name"] == "hero-block"
    assert payload["type"] == BLOCK_TYPE_BLOCK
    assert payload["title"] == "Hero Block"
    assert payload["author"] == "omnisight-agent"
    assert "lucide-react" in payload["dependencies"]
    assert set(payload["registryDependencies"]) >= {"button", "card"}
    assert set(payload["categories"]) == {"marketing", "hero"}
    assert payload["tailwind"]["config"]["theme"]["extend"] == {}
    assert payload["cssVars"]["light"]["primary"] == "240 9% 9%"
    # Per-file shape (the CLI iterates files[].content).
    paths = {f["path"]: f for f in payload["files"]}
    assert paths["components/hero/hero.tsx"]["type"] == BLOCK_TYPE_COMPONENT
    assert paths["components/hero/hero.tsx"]["target"] == "components/hero.tsx"
    assert paths["hooks/use-hero.ts"]["type"] == BLOCK_TYPE_HOOK
    assert "export function" in paths["components/hero/hero.tsx"]["content"]
    # Install command surface for release notes.
    assert res.install_command == \
        "npx shadcn add https://blocks.example.com/r/hero-block.json"


def test_block_export_is_jsonable_via_dataclass_replace():
    b = build_block(name="hero", files=[_hero_file()])
    # We never expose mutation, but make sure the wire dict holds up
    # under a stdlib json.dumps round-trip (no custom encoder needed).
    payload = json.dumps(to_registry_item_dict(b))
    parsed = json.loads(payload)
    assert parsed["name"] == "hero"
