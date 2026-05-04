"""Phase 2 — Skills loader (3-scope walk + lazy `Skill` tool dispatch).

Mirrors Claude Code's skill-pack convention so the runner *and* backend
specialist agents can discover, load, and execute markdown-based skills
from three precedence layers:

  1. **Project** — ``<project>/.claude/skills/`` and
     ``<project>/.omnisight/skills/`` (highest)
  2. **Home** — ``~/.claude/skills/`` and ``~/.omnisight/skills/``
  3. **Bundled** — ``<project>/omnisight/agents/skills/`` (lowest,
     ships with repo). ``<project>/configs/skills/`` remains a legacy
     bundled fallback while existing packs are migrated.

Same skill name in a higher scope shadows lower scopes — operators can
override a bundled skill by dropping a same-named ``SKILL.md`` into
their project ``.claude/skills/``.

Format support:

  * **YAML frontmatter** (canonical, Claude Code style)::

        ---
        name: mcp-builder
        description: Build MCP servers …
        keywords: [mcp, server, integration]
        ---
        <markdown body>

  * **Legacy header-only** (older OmniSight convention)::

        # SKILL-NEXTJS — W6 #280 (pilot)
        First sentence becomes the description.
        <markdown body>

Both formats are loaded; the registry exposes a uniform :class:`Skill`
dataclass.

Injection strategy: **lazy via the `Skill` tool**. Only the catalog
(name + 1-line description, ~80 chars per entry) goes into the LLM's
system prompt — bodies are fetched on demand via a tool handler. This
keeps prompt-cache prefix small even with 30+ skills.

ADR: TODO row WP.2 freezes this contract.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Where to look for skills, in precedence order. Each entry is a
# (resolver, scope_label) pair — resolver takes the project_root and
# returns a list of directories to scan. Lower index = higher priority.
SCOPE_ORDER: tuple[str, ...] = ("project", "home", "bundled")


@dataclass(frozen=True)
class Skill:
    """One loaded skill — markdown body + metadata."""

    name: str
    """Unique within registry (after shadowing). Matches the dir name or
    the frontmatter ``name:`` field; frontmatter wins."""

    description: str
    """One-line description used in the system-prompt catalog."""

    keywords: tuple[str, ...] = ()
    """Topical tags from frontmatter; used by future fuzzy lookup."""

    body: str = ""
    """The markdown body BELOW the frontmatter (or the whole file when
    no frontmatter is present). What the LLM actually reads."""

    source_path: Path | None = None
    """Where on disk this skill was loaded from."""

    scope: str = "bundled"
    """Which scope won shadowing — one of SCOPE_ORDER."""

    def to_catalog_entry(self) -> str:
        """One-line summary for the system-prompt catalog."""
        kw = (
            f" (keywords: {', '.join(self.keywords[:5])})"
            if self.keywords
            else ""
        )
        return f"- **{self.name}** — {self.description}{kw}"


# ─── Parser ──────────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)
_KV_RE = re.compile(r"^([\w-]+)\s*:\s*(.*?)\s*$")
_LIST_INLINE_RE = re.compile(r"^\[(.*)\]$")


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    """Tiny YAML-ish parser for skill frontmatter.

    Supports just the shapes we actually use: scalar ``key: value`` and
    inline lists ``key: [a, b, c]``. Multi-line blocks, anchors, and
    nested structures are rejected — those would mean an over-engineered
    skill metadata schema.
    """
    out: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        # Strip wrapping quotes
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        # Inline list?
        lm = _LIST_INLINE_RE.match(value)
        if lm:
            inner = lm.group(1)
            items = [s.strip().strip("'\"") for s in inner.split(",") if s.strip()]
            out[key] = items
        else:
            out[key] = value
    return out


def _legacy_header_description(body: str) -> tuple[str, str]:
    """Pull (description, name_hint) from a legacy-format SKILL.md.

    Convention used by older OmniSight skills:
      ``# SKILL-NEXTJS — W6 #280 (pilot)``
    becomes name_hint=``skill-nextjs``, description=`first prose paragraph`.
    """
    name_hint = ""
    description = ""
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        # Take everything up to the first whitespace as the name token —
        # hyphens stay (so `SKILL-NEXTJS` survives). E.g.:
        #   "SKILL-NEXTJS — W6 #280 (pilot)" → "skill-nextjs"
        #   "MCP Server Development"        → "mcp"
        head = title.split(maxsplit=1)[0] if title else ""
        name_hint = head.lower()
    # First non-empty line after the title block becomes description.
    for line in lines[1:]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        description = s
        break
    return description, name_hint


def parse_skill_file(path: Path, scope: str) -> Skill | None:
    """Read one ``SKILL.md`` (or ``*.md``) and return a :class:`Skill`.

    Returns None if the file is empty / unreadable. Logs a warning but
    does not raise — a malformed skill file shouldn't kill the loader.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("skill load failed: %s — %s", path, e)
        return None
    if not text.strip():
        return None

    name = ""
    description = ""
    keywords: tuple[str, ...] = ()
    body = text

    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        meta = _parse_yaml_subset(fm_match.group("fm"))
        body = fm_match.group("body").lstrip("\n")
        name = str(meta.get("name", "")).strip()
        description = str(meta.get("description", "")).strip()
        kw_raw = meta.get("keywords", ())
        if isinstance(kw_raw, list):
            keywords = tuple(str(k).strip() for k in kw_raw if str(k).strip())
        elif isinstance(kw_raw, str) and kw_raw:
            keywords = tuple(s.strip() for s in kw_raw.split(",") if s.strip())

    if not name or not description:
        legacy_desc, legacy_name = _legacy_header_description(text)
        if not name:
            name = legacy_name
        if not description:
            description = legacy_desc

    if not name:
        # Fall back to parent directory name.
        if path.parent.name and path.parent.name not in {".", "/"}:
            name = path.parent.name
        else:
            name = path.stem
    name = name.strip()
    if not name:
        return None

    return Skill(
        name=name,
        description=description or "(no description)",
        keywords=keywords,
        body=body,
        source_path=path,
        scope=scope,
    )


