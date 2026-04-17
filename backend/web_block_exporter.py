"""V4 #3 (TODO row 1535) — Block exporter for shadcn-CLI compatible blocks.

Goal
----
Take the React/Tailwind components that the agent generated inside a Web
workspace (V4 #1 ``app/workspace/web/page.tsx``) and pack them into a
*standalone block* whose canonical artefact — a single
``<name>.json`` file — can be installed by anyone with::

    npx shadcn add https://example.com/r/<name>.json

This is the share/distribute path that runs *parallel* to V4 #2's
"instant preview URL" (which shares a *running* site). The block path
shares the *source* — a CLI-installable component bundle that drops
straight into another shadcn project.

Reference
---------
The on-disk JSON conforms to shadcn's published *registry-item* schema
(``https://ui.shadcn.com/schema/registry-item.json``) so the unmodified
shadcn CLI consumes it. We support the v2 / 2024+ surface:

- ``$schema`` / ``name`` / ``type`` / ``title`` / ``description``
- ``dependencies`` (npm), ``devDependencies``, ``registryDependencies``
- ``files: [{path, content, type, target?}]``
- ``tailwind: {config: {...}}`` (optional)
- ``cssVars: {light: {...}, dark: {...}}`` (optional)
- ``meta`` / ``categories`` / ``docs`` (optional)

Non-goals
---------
- Running ``npx shadcn build`` ourselves (operator-side concern; we just
  emit the JSON it would emit).
- HTTP serving of the block (the URL is operator-supplied — could be
  GitHub raw, S3, the W4 docker-nginx adapter, etc.).
- Rewriting code (we ship the agent's source verbatim — no codemods).

Pure helpers do all the parsing / dependency inference so unit tests can
pin the wire format without touching the filesystem.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ── Schema constants (string-pinned for JSON serialisation) ──────────

REGISTRY_ITEM_SCHEMA = "https://ui.shadcn.com/schema/registry-item.json"
REGISTRY_INDEX_SCHEMA = "https://ui.shadcn.com/schema/registry.json"

BLOCK_TYPE_BLOCK = "registry:block"
BLOCK_TYPE_COMPONENT = "registry:component"
BLOCK_TYPE_UI = "registry:ui"
BLOCK_TYPE_HOOK = "registry:hook"
BLOCK_TYPE_LIB = "registry:lib"
BLOCK_TYPE_PAGE = "registry:page"
BLOCK_TYPE_FILE = "registry:file"
BLOCK_TYPE_STYLE = "registry:style"
BLOCK_TYPE_THEME = "registry:theme"

# Item-level ``type`` values (what the JSON's outer ``type`` may be).
REGISTRY_ITEM_TYPES: tuple[str, ...] = (
    BLOCK_TYPE_BLOCK,
    BLOCK_TYPE_COMPONENT,
    BLOCK_TYPE_UI,
    BLOCK_TYPE_HOOK,
    BLOCK_TYPE_LIB,
    BLOCK_TYPE_PAGE,
    BLOCK_TYPE_FILE,
    BLOCK_TYPE_STYLE,
    BLOCK_TYPE_THEME,
)

# Per-file ``type`` values inside ``files: [{type: ...}]``.
REGISTRY_FILE_TYPES: tuple[str, ...] = REGISTRY_ITEM_TYPES

# Block-name lint — same character class shadcn enforces (lowercase,
# kebab-case, ≤64 chars, must start with a letter).
BLOCK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# Per-file size cap so a runaway ``content: "..."`` blob can't OOM the
# CLI consumer. 1 MiB matches the practical block ceiling — anything
# bigger is almost certainly a bundled asset that does not belong inline.
MAX_BLOCK_FILE_BYTES = 1_048_576

# Default subdirectory for registry items inside the export dir. Mirrors
# shadcn's documented convention (``public/r/<name>.json``).
DEFAULT_REGISTRY_DIR = "r"

# Recognised shadcn alias prefix — matches ``components.json`` aliases.
SHADCN_UI_IMPORT_PREFIX = "@/components/ui/"
SHADCN_HOOK_IMPORT_PREFIX = "@/hooks/"
SHADCN_LIB_IMPORT_PREFIX = "@/lib/"

# The shadcn primitives that ship with ``new-york`` style. Imports of
# ``@/components/ui/<X>`` map straight to ``registryDependencies: [X]``
# when ``X`` is in this set — anything else stays an external dep so
# the operator notices a typo before publishing.
KNOWN_SHADCN_UI_COMPONENTS: frozenset[str] = frozenset({
    "accordion", "alert", "alert-dialog", "aspect-ratio", "avatar",
    "badge", "breadcrumb", "button", "button-group", "calendar", "card",
    "carousel", "chart", "checkbox", "collapsible", "command",
    "context-menu", "dialog", "drawer", "dropdown-menu", "empty",
    "field", "form", "hover-card", "input", "input-group", "input-otp",
    "item", "kbd", "label", "menubar", "navigation-menu", "pagination",
    "popover", "progress", "radio-group", "resizable", "scroll-area",
    "select", "separator", "sheet", "sidebar", "skeleton", "slider",
    "sonner", "spinner", "switch", "table", "tabs", "textarea",
    "toast", "toaster", "toggle", "toggle-group", "tooltip",
})

# Common npm packages a generated block typically pulls in. We do *not*
# treat this as an allowlist (any package may appear) — we use it to
# decide whether an import should land in ``dependencies`` vs.
# ``registryDependencies``. Packages outside the shadcn alias prefix
# always land in ``dependencies`` regardless of presence here.
KNOWN_NPM_PACKAGES: frozenset[str] = frozenset({
    "react", "react-dom", "next", "lucide-react", "clsx",
    "tailwind-merge", "class-variance-authority", "tailwindcss-animate",
    "zod", "react-hook-form", "@hookform/resolvers", "date-fns",
    "recharts", "embla-carousel-react", "cmdk", "vaul", "sonner",
    "@radix-ui/react-accordion", "@radix-ui/react-alert-dialog",
    "@radix-ui/react-aspect-ratio", "@radix-ui/react-avatar",
    "@radix-ui/react-checkbox", "@radix-ui/react-collapsible",
    "@radix-ui/react-context-menu", "@radix-ui/react-dialog",
    "@radix-ui/react-dropdown-menu", "@radix-ui/react-hover-card",
    "@radix-ui/react-label", "@radix-ui/react-menubar",
    "@radix-ui/react-navigation-menu", "@radix-ui/react-popover",
    "@radix-ui/react-progress", "@radix-ui/react-radio-group",
    "@radix-ui/react-scroll-area", "@radix-ui/react-select",
    "@radix-ui/react-separator", "@radix-ui/react-slider",
    "@radix-ui/react-slot", "@radix-ui/react-switch",
    "@radix-ui/react-tabs", "@radix-ui/react-toast",
    "@radix-ui/react-toggle", "@radix-ui/react-toggle-group",
    "@radix-ui/react-tooltip",
})

# Built-ins that should NEVER be emitted as a dependency.
_BUILTIN_BARE_IMPORTS: frozenset[str] = frozenset({
    "react", "react-dom",
})

# ES-module ``import`` statements we recognise. We deliberately accept
# the common forms only — the CLI has its own parser if it needs more.
_IMPORT_RE = re.compile(
    r"""
    \bimport
    (?:
        \s+ (?:                       # full ``import X from "src"``
            [\w*${},\s]+? \s+ from
        )
    )?
    \s*
    ['"](?P<src>[^'"\n]+)['"]
    """,
    re.VERBOSE,
)

# Bare-export ``export ... from "src"`` re-exports also count.
_EXPORT_FROM_RE = re.compile(
    r"""
    \bexport
    \s+ (?:[\w*${},\s]+?) \s+ from
    \s* ['"](?P<src>[^'"\n]+)['"]
    """,
    re.VERBOSE,
)


# ── Errors ────────────────────────────────────────────────────────────

class BlockExportError(Exception):
    """Base class for everything raised by this module."""


class InvalidBlockNameError(BlockExportError):
    """Block name failed the kebab-case lint."""


class UnsafeBlockPathError(BlockExportError):
    """A file path attempted to escape the registry root (``..``,
    absolute path, drive prefix, etc.)."""


class EmptyBlockError(BlockExportError):
    """Block has no files — refuse to emit an empty registry item."""


class BlockValidationError(BlockExportError):
    """Schema validation failure (duplicate paths, unknown ``type``,
    oversize file, unserialisable ``content`` etc.)."""


# ── Data models ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class BlockFile:
    """One file inside a registry item.

    ``path`` is relative to the registry root (``r/<name>/...``).
    ``target`` is the optional install destination inside the consuming
    project; when None the CLI defaults to the path under the project's
    aliases (``components/ui/*`` etc.).
    """

    path: str
    content: str
    type: str = BLOCK_TYPE_COMPONENT
    target: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.path,
            "content": self.content,
            "type": self.type,
        }
        if self.target is not None:
            d["target"] = self.target
        return d


@dataclass(frozen=True)
class BlockExport:
    """A single shadcn-CLI compatible registry item (a *block*).

    Mirrors the v2 registry-item schema. ``files`` is the only required
    payload; all other fields are optional but populate sane defaults so
    a freshly-generated agent block remains installable.
    """

    name: str
    files: tuple[BlockFile, ...]
    type: str = BLOCK_TYPE_BLOCK
    title: Optional[str] = None
    description: str = ""
    author: Optional[str] = None
    dependencies: tuple[str, ...] = ()
    dev_dependencies: tuple[str, ...] = ()
    registry_dependencies: tuple[str, ...] = ()
    tailwind: Optional[dict[str, Any]] = None
    css_vars: Optional[dict[str, Any]] = None
    categories: tuple[str, ...] = ()
    docs: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportResult:
    """Outcome of ``export_block(...)``."""

    name: str
    json_path: Path
    bytes_written: int
    sha256: str
    block_url: Optional[str]
    install_command: Optional[str]
    file_count: int
    files_emitted: tuple[Path, ...]
    registry_index_path: Optional[Path] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "json_path": str(self.json_path),
            "bytes_written": self.bytes_written,
            "sha256": self.sha256,
            "block_url": self.block_url,
            "install_command": self.install_command,
            "file_count": self.file_count,
            "files_emitted": [str(p) for p in self.files_emitted],
            "registry_index_path": (
                str(self.registry_index_path)
                if self.registry_index_path is not None
                else None
            ),
        }


# ── Pure helpers (no filesystem / network) ───────────────────────────

def slugify_block_name(value: str) -> str:
    """Coerce ``value`` into a kebab-case block name.

    - Strips surrounding whitespace.
    - Lower-cases.
    - Replaces any run of disallowed chars with ``-``.
    - Trims leading non-letter chars (block names must start with a
      letter per the shadcn schema).
    - Caps the result at 64 chars.
    """
    if not isinstance(value, str):
        raise InvalidBlockNameError(
            f"slugify_block_name expected str, got {type(value).__name__}",
        )
    s = value.strip().lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    s = re.sub(r"^[^a-z]+", "", s)
    if not s:
        raise InvalidBlockNameError(
            f"slugify_block_name: '{value}' yielded an empty slug",
        )
    return s[:64]


def validate_block_name(name: str) -> str:
    """Return ``name`` if it matches the canonical block-name regex.

    Raises ``InvalidBlockNameError`` otherwise. We intentionally do NOT
    coerce here — operators get a clear error so a publish step never
    silently renames their block.
    """
    if not isinstance(name, str) or not BLOCK_NAME_RE.match(name):
        raise InvalidBlockNameError(
            f"Block name '{name}' must match {BLOCK_NAME_RE.pattern}",
        )
    return name


def assert_safe_relative_path(path: str) -> str:
    """Reject absolute paths, drive-letter paths, and ``..`` traversal.

    Returns the normalised forward-slash path. Raises
    ``UnsafeBlockPathError`` on violation. Empty / whitespace-only paths
    also fail.
    """
    if not isinstance(path, str):
        raise UnsafeBlockPathError(
            f"path must be a string, got {type(path).__name__}",
        )
    raw = path.strip()
    if not raw:
        raise UnsafeBlockPathError("path must be non-empty")
    if raw.startswith("/") or raw.startswith("\\"):
        raise UnsafeBlockPathError(f"path '{path}' must be relative")
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        raise UnsafeBlockPathError(
            f"path '{path}' must not include a drive letter",
        )
    norm = PurePosixPath(raw.replace("\\", "/"))
    parts = norm.parts
    if any(part == ".." for part in parts):
        raise UnsafeBlockPathError(
            f"path '{path}' must not contain '..' segments",
        )
    if norm.is_absolute():
        raise UnsafeBlockPathError(f"path '{path}' must be relative")
    return str(norm)


def infer_file_type(path: str) -> str:
    """Guess the per-file ``type`` from its extension + prefix.

    Heuristic — operators may override per file by setting ``BlockFile.type``
    explicitly. Order matters; first match wins:

    - ``components/ui/*``  → ``registry:ui``
    - ``hooks/*``          → ``registry:hook``
    - ``lib/*``            → ``registry:lib``
    - ``app/**/page.tsx``  → ``registry:page``
    - ``*.css``            → ``registry:style``
    - any ``*.tsx`` / ``*.jsx`` / ``*.ts`` / ``*.js`` → ``registry:component``
    - everything else      → ``registry:file``
    """
    if not isinstance(path, str) or not path.strip():
        return BLOCK_TYPE_FILE
    p = path.strip().replace("\\", "/")
    lower = p.lower()
    if "components/ui/" in lower:
        return BLOCK_TYPE_UI
    if lower.startswith("hooks/") or "/hooks/" in lower:
        return BLOCK_TYPE_HOOK
    if lower.startswith("lib/") or "/lib/" in lower:
        return BLOCK_TYPE_LIB
    if lower.endswith("/page.tsx") or lower.endswith("/page.jsx"):
        return BLOCK_TYPE_PAGE
    if lower.endswith(".css") or lower.endswith(".scss"):
        return BLOCK_TYPE_STYLE
    if lower.endswith((".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs")):
        return BLOCK_TYPE_COMPONENT
    return BLOCK_TYPE_FILE


def extract_imports(content: str) -> list[str]:
    """Return the *import sources* found in ``content``.

    Captures both ``import x from "src"`` and ``export ... from "src"``
    forms, plus side-effect ``import "src"``. Order is preserved with
    duplicates removed (first occurrence wins). Non-string / falsy input
    returns ``[]``.
    """
    if not isinstance(content, str) or not content:
        return []
    seen: dict[str, None] = {}
    for m in _IMPORT_RE.finditer(content):
        src = m.group("src").strip()
        if src and src not in seen:
            seen[src] = None
    for m in _EXPORT_FROM_RE.finditer(content):
        src = m.group("src").strip()
        if src and src not in seen:
            seen[src] = None
    return list(seen.keys())


def _is_relative_import(src: str) -> bool:
    return src.startswith(".") or src.startswith("/")


def _is_alias_import(src: str) -> bool:
    return src.startswith("@/")


def detect_shadcn_dependencies(
    content: str,
    *,
    known: Optional[frozenset[str]] = None,
) -> list[str]:
    """Pull out the shadcn primitives this code imports.

    Looks for ``@/components/ui/<name>`` imports whose ``<name>`` (after
    stripping any trailing extension) appears in ``known``. Returns a
    sorted, de-duplicated list. Defaults to the project's known set of
    primitives — pass a custom ``known`` to widen / narrow the lint.
    """
    if known is None:
        known = KNOWN_SHADCN_UI_COMPONENTS
    out: set[str] = set()
    for src in extract_imports(content):
        if not src.startswith(SHADCN_UI_IMPORT_PREFIX):
            continue
        rest = src[len(SHADCN_UI_IMPORT_PREFIX):]
        rest = rest.split("/", 1)[0]
        rest = re.sub(r"\.(tsx?|jsx?|css)$", "", rest)
        if rest and rest in known:
            out.add(rest)
    return sorted(out)


def _bare_import_root(src: str) -> Optional[str]:
    """Return the top-level npm package for a bare specifier, or None.

    ``react``                     → ``react``
    ``react/jsx-runtime``         → ``react``
    ``@radix-ui/react-dialog``    → ``@radix-ui/react-dialog``
    ``@scope/pkg/sub/path``       → ``@scope/pkg``
    """
    if not src or _is_relative_import(src) or _is_alias_import(src):
        return None
    parts = src.split("/")
    if src.startswith("@"):
        if len(parts) < 2:
            return None
        return "/".join(parts[:2])
    return parts[0]


def detect_npm_dependencies(
    content: str,
    *,
    drop_builtins: bool = True,
) -> list[str]:
    """Pull bare-specifier ``import`` packages out of ``content``.

    Returns a sorted, de-duplicated list. ``drop_builtins=True`` filters
    out ``react`` / ``react-dom`` so a block doesn't redundantly declare
    them — every shadcn consumer already has them.
    """
    out: set[str] = set()
    for src in extract_imports(content):
        root = _bare_import_root(src)
        if not root:
            continue
        if drop_builtins and root in _BUILTIN_BARE_IMPORTS:
            continue
        out.add(root)
    return sorted(out)


def merge_unique(*streams: Iterable[str]) -> tuple[str, ...]:
    """Merge several iterables into a sorted, de-duplicated tuple."""
    seen: set[str] = set()
    for s in streams:
        if not s:
            continue
        for item in s:
            if not isinstance(item, str):
                continue
            v = item.strip()
            if v:
                seen.add(v)
    return tuple(sorted(seen))


def block_export_filename(name: str) -> str:
    """Return the canonical ``<name>.json`` filename for a block."""
    return f"{validate_block_name(name)}.json"


def compute_block_url(base_url: str, name: str) -> str:
    """Build the public URL where ``npx shadcn add`` should fetch the JSON.

    ``base_url`` is the operator-supplied registry root (e.g.
    ``https://blocks.example.com``). We append ``/r/<name>.json`` so the
    URL aligns with shadcn's documented convention.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise BlockExportError("base_url must be a non-empty string")
    validate_block_name(name)
    root = base_url.strip().rstrip("/")
    return f"{root}/{DEFAULT_REGISTRY_DIR}/{name}.json"


def compute_install_command(
    block_url: str,
    *,
    runner: str = "npx",
) -> str:
    """Render the operator-facing install command.

    ``runner`` is one of ``npx`` / ``pnpm dlx`` / ``bunx`` / ``yarn dlx``.
    Quoting is conservative so URLs with query strings paste cleanly.
    """
    if not isinstance(block_url, str) or not block_url.strip():
        raise BlockExportError("block_url must be a non-empty string")
    runner_clean = runner.strip()
    if not runner_clean:
        raise BlockExportError("runner must be non-empty")
    needs_quote = any(c in block_url for c in (" ", "&", "?", "#", ";", "*"))
    arg = f'"{block_url}"' if needs_quote else block_url
    return f"{runner_clean} shadcn add {arg}"


# ── Builders ─────────────────────────────────────────────────────────

def _coerce_files(files: Sequence[Any]) -> tuple[BlockFile, ...]:
    if files is None:
        return ()
    out: list[BlockFile] = []
    for entry in files:
        if isinstance(entry, BlockFile):
            out.append(entry)
        elif isinstance(entry, Mapping):
            out.append(BlockFile(
                path=str(entry["path"]),
                content=str(entry.get("content", "")),
                type=str(entry.get("type", BLOCK_TYPE_COMPONENT)),
                target=(
                    str(entry["target"]) if entry.get("target") is not None
                    else None
                ),
            ))
        else:
            raise BlockValidationError(
                f"unsupported file entry type: {type(entry).__name__}",
            )
    return tuple(out)


def build_block(
    *,
    name: str,
    files: Sequence[Any],
    title: Optional[str] = None,
    description: str = "",
    author: Optional[str] = None,
    dependencies: Optional[Iterable[str]] = None,
    dev_dependencies: Optional[Iterable[str]] = None,
    registry_dependencies: Optional[Iterable[str]] = None,
    tailwind: Optional[Mapping[str, Any]] = None,
    css_vars: Optional[Mapping[str, Any]] = None,
    categories: Optional[Iterable[str]] = None,
    docs: Optional[str] = None,
    meta: Optional[Mapping[str, Any]] = None,
    autoinfer: bool = True,
    item_type: str = BLOCK_TYPE_BLOCK,
) -> BlockExport:
    """Assemble a ``BlockExport`` from raw files + optional overrides.

    When ``autoinfer=True`` we scan each file's ``content`` for imports
    and merge the result into ``dependencies`` + ``registryDependencies``
    so a happy-path agent run "just works" without the operator having
    to manually enumerate Radix packages.
    """
    validate_block_name(name)
    if item_type not in REGISTRY_ITEM_TYPES:
        raise BlockValidationError(
            f"unknown item_type '{item_type}' "
            f"(allowed: {', '.join(REGISTRY_ITEM_TYPES)})",
        )
    coerced = _coerce_files(files)
    if not coerced:
        raise EmptyBlockError(f"block '{name}' has no files")

    inferred_npm: list[str] = []
    inferred_shadcn: list[str] = []
    if autoinfer:
        for f in coerced:
            inferred_npm.extend(detect_npm_dependencies(f.content))
            inferred_shadcn.extend(detect_shadcn_dependencies(f.content))

    deps = merge_unique(dependencies, inferred_npm)
    reg_deps = merge_unique(registry_dependencies, inferred_shadcn)
    dev_deps = merge_unique(dev_dependencies)
    cats = merge_unique(categories)

    # Apply path-based type inference for files left at the default
    # ``registry:component`` placeholder.
    final_files: list[BlockFile] = []
    for f in coerced:
        assert_safe_relative_path(f.path)
        if f.type == BLOCK_TYPE_COMPONENT and autoinfer:
            inferred = infer_file_type(f.path)
            f = replace(f, type=inferred)
        if f.type not in REGISTRY_FILE_TYPES:
            raise BlockValidationError(
                f"file '{f.path}' has unknown type '{f.type}' "
                f"(allowed: {', '.join(REGISTRY_FILE_TYPES)})",
            )
        size = len(f.content.encode("utf-8")) if f.content else 0
        if size > MAX_BLOCK_FILE_BYTES:
            raise BlockValidationError(
                f"file '{f.path}' is {size} bytes "
                f"(> MAX_BLOCK_FILE_BYTES={MAX_BLOCK_FILE_BYTES})",
            )
        final_files.append(f)

    seen_paths: set[str] = set()
    for f in final_files:
        if f.path in seen_paths:
            raise BlockValidationError(
                f"duplicate file path '{f.path}' in block '{name}'",
            )
        seen_paths.add(f.path)

    return BlockExport(
        name=name,
        files=tuple(final_files),
        type=item_type,
        title=title if title is not None else name.replace("-", " ").title(),
        description=description or "",
        author=author,
        dependencies=deps,
        dev_dependencies=dev_deps,
        registry_dependencies=reg_deps,
        tailwind=dict(tailwind) if tailwind else None,
        css_vars=dict(css_vars) if css_vars else None,
        categories=cats,
        docs=docs,
        meta=dict(meta) if meta else {},
    )


def build_block_from_directory(
    *,
    name: str,
    source_dir: Path,
    glob_patterns: Sequence[str] = ("**/*.tsx", "**/*.ts", "**/*.css"),
    description: str = "",
    title: Optional[str] = None,
    autoinfer: bool = True,
    file_path_prefix: str = "",
) -> BlockExport:
    """Walk ``source_dir`` and assemble a block from all matching files.

    ``file_path_prefix`` is prepended to each file's ``path`` so the
    block can mirror a deeper ``components/<name>/...`` layout in the
    consuming project.
    """
    if not isinstance(source_dir, Path):
        source_dir = Path(source_dir)
    if not source_dir.exists() or not source_dir.is_dir():
        raise BlockExportError(
            f"source_dir '{source_dir}' does not exist or is not a directory",
        )

    collected: list[Path] = []
    seen: set[Path] = set()
    for pattern in glob_patterns:
        for p in sorted(source_dir.glob(pattern)):
            if p.is_file() and p not in seen:
                seen.add(p)
                collected.append(p)
    if not collected:
        raise EmptyBlockError(
            f"no files matched {list(glob_patterns)} under '{source_dir}'",
        )

    files: list[BlockFile] = []
    prefix = file_path_prefix.strip().strip("/")
    for fp in collected:
        rel = fp.relative_to(source_dir).as_posix()
        path = f"{prefix}/{rel}" if prefix else rel
        try:
            content = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise BlockValidationError(
                f"file '{fp}' is not UTF-8 decodable: {e}",
            ) from e
        files.append(BlockFile(path=path, content=content))

    return build_block(
        name=name,
        files=files,
        title=title,
        description=description,
        autoinfer=autoinfer,
    )


# ── Serialiser ───────────────────────────────────────────────────────

def to_registry_item_dict(export: BlockExport) -> dict[str, Any]:
    """Render a ``BlockExport`` to the canonical wire dict.

    The output is JSON-serialisable and mirrors shadcn's published
    ``registry-item.json`` schema. Only present fields are emitted —
    omitting empty optionals keeps the payload lean for the CLI.
    """
    if not isinstance(export, BlockExport):
        raise BlockValidationError(
            f"to_registry_item_dict expects BlockExport, "
            f"got {type(export).__name__}",
        )
    validate_block_name(export.name)
    if not export.files:
        raise EmptyBlockError(f"block '{export.name}' has no files")

    out: dict[str, Any] = {
        "$schema": REGISTRY_ITEM_SCHEMA,
        "name": export.name,
        "type": export.type,
    }
    if export.title:
        out["title"] = export.title
    if export.description:
        out["description"] = export.description
    if export.author:
        out["author"] = export.author
    if export.dependencies:
        out["dependencies"] = list(export.dependencies)
    if export.dev_dependencies:
        out["devDependencies"] = list(export.dev_dependencies)
    if export.registry_dependencies:
        out["registryDependencies"] = list(export.registry_dependencies)
    out["files"] = [f.to_dict() for f in export.files]
    if export.tailwind:
        out["tailwind"] = export.tailwind
    if export.css_vars:
        out["cssVars"] = export.css_vars
    if export.categories:
        out["categories"] = list(export.categories)
    if export.docs:
        out["docs"] = export.docs
    if export.meta:
        out["meta"] = export.meta
    return out


def serialize_registry_item(
    export: BlockExport,
    *,
    indent: Optional[int] = 2,
    sort_keys: bool = False,
) -> str:
    """Render the registry item as a JSON string.

    ``indent=None`` produces a compact single-line payload (smaller for
    HTTP serving); the default keeps the file diff-friendly.
    """
    payload = to_registry_item_dict(export)
    return json.dumps(payload, indent=indent, sort_keys=sort_keys, ensure_ascii=False)


# ── Registry index (multi-block manifest) ────────────────────────────

def compute_registry_index_entry(export: BlockExport) -> dict[str, Any]:
    """One entry in the multi-block ``registry.json`` manifest."""
    entry: dict[str, Any] = {
        "name": export.name,
        "type": export.type,
    }
    if export.title:
        entry["title"] = export.title
    if export.description:
        entry["description"] = export.description
    entry["files"] = [f.path for f in export.files]
    if export.categories:
        entry["categories"] = list(export.categories)
    return entry


def build_registry_index(
    items: Sequence[BlockExport],
    *,
    name: str = "omnisight-registry",
    homepage: Optional[str] = None,
) -> dict[str, Any]:
    """Build the multi-block index that ``shadcn build`` would emit."""
    seen: set[str] = set()
    for item in items:
        if item.name in seen:
            raise BlockValidationError(
                f"duplicate block name '{item.name}' in registry index",
            )
        seen.add(item.name)
    out: dict[str, Any] = {
        "$schema": REGISTRY_INDEX_SCHEMA,
        "name": name,
        "items": [compute_registry_index_entry(i) for i in items],
    }
    if homepage:
        out["homepage"] = homepage
    return out


# ── Exporter (does write to disk) ────────────────────────────────────

def _write_text(
    path: Path, body: str, *, writer=None,
) -> int:
    """Write ``body`` to ``path`` and return bytes written.

    ``writer`` is a test seam — pass ``writer=lambda p, b: ...`` to
    capture writes without touching disk.
    """
    encoded = body.encode("utf-8")
    if writer is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
    else:
        writer(path, body)
    return len(encoded)


def export_block(
    export: BlockExport,
    output_dir: Path,
    *,
    base_url: Optional[str] = None,
    runner: str = "npx",
    write_individual_files: bool = False,
    indent: Optional[int] = 2,
    update_registry_index: bool = False,
    registry_index_name: str = "omnisight-registry",
    writer=None,
) -> ExportResult:
    """Persist a block to ``output_dir`` and return the artefact location.

    Layout (relative to ``output_dir``)::

        r/<name>.json                ← THE shadcn-CLI artefact
        r/<name>/<file.path>         ← optional raw sources (debug)
        registry.json                ← optional multi-block index

    When ``base_url`` is supplied we also pre-compute the public URL +
    one-line install command so the operator can paste them straight
    into release notes.
    """
    validate_block_name(export.name)
    if not isinstance(output_dir, Path):
        output_dir = Path(output_dir)

    body = serialize_registry_item(export, indent=indent)
    json_path = output_dir / DEFAULT_REGISTRY_DIR / block_export_filename(export.name)
    bytes_written = _write_text(json_path, body, writer=writer)

    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()

    files_emitted: list[Path] = [json_path]
    if write_individual_files:
        block_root = output_dir / DEFAULT_REGISTRY_DIR / export.name
        for bf in export.files:
            sub = block_root / assert_safe_relative_path(bf.path)
            _write_text(sub, bf.content, writer=writer)
            files_emitted.append(sub)

    block_url = (
        compute_block_url(base_url, export.name) if base_url else None
    )
    install_cmd = (
        compute_install_command(block_url, runner=runner)
        if block_url else None
    )

    registry_index_path: Optional[Path] = None
    if update_registry_index:
        index_path = output_dir / "registry.json"
        existing_items: list[BlockExport] = []
        if writer is None and index_path.exists():
            try:
                existing = json.loads(index_path.read_text(encoding="utf-8"))
                for entry in existing.get("items", []):
                    if entry.get("name") and entry["name"] != export.name:
                        existing_items.append(BlockExport(
                            name=entry["name"],
                            files=tuple(
                                BlockFile(path=p, content="")
                                for p in entry.get("files", [])
                            ) or (BlockFile(path="placeholder", content=""),),
                            type=entry.get("type", BLOCK_TYPE_BLOCK),
                            title=entry.get("title"),
                            description=entry.get("description", ""),
                            categories=tuple(entry.get("categories", ())),
                        ))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("registry.json unreadable, recreating: %s", e)
        existing_items.append(export)
        index_payload = build_registry_index(
            existing_items, name=registry_index_name,
        )
        _write_text(
            index_path,
            json.dumps(index_payload, indent=indent, ensure_ascii=False),
            writer=writer,
        )
        registry_index_path = index_path
        files_emitted.append(index_path)

    return ExportResult(
        name=export.name,
        json_path=json_path,
        bytes_written=bytes_written,
        sha256=sha,
        block_url=block_url,
        install_command=install_cmd,
        file_count=len(export.files),
        files_emitted=tuple(files_emitted),
        registry_index_path=registry_index_path,
    )


# ── Convenience: round-trip from on-disk JSON ────────────────────────

def load_block_from_json(path: Path) -> BlockExport:
    """Reverse of ``serialize_registry_item`` — useful for re-publishing
    or migrating an existing registry item.

    Unknown / future schema fields are preserved verbatim into ``meta``
    so a round-trip never silently drops data.
    """
    if not isinstance(path, Path):
        path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise BlockValidationError(
            f"{path}: top-level JSON must be an object",
        )
    name = raw.get("name")
    if not isinstance(name, str):
        raise BlockValidationError(f"{path}: missing 'name' (str)")
    files_raw = raw.get("files") or ()
    if not isinstance(files_raw, Sequence) or not files_raw:
        raise EmptyBlockError(f"{path}: missing 'files'")

    known = {
        "$schema", "name", "type", "title", "description", "author",
        "dependencies", "devDependencies", "registryDependencies",
        "files", "tailwind", "cssVars", "categories", "docs", "meta",
    }
    extra = {k: v for k, v in raw.items() if k not in known}
    meta = dict(raw.get("meta") or {})
    if extra:
        meta.setdefault("_unknown_fields", {}).update(extra)

    return BlockExport(
        name=name,
        files=tuple(
            BlockFile(
                path=str(f["path"]),
                content=str(f.get("content", "")),
                type=str(f.get("type", BLOCK_TYPE_COMPONENT)),
                target=(
                    str(f["target"]) if f.get("target") is not None else None
                ),
            )
            for f in files_raw
        ),
        type=raw.get("type", BLOCK_TYPE_BLOCK),
        title=raw.get("title"),
        description=raw.get("description") or "",
        author=raw.get("author"),
        dependencies=tuple(raw.get("dependencies") or ()),
        dev_dependencies=tuple(raw.get("devDependencies") or ()),
        registry_dependencies=tuple(raw.get("registryDependencies") or ()),
        tailwind=dict(raw["tailwind"]) if raw.get("tailwind") else None,
        css_vars=dict(raw["cssVars"]) if raw.get("cssVars") else None,
        categories=tuple(raw.get("categories") or ()),
        docs=raw.get("docs"),
        meta=meta,
    )


# ── Public surface ───────────────────────────────────────────────────

__all__ = [
    # schema constants
    "REGISTRY_ITEM_SCHEMA",
    "REGISTRY_INDEX_SCHEMA",
    "BLOCK_TYPE_BLOCK",
    "BLOCK_TYPE_COMPONENT",
    "BLOCK_TYPE_UI",
    "BLOCK_TYPE_HOOK",
    "BLOCK_TYPE_LIB",
    "BLOCK_TYPE_PAGE",
    "BLOCK_TYPE_FILE",
    "BLOCK_TYPE_STYLE",
    "BLOCK_TYPE_THEME",
    "REGISTRY_ITEM_TYPES",
    "REGISTRY_FILE_TYPES",
    "BLOCK_NAME_RE",
    "DEFAULT_REGISTRY_DIR",
    "MAX_BLOCK_FILE_BYTES",
    "SHADCN_UI_IMPORT_PREFIX",
    "SHADCN_HOOK_IMPORT_PREFIX",
    "SHADCN_LIB_IMPORT_PREFIX",
    "KNOWN_SHADCN_UI_COMPONENTS",
    "KNOWN_NPM_PACKAGES",
    # errors
    "BlockExportError",
    "InvalidBlockNameError",
    "UnsafeBlockPathError",
    "EmptyBlockError",
    "BlockValidationError",
    # data models
    "BlockFile",
    "BlockExport",
    "ExportResult",
    # pure helpers
    "slugify_block_name",
    "validate_block_name",
    "assert_safe_relative_path",
    "infer_file_type",
    "extract_imports",
    "detect_shadcn_dependencies",
    "detect_npm_dependencies",
    "merge_unique",
    "block_export_filename",
    "compute_block_url",
    "compute_install_command",
    # builders
    "build_block",
    "build_block_from_directory",
    # serialiser
    "to_registry_item_dict",
    "serialize_registry_item",
    "compute_registry_index_entry",
    "build_registry_index",
    # exporter
    "export_block",
    # round-trip
    "load_block_from_json",
]