# ─── Registry ────────────────────────────────────────────────────


class SkillRegistry:
    """In-memory skill catalog with shadowing semantics.

    Add skills in **highest-priority-first** order (project → home →
    bundled); :meth:`add` is a no-op when a higher-priority entry with
    the same name already exists. Iteration order is alphabetical.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def add(self, skill: Skill) -> bool:
        """Add ``skill`` unless a higher-priority entry already won.

        Returns True if accepted, False if shadowed.
        """
        if skill.name in self._skills:
            return False
        self._skills[skill.name] = skill
        return True

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def has(self, name: str) -> bool:
        return name in self._skills

    def names(self) -> list[str]:
        return sorted(self._skills)

    def list_all(self) -> list[Skill]:
        return [self._skills[n] for n in self.names()]

    def __len__(self) -> int:
        return len(self._skills)


# ─── Scope walking ───────────────────────────────────────────────


def _scan_dir_for_skills(
    root: Path,
    scope: str,
) -> list[Skill]:
    """Find every ``SKILL.md`` (or top-level ``*.md``) under ``root``.

    Convention 1: ``<root>/<skill_name>/SKILL.md`` — preferred shape, also
    the format Claude Code's bundled skills use.

    Convention 2: ``<root>/<skill_name>.md`` — flat layout fallback.
    """
    if not root.exists() or not root.is_dir():
        return []
    out: list[Skill] = []
    seen_paths: set[Path] = set()
    # Subdir SKILL.md
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        candidate = skill_dir / "SKILL.md"
        if candidate.is_file() and candidate not in seen_paths:
            sk = parse_skill_file(candidate, scope)
            if sk is not None:
                out.append(sk)
            seen_paths.add(candidate)
    # Flat *.md (skip README to avoid noise)
    for md in sorted(root.glob("*.md")):
        if md.name.lower() in {"readme.md", "index.md"}:
            continue
        if md in seen_paths:
            continue
        sk = parse_skill_file(md, scope)
        if sk is not None:
            out.append(sk)
        seen_paths.add(md)
    return out


def _project_scope_dirs(project_root: Path) -> list[Path]:
    return [
        project_root / ".claude" / "skills",
        project_root / ".omnisight" / "skills",
    ]


def _home_scope_dirs(home: Path | None = None) -> list[Path]:
    h = home or Path.home()
    return [
        h / ".claude" / "skills",
        h / ".omnisight" / "skills",
    ]


def _bundled_scope_dirs(project_root: Path) -> list[Path]:
    return [
        project_root / "omnisight" / "agents" / "skills",
        project_root / "configs" / "skills",
    ]


def load_default_scopes(
    project_root: Path,
    *,
    home: Path | None = None,
    extra_dirs: Iterable[tuple[Path, str]] = (),
) -> SkillRegistry:
    """Load skills with 3-scope precedence.

    Args:
      project_root: Repository root. Project and bundled skill roots are
        scanned relative to it.
      home: Override ``~`` for tests. Defaults to :func:`Path.home`.
      extra_dirs: Optional ``(path, scope_label)`` pairs. Iterated in
        order, treated as the **lowest** priority — useful for embedded
        skill bundles distributed alongside a customer install.
    """
    registry = SkillRegistry()

    def _add_all(dirs: list[Path], scope: str) -> None:
        for d in dirs:
            for sk in _scan_dir_for_skills(d, scope):
                registry.add(sk)

    _add_all(_project_scope_dirs(project_root), "project")
    _add_all(_home_scope_dirs(home), "home")
    _add_all(_bundled_scope_dirs(project_root), "bundled")
    for extra_dir, label in extra_dirs:
        for sk in _scan_dir_for_skills(extra_dir, label):
            registry.add(sk)

    if len(registry) > 0:
        scope_counts: dict[str, int] = {}
        for sk in registry.list_all():
            scope_counts[sk.scope] = scope_counts.get(sk.scope, 0) + 1
        logger.info(
            "skills_loader: %d skills loaded — %s",
            len(registry),
            ", ".join(f"{k}={v}" for k, v in sorted(scope_counts.items())),
        )
    return registry


# ─── Tool handler ────────────────────────────────────────────────


def make_skill_handler(registry: SkillRegistry):
    """Build a sync handler for the ``Skill`` tool schema.

    Returns a callable suitable for
    ``ToolDispatcher.register("Skill", handler)``. The handler reads
    ``payload["skill"]`` and returns the markdown body of that skill;
    unknown names produce a structured error so the LLM can recover.
    """

    def _handler(payload: dict[str, Any]) -> str:
        name = str(payload.get("skill", "")).strip()
        if not name:
            raise ValueError("Skill tool requires non-empty 'skill' field")
        skill = registry.get(name)
        if skill is None:
            top = ", ".join(registry.names()[:20])
            more = (
                f" (showing 20 of {len(registry)})"
                if len(registry) > 20
                else ""
            )
            raise KeyError(
                f"Unknown skill {name!r}. Available{more}: {top}"
            )
        args = str(payload.get("args", "") or "").strip()
        body = skill.body
        if args:
            body = f"_(invoked with args: {args})_\n\n{body}"
        return body

    return _handler


# ─── Catalog rendering for system prompt ────────────────────────


def render_catalog_for_prompt(
    registry: SkillRegistry,
    *,
    max_entries: int = 60,
) -> str:
    """Render a system-prompt-friendly catalog of available skills.

    Caps at ``max_entries`` to avoid blowing the cached prefix when the
    operator has 100+ skills installed. Always includes a hint about how
    to invoke via the ``Skill`` tool.
    """
    if len(registry) == 0:
        return ""
    lines = [
        "# 可用 Skills（lazy load — 用 Skill tool 載入完整內容）",
        f"共 {len(registry)} 個 skill；下面是 catalog（name + 1-line desc）。",
        "需要某 skill 詳細內容時，呼叫 Skill tool 並傳 `skill: <name>`。",
        "",
    ]
    for sk in registry.list_all()[:max_entries]:
        lines.append(sk.to_catalog_entry())
    if len(registry) > max_entries:
        remaining = len(registry) - max_entries
        lines.append(f"… 還有 {remaining} 個未列出（呼叫 Skill 直接載入即可）")
    lines.append("")
    return "\n".join(lines)
